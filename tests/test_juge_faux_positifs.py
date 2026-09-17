"""Les trois faux positifs du juge, mesurés le 2026-09-15.

🔴 CE FICHIER GARDE DES RÈGLES DE PROMPT, pas du code. Un prompt se réécrit sans
qu'aucun test ne tombe : c'est du texte. Ces trois-là ont chacun coûté un refus
de brouillon, et rien n'empêcherait de les retirer en faisant du ménage.

Premier passage de WF-5 sur la copie AC1 : 9 refus sur 20. Les 3 blocages
venaient du dictionnaire (corrigés ailleurs). Sur les 6 « à revoir », **trois
étaient des refus à tort** :

  · « affirmation non ancrée (Facebook) » — la phrase vient du GABARIT, elle
    décrit ce que le système sait recevoir, pas ce que le prospect possède.
  · « le nombre de familles (4) dépasse le signal `metiers_offerts: 3` » — ce
    champ n'existe nulle part. Le juge a inventé le nom ET la valeur.
  · « piscine non couverte de façon indépendante » — la norme écrite est
    « plausiblement couverte », et le libellé nommait bien des piscines.

⚠️ Un refus à tort n'est pas neutre : le brouillon part en relecture manuelle,
et sur un champ imaginaire, personne ne peut le corriger — il n'y a rien à
changer dans le courriel pour satisfaire une règle qui n'existe pas.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PROMPT = (Path(__file__).resolve().parent.parent
          / "src" / "prompts" / "compliance.md").read_text(encoding="utf-8")


def test_le_juge_sait_que_les_canaux_sont_une_phrase_fixe() -> None:
    """« …un message sur ton site ou sur Facebook » est dans le gabarit.

    Le prospect n'a pas besoin d'avoir une page Facebook pour que la phrase soit
    vraie : elle dit ce que le système sait RECEVOIR.
    """
    assert "canaux du bloc SERVICE" in PROMPT
    assert "pas** ce que le prospect possède" in PROMPT


def test_le_juge_sait_qu_aucun_champ_ne_borne_le_nombre_de_metiers() -> None:
    """Le refus le plus coûteux des trois : il porte sur un champ imaginaire.

    Le prompt liste maintenant les VRAIES clés du research_json, pour que
    l'absence soit vérifiable plutôt qu'affirmée.
    """
    assert "metiers_offerts" in PROMPT, "le faux champ doit être nommé pour être nié"
    assert "n'invente jamais un seuil" in PROMPT.lower()
    for vraie_cle in ("services_offered", "lead_potential", "pain_points_detected"):
        assert vraie_cle in PROMPT, vraie_cle


def test_le_juge_applique_plausiblement_couverte_et_pas_plus_strict() -> None:
    """Un service nommé DANS un libellé couvre la famille. Le juge avait
    appliqué « de façon indépendante », qui n'est écrit nulle part."""
    bas = PROMPT.lower()
    assert "plausiblement couverte" in bas
    assert "de façon indépendante" in bas, (
        "la norme plus stricte doit être nommée pour être écartée"
    )


def test_le_tableau_des_familles_ne_classe_plus_le_lavage_a_pression() -> None:
    """🔴 RENVERSÉ LE 2026-09-16. Le tableau enseignait « Lavage à pression →
    lavage de vitres », ce qui n'est plus vrai depuis que William l'a retiré du
    dictionnaire. Un tableau d'exemples périmé est pire qu'absent : il dit au
    juge d'accepter exactement ce qu'on vient d'interdire.
    """
    ligne_famille = [
        L for L in PROMPT.splitlines()
        if "**lavage de vitres**" in L and L.strip().startswith("|")
    ]
    assert ligne_famille, "la ligne du tableau a disparu"
    assert not any("pression" in L for L in ligne_famille), ligne_famille


@pytest.mark.parametrize(
    "phrase_fixe",
    [
        "L'ouvreur de saison des gabarits C et D",
        "Le 2ᵉ temps",
        "Le bloc du site des gabarits C et D",
        "Les quatre chiffres de marché de la relance 2",
        "L'énumération des canaux du bloc SERVICE",
    ],
)
def test_chaque_phrase_imposee_par_le_code_est_nommee(phrase_fixe: str) -> None:
    """La liste des phrases que le juge ne doit PAS signaler.

    Chacune y est arrivée après un refus à tort. En retirer une ne casse rien
    tout de suite : ça se voit seulement au prochain lot, sur des brouillons
    refusés pour une phrase que le rédacteur n'a même pas écrite.
    """
    assert phrase_fixe in PROMPT
