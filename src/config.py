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
    apollo_api_key: str | None = Field(default_factory=lambda: os.environ.get("APOLLO_API_KEY"))
    log_level: str = Field(default_factory=lambda: os.environ.get("LOG_LEVEL", "info"))


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
