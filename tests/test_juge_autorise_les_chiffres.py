"""Le juge doit savoir que les chiffres de la relance 2 sont décidés.

🔴 LE SCÉNARIO QUE CE FICHIER EMPÊCHE, et il est silencieux de bout en bout.

La relance 2 porte quatre chiffres — « 21 fois », « 5 minutes », « 30 minutes »,
« 78 % ». Les contrôles déterministes les valident : `check_statistiques_conformes`
compare chaque valeur à celle qui a été décidée. Couche 1 verte.

Puis le juge lit le triplet. Sa règle §3 lui demande de vérifier toute statistique
chiffrée dans le `research_json` — où il ne la trouvera JAMAIS, ce JSON décrivant
l'entreprise prospect et pas le marché. Il émet une `semantic_violation`, le verdict
tombe en `needs_revision`, et `compliance_check_passed` passe à FALSE.

Et comme la relance 2 est **identique pour les 255 leads**, le juge rend le même
verdict pour tout le monde : 20 brouillons par jour pendant ~13 jours, 255 contacts
gelés à vie, zéro courriel parti.

⚠️ LE PIÈGE LE PLUS FIN, trouvé par un audit adversarial le 2026-09-01 : l'exemple
canonique du §3 était littéralement « 78% des leads quittent en 60 minutes ». Le
prompt pointait donc le juge sur le chiffre même que la relance 2 emploie. Ajouter
une permission ailleurs n'aurait pas suffi — il fallait retirer l'exemple.

Ces tests ne valent que ce que vaut l'obéissance d'un modèle. C'est assumé : la
garde DURE est `check_statistiques_conformes`, qui bloque une valeur dérivée. Ce
qu'on protège ici, c'est que la permission reste ÉCRITE.
"""
from __future__ import annotations

import pytest

from src.lib.compliance_checks import STATISTIQUES_APPROUVEES
from src.tools.compliance import _PROMPT_PATH

PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")


def test_la_permission_est_dans_la_liste_ne_pas_re_checker() -> None:
    """C'est la section que le juge lit comme « ce n'est pas ton travail ».

    La mettre ailleurs — dans une remarque, dans un §LÉGITIME — la rendrait
    consultative au lieu d'être une exclusion.
    """
    entete = "## Ce que les checks déterministes ont déjà couvert (NE PAS RE-CHECKER)"
    assert entete in PROMPT
    section = PROMPT.split(entete, 1)[1].split("\n## ", 1)[0]
    assert "relance 2" in section, "la permission n'est pas dans la bonne section"
    assert "check_statistiques_conformes" in section, (
        "la permission ne nomme pas la garde déterministe qui la justifie"
    )


@pytest.mark.parametrize("valeur", ["21 fois", "5 minutes", "30 minutes", "78 %"])
def test_chaque_chiffre_est_nomme_dans_la_permission(valeur: str) -> None:
    """Une permission qui dit « les chiffres » sans les nommer laisse le juge
    décider lesquels. Les quatre doivent être écrits."""
    assert valeur in PROMPT, f"« {valeur} » n'est pas nommé dans le prompt du juge"


def test_les_chiffres_du_prompt_sont_CEUX_du_code() -> None:
    """🔴 Le test qui rattrape la prochaine réécriture de la copie.

    Si William change une valeur, `STATISTIQUES_APPROUVEES` suit (le contrôle
    déterministe l'exige) mais le prompt du juge peut rester en arrière — et
    une permission qui nomme l'ancienne valeur ne couvre plus la nouvelle. Le
    lot entier repart alors en `needs_revision`, sans que rien ne l'annonce.
    """
    for nom, (_motif, attendu, quoi) in STATISTIQUES_APPROUVEES.items():
        assert attendu in PROMPT, (
            f"la valeur {attendu!r} de « {quoi} » ({nom}) n'apparaît pas dans le "
            f"prompt du juge — la permission ne la couvre pas"
        )


def test_l_exemple_du_paragraphe_3_ne_vise_plus_notre_propre_texte() -> None:
    """L'exemple canonique était « 78% des leads quittent en 60 minutes ».

    Soit le chiffre même de la relance 2. Le prompt disait donc au juge de
    signaler exactement ce qu'on lui envoie. Il a été remplacé le 2026-09-01 ;
    les seules occurrences restantes doivent être des AVERTISSEMENTS qui
    expliquent le changement, jamais une consigne.
    """
    for ligne in PROMPT.splitlines():
        if "78% des leads quittent" not in ligne:
            continue
        assert "ne pas le remettre" in ligne.lower() or "auparavant" in ligne.lower(), (
            f"l'exemple piégeux est revenu comme consigne : {ligne.strip()[:120]}"
        )


def test_la_permission_dit_pourquoi_le_research_json_ne_les_contient_pas() -> None:
    """Sans cette phrase, un juge consciencieux cherche quand même.

    La règle §3 dit « si pas dans le research_json, demander reformulation ».
    Il faut lui dire que l'absence est NORMALE et attendue, pas suspecte : ce
    JSON décrit l'entreprise prospect, pas le marché.
    """
    assert "research_json" in PROMPT
    bas = PROMPT.lower()
    assert "pas le marché" in bas or "pas des faits sur ce prospect" in bas, (
        "le prompt n'explique pas pourquoi ces chiffres sont absents du research_json"
    )
