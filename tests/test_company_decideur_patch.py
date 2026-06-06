"""Test de câblage : update_company_research calcule le décideur résumé.

Pas de réseau : on monkeypatche db.update pour capturer le patch envoyé à
companies et vérifier les colonnes decideur_confirme/decideur_potentiel.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-google-key")
    from src.config import settings
    settings.cache_clear()


@pytest.mark.asyncio
async def test_update_company_research_sets_confirme(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.tools.db as dbt

    captured: dict = {}

    async def fake_update(table, patch, filters=None):
        captured["table"] = table
        captured["patch"] = patch
        return [{"id": "co-1"}]

    monkeypatch.setattr(dbt.db, "update", fake_update)

    research = {"decideur_candidats": [
        {"nom_complet": "Jean Tremblay", "titre": "Propriétaire",
         "source_url": "https://x.com", "confidence": "medium"}
    ]}
    emails = [{"local": "jean.tremblay", "kind": "nominative"}]

    await dbt.update_company_research("co-1", research, emails_found=emails)

    patch = captured["patch"]
    assert patch["decideur_confirme"] == {
        "nom_complet": "Jean Tremblay", "titre": "Propriétaire", "source_url": "https://x.com",
    }
    assert patch["decideur_potentiel"] is None


@pytest.mark.asyncio
async def test_update_company_research_sets_potentiel(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.tools.db as dbt

    captured: dict = {}

    async def fake_update(table, patch, filters=None):
        captured["patch"] = patch
        return [{"id": "co-2"}]

    monkeypatch.setattr(dbt.db, "update", fake_update)

    research = {"decideur_candidats": [
        {"nom_complet": "Luc Roy", "titre": "Directeur", "source_url": "s", "confidence": "medium"}
    ]}
    # Email générique -> pas de match nominatif, pas de high -> potentiel.
    await dbt.update_company_research("co-2", research, emails_found=[{"local": "info", "kind": "generic"}])

    patch = captured["patch"]
    assert patch["decideur_confirme"] is None
    assert patch["decideur_potentiel"]["nom_complet"] == "Luc Roy"
    assert patch["decideur_potentiel"]["confidence"] == "medium"
