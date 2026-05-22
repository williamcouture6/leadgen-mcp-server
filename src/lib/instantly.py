"""Instantly API v2 — push de leads dans une campagne pour envoi.

Pattern adopté : campagne Instantly pré-configurée par William avec template
  Subject: {{email_subject}}
  Body:    {{email_body}}

WF-6 pousse chaque draft approuvé en tant que lead dans la campagne, en passant
le subject + body comme custom variables. Instantly gère :
  - scheduling (warmup ramp, daily cap, sending hours)
  - tracking opens/replies/bounces
  - unsubscribe link (configuré dans la campagne)
  - reputation / deliverability

On NE veut PAS qu'Instantly re-personnalise au-delà des variables qu'on injecte
— le contenu sort intégralement de WF-4 (Personalize Agent) et a déjà été
validé par WF-5 (Compliance Agent).

Ref: https://developer.instantly.ai/api/v2/lead/createlead
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Ne retry QUE les erreurs réseau transitoires (timeout, connection drop).
# Les InstantlyError (4xx/5xx HTTP, payload invalide) signalent un problème
# côté Instantly ou côté config — retry ne va rien arranger et tenacity
# masquerait l'exception originale derrière RetryError sans `reraise=True`.
_RETRY_ON_HTTP_ERROR = retry_if_exception_type(httpx.HTTPError)

INSTANTLY_API_BASE = "https://api.instantly.ai/api/v2"


class InstantlyError(Exception):
    """Raised when Instantly API returns non-2xx ou payload incohérent."""


def _api_key() -> str:
    k = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not k:
        raise InstantlyError("INSTANTLY_API_KEY absent")
    return k


def _campaign_id() -> str:
    cid = os.environ.get("INSTANTLY_CAMPAIGN_ID", "").strip()
    if not cid:
        raise InstantlyError("INSTANTLY_CAMPAIGN_ID absent")
    return cid


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


@retry(
    retry=_RETRY_ON_HTTP_ERROR,
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)
async def add_lead_to_campaign(
    *,
    email: str,
    subject: str,
    body_text: str,
    first_name: str | None = None,
    last_name: str | None = None,
    company_name: str | None = None,
    campaign_id: str | None = None,
    skip_if_in_workspace: bool = True,
    skip_if_in_campaign: bool = True,
) -> dict[str, Any]:
    """Crée un lead dans la campagne Instantly avec subject + body injectés en
    custom variables.

    Args:
      email: destinataire (vérifié en amont par WF-2 ou scrapé par WF-3).
      subject / body_text: contenu déjà personnalisé + validé par WF-5.
      first_name / last_name / company_name: redondance pour Instantly tracking.
      campaign_id: override (sinon INSTANTLY_CAMPAIGN_ID env).
      skip_if_in_workspace: True = pas de duplicate lead à travers tout le compte.
      skip_if_in_campaign: True = idem au scope campagne.

    Returns le payload Instantly (contient `id` = lead_id Instantly).

    Raises InstantlyError sur 4xx/5xx ou format inattendu.
    """
    cid = (campaign_id or _campaign_id()).strip()
    body: dict[str, Any] = {
        "campaign": cid,
        "email": email,
        "skip_if_in_workspace": skip_if_in_workspace,
        "skip_if_in_campaign": skip_if_in_campaign,
        # Custom variables référencées dans le template de la campagne Instantly
        # (Subject = {{email_subject}}, Body = {{email_body}}).
        "custom_variables": {
            "email_subject": subject,
            "email_body": body_text,
        },
    }
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if company_name:
        body["company_name"] = company_name

    url = f"{INSTANTLY_API_BASE}/leads"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=_headers(), json=body)
    except httpx.HTTPError as e:
        raise InstantlyError(f"HTTP error Instantly: {type(e).__name__}: {e}") from e

    if r.status_code >= 400:
        raise InstantlyError(
            f"Instantly /leads status {r.status_code}: {r.text[:300]}"
        )

    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise InstantlyError(f"Instantly response not JSON: {r.text[:200]}") from e

    if not isinstance(data, dict) or not data.get("id"):
        raise InstantlyError(f"Instantly response missing lead id: {data!r}")
    return data


@retry(
    retry=_RETRY_ON_HTTP_ERROR,
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)
async def get_campaign(campaign_id: str | None = None) -> dict[str, Any]:
    """Récupère les métadonnées d'une campagne. Utile pour healthcheck pré-envoi."""
    cid = (campaign_id or _campaign_id()).strip()
    url = f"{INSTANTLY_API_BASE}/campaigns/{cid}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers=_headers())
    if r.status_code >= 400:
        raise InstantlyError(
            f"Instantly /campaigns status {r.status_code}: {r.text[:300]}"
        )
    return r.json()
