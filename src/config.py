"""Chargement de configuration depuis le .env du repo."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


load_dotenv(_repo_root() / ".env")


class Settings(BaseModel):
    supabase_url: str = Field(default_factory=lambda: os.environ["SUPABASE_URL"])
    supabase_service_role_key: str = Field(
        default_factory=lambda: os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )
    google_places_api_key: str = Field(
        default_factory=lambda: os.environ["GOOGLE_PLACES_API_KEY"]
    )
    pexels_api_key: str = Field(
        default_factory=lambda: os.environ.get("PEXELS_API_KEY", "")
    )
    render_service_url: str = Field(
        default_factory=lambda: os.environ.get("RENDER_SERVICE_URL", "")
    )
    render_service_token: str = Field(
        default_factory=lambda: os.environ.get("RENDER_SERVICE_TOKEN", "")
    )
    log_level: str = Field(default_factory=lambda: os.environ.get("LOG_LEVEL", "info"))


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


# ----------------------------------------------------------------------
# Validation des variables d'environnement au démarrage (audit #10)
# ----------------------------------------------------------------------

# Requises : le serveur est inutilisable sans (auth + accès DB).
REQUIRED_ENV: tuple[str, ...] = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "AGENTS_HTTP_TOKEN",
)

# Recommandées : leur absence fait échouer SILENCIEUSEMENT une étape du pipeline
# (pas le boot). On loggue un warning clair au démarrage pour attraper un
# misconfig au deploy plutôt qu'au 1er envoi/cron.
RECOMMENDED_ENV: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",       # research / personalize / compliance / reply / meeting
    "GOOGLE_PLACES_API_KEY",   # WF-1 sourcing
    "INSTANTLY_API_KEY",       # WF-6 send + WF-7 poll
    "INSTANTLY_CAMPAIGN_ID",   # WF-6 send OPT
    "CALCOM_API_KEY",          # créneaux CTA (personalize / reply)
    "CALCOM_WEBHOOK_SECRET",   # WF-8 booking webhook
    "PEXELS_API_KEY",          # build_brand_kit images stock par industrie
    "RENDER_SERVICE_URL",      # build_brand_kit escalade headless (sites JS)
)


def validate_env() -> dict[str, list[str]]:
    """Liste les variables d'env manquantes (requises vs recommandées).

    Ne lève PAS d'exception — retourne les listes pour que l'appelant décide
    (le startup hook loggue ; il ne bloque pas le boot, fail-soft)."""
    def _missing(names: tuple[str, ...]) -> list[str]:
        return [n for n in names if not os.environ.get(n, "").strip()]

    return {
        "missing_required": _missing(REQUIRED_ENV),
        "missing_recommended": _missing(RECOMMENDED_ENV),
    }
