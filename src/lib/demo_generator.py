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
