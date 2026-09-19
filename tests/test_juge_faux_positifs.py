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


def test_le_juge_sait_que_metiers_offerts_NE_BORNE_RIEN() -> None:
    """🔴 CE TEST FIGEAIT UNE FAUSSETE, et elle a vecu deux jours.

    Le refus d'origine (2026-09-15) disait : « le nombre de familles enumerees
    (4) depasse le signal `metiers_offerts: 3` ». La correction ecrite le
    lendemain affirmait qu'« il n'existe PAS de `metiers_offerts` dans le
    `research_json` — ni sous ce nom ni sous un autre ». **C'etait faux** :
    mesure le 2026-09-18, `lead_potential.signaux.metiers_offerts` est present
    sur 111 des 537 fiches recherchees.

    On apprenait donc au juge a nier un champ qu'il peut lire — et ce test
    gardait le mensonge. La verite est plus simple : le champ existe, c'est un
    COMPTE estime par le modele de recherche pour le scoring, et il ne borne
    rien. Le nombre de familles vient du bloc « Faits verifies ».
    """
    bas = PROMPT.lower()
    assert "metiers_offerts" in PROMPT, "le champ doit etre nomme pour etre cadre"
    assert "n'invente jamais un seuil" in bas
    assert "compte estimé" in bas, (
        "le prompt doit dire CE QUE le champ est, pas qu'il n'existe pas"
    )
    assert "ni sous ce nom" not in bas, (
        "l'affirmation fausse (« il n'existe PAS ») est revenue dans le prompt"
    )


def test_le_champ_existe_vraiment() -> None:
    """Le temoin du test ci-dessus : si le schema de recherche cessait de
    produire ce champ, la consigne deviendrait fausse dans l'autre sens."""
    from pathlib import Path

    schema = (
        Path(__file__).resolve().parent.parent / "src/tools/research.py"
    ).read_text(encoding="utf-8")
    assert "metiers_offerts" in schema, (
        "le champ a disparu du schema de recherche : la consigne du juge parle "
        "d'un champ qui n'est plus produit"
    )


def test_le_juge_ne_reconstitue_plus_le_classement_des_familles() -> None:
    """🔴 REMPLACE « plausiblement couverte », retiree le 2026-09-18.

    Cette norme demandait au juge de decider lui-meme si un libelle couvrait
    une famille. C'est exactement le jugement qu'on lui retire : il recoit
    desormais la liste resolue dans le bloc « Faits verifies ».

    Les trois faux positifs qui ont motive le changement — « tonte » chez un
    « Entretien de gazon », « pavage » chez un scelleur de revetement,
    « menage » chez un « Entretien menager » — venaient tous de ce jugement.
    """
    bas = PROMPT.lower()
    assert "plausiblement couverte" not in bas, (
        "la norme est revenue : elle redemande au juge de classer lui-meme"
    )
    assert "ne refais pas le classement" in bas
    assert "métiers reconnus" in bas


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
