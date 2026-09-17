"""Le juge doit garder le droit de refuser un métier qui décrit un AUTRE geste.

🔴 DÉFAUT INTRODUIT LE 2026-09-16, TROUVÉ PAR UN CONSEIL DE RELECTURE LE
2026-09-17 ET CORRIGÉ LE MÊME JOUR.

Pour supprimer un faux positif sur « pavage » (le juge refusait « du pavage »
chez une entreprise qui liste « Nivellement de pavé uni »), une consigne avait
été ajoutée au prompt du juge :

    « tu n'as PAS à arbitrer entre poser, réparer et niveler. Découper une
      famille en sous-métiers est un jugement qui ne t'appartient pas.
      Le dictionnaire du code a déjà tranché. »

Le cas visé était bon — `RACINES["pavage"]` contient vraiment « pave uni ».
C'est la GÉNÉRALISATION qui ouvrait un trou, pour deux raisons mesurées :

  1. **Le juge ne reçoit jamais les familles résolues.** `_message_utilisateur_juge`
     lui envoie le corps, les relances, les faits vérifiés, la fiche contact,
     le `research_json` et `social_proof` — jamais la sortie de `lib/metiers`.
     On le renvoyait donc à une autorité qu'il ne peut pas consulter.
  2. **Le dictionnaire fait lui-même cet arbitrage**, et c'est tout l'objet de
     `EXCLUSIONS` : poser du gazon en rouleau n'est PAS de la tonte, installer
     une clôture de piscine n'est PAS de l'entretien de piscine. Le commentaire
     du code dit pourquoi : « il le voit tout de suite ».

Et le juge est le SEUL garde-fou sur ce point : aucun `check_*` déterministe ne
lit les métiers nommés dans le corps. Le désarmer revenait à n'avoir plus rien.

Ce fichier attache le prompt au dictionnaire, pour qu'ils ne puissent plus
diverger en silence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.lib.metiers import EXCLUSIONS, EXIGE

PROMPT = (
    Path(__file__).resolve().parent.parent / "src" / "prompts" / "compliance.md"
).read_text(encoding="utf-8")

# Les formulations qui RETIRAIENT au juge son jugement. Elles ne doivent pas
# revenir, sous aucune des deux formes essayées.
PHRASES_QUI_DESARMENT = (
    "ne t'appartient pas",
    "dictionnaire du code a déjà tranché",
    "PAS à arbitrer",
)


@pytest.mark.parametrize("phrase", PHRASES_QUI_DESARMENT)
def test_aucune_consigne_ne_retire_au_juge_son_jugement(phrase: str) -> None:
    assert phrase not in PROMPT, (
        f"« {phrase} » est de retour dans le prompt du juge. Il est le SEUL "
        "contrôle qui lit les métiers nommés — aucun check déterministe ne le "
        "fait. Lui dire de s'en remettre à autre chose ne lui laisse qu'une "
        "conduite possible : tout accepter."
    )


@pytest.mark.parametrize("famille", sorted(EXCLUSIONS))
def test_chaque_exclusion_du_dictionnaire_est_expliquee_au_juge(
    famille: str,
) -> None:
    """🔴 LE LIEN QUI MANQUAIT.

    `EXCLUSIONS` dit au CODE de ne pas classer un libellé dans une famille. Mais
    le rédacteur peut nommer cette famille quand même, et alors seul le juge
    peut l'attraper. Si une famille entre dans `EXCLUSIONS` sans que le prompt
    apprenne la distinction, le juge laisse passer exactement le mensonge que
    l'exclusion voulait empêcher.

    On exige donc que la famille soit nommée dans le prompt, ET qu'au moins un
    des libellés exclus y figure — sinon la consigne serait trop vague pour
    servir.
    """
    assert famille in PROMPT, (
        f"la famille « {famille} » a une exclusion dans lib/metiers mais le "
        "prompt du juge ne la nomme jamais"
    )
    libelles = EXCLUSIONS[famille]
    # Les libellés du dictionnaire sont sans accents (ils servent à comparer du
    # texte déjà normalisé) ; le prompt, lui, s'écrit en français lisible.
    def _sans_accents(t: str) -> str:
        import unicodedata

        return "".join(
            c for c in unicodedata.normalize("NFD", t.lower())
            if unicodedata.category(c) != "Mn"
        )

    prompt_nu = _sans_accents(PROMPT)
    assert any(_sans_accents(x) in prompt_nu for x in libelles), (
        f"le prompt nomme « {famille} » mais aucun des libellés exclus "
        f"{libelles} : la consigne est trop vague pour que le juge sache quoi "
        "refuser"
    )


@pytest.mark.parametrize("famille", sorted(EXIGE))
def test_chaque_exigence_du_dictionnaire_est_expliquee_au_juge(
    famille: str,
) -> None:
    """Même raisonnement que pour les exclusions, dans l'autre sens : `EXIGE`
    demande un mot qui prouve le métier. `toiture` est né du cas Net-Pro, un
    laveur de toits classé couvreur."""
    assert famille in PROMPT, (
        f"la famille « {famille} » exige un mot qui la prouve (lib/metiers."
        "EXIGE) mais le prompt du juge ne la nomme jamais"
    )


def test_le_critere_de_la_frontiere_est_ecrit() -> None:
    """La règle qui tranche les cas nouveaux, et qui date du 2026-09-02 : on
    accepte ce qui est LARGE, on refuse ce qui est FAUX. Sans elle, le juge n'a
    que des exemples et rien pour raisonner sur un cas absent de la liste."""
    assert "LARGE" in PROMPT and "FAUX" in PROMPT
    assert "c'est pas ça que je fais" in PROMPT, (
        "le test du destinataire a disparu : c'est lui qui rend la règle "
        "applicable à un cas qu'on n'a pas prévu"
    )
