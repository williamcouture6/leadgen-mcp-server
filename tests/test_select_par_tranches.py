"""Le découpage des filtres `in.(...)` — la moitié de la sélection que rien ne couvrait.

Relevé par un conseil le 2026-09-13 : aucun test ne mentionnait
`_select_par_tranches`, `TAILLE_TRANCHE_IN` ni `PLAFOND_POSTGREST`, alors que
c'est ce code qui empêche trois modes de panne muets.

**Pourquoi il existe.** Un filtre PostgREST `in.(...)` voyage dans l'URL. La
file fait 345 contacts au 2026-09-09 et grossit ; leurs identifiants font déjà
~13 ko. Au-delà du seuil du serveur, la requête revient en 414 et le lot se
vide **en silence**.

**Et pourquoi certaines tranches paginent.** PostgREST coupe TOUTE réponse à
1000 lignes, sans erreur ni en-tête. Une tranche de 120 contacts qui porte plus
de 1000 messages perdrait des lignes — donc des contacts disparaîtraient
d'`already_drafted`, et recevraient un **deuxième courriel**.
"""
from __future__ import annotations

import pytest

from src.tools import db as dbt


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _faux_select(monkeypatch: pytest.MonkeyPatch, reponse, vus: list):
    async def select(table, params=None):
        p = params or {}
        vus.append(p)
        return reponse(p) if callable(reponse) else reponse

    monkeypatch.setattr(dbt.db, "select", select)


async def test_les_identifiants_sont_decoupes_en_tranches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vus: list[dict] = []
    ids = [f"id{i}" for i in range(250)]
    _faux_select(monkeypatch, lambda p: [], vus)

    await dbt._select_par_tranches("contacts", params={"select": "id"}, ids=ids)

    assert len(vus) == 3, f"250 identifiants par tranches de 120 → 3 requêtes, vu {len(vus)}"
    # Aucune tranche ne dépasse la taille prévue…
    tailles = [len(p["id"].removeprefix("in.(").rstrip(")").split(",")) for p in vus]
    assert tailles == [120, 120, 10], tailles
    # …et tous les identifiants passent, une seule fois chacun.
    envoyes = [x for p in vus for x in p["id"].removeprefix("in.(").rstrip(")").split(",")]
    assert envoyes == ids


async def test_les_resultats_des_tranches_sont_concatenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vus: list[dict] = []
    ids = [f"id{i}" for i in range(200)]

    def reponse(p):
        demandes = p["id"].removeprefix("in.(").rstrip(")").split(",")
        return [{"id": x} for x in demandes]

    _faux_select(monkeypatch, reponse, vus)

    lignes = await dbt._select_par_tranches("contacts", params={"select": "id"}, ids=ids)

    assert [x["id"] for x in lignes] == ids, "une tranche a perdu ou dupliqué des lignes"


async def test_une_liste_vide_ne_demande_rien(monkeypatch: pytest.MonkeyPatch) -> None:
    vus: list[dict] = []
    _faux_select(monkeypatch, [], vus)
    assert await dbt._select_par_tranches("messages", params={}, ids=[]) == []
    assert vus == [], "une liste vide ne doit produire AUCUNE requête"


async def test_la_cle_de_filtre_est_respectee(monkeypatch: pytest.MonkeyPatch) -> None:
    # `companies` filtre sur `id`, `messages` sur `contact_id`, les frères sur
    # `company_id`. Se tromper de clé rend un lot vide, ou pire, le mauvais.
    vus: list[dict] = []
    _faux_select(monkeypatch, [], vus)
    await dbt._select_par_tranches("messages", params={}, ids=["a"], cle="contact_id")
    assert "contact_id" in vus[0] and "id" not in vus[0]


async def test_le_mode_pagine_redemande_tant_que_la_page_est_pleine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une tranche de messages peut à elle seule dépasser le plafond."""
    vus: list[dict] = []
    plafond = dbt.PLAFOND_POSTGREST

    def reponse(p):
        depart = int(p["offset"])
        # 1500 lignes au total : une page pleine, puis une page courte.
        restant = max(0, 1500 - depart)
        return [{"contact_id": "c"}] * min(plafond, restant)

    _faux_select(monkeypatch, reponse, vus)

    lignes = await dbt._select_par_tranches(
        "messages", params={}, ids=["a"], cle="contact_id", order="id",
    )

    assert len(lignes) == 1500, f"{len(lignes)} lignes au lieu de 1500 — troncature muette"
    assert len(vus) == 2, "il faut redemander tant que la page revient pleine"
    assert vus[0]["order"] == "id", "sans ordre stable, deux pages peuvent sauter une ligne"


async def test_une_tranche_non_paginee_qui_revient_pleine_crie(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Le mode de panne le plus cher du projet est celui qu'on ne voit pas.

    Une tranche sans pagination qui revient EXACTEMENT au plafond a très
    probablement été coupée. On ne peut pas le prouver, on peut le dire.
    """
    vus: list[dict] = []
    _faux_select(monkeypatch, lambda p: [{"id": "x"}] * dbt.PLAFOND_POSTGREST, vus)

    with caplog.at_level("WARNING", logger="wf4"):
        await dbt._select_par_tranches("contacts", params={}, ids=["a"], cle="company_id")

    assert any("TRONQU" in m.upper() for m in caplog.messages), caplog.messages


async def test_une_tranche_normale_ne_crie_pas(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    vus: list[dict] = []
    _faux_select(monkeypatch, lambda p: [{"id": "x"}] * 150, vus)
    with caplog.at_level("WARNING", logger="wf4"):
        await dbt._select_par_tranches("contacts", params={}, ids=["a"], cle="company_id")
    assert caplog.messages == []
