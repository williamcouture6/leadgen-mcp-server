"""Le lexique de métier — la table déterministe et sa règle de résolution."""
from __future__ import annotations

from datetime import date

import pytest

from src.lib.lexique_metiers import METIERS_COUVERTS, REPLI, lexique_pour
from src.lib.metiers import RACINES, resoudre_metiers


def test_un_laveur_de_vitres_recoit_le_nombre_detages() -> None:
    """Critère de fin nº5 de la spec du 2026-08-26, mot pour mot : un lead de
    métier « lavage de vitres » reçoit « le nombre d'étages », jamais « la
    grandeur d'entrée »."""
    lex = lexique_pour("lavage de vitres")
    assert "le nombre d'étages" in lex.questions
    assert "la grandeur de l'entrée" not in lex.questions


def test_le_lexique_suit_le_dominant_pas_la_scene() -> None:
    """🔴 Le cas qui a fait trancher la §3. Un laveur de vitres démarché en août
    a pour SCÈNE le déneigement (seule fenêtre ouverte) mais pour DOMINANT le
    lavage de vitres. Le bloc service doit lui demander le nombre d'étages, pas
    la grandeur de son entrée de garage — il lave des vitres commerciales onze
    mois par année."""
    services = [
        "lavage de vitres commercial",
        "lavage de vitres résidentiel",
        "nettoyage de fenêtres",
        "déneigement",
    ]
    r = resoudre_metiers(services, date(2026, 8, 20))
    assert r.scene == "déneigement"
    assert r.dominant == "lavage de vitres"

    lex = lexique_pour(r.dominant)
    assert "le nombre d'étages" in lex.questions
    assert "la grandeur de l'entrée" not in lex.questions, (
        "passer `scene` au lieu de `dominant` est exactement l'erreur que la "
        "§3 a dû trancher"
    )


@pytest.mark.parametrize("inconnu", [None, "", "réparation de clôtures", "plomberie"])
def test_le_repli_sert_quand_le_metier_est_inconnu(inconnu: str | None) -> None:
    lex = lexique_pour(inconnu)
    assert lex is REPLI
    assert lex.est_repli
    assert lex.ou_il_est == "sur un contrat"


def test_le_repli_est_signale_pour_pouvoir_etre_compte() -> None:
    """Le repli est COMPTÉ, comme les suppositions. S'il sert souvent, ce n'est
    pas la copie qui est en cause, c'est WF-3 qui n'a pas assez creusé — et on
    le saura au lieu de le deviner."""
    assert lexique_pour("déneigement").est_repli is False
    assert lexique_pour(None).est_repli is True


def test_tous_les_metiers_du_dictionnaire_ont_un_lexique() -> None:
    """Un métier reconnu par `metiers.py` mais absent de la table retomberait
    sur le repli, et le compteur de repli accuserait WF-3 d'une lacune qui
    serait en fait celle de cette table."""
    manquants = set(RACINES) - METIERS_COUVERTS
    assert not manquants, f"métiers sans lexique : {sorted(manquants)}"


def test_paysagement_et_tonte_partagent_le_meme_terrain() -> None:
    """Deux entrées distinctes dans le dictionnaire des racines (leurs saisons
    diffèrent), mais le même vocabulaire : c'est le même terrain."""
    assert lexique_pour("tonte").questions == lexique_pour("paysagement").questions
    assert lexique_pour("tonte").ou_il_est == "sur un terrain"


@pytest.mark.parametrize("metier", sorted(METIERS_COUVERTS))
def test_chaque_lexique_pose_trois_questions_dont_ladresse(metier: str) -> None:
    """L'adresse est la seule question commune à tous les métiers : sans elle,
    le résumé envoyé à l'entrepreneur ne sert à rien."""
    lex = lexique_pour(metier)
    assert len(lex.questions) == 3
    assert lex.questions[0] == "l'adresse"
    assert lex.ou_il_est and not lex.ou_il_est.endswith(".")
