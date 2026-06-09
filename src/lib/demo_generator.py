"""Demo-generator (P3) — frappe un lien de démo unique par prospect, le persiste
dans agence.demo_sites, et l'injecte dans le corps du cold email.

Le lien ne résout vers rien jusqu'à P4 (site Next.js servi). Ici = couche donnée
+ wiring uniquement. Voir docs/superpowers/specs/2026-06-07-agence-ia-p3-demo-generator-design.md.
"""
from __future__ import annotations

import os
import secrets

from .. import supabase_client as db

DEMO_URL_PLACEHOLDER = "{{DEMO_URL}}"
_AGENCE_SCHEMA = "agence"


def _demo_base_url() -> str:
    return os.environ.get("DEMO_BASE_URL", "https://couture-ia.com").rstrip("/")


def inject_demo_link(body: str, demo_url: str) -> str:
    """Garantit que demo_url est présent dans body.

    Placeholder présent => remplace toutes les occurrences.
    Absent => append une ligne CTA fallback (le lien doit TOUJOURS finir dans le body).
    """
    if DEMO_URL_PLACEHOLDER in body:
        return body.replace(DEMO_URL_PLACEHOLDER, demo_url)
    suffix = f"\n\nVoici un aperçu personnalisé pour vous : {demo_url}"
    return f"{body}{suffix}"


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
