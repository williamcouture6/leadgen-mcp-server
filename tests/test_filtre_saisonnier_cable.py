"""Le filtre saisonnier est-il VRAIMENT appelé par la sélection ?

🔴 LE TROU QUE CE FICHIER FERME, et c'est le TROISIÈME de la même famille en
deux jours.

`fenetre_saisonniere_ouverte` est testée en profondeur — cas limites, défaut
inversé, règle des métiers 12 mois sur 12. Aucun test ne vérifiait qu'elle est
APPELÉE. On pouvait supprimer sa ligne dans `list_contacts_to_personalize`,
la déplacer après le `return`, ou inverser sa condition, et voir 1487 tests
rester verts — pendant que le pipeline réécrit à des paysagistes en septembre,
en silence. Rien ne compte les écartés, et l'alerte de famine ne se déclenche
que sur zéro brouillon.

Les deux précédents, pour que le motif soit visible :
  · le motif `[A-ZÀ-Ü]` routé par un `_find_matches` qui baisse la casse ;
  · `_alerter_file_bloquee`, testée six fois, jamais son appel dans `run_wf6`.
La fonction est bonne, le chemin ne passe plus par elle.

⚠️ LE MOIS EST FIGÉ dans chaque test. Le filtre lit `date.today()` par défaut :
un test écrit sans date deviendrait vert tout seul de février à juillet, quand
la fenêtre du paysagement est ouverte. Un test qui passe pour une raison
calendaire ne teste rien.
"""
from __future__ import annotations

import datetime

import pytest

from src.tools import db as db_tools

SEPTEMBRE = datetime.date(2026, 9, 2)   # seul le déneigement est ouvert

PAYSAGISTE = {
    "id": "co-paysagiste",
    "name": "Paysagiste Hors Saison",
    "industry": "paysagiste",
    "website": "https://ex.ca",
    # ⚠️ `track` est OBLIGATOIRE : `by_id` ne garde que les entreprises du
    # track demandé (db.py:66), et son défaut est "OPT". Sans ce champ, la
    # fiche est écartée AVANT le filtre saisonnier — et le test du
    # paysagiste passerait pour cette raison-là. C'est le contrôle négatif
    # qui l'a révélé.
    "track": "agence-ia",
    "research_json": {"services_offered": ["Aménagement paysager", "Plantations"]},
}
DENEIGEUR = {
    "id": "co-deneigeur",
    "name": "Déneigement En Saison",
    "industry": "entrepreneur en déneigement",
    "website": "https://ex.ca",
    "track": "agence-ia",
    "research_json": {"services_offered": ["Déneigement résidentiel"]},
}
CONTACTS = [
    {"id": "ct-1", "company_id": "co-paysagiste", "email": "a@ex.ca",
     "status": "new", "track": "agence-ia", "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ct-2", "company_id": "co-deneigeur", "email": "b@ex.ca",
     "status": "new", "track": "agence-ia", "created_at": "2026-01-02T00:00:00Z"},
]


@pytest.fixture
def base(monkeypatch):
    """Remplace les trois lectures Supabase de la sélection."""
    async def _select(table, params=None):
        if table == "contacts":
            return [dict(c) for c in CONTACTS]
        if table == "companies":
            return [dict(PAYSAGISTE), dict(DENEIGEUR)]
        if table == "messages":
            return []          # aucun contact déjà rédigé
        return []

    monkeypatch.setattr(db_tools.db, "select", _select)


@pytest.mark.asyncio
async def test_le_paysagiste_hors_saison_est_ecarte_par_la_selection(base, monkeypatch) -> None:
    """🔴 Le test qui rougit si la ligne d'appel disparaît."""
    monkeypatch.setattr(db_tools, "date", _FauxDate(SEPTEMBRE))
    rendus = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    noms = {(r.get("company") or {}).get("name") for r in rendus}
    assert "Paysagiste Hors Saison" not in noms, (
        "le filtre saisonnier n'est pas appelé par `list_contacts_to_personalize` — "
        "un paysagiste est servi en septembre, sa saison étant fermée depuis juin"
    )


@pytest.mark.asyncio
async def test_le_deneigeur_en_saison_ressort_bien(base, monkeypatch) -> None:
    """Le contrôle négatif, et il compte autant.

    Sans lui, un filtre qui écarterait TOUT LE MONDE ferait passer le test
    précédent — et viderait la file en silence, ce qui est exactement le mode
    de panne qu'on chasse.
    """
    monkeypatch.setattr(db_tools, "date", _FauxDate(SEPTEMBRE))
    rendus = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    noms = {(r.get("company") or {}).get("name") for r in rendus}
    assert "Déneigement En Saison" in noms, (
        "le filtre écarte aussi les entreprises EN saison — il est trop large"
    )


@pytest.mark.asyncio
async def test_la_piste_OPT_n_est_pas_filtree(base, monkeypatch) -> None:
    """OPT est gelée et ses métiers n'ont pas de saison.

    Y appliquer la fenêtre écarterait tout le monde, en silence. La garde de
    piste est la PREMIÈRE ligne du filtre ; ce test la fige.
    """
    monkeypatch.setattr(db_tools, "date", _FauxDate(SEPTEMBRE))
    for c in CONTACTS:
        c["track"] = "OPT"
    PAYSAGISTE["track"] = DENEIGEUR["track"] = "OPT"
    try:
        rendus = await db_tools.list_contacts_to_personalize(limit=10, track="OPT")
        noms = {(r.get("company") or {}).get("name") for r in rendus}
        assert "Paysagiste Hors Saison" in noms, (
            "le filtre saisonnier s'applique à OPT — il ne doit pas"
        )
    finally:
        for c in CONTACTS:
            c["track"] = "agence-ia"
        PAYSAGISTE["track"] = DENEIGEUR["track"] = "agence-ia"


class _FauxDate(datetime.date):
    """Fige `date.today()` sans toucher au reste du module `datetime`."""

    def __new__(cls, jour: datetime.date):
        obj = super().__new__(cls, jour.year, jour.month, jour.day)
        obj._jour = jour
        return obj

    def __call__(self, *a, **k):  # `date(...)` reste utilisable
        return datetime.date(*a, **k)

    def today(self):  # type: ignore[override]
        return self._jour
