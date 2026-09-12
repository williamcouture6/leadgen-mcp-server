"""La ceinture COMPARE, elle ne refuse pas.

🔴 EN CONVERSATION A, LE VERDICT NE CHANGE JAMAIS. La colonne est neuve, le
backfill peut avoir été rattrapé par le workflow de recherche, et 119 tests du
dépôt construisent des fiches sans `fenetre_mois`. Une ceinture qui refuse quand
la colonne manque rendrait `drafts_created=0` deux fois par jour.

Ce que la comparaison achète : la parité sur données RÉELLES, deux fois par jour,
sans toucher un destinataire.
"""
from __future__ import annotations

import datetime
import inspect

from src import http_api
from src.tools import db as db_tools

import pytest

SEPTEMBRE = datetime.date(2026, 9, 15)   # seul le déneigement est ouvert

DENEIGEUR = {
    "id": "co-1", "name": "Déneigement Test", "track": "agence-ia",
    "website": "https://ex.ca",
    "research_json": {"services_offered": ["Déneigement résidentiel"]},
}
CONTACT = {
    "id": "ct-1", "company_id": "co-1", "email": "a@ex.ca", "status": "new",
    "track": "agence-ia", "created_at": "2026-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _compteurs_propres():
    db_tools.DIVERGENCES_FENETRE.clear()
    db_tools.COMPARAISONS_FENETRE.clear()
    yield
    db_tools.DIVERGENCES_FENETRE.clear()
    db_tools.COMPARAISONS_FENETRE.clear()


def test_sans_colonne_le_verdict_est_celui_du_calcul():
    """Le cas des 119 tests du dépôt : aucune colonne, aucun refus, rien à
    comparer."""
    assert db_tools.fenetre_saisonniere_ouverte(
        DENEIGEUR, track="agence-ia", aujourdhui=SEPTEMBRE
    ) is True
    assert db_tools.COMPARAISONS_FENETRE == set()


def test_une_divergence_ne_change_PAS_le_verdict():
    """🔴 Le cœur du mode observation : la colonne ment, le lead sort quand même."""
    fiche = {**DENEIGEUR, "fenetre_mois": [1, 2, 3]}

    verdict = db_tools.fenetre_saisonniere_ouverte(
        fiche, track="agence-ia", aujourdhui=SEPTEMBRE
    )

    assert verdict is True
    assert "co-1" in db_tools.DIVERGENCES_FENETRE
    assert "co-1" in db_tools.COMPARAISONS_FENETRE


def test_laccord_compte_une_comparaison_sans_divergence():
    fiche = {**DENEIGEUR, "fenetre_mois": [8, 9, 10, 11, 12]}
    db_tools.fenetre_saisonniere_ouverte(fiche, track="agence-ia", aujourdhui=SEPTEMBRE)
    assert db_tools.DIVERGENCES_FENETRE == set()
    assert "co-1" in db_tools.COMPARAISONS_FENETRE


def test_le_champ_existe_et_le_compteur_est_remis_a_zero():
    """🔴 Sans ça, l'étape « remonter le compte » peut être sautée en silence :
    Pydantic AVALE un kwarg inconnu. C'est déjà arrivé dans ce fichier —
    `repli_lexique` était passé au site du verrou alors que le modèle déclare
    `lexique_de_repli`."""
    assert "divergences_fenetre" in http_api.RunWf4Out.model_fields
    assert "comparaisons_fenetre" in http_api.RunWf4Out.model_fields
    assert "lexique_de_repli" in http_api.RunWf4Out.model_fields
    src = inspect.getsource(http_api._run_wf4)
    # 🔴 LE NOM AU SITE D'APPEL, PAS SEULEMENT DANS LE MODELE. Mesure par
    # mutation : renommer `divergences_fenetre=` en `divergence_fenetre=` au
    # site nominal passait les 1776 tests -- Pydantic avale le kwarg inconnu et
    # le compteur reste a 0 pour toujours. C'est exactement ce qui etait arrive
    # a `repli_lexique` au site du verrou.
    nominal = inspect.getsource(http_api)
    assert "divergences_fenetre=len(" in nominal, (
        "le compteur de divergences n'est pas remonte : Pydantic avale le kwarg"
    )
    assert "comparaisons_fenetre=len(" in nominal, (
        "le compteur de comparaisons n'est pas remonte -- or c'est LUI qui leve "
        "l'ambiguite de divergences=0"
    )
    assert "COMPARAISONS_FENETRE.clear()" in src, (
        "seul un des deux compteurs est remis a zero"
    )
    assert "DIVERGENCES_FENETRE.clear()" in src, (
        "le compteur n'est pas remis à zéro : il cumule sur toute la vie du "
        "processus et le chiffre remonté ne veut plus rien dire"
    )


async def test_la_ligne_divergente_SORT_bien_de_la_selection(monkeypatch):
    """🔴 Le test de CÂBLAGE. Le test unitaire ne prouve pas que la sélection
    laisse sortir la ligne — on pourrait mettre un `return False` dans la
    ceinture et le voir rester vert."""
    fiche = {**DENEIGEUR, "fenetre_mois": [1, 2, 3]}

    async def _select(table, params=None, **_):
        if table == "companies":
            # 🔴 LE FAUX EXIGE LA PROJECTION. Mesure par mutation le 2026-09-12 :
            # retirer `fenetre_mois` de la chaine `select` de `_retenir` passait
            # les 1776 tests. En production la colonne serait absente de chaque
            # ligne, l'observation court-circuitee, et `comparaisons_fenetre`
            # resterait a 0 -- ce qui se lit, par la convention que cette meme
            # conversation installe, comme << l'ecrivain est en panne >>.
            demande = (params or {}).get("select", "")
            assert "fenetre_mois" in demande, (
                "la selection ne PROJETTE plus fenetre_mois : l'observation "
                "sera court-circuitee en production"
            )
        return {"contacts": [dict(CONTACT)], "companies": [dict(fiche)],
                "messages": []}.get(table, [])

    monkeypatch.setattr(db_tools.db, "select", _select)

    class _FauxDate(datetime.date):
        @classmethod
        def today(cls):
            return SEPTEMBRE

    monkeypatch.setattr(db_tools, "date", _FauxDate)

    rendus = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    assert [r["contact"]["id"] for r in rendus] == ["ct-1"], "la ceinture a REFUSÉ"
    assert "co-1" in db_tools.DIVERGENCES_FENETRE, "la divergence n'est pas comptée"
