"""Slack Incoming Webhook — notifs pour WF-7 / WF-8.

Routing par catégorie (depuis 2026-05-27) :
  - `bookings` → SLACK_WEBHOOK_BOOKINGS (WF-8 events)
  - `leads`    → SLACK_WEBHOOK_LEADS (WF-7 hot lead, review)
  - `alerts`   → SLACK_WEBHOOK_ALERTS (orphans, classifier errors)

Fallback : si la var spécifique à la catégorie n'est pas set, on retombe
sur SLACK_WEBHOOK_URL (legacy single-channel). Si rien n'est configuré,
les notifs sont silencieusement no-op — utile pour dev/test sans Slack.

Failure-mode : Slack DOWN ne DOIT JAMAIS casser la pipeline (auto-reply,
booking, etc.). Les exceptions sont avalées + loggées en stderr. Le caller
peut inspecter `notify(...)` return = True/False pour savoir si le ping est
passé, mais ne doit pas crasher si False.

Ref: https://api.slack.com/messaging/webhooks
"""
from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from .reacti_tickets import ReactiTicket

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
SLACK_TIMEOUT_SECONDS = 5.0  # court — on ne veut pas bloquer la pipeline

Category = Literal["bookings", "leads", "alerts", "errors", "summary"]

# Mapping catégorie → env var dédiée. Si non set, on retombe sur SLACK_WEBHOOK_URL.
_CATEGORY_ENV: dict[str, str] = {
    "bookings": "SLACK_WEBHOOK_BOOKINGS",
    "leads": "SLACK_WEBHOOK_LEADS",
    "alerts": "SLACK_WEBHOOK_ALERTS",
    "errors": "SLACK_WEBHOOK_ERRORS",    # pannes pipeline (n8n error workflow)
    "summary": "SLACK_WEBHOOK_SUMMARY",  # résumé quotidien
}


def _webhook_url(category: str | None = None) -> str | None:
    """Résout l'URL webhook pour une catégorie donnée.

    Ordre : env catégorie spécifique → SLACK_WEBHOOK_URL fallback → None.
    None = pas configuré, notify devient no-op silencieux.
    """
    if category:
        env_name = _CATEGORY_ENV.get(category)
        if env_name:
            url = os.environ.get(env_name, "").strip()
            if url:
                return url
    url = os.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    return url or None


async def notify(
    *,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    context: str | None = None,
    category: Category | None = None,
) -> bool:
    """Envoie un message Slack via Incoming Webhook. Async (httpx).

    Args:
      text: texte fallback (utilisé par notifs mobile + accessibilité).
      blocks: blocks Block Kit optionnels pour mise en forme riche.
      context: prefix court ajouté au log stderr en cas d'erreur (ex: "wf7_hot_lead").
      category: route vers le webhook dédié ("bookings"/"leads"/"alerts"). Fallback
        sur SLACK_WEBHOOK_URL si la var catégorie n'est pas set.

    Returns True si Slack a accepté (200 OK), False sinon ou si pas configuré.
    NE LÈVE JAMAIS — la pipeline ne doit pas casser à cause de Slack.
    """
    url = _webhook_url(category)
    if not url:
        return False  # pas configuré = no-op silencieux

    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SECONDS) as client:
            r = await client.post(url, json=payload)
        if r.status_code == 200 and r.text.strip() == "ok":
            return True
        print(
            f"[slack:{context or '-'}] non-2xx response: {r.status_code} {r.text[:200]}",
            file=sys.stderr,
        )
        return False
    except Exception as e:  # noqa: BLE001 — Slack DOWN ne casse rien
        print(
            f"[slack:{context or '-'}] exception {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False


# ----------------------------------------------------------------------
# Helpers de mise en forme — réutilisés par WF-7 et WF-8
# ----------------------------------------------------------------------

def _kv_field(label: str, value: str) -> dict[str, Any]:
    """Field Block Kit avec label en gras + valeur."""
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _track_prefix(track: str | None) -> str:
    """Préfixe visible du track pour les notifs partagées OPT/REACTI (ex: '[REACTI] ').

    Vide si track inconnu — la notif reste propre. Mis sur le fallback (notif mobile)
    ET le header pour qu'on sache d'un coup d'œil d'où vient l'event.
    """
    t = (track or "").strip().upper()
    return f"[{t}] " if t in ("OPT", "REACTI") else ""


def build_hot_lead_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    reply_preview: str,
    auto_reply_sent: bool,
    confidence: float | None = None,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply classé 'interested' (WF-7).

    Returns (fallback_text, blocks) — passer aux 2 args de `notify`.
    """
    tp = _track_prefix(track)
    status = "Auto-reply envoyé (Cal.com link)" if auto_reply_sent else "À répondre manuellement"
    fallback = f"{tp}🔥 Hot lead — {contact_name} @ {company_name} ({status})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}🔥 Hot lead"},
        },
        {
            "type": "section",
            "fields": [
                _kv_field("Contact", f"{contact_name}\n{contact_email}"),
                _kv_field("Entreprise", company_name),
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Statut*: {status}"
                + (f"\n*Confidence*: {confidence:.0%}" if confidence is not None else ""),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reply (extrait)*\n```{_truncate(reply_preview, 400)}```",
            },
        },
    ]
    return fallback, blocks


def build_review_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    category: str,
    confidence: float,
    reasoning: str,
    reply_preview: str,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply en review manuel (classifier hésite / 'other')."""
    tp = _track_prefix(track)
    fallback = f"{tp}⚠️ Review manuel — {contact_name} ({category}, conf {confidence:.0%})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}⚠️ Reply à reviewer"},
        },
        {
            "type": "section",
            "fields": [
                _kv_field("Contact", f"{contact_name}\n{contact_email}"),
                _kv_field("Entreprise", company_name),
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Classification*: `{category}` (confidence {confidence:.0%})\n"
                    f"*Raisonnement*: {reasoning}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reply (extrait)*\n```{_truncate(reply_preview, 400)}```",
            },
        },
    ]
    return fallback, blocks


def _research_brief_blocks(research_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Construit la section 'Brief pré-RDV' depuis `companies.research_json` (WF-3).

    Tout est déterministe (pas de LLM) — `research_json` est déjà structuré. Si le
    champ est absent/vide, on retourne [] et le ping booking garde son format minimal.

    Lecture défensive : `research_json` en DB est l'objet research direct (clés
    `company_summary`, `pain_points_detected`, etc.), mais on tolère un éventuel
    wrapper `{"research": {...}}` au cas où le shape changerait.
    """
    if not isinstance(research_json, dict) or not research_json:
        return []
    rj = research_json.get("research") if isinstance(research_json.get("research"), dict) else research_json

    sections: list[str] = []

    summary = (rj.get("company_summary") or "").strip()
    if summary:
        sections.append(f"*Résumé*\n{_truncate(summary, 300)}")

    pains = rj.get("pain_points_detected")
    if isinstance(pains, list) and pains:
        lines = []
        for p in pains[:3]:
            txt = (p.get("pain") if isinstance(p, dict) else str(p)) or ""
            txt = txt.strip()
            if txt:
                lines.append(f"• {_truncate(txt, 160)}")
        if lines:
            sections.append("*Pain points détectés*\n" + "\n".join(lines))

    hooks = rj.get("personalization_hooks")
    if isinstance(hooks, list) and hooks:
        lines = [f"• {_truncate(str(h).strip(), 160)}" for h in hooks[:3] if str(h).strip()]
        if lines:
            sections.append("*Accroches / pistes d'automatisation*\n" + "\n".join(lines))

    meta_bits: list[str] = []
    tss = rj.get("tech_savvy_score")
    score = tss.get("score") if isinstance(tss, dict) else None
    if score:
        meta_bits.append(f"Tech-savvy : *{score}*")
    decideurs = rj.get("decideur_candidats")
    if isinstance(decideurs, list) and decideurs:
        names = []
        for d in decideurs[:2]:
            if isinstance(d, dict) and d.get("nom_complet"):
                titre = d.get("titre")
                names.append(f"{d['nom_complet']}" + (f" ({titre})" if titre else ""))
        if names:
            meta_bits.append("Décideur(s) : " + ", ".join(names))
    if meta_bits:
        sections.append(" · ".join(meta_bits))

    if not sections:
        return []

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔎 Brief pré-RDV*"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n\n".join(sections)},
        },
    ]


def build_booked_blocks(
    *,
    contact_name: str,
    company_name: str | None,
    contact_email: str | None,
    meeting_start_iso: str,
    meeting_url: str | None = None,
    event_type: str | None = None,
    research_json: dict[str, Any] | None = None,
    reacti_ticket: "ReactiTicket | None" = None,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un meeting confirmé via Cal.com (WF-8).

    Si `research_json` est fourni (depuis `companies.research_json`), on ajoute
    une section 'Brief pré-RDV' avec résumé, pain points, accroches et décideurs —
    pour arriver au RDV préparé sans ouvrir la DB.

    Si `reacti_ticket` est fourni (track REACTI uniquement), on ajoute une ligne
    'économie commission' : verticale + ticket moyen défaut + commission estimée,
    pour arriver à l'appel avec les chiffres en tête. Absent pour un prospect OPT
    => le brief reste strictement inchangé.
    """
    tp = _track_prefix(track)
    fallback = f"{tp}✅ RDV booké — {contact_name} le {meeting_start_iso}"
    fields = [_kv_field("Contact", contact_name)]
    if contact_email:
        fields.append(_kv_field("Email", contact_email))
    if company_name:
        fields.append(_kv_field("Entreprise", company_name))
    fields.append(_kv_field("Quand", meeting_start_iso))
    if event_type:
        fields.append(_kv_field("Type", event_type))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}✅ Meeting booké"},
        },
        {"type": "section", "fields": fields},
    ]
    if meeting_url:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{meeting_url}|Ouvrir dans Cal.com>",
            },
        })
    if reacti_ticket is not None:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*💰 REACTI — économie commission (défaut)*\n"
                    f"Secteur : *{reacti_ticket.label}* · "
                    f"Ticket moyen : *~{reacti_ticket.ticket} $* · "
                    f"Commission {reacti_ticket.rate_pct} % : *~{reacti_ticket.commission} $/client*\n"
                    "_Confirmer le vrai ticket du client à l'appel._"
                ),
            },
        })
    blocks.extend(_research_brief_blocks(research_json))
    return fallback, blocks


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ----------------------------------------------------------------------
# Sync versions (pour scripts CLI / tests qui ne veulent pas async)
# ----------------------------------------------------------------------

def notify_sync(
    *,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    context: str | None = None,
    category: Category | None = None,
) -> bool:
    """Version sync de `notify` pour usage en script ou test. Bloquant."""
    url = _webhook_url(category)
    if not url:
        return False
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        r = httpx.post(url, json=payload, timeout=SLACK_TIMEOUT_SECONDS)
        if r.status_code == 200 and r.text.strip() == "ok":
            return True
        print(
            f"[slack:{context or '-'}] non-2xx response: {r.status_code} {r.text[:200]}",
            file=sys.stderr,
        )
        return False
    except Exception as e:  # noqa: BLE001
        print(
            f"[slack:{context or '-'}] exception {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False
