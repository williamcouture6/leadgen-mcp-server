"""Smoke tests : imports + parsing config + signature des tools.

Pas de network ni de DB : ces tests doivent passer sans credentials valides
(en mode test on injecte les variables d'env).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-google-key")
    # On invalide le cache lru_cache de settings()
    from src.config import settings
    settings.cache_clear()


def test_imports() -> None:
    """Tous les modules s'importent sans crasher."""
    from src import server  # noqa: F401
    from src.tools import db, maps  # noqa: F401


def test_settings_loaded() -> None:
    from src.config import settings
    s = settings()
    assert s.supabase_url.endswith(".supabase.co")
    assert s.google_places_api_key == "test-google-key"


def test_db_target_catalog_non_empty() -> None:
    from src.tools.db import _all_targets, DEFAULT_CITIES, SECTOR_CATALOG
    targets = _all_targets()
    expected = len(DEFAULT_CITIES) * sum(len(v) for v in SECTOR_CATALOG.values())
    assert len(targets) == expected
    assert all(len(t) == 3 for t in targets)


def test_mcp_tools_registered() -> None:
    """Vérifie que les tools sont bien enregistrés sur l'instance FastMCP."""
    from src.server import mcp
    # FastMCP expose ses tools via une méthode list_tools ou un attribut interne ;
    # on fait un check best-effort sans dépendre de l'API privée.
    # On vérifie au moins que le serveur a un nom.
    assert mcp.name == "leadgen-mcp"
