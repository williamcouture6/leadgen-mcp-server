"""Tests REACTI — catalogue de sourcing dédié + isolation track du pipeline
(anti double-fichage). Le param `track` (défaut OPT) ne doit jamais laisser une
verticale REACTI fuiter dans le flux OPT et inversement."""
from __future__ import annotations

import pytest

from src import supabase_client as real_db
import src.tools.db as dbt

REACTI_VERTICALS = {
    "entrepreneur en déneigement",
    "paysagiste",
    "tonte de gazon",
    "tonte de pelouse",
    "exterminateur",
    "piscines et spas",
    "lavage de vitres",
}


# ---------------------------------------------------------------- Catalogue

def test_reacti_catalog_contient_les_5_verticales() -> None:
    assert set(dbt.REACTI_SECTOR_CATALOG["commerce_local"]) == REACTI_VERTICALS


def test_catalogs_map_opt_et_reacti() -> None:
    assert dbt._CATALOGS["OPT"] is dbt.SECTOR_CATALOG
    # Clé 'agence-ia' (pivot 2026-06-07) ; variable REACTI_SECTOR_CATALOG legacy.
    assert dbt._CATALOGS["agence-ia"] is dbt.REACTI_SECTOR_CATALOG


def test_all_targets_reacti_ne_renvoie_que_verticales_reacti() -> None:
    sectors = {sector for _city, sector, _icp in dbt._all_targets("agence-ia")}
    assert sectors == REACTI_VERTICALS


def test_all_targets_opt_exclut_les_verticales_reacti() -> None:
    """Régression anti double-fichage : OPT ne source plus les verticales REACTI."""
    sectors = {sector for _city, sector, _icp in dbt._all_targets("OPT")}
    assert sectors.isdisjoint(REACTI_VERTICALS)


def test_all_targets_track_inconnu_retombe_sur_opt() -> None:
    assert dbt._all_targets("BOGUS") == dbt._all_targets("OPT")


def test_company_in_track_defaut_opt() -> None:
    assert dbt.CompanyIn(name="X").track == "OPT"
    assert dbt.CompanyIn(name="Y", track="agence-ia").track == "agence-ia"


# ----------------------------------------------- Isolation track (sélection)

@pytest.mark.asyncio
async def test_research_filtre_track(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_select(table, params=None):
        captured["params"] = params or {}
        return []

    monkeypatch.setattr(real_db, "select", fake_select)

    await dbt.list_companies_to_research(track="agence-ia")
    assert captured["params"].get("track") == "eq.agence-ia"


@pytest.mark.asyncio
async def test_personalize_isole_par_track_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un contact dont la company est OPT ne sort PAS quand on demande agence-ia."""

    async def fake_select(table, params=None):
        if table == "contacts":
            return [
                {"id": "ct-opt", "company_id": "co-opt", "email": "a@opt.ca", "status": "new"},
                {"id": "ct-rea", "company_id": "co-rea", "email": "b@rea.ca", "status": "new"},
            ]
        if table == "companies":
            return [
                {"id": "co-opt", "name": "OPT Co", "track": "OPT", "research_json": {"x": 1}},
                {"id": "co-rea", "name": "REA Co", "track": "agence-ia", "research_json": {"x": 1}},
            ]
        return []  # messages

    monkeypatch.setattr(real_db, "select", fake_select)

    out = await dbt.list_contacts_to_personalize(track="agence-ia")
    assert {o["contact"]["email"] for o in out} == {"b@rea.ca"}

    out_opt = await dbt.list_contacts_to_personalize(track="OPT")
    assert {o["contact"]["email"] for o in out_opt} == {"a@opt.ca"}


# ----------------------------------------------- Prompt personalize par track

def test_reacti_personalize_prompt_wired() -> None:
    """Le track REACTI charge prompts/reacti/personalize.md, pas le prompt OPT."""
    import src.tools.personalize as pz

    assert pz._PROMPT_PATHS["OPT"] != pz._PROMPT_PATHS["agence-ia"]
    assert pz._PROMPT_PATHS["agence-ia"].exists()
    txt = pz._PROMPT_PATHS["agence-ia"].read_text(encoding="utf-8")
    assert "REACTI" in txt
    assert "réactivation" in txt.lower()
    # garde-fous critiques présents dans le prompt REACTI
    assert "Loi 25" in txt
    assert "preuve sociale" in txt.lower()
