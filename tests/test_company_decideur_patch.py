"""Test de câblage : update_company_research calcule le décideur résumé.

Pas de réseau : on monkeypatche db.update pour capturer le patch envoyé à
companies et vérifier les colonnes decideur_confirme/decideur_potentiel.
db.select est mocké aussi depuis que le statut dépend des contacts en base.
"""
from __future__ import annotations

import pytest


def _fake_contacts(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Le statut lit désormais `contacts` : sans ce mock, appel réseau réel."""
    import src.tools.db as dbt

    async def fake_select(table, *, params=None, schema=None):
        return [{"id": f"ct-{i}"} for i in range(n)] if table == "contacts" else []

    monkeypatch.setattr(dbt.db, "select", fake_select)


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
    _fake_contacts(monkeypatch, 1)

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
    _fake_contacts(monkeypatch, 1)

    research = {"decideur_candidats": [
        {"nom_complet": "Luc Roy", "titre": "Directeur", "source_url": "s", "confidence": "medium"}
    ]}
    # Email générique -> pas de match nominatif, pas de high -> potentiel.
    await dbt.update_company_research("co-2", research, emails_found=[{"local": "info", "kind": "generic"}])

    patch = captured["patch"]
    assert patch["decideur_confirme"] is None
    assert patch["decideur_potentiel"]["nom_complet"] == "Luc Roy"
    assert patch["decideur_potentiel"]["confidence"] == "medium"


@pytest.mark.asyncio
async def test_update_company_research_promotes_status_enriched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research AVEC contact => status passe à 'enriched', mais jamais sur une boîte
    déjà disqualified/suppressed (gardé par le filtre PostgREST).

    Sans contact le statut vaut 'researched_no_contact' — couvert par
    tests/test_save_research_status.py.
    """
    import src.tools.db as dbt

    captured: dict = {}

    async def fake_update(table, patch, *, filters):
        captured["patch"] = patch
        captured["filters"] = filters
        return [{"id": "co-3"}]

    monkeypatch.setattr(dbt.db, "update", fake_update)
    _fake_contacts(monkeypatch, 1)

    await dbt.update_company_research("co-3", {"company_summary": "x"}, emails_found=[])

    assert captured["patch"]["status"] == "enriched"
    # Le filtre protège les boîtes écartées : on ne re-promeut pas un statut terminal.
    assert captured["filters"]["status"] == "not.in.(disqualified,suppressed)"
    assert captured["filters"]["id"] == "eq.co-3"
