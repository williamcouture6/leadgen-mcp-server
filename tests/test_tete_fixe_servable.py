"""C et D ne se servent pas à tout le monde : leur premier paragraphe est FIXE.

Trois constats d'un conseil de relecture, le 2026-09-07. Tous partent du même
fait : **les gabarits C et D ne s'adaptent pas.** Leur premier paragraphe se
recopie au mot près, avec trois trous seulement. Quand une des valeurs manque,
le rédacteur n'a pas de porte de sortie — il invente, ou il laisse un blanc.

A et B n'ont pas ce problème : leur ouvreur est GÉNÉRÉ.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.lib.gabarits import GABARITS_A_TETE_FIXE, bras_du_lot, tete_fixe_servable
from src.tools.personalize import bloc_metiers_resolus

FEVRIER = date(2027, 2, 10)


# ======================================================== 1. LE PRÉDICAT ====


@pytest.mark.parametrize(
    "metiers,citation,nb_services,attendu,pourquoi",
    [
        (True, True, 4, True, "le cas normal"),
        (False, True, 4, False, "aucun métier reconnu : {METIER} n'a rien à recevoir"),
        (
            True,
            False,
            1,
            False,
            "repli d'avis + un seul service : « autant X que Y » est impossible "
            "et « tu en couvres beaucoup » est faux",
        ),
        (True, False, 2, True, "repli, mais deux services suffisent à l'énumération"),
        (True, True, 1, True, "un seul service, mais la citation d'avis passe : "
                              "l'énumération de repli ne sert pas"),
        (False, False, 1, False, "les deux manques à la fois"),
    ],
)
def test_le_predicat(
    metiers: bool, citation: bool, nb_services: int, attendu: bool, pourquoi: str
) -> None:
    assert (
        tete_fixe_servable(
            metiers_reconnus=metiers,
            citation_autorisee=citation,
            nb_services=nb_services,
        )
        is attendu
    ), pourquoi


def test_le_metier_reconnu_prime_sur_tout() -> None:
    """Contrôle négatif : sans métier, aucune abondance de services ni note
    parfaite ne rend C et D servables. `{METIER}` reste vide."""
    assert (
        tete_fixe_servable(
            metiers_reconnus=False, citation_autorisee=True, nb_services=9
        )
        is False
    )


# ================================================ 2. IL EST BRANCHÉ, PARTOUT


def _company(services: list[str], note: float | None, avis: int | None) -> dict:
    return {
        "research_json": {"services_offered": services},
        "google_rating": note,
        "google_reviews_count": avis,
    }


@pytest.mark.parametrize(
    "services,note,avis,servable",
    [
        (["Déneigement résidentiel", "Tonte de pelouse"], 4.8, 47, True),
        (["Déneigement résidentiel"], 3.2, 4, False),  # repli + un seul service
        (["Plomberie résidentielle", "Débouchage"], 4.9, 80, False),  # aucun métier
    ],
)
def test_les_deux_routes_appellent_le_meme_predicat(
    services: list[str], note: float, avis: int, servable: bool
) -> None:
    """🔴 Quatrième fois cette semaine qu'un garde-fou est écrit sans être
    branché. Ici il y a DEUX routes — le lot (`/wf4/run`) et le rejeu manuel
    (`/personalize/contact`) — et la seconde n'avait aucune garde du tout
    jusqu'au 2026-09-07.

    On lit la fonction que les deux appellent, pas le code source.
    """
    import src.http_api as http_api

    assert http_api._tete_fixe_servable(_company(services, note, avis)) is servable


def test_sans_tete_fixe_servable_ni_C_ni_D() -> None:
    """Le lien entre le prédicat et le tirage, énoncé plutôt que supposé."""
    assert {bras_du_lot("ABCD", i, metier_connu=False) for i in range(20)} == {"A", "B"}
    assert {bras_du_lot("ABCD", i, metier_connu=True) for i in range(20)} == {
        "A",
        "B",
        "C",
        "D",
    }


# ============================== 3. LE LEXIQUE NE SERT QU'À A ET B ==========

SERVICES = ["Déneigement résidentiel", "Aménagement paysager"]


@pytest.mark.parametrize("gabarit", sorted(GABARITS_A_TETE_FIXE))
def test_le_lexique_n_est_pas_servi_aux_gabarits_fixes(gabarit: str) -> None:
    """Ses deux entrées — « où il est » pour l'ouvreur, « les trois questions »
    pour le bloc service — ne visent que des paragraphes GÉNÉRÉS. C et D n'ont
    nulle part où les mettre.

    Le leur servir, c'était donner des consignes détaillées sur des paragraphes
    qu'ils doivent recopier sans changer une virgule : du bruit qui invite à
    improviser là où on demande exactement le contraire.
    """
    txt = bloc_metiers_resolus(SERVICES, FEVRIER, gabarit=gabarit)
    assert "Lexique" not in txt
    assert "trois questions" not in txt
    # Mais le reste est toujours là : c'est le lexique qu'on retire, pas la
    # résolution des métiers.
    assert "Métier de la scène" in txt
    assert "2ᵉ temps OBLIGATOIRE" in txt


@pytest.mark.parametrize("gabarit", ["A", "B", None])
def test_le_lexique_reste_servi_a_A_et_B(gabarit: str | None) -> None:
    """Contrôle négatif. Sans lui, retirer le lexique pour TOUT LE MONDE
    passerait le test précédent — et A et B perdraient les consignes qui
    gouvernent leur ouvreur généré.

    `None` = l'appelant ne sait pas quel bras sera tiré : on sert, par défaut.
    """
    txt = bloc_metiers_resolus(SERVICES, FEVRIER, gabarit=gabarit)
    assert "Lexique" in txt
    assert "trois questions" in txt


# ================== 4. LE 4ᵉ PARAGRAPHE DE C NOMME LA SCÈNE ================


def test_le_gabarit_dit_quel_metier_va_au_4e_paragraphe() -> None:
    """Le paragraphe « j'aide les PME de {METIER} » porte un trou que le bloc
    « Métiers résolus » ne désignait pas explicitement. Un courriel qui ouvre
    sur le déneigement puis dit « j'aide les PME de paysagement » se contredit
    en trois lignes, et le prospect le voit."""
    from pathlib import Path

    prompt = (
        Path(__file__).parent.parent / "src/prompts/reacti/personalize.md"
    ).read_text(encoding="utf-8")
    i = prompt.find("## LE GABARIT C")
    j = prompt.find("j'aide les PME de {METIER}", i)
    assert i > 0 and j > i
    consigne = prompt[i:j]
    assert "métier de la scène" in consigne, (
        "le gabarit C ne dit pas quel métier va dans « j'aide les PME de … »"
    )
    assert "jamais le dominant" in consigne
