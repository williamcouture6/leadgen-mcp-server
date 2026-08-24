"""Le résumé quotidien montre l'état du parc, pas seulement la journée.

Sans filtre de date, comme la ligne « 🔥 intéressés en attente de site » du
pivot tri : c'est un lead coincé depuis deux semaines qu'on veut voir, pas
l'activité du jour.

Tous les tests d'ici piègent `sb.select` pour qu'il LÈVE : le résumé compte des
relations qui dépassent 1000 lignes, et `select()` est plafonné par PostgREST.
Un appel qui repasserait par `select()` casse le test au lieu de rendre 1000 en
silence. Voir tests/test_supabase_plafond_1000.py.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# Le seul `select()` légitime du résumé : la sonde `agence.demo_sites` par
# intéressé (limit=1, donc insensible au plafond). Tout le reste doit passer par
# count() / select_all(). Interdire TOUT select mentirait le jour où un autre
# select borné, tout aussi légitime, s'ajoute — voir tests/test_summary_interested.py.
_SELECTS_LEGITIMES = {("demo_sites", "agence")}


def _interdit_select(monkeypatch) -> None:
    """`select()` est plafonné à 1000 : le résumé ne doit s'en servir que sur
    les lectures bornées connues."""
    from src import supabase_client as sb

    async def _boom(table, *, params=None, schema=None):
        if (table, schema) in _SELECTS_LEGITIMES:
            return []
        raise AssertionError(
            f"select() plafonné à 1000 lignes utilisé sur {table!r} — "
            "utiliser count() ou select_all()"
        )

    monkeypatch.setattr(sb, "select", _boom)


async def test_le_resume_montre_le_top_3_des_motifs(monkeypatch) -> None:
    from src import http_api
    from src import supabase_client as sb

    vues: list[dict] = []

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table == "v_pourquoi_pas_de_courriel":
            vues.append({**(params or {}), "_order": order})
            return (
                [{"motif": "aucun_contact", "recontactable": "plus_tard"}] * 40
                + [{"motif": "aucune_presence_web", "recontactable": "a_juger"}] * 20
                + [{"motif": "draft_a_relire", "recontactable": "a_juger"}] * 10
                + [{"motif": "en_file", "recontactable": "oui"}] * 5
            )
        return []

    async def _count(table, *, params=None, schema=None):
        return 0

    _interdit_select(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)
    monkeypatch.setattr(sb, "count", _count)
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
    # Requête faite sur l'état du parc : pas de filtre de date, track explicite,
    # et un ordre stable sans lequel la pagination sauterait des lignes.
    assert len(vues) == 1
    assert "created_at" not in vues[0]
    assert vues[0]["track"] == "eq.agence-ia"
    assert vues[0]["_order"] == "company_id"


async def test_un_parc_de_plus_de_1000_entreprises_est_compte_en_entier(monkeypatch) -> None:
    """Le cas qui casse : 1895 entreprises, le plafond PostgREST est à 1000.

    Compter par `len(select(...))` aurait rendu 1000 lignes en planner order,
    donc un top 3 faux et un `a_juger` faux, sans la moindre erreur."""
    from src import http_api
    from src import supabase_client as sb

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table == "v_pourquoi_pas_de_courriel":
            return (
                [{"motif": "jamais_researchee", "recontactable": "plus_tard"}] * 1200
                + [{"motif": "hors_cible", "recontactable": "hors_cible"}] * 500
                + [{"motif": "envoi_fantome", "recontactable": "a_juger"}] * 195
            )
        return []

    async def _count(table, *, params=None, schema=None):
        return 0

    _interdit_select(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)
    monkeypatch.setattr(sb, "count", _count)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert "jamais_researchee 1200" in out["text"]
    assert "hors_cible 500" in out["text"]
    assert "envoi_fantome 195" in out["text"]
    assert "🔎 195 " in out["text"]


async def test_les_chiffres_du_jour_viennent_du_compte_exact(monkeypatch) -> None:
    """Les six compteurs par track passent par count(), pas par len(select())."""
    from src import http_api
    from src import supabase_client as sb

    appels: list[tuple[str, dict]] = []

    async def _count(table, *, params=None, schema=None):
        appels.append((table, params or {}))
        return {"companies": 7, "contacts": 5}.get(table, 0)

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        return []

    _interdit_select(monkeypatch)
    monkeypatch.setattr(sb, "count", _count)
    monkeypatch.setattr(sb, "select_all", _select_all)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert out["totals"]["agence-ia"]["sourced"] == 7
    assert out["totals"]["agence-ia"]["emails"] == 5
    assert "sourcées 7 · emails 5" in out["text"]
    # le filtre de date du jour tient toujours sur ces compteurs-là
    tables = {t for t, _ in appels}
    assert "companies" in tables
    assert any("created_at" in p for _, p in appels)


async def test_un_parc_sain_nencombre_pas_le_resume(monkeypatch) -> None:
    from src import http_api
    from src import supabase_client as sb

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table == "v_pourquoi_pas_de_courriel":
            return [{"motif": "en_file", "recontactable": "oui"}] * 3
        return []

    async def _count(table, *, params=None, schema=None):
        return 0

    _interdit_select(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)
    monkeypatch.setattr(sb, "count", _count)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "à juger" not in out["text"]
    # Les deux vraies lignes, celles que le code produit réellement.
    assert "🧱" not in out["text"]
    assert "le temps ne réparera pas" not in out["text"]
