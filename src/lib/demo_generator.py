"""Demo-generator — frappe un lien de démo unique par prospect et le persiste
dans agence.demo_sites.

Depuis le pivot tri (2026-08-20), la frappe n'a plus lieu dans le pipeline
d'envoi : `ensure_demo_site` est appelé par le geste CLI de la session
artisanale (PT2), et William colle lui-même le lien dans sa réponse au
prospect. La ligne demo_sites créée fait aussi sortir le lead du compteur
« intéressés en attente de site » du résumé quotidien.
"""
from __future__ import annotations

import os
import secrets

from .. import supabase_client as db

_AGENCE_SCHEMA = "agence"


def _demo_base_url() -> str:
    return os.environ.get("DEMO_BASE_URL", "https://couture-ia.com").rstrip("/")


async def ensure_demo_site(company_id: str | None, contact_id: str) -> str:
    """Retourne l'url_unique du demo_site du prospect. Idempotent par contact_id.

    Si une ligne existe déjà pour ce contact, on la réutilise (pas de re-frappe,
    pas de doublon). Sinon on frappe token+url et on insère dans agence.demo_sites.
    Lève (httpx.HTTPStatusError) si l'accès au schéma agence échoue — l'appelant
    décide quoi faire (soft-fail au draft, garde au send).
    """
    existing = await db.select(
        "demo_sites",
        params={
            "select": "url_unique",
            "contact_id": f"eq.{contact_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
        schema=_AGENCE_SCHEMA,
    )
    if existing:
        return existing[0]["url_unique"]

    token = secrets.token_urlsafe(16)
    url_unique = f"{_demo_base_url()}/demo/{token}"
    rows = await db.insert(
        "demo_sites",
        {
            "company_id": company_id,
            "contact_id": contact_id,
            "url_unique": url_unique,
            "token": token,
            "statut": "genere",
        },
        schema=_AGENCE_SCHEMA,
    )
    return rows[0]["url_unique"] if rows else url_unique
