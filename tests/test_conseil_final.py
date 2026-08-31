"""Les trois défauts trouvés par le conseil de revue final du 2026-08-30.

Les trois avaient survécu à 1153 tests verts. Deux d'entre eux étaient dans du
code écrit le jour même, l'un dans la garde censée empêcher exactement le
désastre qu'elle produisait.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.lib.avis import nom_commercial
from src.tools import personalize as perso


# ---------------- 1. La garde de config gelait ce qu'elle devait sauver ----------------

def test_le_verdict_error_ne_marque_RIEN_sur_le_message() -> None:
    """🔴 Le bloquant du conseil final.

    Le layer 0 rend `error` PRÉCISÉMENT pour ne pas marquer le brouillon : la
    faute est dans l'environnement, pas dans le texte. Mais la route persistait
    ce verdict comme les autres, et `_patch_verdict_conformite` écrivait
    `compliance_check_passed = ("error" == "approved") = False`.

    La garde écrite pour empêcher le gel des contacts était exactement ce qui
    les gelait : brouillon sorti du lot (la requête ne reprend que `is.null`),
    contact gelé à vie, 20 par jour, et 1153 tests verts.
    """
    patch = http_api._patch_verdict_conformite("error", tentatives_avant=0)
    assert "compliance_check_passed" not in patch, (
        f"écrire passed={patch.get('compliance_check_passed')} sort le brouillon "
        "du lot POUR TOUJOURS, pour une variable d'environnement vide"
    )


def test_le_verdict_error_ne_consomme_pas_de_tentative() -> None:
    """Une configuration absente n'est pas une tentative de jugement.
    L'incrémenter ferait atteindre le plafond anti-boucle en trois passes, et un
    problème de variable deviendrait un refus définitif."""
    patch = http_api._patch_verdict_conformite("error", tentatives_avant=0)
    assert "compliance_tentatives" not in patch


@pytest.mark.parametrize(
    "verdict,passed_attendu",
    [("approved", True), ("blocked", False), ("needs_revision", False)],
)
def test_les_autres_verdicts_gardent_leur_comportement(verdict, passed_attendu) -> None:
    """Contrôle négatif : le correctif ne doit exempter QUE `error`."""
    patch = http_api._patch_verdict_conformite(verdict, tentatives_avant=0)
    assert patch["compliance_check_passed"] is passed_attendu
    assert patch["compliance_tentatives"] == 1


def test_non_juge_garde_son_exemption_historique() -> None:
    patch = http_api._patch_verdict_conformite("non_juge", tentatives_avant=1)
    assert "compliance_check_passed" not in patch
    assert patch["compliance_tentatives"] == 2


def test_lalerte_compte_les_erreurs() -> None:
    """🔴 Sans ça, la seule panne qui arrête l'envoi EN ENTIER serait aussi la
    seule totalement muette. Vérifié : le workflow n8n WF-5 ne porte aucun nœud
    d'alerte, donc le silence serait total, pas seulement côté serveur."""
    assert http_api._doit_alerter_wf5(
        needs_revision=0, blocked=0, non_juge=0, orphelins=0, errors=1
    )
    assert not http_api._doit_alerter_wf5(
        needs_revision=0, blocked=0, non_juge=0, orphelins=0, errors=0
    )


# ---------------- 2. Le nom propre compose etait tronque ----------------

@pytest.mark.parametrize(
    "brut,attendu,pourquoi",
    [
        ("Chasse-Neige Express", "Chasse-Neige Express", "trait d'union interne"),
        ("Déneigement Rive-Sud", "Déneigement Rive-Sud", "toponyme composé"),
        ("Paysagement Saint-Nicolas", "Paysagement Saint-Nicolas", "saint composé"),
        ("9265-1234 Québec inc.", "9265-1234 Québec inc.", "numéro d'entreprise"),
        ("Vitro-Services Lévis", "Vitro-Services Lévis", "marque composée"),
    ],
)
def test_un_trait_dunion_interne_ne_coupe_pas(brut, attendu, pourquoi) -> None:
    """🔴 Le deuxième défaut du conseil. La première version coupait au premier
    tiret sans regarder ce qui le précédait.

    Mesuré sur la base : 93 noms sur 816 portent un trait d'union, dont 56
    au-dessus du plancher d'avis — soit ~10 % de la liste dont le SEUL fait
    personnalisé du courriel, imprimé dans l'ancre chiffrée, partait écorché.
    """
    assert nom_commercial(brut) == attendu, pourquoi


@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("Vitres Ultra Nettes -lavage de vitres condo", "Vitres Ultra Nettes"),
        ("Déneigement ABC | Excavation", "Déneigement ABC"),
        ("Piscines Élégance, Québec", "Piscines Élégance"),
        ("Toiture Pro / Bardeaux", "Toiture Pro"),
        ("Paysagement Rivard", "Paysagement Rivard"),
    ],
)
def test_le_bourrage_de_mots_cles_coupe_toujours(brut, attendu) -> None:
    """Contrôle négatif : le correctif ne doit pas désarmer la coupe. Le
    bourrage est TOUJOURS précédé d'une espace — c'est ce qui sépare
    exactement les deux cas."""
    assert nom_commercial(brut) == attendu


def test_la_virgule_coupe_meme_collee() -> None:
    """Exception assumée : la virgule s'écrit collée au mot qui précède, donc
    exiger une espace avant elle ne couperait jamais rien."""
    assert nom_commercial("Piscines Élégance, Québec inc.") == "Piscines Élégance"


# ---------------- 3. L'article francais etait faux, et IMPOSE ----------------

@pytest.mark.parametrize(
    "metier,attendu",
    [
        ("déneigement", "du déneigement"),
        ("paysagement", "du paysagement"),
        ("pavage", "du pavage"),
        ("ménage", "du ménage"),
        ("tonte", "de la tonte"),
        ("toiture", "de la toiture"),
        ("piscine", "de la piscine"),
        ("excavation", "de l'excavation"),
        ("extermination", "de l'extermination"),
    ],
)
def test_larticle_est_juste_pour_chaque_metier(metier: str, attendu: str) -> None:
    """🔴 Le troisième défaut, et il comptait parce que le prompt présente cette
    phrase comme une **formulation IMPOSÉE** et interdit de la reformuler
    « même mieux » : le modèle était sommé de recopier « du piscine » et
    « de la excavation ». ~19 % des entreprises concernées."""
    assert perso._avec_article(metier) == attendu


def test_chaque_metier_recoit_SON_article() -> None:
    """L'article était posé sur la chaîne DÉJÀ jointe : « de la excavation pis
    pavage » — un seul article pour deux métiers."""
    assert perso._enumerer_metiers(["excavation", "pavage"]) == "de l'excavation pis du pavage"
    assert perso._enumerer_metiers(["tonte"]) == "de la tonte"
    assert perso._enumerer_metiers(["tonte", "piscine", "toiture"]) == (
        "de la tonte, de la piscine pis de la toiture"
    )


def test_tous_les_metiers_du_dictionnaire_ont_un_article_lisible() -> None:
    """Aucun métier ne doit produire « du a… » ou « de la e… »."""
    from src.lib.metiers import RACINES

    for metier in RACINES:
        rendu = perso._avec_article(metier)
        assert not rendu.startswith("de la a"), rendu
        assert not rendu.startswith("de la e"), rendu
        assert not rendu.startswith("de la i"), rendu
        assert not rendu.startswith("de la o"), rendu
        assert not rendu.startswith("de la u"), rendu


def test_la_formulation_imposee_est_lisible_de_bout_en_bout() -> None:
    """Le test qui aurait attrapé les trois : on lit la phrase que le prompt
    dicte, en entier."""
    from datetime import date

    bloc = perso.bloc_metiers_resolus(
        ["excavation", "pavage", "déneigement"], date(2026, 12, 10)
    )
    assert "de la excavation" not in bloc
    assert "du piscine" not in bloc
    assert "de l'excavation" in bloc
