"""WF-6 sync-status — réconcilie le statut d'envoi des messages depuis Instantly.

Problème résolu (audit #5) : WF-6 (`send.py`) pousse un draft à Instantly puis met
`messages.status='queued'` et stocke l'id du LEAD Instantly dans
`provider_message_id`. Rien ne ramenait ensuite le résultat — les messages
restaient `queued` à vie, et les hard bounces n'étaient JAMAIS ajoutés à
`suppression_list`. Conséquence : on re-mailait des adresses mortes → bounce rate
qui monte → réputation de domaine qui s'effondre (échec #1 du cold email).

Ce module poll Instantly pour chaque message `queued` et flippe son statut :
  - lead envoyé        → `messages.status='sent'` (+ `sent_at`)
  - lead bouncé (hard) → `messages.status='bounced'` (+ `bounced_at`)
                         + insert `suppression_list` (reason `hard_bounce`)
  - lead désabonné     → `suppression_list` (reason `opt_out`) + contact `opted_out`
  - lead a répondu     → `messages.status='replied'` (le WF-7 reply le fait aussi ;
                         idempotent car on ne touche que les `queued`)
  - sinon (pending)    → on laisse `queued`, on retentera au prochain run.

On interroge l'endpoint LEAD (`GET /leads/{id}`) avec l'id stocké, ce qui
contourne le piège connu « id lead ≠ id email envoyé » : pas de join ambigu,
l'id qu'on a EST la clé du lead.

⚠️ À VALIDER SUR LE 1er PAYLOAD RÉEL : le mapping des champs Instantly (status
numérique, flags de bounce, compteurs) est best-effort — plusieurs noms possibles
sont tentés dans `classify_lead_outcome`. Dès le 1er vrai bounce observé, ajuster
CETTE SEULE fonction si les champs réels diffèrent. Cf docs/go-live-checklist.md.

Limite connue : on ne resynchronise que les messages `queued`. Un hard bounce
tardif (après qu'on ait marqué `sent`) ne serait pas rattrapé — acceptable car en
cold email un hard bounce est quasi immédiat (destinataire inconnu).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from .. import supabase_client as db
from ..lib import instantly as instantly_lib
from . import db as db_tools

Outcome = Literal["sent", "bounced", "unsubscribed", "replied", "pending", "not_found"]


def _truthy(v: Any) -> bool:
    """Interprète un flag Instantly potentiellement bool/int/str comme booléen."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "bounce", "bounced")
    return False


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def classify_lead_outcome(lead: dict[str, Any] | None) -> Outcome:
    """Mappe un lead Instantly → outcome. Best-effort multi-shape (cf module docstring).

    Ordre de priorité : bounce d'abord (signal le plus important pour la
    réputation), puis unsub, puis reply, puis sent. Défaut `pending` : on ne
    flippe JAMAIS sans signal clair (mieux vaut retenter au prochain run que
    marquer un faux 'sent').
    """
    if lead is None:
        return "not_found"

    # 1) Bounce — flags explicites + compteur
    if _truthy(lead.get("is_bounced")) or _truthy(lead.get("bounced")) or _truthy(lead.get("email_bounced")):
        return "bounced"
    if _int(lead.get("email_bounced_count")) >= 1:
        return "bounced"

    # 2) Statut texte (best-effort — divers noms selon version Instantly)
    status_text = " ".join(
        str(lead.get(k) or "") for k in ("status_text", "status_summary", "lead_status")
    ).lower()
    if "bounce" in status_text:
        return "bounced"
    if "unsub" in status_text:
        return "unsubscribed"

    # 3) Statut numérique (best-effort — VALIDER sur 1er payload réel)
    status = lead.get("status")
    if isinstance(status, (int, float)):
        s = int(status)
        if s in (-1, -3):  # états terminaux problématiques (bounce / skipped-bounce)
            return "bounced"
        if s == -2:        # unsubscribed
            return "unsubscribed"

    # 4) Unsub — flags explicites
    if _truthy(lead.get("is_unsubscribed")) or _truthy(lead.get("unsubscribed")):
        return "unsubscribed"

    # 5) Reply
    if _int(lead.get("email_reply_count")) >= 1 or _int(lead.get("reply_count")) >= 1:
        return "replied"

    # 6) Envoyé — au moins 1 email parti, OU statut Completed (3)
    sent_count = (
        _int(lead.get("email_sent_count"))
        or _int(lead.get("emails_sent_count"))
        or _int(lead.get("sent_count"))
    )
    if sent_count >= 1:
        return "sent"
    if isinstance(status, (int, float)) and int(status) == 3:
        return "sent"

    return "pending"


class SyncStatusIn(BaseModel):
    limit: int = 100        # nb max de messages `queued` à réconcilier par run
    dry_run: bool = False   # True = ne touche pas la DB, retourne juste les outcomes


class SyncStatusItem(BaseModel):
    message_id: str
    provider_message_id: str | None = None
    to_email: str | None = None
    outcome: str
    new_status: str | None = None
    suppressed: bool = False
    error_text: str | None = None


class SyncStatusOut(BaseModel):
    processed: int
    flipped_sent: int
    flipped_bounced: int
    flipped_replied: int
    unsubscribed: int
    suppressed: int
    still_pending: int
    not_found: int
    errors: int
    dry_run: bool
    items: list[SyncStatusItem]


async def sync_send_status(payload: SyncStatusIn) -> SyncStatusOut:
    """Pass complet : réconcilie les messages outbound `queued` avec Instantly.

    Idempotent et sûr : ne touche que les `queued`, ne flippe que sur signal clair,
    suppression best-effort. Volume quotidien petit (~10) → 1 GET lead par message
    est acceptable.
    """
    now = datetime.now(timezone.utc).isoformat()
    queued = await db.select(
        "messages",
        params={
            "select": "id,provider_message_id,to_email,contact_id,status,sent_at",
            "direction": "eq.outbound",
            "status": "eq.queued",
            "provider": "eq.instantly",
            "provider_message_id": "not.is.null",
            "order": "scheduled_at.asc",
            "limit": str(max(1, payload.limit)),
        },
    )

    items: list[SyncStatusItem] = []
    flipped_sent = flipped_bounced = flipped_replied = 0
    unsub = suppressed = pending = not_found = errors = 0

    for m in queued:
        mid = m["id"]
        lead_id = m.get("provider_message_id")
        to_email = m.get("to_email")

        try:
            lead = await instantly_lib.get_lead(lead_id)
        except instantly_lib.InstantlyError as e:
            errors += 1
            items.append(SyncStatusItem(
                message_id=mid, provider_message_id=lead_id, to_email=to_email,
                outcome="error", error_text=str(e)[:300],
            ))
            continue

        outcome = classify_lead_outcome(lead)
        new_status: str | None = None

        if outcome == "pending":
            pending += 1
        elif outcome == "not_found":
            not_found += 1
        elif outcome == "sent":
            new_status = "sent"
            flipped_sent += 1
        elif outcome == "replied":
            new_status = "replied"
            flipped_replied += 1
        elif outcome == "bounced":
            new_status = "bounced"
            flipped_bounced += 1
        elif outcome == "unsubscribed":
            # L'email a été livré puis le lead s'est désabonné → 'sent' côté message.
            new_status = "sent"
            unsub += 1

        did_suppress = False
        if not payload.dry_run:
            if new_status:
                patch: dict[str, Any] = {"status": new_status}
                if new_status == "sent" and not m.get("sent_at"):
                    patch["sent_at"] = now
                elif new_status == "bounced":
                    patch["bounced_at"] = now
                elif new_status == "replied":
                    patch["replied_at"] = now
                try:
                    await db.update("messages", patch, filters={"id": f"eq.{mid}"})
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    items.append(SyncStatusItem(
                        message_id=mid, provider_message_id=lead_id, to_email=to_email,
                        outcome=outcome, new_status=new_status,
                        error_text=f"update_failed: {e!r}"[:300],
                    ))
                    continue

            if outcome == "bounced" and to_email:
                did_suppress = await db_tools.add_to_suppression(
                    email=to_email, reason="hard_bounce", source="instantly_sync",
                    notes=f"lead status sync; lead_id={lead_id}",
                )
            elif outcome == "unsubscribed" and to_email:
                did_suppress = await db_tools.add_to_suppression(
                    email=to_email, reason="opt_out", source="instantly_sync",
                    notes=f"lead unsubscribed via Instantly; lead_id={lead_id}",
                )
                cid = m.get("contact_id")
                if cid:
                    try:
                        await db.update(
                            "contacts", {"status": "opted_out"},
                            filters={"id": f"eq.{cid}"},
                        )
                    except Exception:  # noqa: BLE001
                        pass

        if did_suppress:
            suppressed += 1

        items.append(SyncStatusItem(
            message_id=mid, provider_message_id=lead_id, to_email=to_email,
            outcome=outcome, new_status=new_status, suppressed=did_suppress,
        ))

    return SyncStatusOut(
        processed=len(items),
        flipped_sent=flipped_sent, flipped_bounced=flipped_bounced,
        flipped_replied=flipped_replied, unsubscribed=unsub,
        suppressed=suppressed, still_pending=pending, not_found=not_found,
        errors=errors, dry_run=payload.dry_run, items=items,
    )
