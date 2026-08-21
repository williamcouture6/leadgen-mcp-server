"""PT1 — la ligne « intéressés en attente de site » du résumé quotidien.
N = contacts.interested_at non nul ET aucune ligne agence.demo_sites pour ce
contact. La frappe du jeton (PT2) fait redescendre N sans écriture dédiée."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _socle(monkeypatch, *, interesses, demo_par_contact):
    """demo_par_contact : dict contact_id -> lignes demo_sites à retourner.

    ⚠️ summary_daily importe `sb` et `slack_lib` LOCALEMENT dans la fonction
    (`from . import supabase_client as sb` / `from .lib import slack as
    slack_lib`) : patcher les attributs des MODULES SOURCE, pas http_api."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, **kw):
        if table == "contacts" and "interested_at" in (params or {}):
            return interesses
        return []  # dont la vue v_pourquoi_pas_de_courriel : vide suffit ici

    async def fake_select(table, params=None, schema=None, **kw):
        assert table == "demo_sites" and schema == "agence"
        cid = (params or {}).get("contact_id", "").removeprefix("eq.")
        return demo_par_contact.get(cid, [])

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def test_compte_les_interesses_sans_demo(monkeypatch):
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1"}, {"id": "ct-2"}, {"id": "ct-3"}],
        demo_par_contact={"ct-2": [{"id": "d-1"}]},  # ct-2 est servi → sort du compteur
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 2
    assert "intéressés en attente de site 2" in out["text"]


async def test_zero_interesse_pas_de_ligne(monkeypatch):
    http_api = _socle(monkeypatch, interesses=[], demo_par_contact={})
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert "en attente de site" not in out["text"]
