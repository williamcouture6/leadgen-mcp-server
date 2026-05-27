"""Slack Incoming Webhook — notifs minimales pour WF-7 / WF-8.

Pattern: un seul webhook URL (SLACK_WEBHOOK_URL env) pour tout le système.
Si l'env var est absente, les notifs sont silencieusement no-op — utile pour
les environnements dev/test sans Slack configuré.

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
from typing import Any

import httpx

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
SLACK_TIMEOUT_SECONDS = 5.0  # court — on ne veut pas bloquer la pipeline


def _webhook_url() -> str | None:
    url = os.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    return url or None


async def notify(
    *,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    context: str | None = None,
) -> bool:
    """Envoie un message Slack via Incoming Webhook. Async (httpx).

    Args:
      text: texte fallback (utilisé par notifs mobile + accessibilité).
      blocks: blocks Block Kit optionnels pour mise en forme riche.
      context: prefix court ajouté au log stderr en cas d'erreur (ex: "wf7_hot_lead").

    Returns True si Slack a accepté (200 OK), False sinon ou si pas configuré.
    NE LÈVE JAMAIS — la pipeline ne doit pas casser à cause de Slack.
    """
    url = _webhook_url()
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


def build_hot_lead_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    reply_preview: str,
    auto_reply_sent: bool,
    confidence: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply classé 'interested' (WF-7).

    Returns (fallback_text, blocks) — passer aux 2 args de `notify`.
    """
    status = "Auto-reply envoyé (Cal.com link)" if auto_reply_sent else "À répondre manuellement"
    fallback = f"🔥 Hot lead — {contact_name} @ {company_name} ({status})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔥 Hot lead"},
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
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply en review manuel (classifier hésite / 'other')."""
    fallback = f"⚠️ Review manuel — {contact_name} ({category}, conf {confidence:.0%})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️ Reply à reviewer"},
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


def build_booked_blocks(
    *,
    contact_name: str,
    company_name: str | None,
    contact_email: str | None,
    meeting_start_iso: str,
    meeting_url: str | None = None,
    event_type: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un meeting confirmé via Cal.com (WF-8)."""
    fallback = f"✅ RDV booké — {contact_name} le {meeting_start_iso}"
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
            "text": {"type": "plain_text", "text": "✅ Meeting booké"},
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
) -> bool:
    """Version sync de `notify` pour usage en script ou test. Bloquant."""
    url = _webhook_url()
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
