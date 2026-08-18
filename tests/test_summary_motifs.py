"""Le résumé quotidien montre l'état du parc, pas seulement la journée.

Sans filtre de date, comme la ligne « ⏸ en attente de config » de P4.10 : c'est
un draft coincé depuis deux semaines qu'on veut voir, pas l'activité du jour.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


async def test_le_resume_montre_le_top_3_des_motifs(monkeypatch) -> None:
    from src import http_api
    from src import supabase_client as sb

    vues: list[dict] = []

    async def _select(table, *, params=None, schema=None):
        if table == "v_pourquoi_pas_de_courriel":
            vues.append(params or {})
            return (
                [{"motif": "aucun_contact", "recontactable": "plus_tard"}] * 40
                + [{"motif": "aucune_presence_web", "recontactable": "a_juger"}] * 20
                + [{"motif": "draft_a_relire", "recontactable": "a_juger"}] * 10
                + [{"motif": "en_file", "recontactable": "oui"}] * 5
            )
        return []

    monkeypatch.setattr(sb, "select", _select)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert "aucun_contact 40" in out["text"]
    assert "aucune_presence_web 20" in out["text"]
    assert "draft_a_relire 10" in out["text"]
    assert "en_file" not in out["text"], "le top 3 ne montre que les 3 premiers"
    assert "30" in out["text"], "le compte des a_juger doit apparaître"
    # La ligne « le temps ne réparera pas » porte le compte des a_juger : l'assert
    # « "30" in text » ci-dessus passerait aussi un 30 du 30 du mois dans la date.
    assert "🔎 30 " in out["text"]
    # Requête faite sur l'état du parc : pas de filtre de date, track explicite.
    assert len(vues) == 1
    assert "created_at" not in vues[0]
    assert vues[0]["track"] == "eq.agence-ia"


async def test_un_parc_sain_nencombre_pas_le_resume(monkeypatch) -> None:
    from src import http_api
    from src import supabase_client as sb

    async def _select(table, *, params=None, schema=None):
        if table == "v_pourquoi_pas_de_courriel":
            return [{"motif": "en_file", "recontactable": "oui"}] * 3
        return []

    monkeypatch.setattr(sb, "select", _select)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "à juger" not in out["text"]
    # Les deux vraies lignes, celles que le code produit réellement.
    assert "🧱" not in out["text"]
    assert "le temps ne réparera pas" not in out["text"]
