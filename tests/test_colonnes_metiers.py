"""`colonnes_metiers` : ce qui sera écrit en base.

🔴 La colonne porte LE RÉSULTAT de la règle, pas les données brutes. La vue de
sélection (conversation B) n'aura ainsi qu'un seul test à faire, et AUCUN
dictionnaire de métiers ne descendra en SQL.

Les valeurs attendues se dérivent de `SAISONS` et de `fenetre_mois()` :
déneigement (saison 15 nov, -3/+1) → {8,9,10,11,12} · paysagement (mi-avril,
-3/+2) → {1..6} · tonte (début mai, -4/+2) → {1..7} · piscine (mai, -4/+2) →
{1..7}. Pavage, excavation, toiture, ménage : aucune saison documentée → ils
n'ouvrent RIEN.
"""
from __future__ import annotations

import json

from src.lib.metiers import colonnes_metiers


def test_un_deneigeur_ouvre_daout_a_decembre():
    c = colonnes_metiers(["Déneigement résidentiel", "Transport de neige"])
    assert c["metiers"] == ["déneigement"]
    assert c["fenetre_mois"] == [8, 9, 10, 11, 12]
    assert c["metier_source"] == "services_offered"


def test_multi_metier_prend_lunion():
    c = colonnes_metiers(["Déneigement", "Aménagement paysager"])
    assert c["fenetre_mois"] == [1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12]


def test_les_metiers_sont_ordonnes_par_nombre_de_libelles():
    """Le `comment on column` de la migration le garantit contractuellement :
    `metiers[1]` est le dominant, celui qui gouverne le lexique. Le type ne
    porte pas cet ordre — seul ce test le tient."""
    c = colonnes_metiers(
        ["Déneigement", "Déneigement commercial", "Aménagement paysager"]
    )
    assert c["metiers"] == ["déneigement", "paysagement"]


def test_aucun_metier_reconnu_ouvre_les_douze_mois():
    assert colonnes_metiers(["Consultation"])["fenetre_mois"] == list(range(1, 13))
    assert colonnes_metiers([])["metier_source"] == "inconnu"


def test_un_metier_douze_mois_sur_douze_n_ouvre_RIEN():
    """Règle du 2026-09-02 : le pavage ne peut pas enclencher une séquence de
    contact. Il reste nommé au 2ᵉ temps du courriel.
    ⚠️ Ce test mord : `fenetre_mois("pavage")` rend les douze mois — sans le
    garde `not in SAISONS`, il rougirait."""
    c = colonnes_metiers(["Pavage", "Asphalte"])
    assert c["metiers"] == ["pavage"]
    assert c["fenetre_mois"] == []


def test_la_piscine_sans_verbe_dentretien_n_ouvre_RIEN():
    """🔴 `EXIGE`. Mesuré : 29 fiches sur 40 sont dans ce cas, et l'une d'elles
    n'a QUE ça — elle n'est joignable aucun mois de l'année. Une règle « union
    des métiers saisonniers » lui rouvrirait sept mois par an, et le défaut
    serait invisible en septembre."""
    sans = colonnes_metiers(["Installation de piscine creusée"])
    assert "piscine" in sans["metiers"]
    assert sans["fenetre_mois"] == []

    avec = colonnes_metiers(["Ouverture et fermeture de piscine"])
    assert avec["fenetre_mois"] == [1, 2, 3, 4, 5, 6, 7]


def test_le_patch_survit_a_un_aller_retour_json():
    """🔴 `fenetre_mois()` rend un frozenset : non sérialisable. Le patch doit
    porter des listes triées, sinon l'écriture lève.
    ⚠️ Une version antérieure de ce test assertait `== sorted(...)`, une
    tautologie sur toute liste rendue par `sorted()`, et sur une entrée qui
    rendait les douze mois — donc indistinguable du défaut inversé."""
    c = colonnes_metiers(["Déneigement résidentiel"])
    assert json.loads(json.dumps(c))["fenetre_mois"] == [8, 9, 10, 11, 12]
