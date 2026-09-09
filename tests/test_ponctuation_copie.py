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
