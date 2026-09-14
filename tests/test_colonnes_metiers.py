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
        # ⚠️ CORPUS CHOISI POUR QUE LES DEUX ORDRES DIVERGENT. Le precedent
        # (deneigement x2 + paysagement) etait degenere : `deneigement` precede
        # `paysagement` AUSSI en ordre alphabetique, donc un tri alphabetique
        # passait le test. Ici le compte dit paysagement d'abord, l'alphabet dit
        # deneigement : seul le bon tri passe.
        ["Aménagement paysager", "Plantations et haies", "Déneigement"]
    )
    assert c["metiers"] == ["paysagement", "déneigement"]


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
    """🔴 `EXIGE`. Mesuré le 2026-09-12 sur les 441 fiches : `piscine` est reconnue sur
    **32**, dont **17** SANS verbe d'entretien, et l'une d'elles
    n'a QUE ça — elle n'est joignable aucun mois de l'année. Une règle « union
    des métiers saisonniers » lui rouvrirait sept mois par an, et le défaut
    serait invisible en septembre."""
    sans = colonnes_metiers(["Installation de piscine creusée"])
    assert "piscine" in sans["metiers"]
    assert sans["fenetre_mois"] == []

    avec = colonnes_metiers(["Ouverture et fermeture de piscine"])
    assert avec["fenetre_mois"] == [1, 2, 3, 4, 5, 6, 7]


def test_industry_complete_la_colonne():
    """🔴 GÈLE UN DÉBRANCHEMENT VOLONTAIRE — décision William du 2026-09-02.

    `metier_depuis_industry` existe et résout bien « déneigement » depuis ce
    secteur, mais `classer_services` l'IGNORE, et donc la colonne aussi. La
    raison, mot pour mot : si la seule chose qu'on reconnaît d'un paysagiste
    est « pavage », notre donnée sur lui est mauvaise — et on ne devine pas son
    métier depuis le mot-clé de sourcing, puisque tout le courriel repose sur
    le fait qu'on parle de ce qu'il fait vraiment.

    ⚠️ Sans ce test, quelqu'un qui rebranche le repli dans `classer_services`
    changerait le comportement de la COLONNE sans s'en apercevoir : le
    paramètre traverse `colonnes_metiers` sans que rien ne l'exerce. Une fiche
    non reconnue doit rester `inconnu` + les douze mois (défaut inversé), et
    surtout PAS devenir un déneigeur joignable d'août à décembre.
    """
    # 💀 Jusqu'au 2026-09-14 ce test exigeait l'INVERSE : metiers == [],
    # source « inconnu », douze mois. Décision William renversée sur les cas
    # Niwa et Spray Green — voir tests/test_industry_complement.py pour le
    # raisonnement et la mesure (97 % des fiches inchangées).
    c = colonnes_metiers(["Consultation"], industry="entrepreneur en déneigement")
    assert c["metiers"] == ["déneigement"]
    assert c["metier_source"] == "industry"
    assert c["fenetre_mois"] == [8, 9, 10, 11, 12], (
        "une fiche dont on ne reconnaît que le secteur doit suivre la saison de "
        "ce secteur, pas rester douze mois par défaut inversé"
    )

    # ⚠️ Le défaut inversé TIENT TOUJOURS quand le secteur ne dit rien non plus.
    inconnue = colonnes_metiers(["Consultation"], industry="boulangerie")
    assert inconnue["metiers"] == []
    assert inconnue["metier_source"] == "inconnu"
    assert inconnue["fenetre_mois"] == list(range(1, 13))


def test_le_patch_survit_a_un_aller_retour_json():
    """🔴 `fenetre_mois()` rend un frozenset : non sérialisable. Le patch doit
    porter des listes triées, sinon l'écriture lève.
    ⚠️ Une version antérieure de ce test assertait `== sorted(...)`, une
    tautologie sur toute liste rendue par `sorted()`, et sur une entrée qui
    rendait les douze mois — donc indistinguable du défaut inversé."""
    c = colonnes_metiers(["Déneigement résidentiel"])
    assert json.loads(json.dumps(c))["fenetre_mois"] == [8, 9, 10, 11, 12]
