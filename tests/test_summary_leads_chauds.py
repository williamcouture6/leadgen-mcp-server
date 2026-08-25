"""PT3 — le bloc « leads chauds » du résumé quotidien.

Remplace le compteur aveugle « intéressés en attente de site : N » par une liste
nominative. Le socle patche les MODULES SOURCE (`sb`, `slack_mod`) et non
`http_api`, parce que summary_daily les importe LOCALEMENT dans la fonction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _il_y_a(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()


def _dans(jours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=jours)).isoformat()


def _socle(monkeypatch, *, chauds, supprimes=(), lectures_vue=None, vue_leve=False):
    """chauds : lignes rendues par agence.v_suivi_lead_courant.
    supprimes : itérable de courriels présents dans suppression_list (motif opt_out).
    lectures_vue : liste où empiler les params de chaque lecture de la vue.
    vue_leve : la lecture de la vue lève — le résumé doit le DIRE, pas se taire."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, schema=None, **kw):
        if table == "v_suivi_lead_courant":
            if lectures_vue is not None:
                lectures_vue.append({"params": params or {}, "schema": schema})
            if vue_leve:
                raise RuntimeError("boom")
            return chauds
        if table == "suppression_list":
            return [{"email": c, "reason": "opt_out", "created_at": None} for c in supprimes]
        if table == "contacts":
            return []
        return []

    async def fake_select(table, params=None, schema=None, **kw):
        return []

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def test_la_vue_est_lue_dans_le_schema_agence_et_epinglee_agence_ia(monkeypatch):
    """La vue vit dans le schéma `agence` et le bloc ne suit PAS payload.tracks :
    le projet a une seule offre, et la ligne s'imprimerait deux fois si elle
    restait dans la boucle par track (le cron passe OPT + agence-ia)."""
    lectures = []
    http_api = _socle(monkeypatch, chauds=[], lectures_vue=lectures)
    await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["OPT", "agence-ia"], post=False)
    )
    assert len(lectures) == 1, "la vue doit être lue UNE fois, hors de la boucle par track"
    assert lectures[0]["schema"] == "agence"
    assert lectures[0]["params"].get("track") == "eq.agence-ia"
