"""La découverte ne repaie pas un courriel qu'on possède déjà.

🔴 LE TROU ÉTAIT DORMANT, ET UN BACKFILL L'A RÉVEILLÉ.

`list_companies_to_discover` filtre sur « aucun site, aucune recherche,
status=sourced ». Ces quatre conditions ne disent RIEN des contacts — elles
décrivaient « jamais traitée » tant que `status='sourced'` n'était posé qu'au
sourcing. Le backfill du 2026-09-20 a remis 528 fiches en `sourced` : d'un coup,
26 des 112 candidates avaient déjà un courriel en base, dont 21 venaient de là.

Coût du trou : ~0,09 $ par fiche (`reacti_discover` est l'agent le plus cher du
dépôt) pour retrouver une adresse qu'on possède. Et tout reset futur le
rouvrirait de la même façon — d'où un correctif dans la SÉLECTION, jamais un
rattrapage en base.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


@pytest.mark.anyio
async def test_une_entreprise_avec_courriel_est_ecartee(monkeypatch) -> None:
    from src.tools import db as dbt

    async def _faux_select(table, *, params=None, **_k):
        return [
            {"id": "a", "name": "Sans courriel"},
            {"id": "b", "name": "A DEJA un courriel"},
        ]

    async def _faux_tranches(table, *, ids, cle="id", params=None, **_k):
        assert cle == "company_id"
        assert params["email"] == "not.is.null"
        return [{"company_id": "b"}]

    monkeypatch.setattr(dbt.db, "select", _faux_select)
    monkeypatch.setattr(dbt, "_select_par_tranches", _faux_tranches)

    lot = await dbt.list_companies_to_discover(10)
    assert [c["id"] for c in lot] == ["a"]


@pytest.mark.anyio
async def test_la_selection_sur_lit_pour_pouvoir_ecarter(monkeypatch) -> None:
    """Sans sur-lecture, écarter des candidates rendrait un lot plus court que
    `limit` — et le débit quotidien tomberait en silence."""
    from src.tools import db as dbt

    vus: dict[str, str] = {}

    async def _faux_select(table, *, params=None, **_k):
        vus.update(params or {})
        return []

    monkeypatch.setattr(dbt.db, "select", _faux_select)
    await dbt.list_companies_to_discover(20)
    assert int(vus["limit"]) > 20, "il faut sur-lire pour pouvoir écarter"


@pytest.mark.anyio
async def test_le_lot_ne_depasse_jamais_la_limite(monkeypatch) -> None:
    from src.tools import db as dbt

    async def _faux_select(table, *, params=None, **_k):
        return [{"id": str(i), "name": f"n{i}"} for i in range(80)]

    async def _aucun(table, *, ids, cle="id", params=None, **_k):
        return []

    monkeypatch.setattr(dbt.db, "select", _faux_select)
    monkeypatch.setattr(dbt, "_select_par_tranches", _aucun)

    assert len(await dbt.list_companies_to_discover(20)) == 20


@pytest.mark.anyio
async def test_aucune_candidate_ne_declenche_aucune_lecture_de_contacts(
    monkeypatch,
) -> None:
    """Un `in.()` vide est une URL malformée — et une requête pour rien."""
    from src.tools import db as dbt

    appels: list[str] = []

    async def _vide(table, *, params=None, **_k):
        return []

    async def _tranches(table, **_k):
        appels.append(table)
        return []

    monkeypatch.setattr(dbt.db, "select", _vide)
    monkeypatch.setattr(dbt, "_select_par_tranches", _tranches)

    assert await dbt.list_companies_to_discover(10) == []
    assert appels == []
