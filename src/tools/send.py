"""WF-6 Send — push approved drafts vers Instantly.

Logique :
  1. Lit `messages` où status='draft' AND compliance_check_passed=true AND
     direction='outbound' (déjà validé par WF-5).
  2. Defense in depth :
     - Warmup gate (WARMUP_END_DATE) — refuse l'envoi pendant le warmup même
       si WF-5 a approuvé (cas où le draft a été approuvé avant la fenêtre).
     - Suppression list — check email + domaine du contact contre
       suppression_list (opt-outs, hard bounces, DNCL).
     - Daily cap — limite N pushs/jour, fenêtre America/Toronto.
  3. Fetch contact + company pour enrichir le lead Instantly (first_name,
     last_name, company_name).
  4. Push à Instantly via `lib/instantly.add_lead_to_campaign` — passe
     subject + body comme custom variables. La campagne Instantly est
     configurée par William avec template `{{email_subject}}` / `{{email_body}}`.
  5. Update messages : status='queued', provider='instantly',
     provider_message_id=<lead_id Instantly>, scheduled_at=now().

L'envoi réel se fera selon le schedule de la campagne Instantly.

SYNC DU STATUT (audit #5 — fermé 2026-05-31) : la réconciliation du statut
d'envoi est faite par `tools/send_status.py` (`POST /wf6/sync-status`, cron
WF-6b). Il interroge le LEAD Instantly via le `provider_message_id` stocké ici
et flippe `messages.status` → 'sent' / 'bounced' / 'replied', + ajoute les hard
bounces à `suppression_list`. Le mapping des champs Instantly reste à valider
sur le 1er vrai bounce (cf `classify_lead_outcome` + docs/go-live-checklist.md).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .. import supabase_client as db
from ..lib import instantly as instantly_lib
from ..lib import slack
from ..lib.compliance_checks import check_warmup_window
from ..lib.demo_generator import DEMO_URL_PLACEHOLDER, ensure_demo_site, inject_demo_link
from ..lib.platform_domains import is_email_on_blocked_domain

DAILY_CAP_DEFAULT = 10
DAILY_CAP_ENV = "INSTANTLY_DAILY_CAP"
SEND_TIMEZONE = "America/Toronto"
# Anti-spam de l'alerte demo (P3) : 1 ping #alertes par message coincé, pas par run.
DEMO_ALERT_MARKER = "demo_alert_sent"


# ----------------------------------------------------------------------
# Modèles
# ----------------------------------------------------------------------

class SendMessageIn(BaseModel):
    message_id: str
    # Override l'ID de campagne — par défaut INSTANTLY_CAMPAIGN_ID env.
    campaign_id: str | None = None
    # True = ne pousse pas vraiment à Instantly, mais simule le succès.
    # Utile pour tester la sélection des drafts pendant le warmup.
    dry_run: bool = False


class SendMessageOut(BaseModel):
    message_id: str
    status: str  # ok | skipped_warmup | skipped_not_eligible | skipped_suppressed | skipped_platform_domain | skipped_no_demo | error
    provider_message_id: str | None = None
    skipped_reason: str | None = None
    error_text: str | None = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _daily_cap() -> int:
    raw = os.environ.get(DAILY_CAP_ENV, "").strip()
    if not raw:
        return DAILY_CAP_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return DAILY_CAP_DEFAULT


def _today_start_utc_iso() -> str:
    """Début de la journée en heure de Toronto, converti UTC pour Postgres.

    On compte le daily cap sur la journée locale (Toronto), pas UTC, pour
    matcher l'expérience humaine (« j'ai envoyé 10 emails aujourd'hui »)
    et le sending window d'Instantly qui suit aussi le fuseau local.
    """
    tz = ZoneInfo(SEND_TIMEZONE)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).isoformat()


async def count_pushed_today() -> int:
    """Combien de drafts on a déjà handé off à Instantly aujourd'hui (Toronto).

    On compte les messages outbound dont scheduled_at >= today_start_local.
    `scheduled_at` est set par cette même fonction au moment du push.
    Note: cnt-only via PostgREST `select=count`/`Prefer: count=exact` aurait
    été plus efficace, mais ici N quotidien est petit (~10), SELECT suffit.
    """
    today_start = _today_start_utc_iso()
    rows = await db.select(
        "messages",
        params={
            "select": "id",
            "direction": "eq.outbound",
            "scheduled_at": f"gte.{today_start}",
            "status": "neq.draft",
        },
    )
    return len(rows)


async def _is_suppressed(email: str | None, domain: str | None) -> tuple[bool, str | None]:
    """True si l'email OU le domain est sur suppression_list.

    Couvre les 3 cas de suppression_list : email exact, domaine entier.
    (phone n'est pas pertinent pour l'envoi email.)
    """
    if email:
        em_rows = await db.select(
            "suppression_list",
            params={"select": "reason", "email": f"eq.{email}", "limit": "1"},
        )
        if em_rows:
            return True, f"email on suppression ({em_rows[0].get('reason')})"
    if domain:
        dom_rows = await db.select(
            "suppression_list",
            params={"select": "reason", "domain": f"eq.{domain}", "limit": "1"},
        )
        if dom_rows:
            return True, f"domain on suppression ({dom_rows[0].get('reason')})"
    return False, None


# ----------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------

async def send_one_message(payload: SendMessageIn) -> SendMessageOut:
    """Push UN draft à Instantly. Idempotent par message_id : si la message
    n'est plus en status='draft', on skip (évite double-push si retry n8n).
    """
    # 1) Fetch message + verify éligibilité
    msgs = await db.select(
        "messages",
        params={
            "select": "id,subject,body_text,to_email,status,direction,compliance_check_passed,contact_id,demo_url,track,compliance_notes",
            "id": f"eq.{payload.message_id}",
            "limit": "1",
        },
    )
    if not msgs:
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            error_text="message_not_found",
        )
    msg = msgs[0]

    if msg.get("status") != "draft":
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason=f"status={msg.get('status')!r} (attendu 'draft')",
        )
    if msg.get("direction") != "outbound":
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason=f"direction={msg.get('direction')!r}",
        )
    if msg.get("compliance_check_passed") is not True:
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason="compliance_check_passed != true",
        )
    if not msg.get("to_email") or not msg.get("subject") or not msg.get("body_text"):
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason="to_email/subject/body_text manquant",
        )

    # 2) Defense — warmup gate. WF-5 le bloque déjà mais on revérifie au
    # send (au cas où le draft a été approuvé avant l'activation du gate ou
    # si WARMUP_END_DATE a été remis après coup).
    warmup = check_warmup_window()
    if not warmup.passed:
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_warmup",
            skipped_reason=warmup.message,
        )

    # 2b) Defense — platform / big tech email domain.
    # Filet final après la blocklist domaine en amont (WF-1 sourcing + scrape WF-3).
    # Si malgré tout un contact @meta.com / @doordash.com / etc. est arrivé en DB
    # (import manuel, ancienne pollution avant cleanup du 14 mai, contact legacy
    # Apollo, edge case), on bloque ici AVANT l'action irréversible (push Instantly).
    blocked, reason = is_email_on_blocked_domain(msg.get("to_email"))
    if blocked:
        # Marquer le message 'failed' pour qu'il ne soit pas re-tenté.
        try:
            await db.update(
                "messages",
                {"status": "failed", "compliance_notes": (
                    (msg.get("compliance_notes") or "")
                    + f" | send_blocked: platform_domain ({reason})"
                ).strip(" |")},
                filters={"id": f"eq.{payload.message_id}"},
            )
        except Exception:  # noqa: BLE001
            pass
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_platform_domain",
            skipped_reason=f"email domain dans blocklist: {reason}",
        )

    # 3) Fetch contact + company pour Instantly metadata
    contact_id = msg.get("contact_id")
    contact_rows = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,company_id",
            "id": f"eq.{contact_id}",
            "limit": "1",
        },
    ) if contact_id else []
    if not contact_rows:
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            error_text="contact_not_found",
        )
    contact = contact_rows[0]

    company_rows = await db.select(
        "companies",
        params={"select": "name,domain", "id": f"eq.{contact['company_id']}", "limit": "1"},
    ) if contact.get("company_id") else []
    company = company_rows[0] if company_rows else {}

    # 3b) Garde demo (P3) — aucun email agence-ia ne part sans lien démo unique.
    # Si manquant, on retente la frappe ici ; échec persistant => skip sans push.
    if (msg.get("track") or "OPT") == "agence-ia":
        needs_demo = (not msg.get("demo_url")) or (DEMO_URL_PLACEHOLDER in (msg.get("body_text") or ""))
        if needs_demo:
            try:
                demo_url = await ensure_demo_site(contact.get("company_id"), msg["contact_id"])
                new_body = inject_demo_link(msg.get("body_text") or "", demo_url)
                await db.update(
                    "messages",
                    {"demo_url": demo_url, "body_text": new_body},
                    filters={"id": f"eq.{payload.message_id}"},
                )
                msg["demo_url"] = demo_url
                msg["body_text"] = new_body
            except Exception as e:  # noqa: BLE001 — pas de push sans lien
                existing_notes = msg.get("compliance_notes") or ""
                if DEMO_ALERT_MARKER not in existing_notes:
                    await slack.notify(
                        text=(
                            f":rotating_light: Demo non générée — email bloqué.\n"
                            f"message_id={payload.message_id} contact_id={msg['contact_id']} "
                            f"company_id={contact.get('company_id')}\nerreur: {e!r}\n"
                            f"(Vérifier que le schéma `agence` est exposé à l'API REST.)"
                        ),
                        category="alerts",
                        context="p3_demo_guard",
                    )
                    new_notes = f"{existing_notes} | {DEMO_ALERT_MARKER}".strip(" |")
                    try:
                        await db.update(
                            "messages", {"compliance_notes": new_notes},
                            filters={"id": f"eq.{payload.message_id}"},
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return SendMessageOut(
                    message_id=payload.message_id, status="skipped_no_demo",
                    skipped_reason=f"demo_generation_failed: {e!r}",
                )

    # 4) Defense — suppression list (post-draft, pre-push). Un opt-out reçu
    # après la création du draft doit bloquer ici.
    suppressed, reason = await _is_suppressed(msg["to_email"], company.get("domain"))
    if suppressed:
        # On marque le message 'failed' pour que les futurs runs ne le re-tentent pas.
        try:
            await db.update(
                "messages",
                {"status": "failed", "compliance_notes": (
                    (msg.get("compliance_notes") or "") + f" | send_blocked: {reason}"
                ).strip(" |")},
                filters={"id": f"eq.{payload.message_id}"},
            )
        except Exception:  # noqa: BLE001
            pass
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_suppressed",
            skipped_reason=reason,
        )

    # 5) Push à Instantly (ou simule si dry_run)
    provider_message_id: str | None = None
    if payload.dry_run:
        provider_message_id = f"dry_run_{payload.message_id[:8]}"
    else:
        try:
            res = await instantly_lib.add_lead_to_campaign(
                email=msg["to_email"],
                subject=msg["subject"],
                body_text=msg["body_text"],
                first_name=contact.get("first_name"),
                last_name=contact.get("last_name"),
                company_name=company.get("name"),
                campaign_id=payload.campaign_id,
            )
            provider_message_id = str(res.get("id"))
        except instantly_lib.InstantlyError as e:
            return SendMessageOut(
                message_id=payload.message_id, status="error",
                error_text=f"instantly: {e}",
            )

    # 6) Update messages : queued + provider + scheduled_at
    now_iso = datetime.now(timezone.utc).isoformat()
    patch: dict[str, Any] = {
        "status": "queued",
        "provider": "instantly",
        "provider_message_id": provider_message_id,
        "scheduled_at": now_iso,
    }
    try:
        await db.update(
            "messages", patch, filters={"id": f"eq.{payload.message_id}"}
        )
    except Exception as e:  # noqa: BLE001
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            provider_message_id=provider_message_id,
            error_text=f"db_update_after_push: {e!r}",
        )

    # Side effect : flip contact.status à 'contacted'. Si déjà 'contacted'+,
    # on laisse (un follow-up Phase 3 ne doit pas régresser à 'contacted').
    if contact.get("id"):
        try:
            cur = await db.select(
                "contacts",
                params={"select": "status", "id": f"eq.{contact['id']}", "limit": "1"},
            )
            if cur and cur[0].get("status") in ("new", "ready", "researching"):
                await db.update(
                    "contacts", {"status": "contacted"},
                    filters={"id": f"eq.{contact['id']}"},
                )
        except Exception:  # noqa: BLE001
            pass

    return SendMessageOut(
        message_id=payload.message_id, status="ok",
        provider_message_id=provider_message_id,
    )


# ----------------------------------------------------------------------
# Batch (WF-6 run)
# ----------------------------------------------------------------------

class RunWf6In(BaseModel):
    limit: int = 10
    campaign_id: str | None = None
    dry_run: bool = False
    # Override le daily cap (défaut: env INSTANTLY_DAILY_CAP ou 10).
    daily_cap: int | None = None
    track: str = "OPT"  # OPT | REACTI — filtre les drafts + choisit la campagne Instantly


def _campaign_for_track(track: str) -> str | None:
    """Campagne Instantly selon le track. agence-ia → INSTANTLY_CAMPAIGN_ID_REACTI
    (nom d'env legacy gardé) ; OPT/défaut → None (lib instantly utilise
    INSTANTLY_CAMPAIGN_ID)."""
    if track and track.strip().lower() == "agence-ia":
        return os.environ.get("INSTANTLY_CAMPAIGN_ID_REACTI", "").strip() or None
    return None


class RunWf6Item(BaseModel):
    message_id: str
    to_email: str | None = None
    status: str
    provider_message_id: str | None = None
    skipped_reason: str | None = None
    error_text: str | None = None


class RunWf6Out(BaseModel):
    processed: int
    pushed: int
    skipped_cap: int
    skipped_warmup: int
    skipped_suppressed: int
    skipped_platform_domain: int = 0
    skipped_no_demo: int = 0
    skipped_other: int
    errors: int
    daily_cap: int
    already_pushed_today: int
    items: list[RunWf6Item]


async def run_wf6(payload: RunWf6In) -> RunWf6Out:
    """Pass complet WF-6 : pousse jusqu'à `limit` drafts approuvés à Instantly,
    en respectant le daily cap (compté sur fenêtre Toronto)."""
    daily_cap = payload.daily_cap if payload.daily_cap is not None else _daily_cap()
    already = await count_pushed_today()
    remaining = max(0, daily_cap - already)
    effective_limit = min(payload.limit, remaining)

    track = (payload.track or "OPT").strip() or "OPT"
    campaign = payload.campaign_id or _campaign_for_track(track)
    # Garde : un track non-OPT DOIT avoir sa campagne dédiée, sinon on refuse —
    # ne JAMAIS pousser des drafts REACTI vers la campagne OPT par défaut.
    if track.upper() != "OPT" and not campaign:
        return RunWf6Out(
            processed=0, pushed=0, skipped_cap=0, skipped_warmup=0,
            skipped_suppressed=0, skipped_other=0, errors=0,
            daily_cap=daily_cap, already_pushed_today=already, items=[],
        )

    items: list[RunWf6Item] = []
    pushed = sk_cap = sk_warm = sk_supp = sk_plat = sk_nodemo = sk_other = errors = 0

    if effective_limit <= 0:
        return RunWf6Out(
            processed=0, pushed=0,
            skipped_cap=0, skipped_warmup=0,
            skipped_suppressed=0, skipped_other=0, errors=0,
            daily_cap=daily_cap, already_pushed_today=already, items=[],
        )

    # Fetch drafts éligibles, ordre FIFO (created_at asc)
    drafts = await db.select(
        "messages",
        params={
            "select": "id,to_email,created_at,track",
            "direction": "eq.outbound",
            "status": "eq.draft",
            "compliance_check_passed": "is.true",
            "track": f"eq.{track}",
            "order": "created_at.asc",
            "limit": str(effective_limit),
        },
    )

    for d in drafts:
        try:
            res = await send_one_message(
                SendMessageIn(
                    message_id=d["id"],
                    campaign_id=campaign,
                    dry_run=payload.dry_run,
                )
            )
        except Exception as e:  # noqa: BLE001
            errors += 1
            items.append(RunWf6Item(
                message_id=d["id"], to_email=d.get("to_email"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok":
            pushed += 1
        elif res.status == "skipped_warmup":
            sk_warm += 1
        elif res.status == "skipped_suppressed":
            sk_supp += 1
        elif res.status == "skipped_platform_domain":
            sk_plat += 1
        elif res.status == "skipped_no_demo":
            sk_nodemo += 1
        elif res.status == "skipped_not_eligible":
            sk_other += 1
        else:
            errors += 1

        items.append(RunWf6Item(
            message_id=d["id"], to_email=d.get("to_email"),
            status=res.status,
            provider_message_id=res.provider_message_id,
            skipped_reason=res.skipped_reason,
            error_text=res.error_text,
        ))

    # Si on a hit le cap avant même de fetch, marquer en compteur dédié
    if payload.limit > effective_limit:
        sk_cap = payload.limit - effective_limit

    return RunWf6Out(
        processed=len(items),
        pushed=pushed,
        skipped_cap=sk_cap,
        skipped_warmup=sk_warm,
        skipped_suppressed=sk_supp,
        skipped_platform_domain=sk_plat,
        skipped_no_demo=sk_nodemo,
        skipped_other=sk_other,
        errors=errors,
        daily_cap=daily_cap,
        already_pushed_today=already,
        items=items,
    )
