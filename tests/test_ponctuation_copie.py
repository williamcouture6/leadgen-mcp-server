"""Aucun tiret long ne part chez un prospect.

🔴 Corrigé PAR LE CODE, pas par une consigne — décision du 2026-09-09, en
relisant les brouillons réels : 5 sur 60 en portaient un, tous dans l'ouvreur
GÉNÉRÉ du gabarit A. Les gabarits fixes n'en produisent aucun.

Une règle de plus dans le prompt aurait été le réflexe. Mais un caractère
typographique est exactement ce qu'un modèle oublie sous charge, et le vérifier
coûterait un contrôle de conformité qui ne peut, lui, qu'ANNOTER : le tiret
partirait quand même. Une substitution mécanique ne peut pas être oubliée, et
elle ne peut pas changer le sens — on échange un signe de ponctuation contre un
autre.

Le tiret long est un tic d'écriture de modèle. Un contracteur ne l'emploie pas,
et il signe le courriel comme n'ayant pas été écrit par un humain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.personalize import sans_tiret_long

TIRETS = ("—", "–", "―")


@pytest.mark.parametrize(
    "avant,apres",
    [
        # Le cas réel, tiré du brouillon de Jardins Verdana.
        (
            "ça se fait pas les mains libres — t'es dans ta machine.",
            "ça se fait pas les mains libres, t'es dans ta machine.",
        ),
        # Sans espaces autour : la virgule doit quand même tomber juste.
        ("souvent—c'est le premier", "souvent, c'est le premier"),
        # Le tiret demi-cadratin et la barre horizontale, mêmes tics.
        ("un texto – un courriel", "un texto, un courriel"),
        ("un texto ― un courriel", "un texto, un courriel"),
        # Deux dans la même phrase.
        ("a — b — c", "a, b, c"),
        # Rien à faire : le texte ressort intact.
        ("aucun tiret, juste une virgule", "aucun tiret, juste une virgule"),
        # Le trait d'union ORDINAIRE ne bouge pas : « pavé-uni », « peux-tu ».
        ("tu peux-tu me pointer la bonne personne?", "tu peux-tu me pointer la bonne personne?"),
        ("du pavé-uni", "du pavé-uni"),
    ],
)
def test_le_tiret_long_devient_une_virgule(avant: str, apres: str) -> None:
    assert sans_tiret_long(avant) == apres


@pytest.mark.parametrize("vide", [None, ""])
def test_un_texte_absent_ressort_absent(vide) -> None:
    """`None` n'est pas une chaîne vide : un sujet absent doit le rester, sinon
    on écrirait `""` en base là où il y avait `NULL`."""
    assert sans_tiret_long(vide) == vide


def test_aucun_tiret_ne_survit() -> None:
    """Contrôle négatif du tableau ci-dessus : si un des trois caractères
    disparaissait de `_TIRETS_LONGS`, les cas nommés passeraient encore mais
    celui-là tomberait."""
    for t in TIRETS:
        rendu = sans_tiret_long(f"avant {t} après")
        assert t not in rendu, f"le caractère {t!r} survit"


def test_le_gabarit_dit_aussi_la_regle() -> None:
    """Le code garantit le résultat, mais le modèle doit quand même savoir :
    la virgule qu'IL choisit tombera mieux que celle qu'une substitution
    mécanique pose à sa place."""
    prompt = (
        Path(__file__).parent.parent / "src/prompts/reacti/personalize.md"
    ).read_text(encoding="utf-8")
    i = prompt.find("# LA PONCTUATION")
    assert i > 0, "le gabarit ne porte pas la règle de ponctuation"
    section = prompt[i:i + 600]
    assert "virgule" in section


def test_les_relances_passent_aussi_au_filtre() -> None:
    """Les relances sont des constantes écrites à la main et n'en portent
    aucun — mais la règle doit valoir pour TOUT ce qui part, sans exception à
    retenir. Ce test vérifie qu'elles sont propres à la source."""
    from src.lib.relances import CORPS_RELANCES

    for cle, corps in CORPS_RELANCES.items():
        for t in TIRETS:
            assert t not in corps, f"{cle} porte un {t!r}"


# ================== LES PARAGRAPHES SE RECOLLENT ============================
#
# Le corps part en TEXTE BRUT dans la variable {{email_body}} du gabarit
# Instantly, qui convertit chaque retour de ligne en saut visible. Une ligne
# vide devient donc un blanc entre paragraphes — ce qu'on veut — mais un retour
# AU MILIEU d'un paragraphe devient une coupure que le prospect voit.
#
# Mesuré le 2026-09-09 : 7 brouillons sur 69, tous en A et B. Jamais en C ni D,
# dont les paragraphes sont fixes. C'est le modèle qui formate son texte généré
# en colonnes, comme dans un éditeur.

from src.tools.personalize import recoller_les_paragraphes

SAUT = chr(10)


def test_un_paragraphe_coupe_se_recolle() -> None:
    """Le cas réel, tiré du brouillon de CM Gravel."""
    coupe = (
        "Moi c'est William, et je fais en sorte de régler ces problèmes-là. Ce que"
        + SAUT
        + "je propose aux entreprises, c'est de créer un système qui répond"
        + SAUT
        + "à tout ce qui rentre."
    )
    rendu = recoller_les_paragraphes(coupe)
    assert SAUT not in rendu
    assert "Ce que je propose" in rendu, "le recollage doit poser une espace"


def test_les_blancs_entre_paragraphes_survivent() -> None:
    """🔴 LE CONTRÔLE QUI COMPTE. Tout aplatir donnerait un pavé illisible —
    l'inverse exact du défaut qu'on répare. Un test qui ne vérifierait que
    « plus de saut au milieu » passerait avec un code qui détruit tout."""
    texte = (
        "Bonjour," + SAUT + SAUT
        + "Un paragraphe coupé" + SAUT + "en deux lignes." + SAUT + SAUT
        + "Dis-moi juste si tu veux le voir."
    )
    rendu = recoller_les_paragraphes(texte)
    assert rendu.count(SAUT + SAUT) == 2, "les paragraphes ont fusionné"
    assert len([b for b in rendu.split(SAUT + SAUT)]) == 3
    assert "coupé en deux lignes." in rendu


def test_un_texte_deja_propre_ne_bouge_pas() -> None:
    """Contrôle négatif : le recollage ne doit rien changer à ce qui est déjà
    correct — c'est le cas de 62 brouillons sur 69, et des gabarits C et D."""
    propre = "Bonjour," + SAUT + SAUT + "Une seule ligne." + SAUT + SAUT + "Merci."
    assert recoller_les_paragraphes(propre) == propre


@pytest.mark.parametrize("vide", [None, ""])
def test_le_vide_ressort_vide(vide) -> None:
    assert recoller_les_paragraphes(vide) == vide


def test_les_lignes_vides_en_trop_disparaissent() -> None:
    """Trois sauts d'affilée ne doivent pas produire un paragraphe vide, qui
    donnerait un blanc double chez le prospect."""
    texte = "A" + SAUT * 4 + "B"
    rendu = recoller_les_paragraphes(texte)
    assert rendu == "A" + SAUT + SAUT + "B"


def test_les_relances_restent_intactes() -> None:
    """Les relances sont écrites à la main par William, avec leurs paragraphes
    voulus. Le recollage ne doit RIEN y changer — sinon on réécrirait sa copie
    en passant."""
    from src.lib.relances import CORPS_RELANCES

    for cle, corps in CORPS_RELANCES.items():
        assert recoller_les_paragraphes(corps) == corps, (
            f"{cle} a été modifiée par le recollage"
        )
