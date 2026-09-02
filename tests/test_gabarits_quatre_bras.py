"""Quatre gabarits, et un paramètre qui LISTE les bras à servir.

Avant le 2026-09-01, « les gabarits sont A et B » vivait dans quatre tests
d'appartenance sur deux fichiers, plus une fonction qui rendait littéralement
`"A" if rang % 2 == 0 else "B"`. Ajouter C et D demandait de retrouver les
cinq, et en rater un ne cassait rien de bruyant : `template_used` serait
retombé sur le paramètre, et `messages.template_choice` aurait porté une
valeur fausse — donc un test A/B qui mesure autre chose que ce qu'on croit.

🔴 LA COMPATIBILITÉ QUI COMPTE : « AB » doit continuer de signifier A et B
seulement. Le JSON n8n de WF-4 poste cette valeur. Si l'arrivée de C et D
l'avait silencieusement transformée en « les quatre », la campagne aurait
changé de contenu sans que personne ne l'ait demandé — et sans qu'aucun test
ne le dise.
"""
from __future__ import annotations

import pytest

from src.lib.gabarits import GABARITS, bras_demandes, bras_du_lot, est_un_gabarit


def test_les_quatre_gabarits_sont_la_dans_l_ordre() -> None:
    assert GABARITS == ("A", "B", "C", "D")


@pytest.mark.parametrize(
    ("choix", "attendu"),
    [
        ("A", ("A",)),
        ("D", ("D",)),
        ("AB", ("A", "B")),
        ("ABCD", ("A", "B", "C", "D")),
        ("CD", ("C", "D")),
        ("ab", ("A", "B")),
        ("  AB  ", ("A", "B")),
        ("ABA", ("A", "B")),
    ],
)
def test_le_parametre_liste_les_bras(choix: str, attendu: tuple[str, ...]) -> None:
    assert bras_demandes(choix) == attendu


@pytest.mark.parametrize("choix", ["XY", "", "  ", "AZ", "ABX", None])
def test_une_valeur_inconnue_ne_devine_rien(choix: str | None) -> None:
    """Un défaut silencieux enverrait 255 courriels sur un gabarit non choisi."""
    assert bras_demandes(choix) == ()


def test_AB_signifie_toujours_A_et_B_seulement() -> None:
    """🔴 Le test de non-régression le plus important du fichier.

    Le JSON n8n de WF-4 poste « AB ». Si l'ajout de C et D avait élargi cette
    valeur aux quatre bras, la campagne aurait changé de contenu sans décision.
    Ouvrir aux quatre doit rester un geste EXPLICITE : écrire « ABCD ».
    """
    lot = [bras_du_lot("AB", rang) for rang in range(20)]
    assert set(lot) == {"A", "B"}, f"« AB » a servi autre chose que A et B : {set(lot)}"


def test_l_alternance_suit_le_rang_du_lot() -> None:
    """Jamais une propriété du contact.

    La file est triée `created_at.asc` : toute répartition dérivée du contact
    serait corrélée à son ancienneté, et le test mesurerait l'ordre de la file
    au lieu du courriel.
    """
    assert [bras_du_lot("ABCD", r) for r in range(8)] == list("ABCDABCD")


def test_l_ecart_reste_d_un_courriel_sur_un_lot_de_dix() -> None:
    """Quatre bras sur dix contacts ne tombent pas juste — mais l'écart est de 1.

    Chiffre à connaître avant de lire les résultats du test : sur un petit lot,
    le premier bras est servi une fois de plus que le dernier.
    """
    lot = [bras_du_lot("ABCD", r) for r in range(10)]
    comptes = {g: lot.count(g) for g in GABARITS}
    assert max(comptes.values()) - min(comptes.values()) == 1, comptes


def test_un_bras_force_ne_alterne_pas() -> None:
    """Le rejeu manuel d'un lead précis doit rester déterministe."""
    assert {bras_du_lot("C", r) for r in range(10)} == {"C"}


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [("A", True), ("D", True), ("d", True), ("AB", False), ("ABCD", False),
     ("", False), (None, False)],
)
def test_une_consigne_d_alternance_n_est_pas_un_gabarit(valeur, attendu) -> None:
    """« AB » est une DEMANDE, pas une réponse.

    L'écrire dans `messages.template_choice` mettrait la même valeur sur toutes
    les lignes : il n'y aurait plus de test, juste quatre textes et aucune trace
    de qui a reçu quoi. La contrainte de la migration 0048 le refuse en base ;
    `est_un_gabarit` le refuse en amont.
    """
    assert est_un_gabarit(valeur) is attendu


def test_les_bornes_de_longueur_existent_pour_les_quatre() -> None:
    """Un gabarit sans borne retomberait sur celle de A — juste par accident.

    Le repli de `check_length` reste dans la piste, donc C et D auraient eu les
    bonnes bornes sans qu'on les écrive. Un repli qui tombe juste masque le jour
    où il tombera faux.
    """
    from src.lib.compliance_checks import _BORNES_LONGUEUR

    for gabarit in GABARITS:
        assert ("agence-ia", gabarit) in _BORNES_LONGUEUR, gabarit
