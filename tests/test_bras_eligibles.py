"""Quels bras étaient EN JEU au moment où le brouillon a été écrit.

🔴 POURQUOI CETTE TRACE EXISTE. `messages.template_choice` dit quelle lettre a
été servie, jamais lesquelles auraient pu l'être. Un lot tiré en `"CD"` écrit
'C' et 'D' sans que A et B aient eu la moindre chance — mesuré le 2026-09-10 :
30 des 46 leads C/D de la base sont dans ce cas, soit 65 %. Comparer A/B à C/D
sur ces données revient à comparer deux LOTS et à appeler ça un test.

Et une seconde rupture, plus discrète, vient du garde-fou des têtes fixes :
sans métier reconnu, C et D sont écartés et le tirage retombe sur A ou B. Ces
leads-là ne sont donc PAS des témoins pour C et D, même dans un lot 'ABCD'.
C'est pourquoi la trace enregistre l'ensemble éligible APRÈS la garde, jamais
le paramètre du lot : écrire 'ABCD' laisserait le biais intact en ayant l'air
corrigé, ce qui est pire que de le laisser visible.
"""
from __future__ import annotations

import pytest


def test_une_consigne_dalternance_rend_tous_ses_bras() -> None:
    from src.lib.gabarits import bras_eligibles

    assert bras_eligibles("ABCD") == ("A", "B", "C", "D")
    assert bras_eligibles("AB") == ("A", "B")
    assert bras_eligibles("CD") == ("C", "D")


def test_un_bras_force_est_seul_eligible() -> None:
    """Le rejeu manuel d'un lead precis : la lettre gagne, et elle est la seule
    en jeu. Ce lead ne temoigne donc pour aucune comparaison."""
    from src.lib.gabarits import bras_eligibles

    assert bras_eligibles("C") == ("C",)


def test_sans_metier_reconnu_les_gabarits_a_tete_fixe_sortent() -> None:
    """C et D ouvrent sur « J'ai vu que tu fais du {METIER} ». Sans metier, ils
    ne sont pas eligibles — et la trace doit le dire, sinon on croira que ce
    lead pouvait les recevoir."""
    from src.lib.gabarits import bras_eligibles

    assert bras_eligibles("ABCD", metier_connu=False) == ("A", "B")
    assert bras_eligibles("AB", metier_connu=False) == ("A", "B")


def test_quand_on_ne_demande_que_des_tetes_fixes_le_repli_est_visible() -> None:
    """🔴 Le trou de `"CD"` seul : il ne reste rien a quoi basculer, donc C et D
    restent eligibles et le redacteur nommera un metier qu'on n'a pas reconnu.
    La trace ne le MASQUE pas — c'est ce qui rend le defaut mesurable."""
    from src.lib.gabarits import bras_eligibles

    assert bras_eligibles("CD", metier_connu=False) == ("C", "D")


def test_une_valeur_inconnue_ne_devine_rien() -> None:
    from src.lib.gabarits import bras_eligibles

    assert bras_eligibles("XY") == ()
    assert bras_eligibles("") == ()
    assert bras_eligibles(None) == ()


def test_le_texte_stocke_est_la_concatenation_ou_NULL() -> None:
    """La forme rangee en base : 'ABCD', 'AB', 'C'... et NULL quand on ne sait
    pas, plutot qu'une chaine vide qui se lirait comme une reponse."""
    from src.lib.gabarits import bras_eligibles_texte

    assert bras_eligibles_texte("ABCD") == "ABCD"
    assert bras_eligibles_texte("ABCD", metier_connu=False) == "AB"
    assert bras_eligibles_texte("A") == "A"
    assert bras_eligibles_texte("XY") is None


@pytest.mark.parametrize("consigne", ["A", "AB", "CD", "ABCD"])
@pytest.mark.parametrize("metier_connu", [True, False])
def test_le_bras_servi_est_toujours_un_bras_eligible(consigne, metier_connu) -> None:
    """🔴 L'INVARIANT QUI COMPTE. `bras_du_lot` et `bras_eligibles` doivent dire
    la meme chose : si le bras servi pouvait ne pas figurer dans l'ensemble
    enregistre, la trace mentirait, et elle mentirait exactement sur les cas
    limites qu'elle existe pour eclairer."""
    from src.lib.gabarits import bras_du_lot, bras_eligibles

    eligibles = bras_eligibles(consigne, metier_connu=metier_connu)
    for rang in range(10):
        servi = bras_du_lot(consigne, rang, metier_connu=metier_connu)
        assert servi in eligibles, (
            f"consigne={consigne!r} metier_connu={metier_connu} rang={rang} : "
            f"servi {servi!r} hors de {eligibles}"
        )


def test_lalternance_couvre_tous_les_bras_eligibles() -> None:
    """Sur un lot assez long, chaque bras eligible sort au moins une fois."""
    from src.lib.gabarits import bras_du_lot, bras_eligibles

    eligibles = bras_eligibles("ABCD")
    servis = {bras_du_lot("ABCD", rang) for rang in range(8)}
    assert servis == set(eligibles)


@pytest.mark.parametrize("consigne", ["DC", "BA", "CA", "DCBA", "DB"])
def test_le_texte_range_est_toujours_dans_lordre_canonique(consigne) -> None:
    """🔴 La contrainte `messages_bras_eligibles_domaine` exige `^A?B?C?D?$` —
    les lettres dans l'ordre. Or `bras_demandes` PRÉSERVE l'ordre de
    l'appelant : `dict.fromkeys` dédoublonne, il ne trie pas.

    Une consigne `"DC"` — parfaitement licite au regard de la docstring, qui
    présente le paramètre comme une LISTE de bras — produisait donc
    `bras_eligibles='DC'`, refusé par la base. Chaque contact du lot levait à
    l'insert, `drafts == 0`, `failed == limit`, zéro brouillon tous les jours.
    Et l'alerte de famine aurait crié « la file est bouchée », pas « la
    consigne est dans le mauvais ordre ».

    ⚠️ L'ORDRE D'ALTERNANCE, LUI, RESTE CELUI DE L'APPELANT : `"DC"` doit
    servir D au rang 0. Seul le texte RANGÉ EN BASE est canonique — voir le
    test suivant.
    """
    import re

    from src.lib.gabarits import bras_eligibles_texte

    texte = bras_eligibles_texte(consigne)
    assert texte is not None
    assert re.fullmatch(r"A?B?C?D?", texte), (
        f"{consigne!r} rend {texte!r}, que la contrainte SQL refuse"
    )
    assert len(texte) >= 1


def test_lordre_dalternance_reste_celui_de_lappelant() -> None:
    """Contrôle négatif du test précédent : canoniser le TEXTE ne doit pas
    canoniser le TIRAGE. Demander « CD » sert C en premier, « DC » sert D."""
    from src.lib.gabarits import bras_du_lot, bras_eligibles

    assert bras_du_lot("DC", 0) == "D"
    assert bras_du_lot("CD", 0) == "C"
    assert bras_eligibles("DC") == ("D", "C")
