"""Les permissions du juge : ordonnées, et leurs renvois pointent quelque part.

🔴 Ce fichier existe à cause d'un mécanisme qui a déjà coûté cher, deux fois.

Le juge sémantique refuse ce qu'il ne peut pas vérifier, et un refus fait
quitter le lot au brouillon POUR TOUJOURS — le contact reste gelé à vie. Chaque
phrase FIXE des gabarits a donc sa permission nommée. Deux fois déjà, une
permission existait mais le juge ne la trouvait pas :

  · le 2026-09-01, un exemple concret citait notre propre pied de page comme
    mensonge à signaler — l'exemple l'emportait sur la règle ;
  · le 2026-09-08, un renvoi « voir juste en dessous » pointait 49 lignes plus
    bas, parce que six permissions avaient été insérées entre les deux.

Un prompt n'a pas de compilateur. Ces tests en tiennent lieu.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT = (Path(__file__).parent.parent / "src/prompts/compliance.md").read_text(
    encoding="utf-8"
)

RANG_LATIN = [
    "1", "1bis", "1ter", "1quater", "1quinquies",
    "1sexies", "1septies", "1octies", "1nonies", "1decies",
    # 🔴 Le 2026-09-14 : la supposition sur le rush de saison, 3ᵉ version du
    # 2ᵉ paragraphe de C et D. Allonger cette liste fait PARTIE d'ajouter une
    # permission — sans ça le test tombe, ce qui est exactement le but.
    "1undecies",
    # 🔴 Le 2026-09-17 : le NOM de l'entreprise. Le juge n'en recevait aucun et
    # deduisait le vrai du `company_summary`, pendant que le redacteur imprimait
    # le libelle Google coupe. Deux sources de verite, 67 divergences sur 343
    # fiches joignables, et un brouillon BLOQUE pour « fait invente » alors que
    # le redacteur avait obei a sa regle. Depuis la migration 0072, le bloc
    # « Faits verifies » porte le nom, et c'est LUI qui fait foi.
    "1duodecies",
    # 🔴 Le 2026-09-22 : « on comprend que tu en couvres beaucoup! » reste vraie
    # quand l'entreprise n'a qu'UN métier. Le juge la refusait au titre des
    # faits non ancrés (§1) en jugeant la largeur sur le nombre de MÉTIERS,
    # pendant que le rédacteur la juge sur le nombre de SERVICES — deux unités
    # pour une même phrase. Mesuré le 2026-09-21 : 2 des 3 refus du matin, et
    # 7 des 18 entreprises vivantes et recherchées dans ce cas.
    "1terdecies",
]


def _ancres() -> list[str]:
    return [m.group(1) for m in re.finditer(r"^(1[a-z]*)\. ", PROMPT, re.M)]


def test_les_permissions_sont_dans_l_ordre_latin() -> None:
    """Un lecteur qui cherche §1quater doit le trouver entre §1ter et
    §1quinquies. Le désordre n'est pas qu'esthétique : c'est lui qui a fait
    pointer « voir juste en dessous » vers la mauvaise permission."""
    ancres = _ancres()
    attendu = [a for a in RANG_LATIN if a in ancres]
    assert ancres == attendu, f"ordre trouvé : {ancres}"


def test_chaque_renvoi_pointe_vers_une_permission_qui_existe() -> None:
    """Un renvoi vers §1X qui n'existe pas envoie le juge dans le vide, et il
    retombe alors sur la règle générale — celle qui refuse."""
    ancres = set(_ancres())
    # 🔴 IL Y AVAIT UN VRAI CARACTERE BACKSPACE ICI, corrige le 2026-09-14.
    # La frontiere de mot avait ete mangee par un echappement et remplacee par
    # U+0008. Le motif cherchait donc un backspace apres la reference ; le
    # prompt n'en contient aucun, donc `renvois` valait TOUJOURS l'ensemble
    # vide et l'assertion comparait le vide au vide. Ce test passait au vert
    # depuis toujours sans rien regarder. Mesure au moment du correctif : la
    # vraie frontiere trouve 10 renvois, tous vers des ancres existantes --
    # le prompt est sain, mais ce garde ne pouvait pas le prouver.
    renvois = set(re.findall(r"§(1[a-z]*)\b", PROMPT))
    manquants = sorted(renvois - ancres)
    assert not manquants, f"renvois vers des permissions inexistantes : {manquants}"


def test_aucun_renvoi_de_position_ne_subsiste() -> None:
    """« voir juste en dessous » et compagnie se périment dès qu'on insère un
    paragraphe. Les renvois doivent NOMMER leur cible."""
    for tournure in ("voir juste en dessous", "voir juste au-dessus", "le paragraphe suivant"):
        assert tournure not in PROMPT.lower(), (
            f"« {tournure} » se périmera à la prochaine insertion : nommer §1X"
        )


def test_chaque_phrase_fixe_des_gabarits_a_sa_permission() -> None:
    """La liste de ce que le juge NE DOIT PAS re-vérifier, confrontée aux
    phrases fixes réelles. Chacune a coûté ou aurait coûté un refus."""
    # `.lower()` des deux côtés : la liste écrit « FAMILLES normalisées » en
    # capitales d'insistance, et un test qui casse sur une majuscule ne dit rien
    # d'utile — il dit seulement que quelqu'un a crié.
    liste = PROMPT.split("## LÉGITIME")[0].lower()
    for fragment in (
        "ouvreur de saison",   # les trois têtes de C et D
        "2ᵉ temps",            # « j'ai aussi vu que tu fais X »
        "La ville",            # « dans la région de … »
        "j'aide les PME",      # 4ᵉ paragraphe de C
        "avantages",           # 5ᵉ paragraphe, propre à C
        "bloc du site",        # la fermeture de C et D
        "familles normalisées",  # « paysagement » ≠ « aménagement paysager »
    ):
        assert fragment.lower() in liste, (
            f"« {fragment} » n'est pas dans la liste NE PAS RE-CHECKER — le juge "
            "la lit en premier, une permission qui n'y figure pas se fait ignorer"
        )
