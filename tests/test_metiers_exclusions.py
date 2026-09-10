"""Un libellé peut porter la racine d'un métier sans être ce métier.

🔴 CES DEUX DÉFAUTS N'ONT PAS ÉTÉ TROUVÉS PAR UN TEST, MAIS PAR LE JUGE LLM,
au premier passage réel du pipeline, le 2026-09-02.

Il a refusé le gabarit A de « Côté Ruelle - Paysagiste » en écrivant :
« 'tonte' et 'excavation' sont attribués à Côté Ruelle sans ancrage dans le
research_json ». Vérification faite, il avait raison sur la tonte : l'entreprise
ne tond rien, elle POSE DU GAZON EN ROULEAU. La racine `gazon` attrapait la
tourbe.

Ça vaut d'être noté, parce que ça contredit l'intuition : la table des métiers
avait 30 tests et aucun ne couvrait ce cas. C'est un modèle probabiliste, lisant
une vraie fiche d'entreprise, qui a vu ce qu'aucune assertion écrite d'avance
n'avait prévu. Le coût aurait été un courriel disant « tu fais de la tonte
aussi » à un paysagiste — une erreur que le destinataire voit immédiatement, sur
8 entreprises de la liste.

LA FRONTIÈRE RETENUE : on retire ce qui est FAUX, pas ce qui est large.
`terrassement` → excavation est conservé (37 entreprises) parce que dire « tu
fais du terrassement aussi » à un paysagiste qui fait du nivellement est vrai.
Le juge l'avait signalé aussi ; il avait tort sur ce point.
"""
from __future__ import annotations

import datetime

import pytest

from src.lib.metiers import EXCLUSIONS, resoudre_metiers

AUJOURDHUI = datetime.date(2026, 9, 2)

# La fiche réelle qui a déclenché le refus, telle qu'elle est en base.
COTE_RUELLE = [
    "Conception de plans paysagers 3D",
    "Pavage (dalles, pavés, béton, pierre naturelle)",
    "Menuiserie extérieure (terrasses, pergolas, clôtures, cabanons)",
    "Plantations (arbres, arbustes, gazon en rouleau)",
    "Murets décoratifs",
    "Éclairage extérieur",
    "Nivellement et terrassement",
    "Installation de clôtures de piscine",
    "Installation de jacuzzi",
]


def test_le_cas_reel_ne_produit_plus_de_metier_faux() -> None:
    metiers = resoudre_metiers(COTE_RUELLE, AUJOURDHUI).metiers
    assert "tonte" not in metiers, "« gazon en rouleau » n'est pas de la tonte"
    assert "piscine" not in metiers, "« clôtures de piscine » n'est pas de la piscine"
    assert "paysagement" in metiers, "le vrai métier a disparu avec les faux"


def test_terrassement_reste_de_l_excavation() -> None:
    """Conservé DÉLIBÉRÉMENT, même si le juge l'a signalé.

    « Tu fais du terrassement aussi » dit à un paysagiste qui fait du
    nivellement est vrai. Retirer ce qui est large en même temps que ce qui est
    faux appauvrirait 37 fiches pour rien.
    """
    assert "excavation" in resoudre_metiers(COTE_RUELLE, AUJOURDHUI).metiers


@pytest.mark.parametrize(
    ("libelle", "metier_a_exclure"),
    [
        ("Plantations (arbres, arbustes, gazon en rouleau)", "tonte"),
        ("Pose de gazon en plaques", "tonte"),
        ("Installation de gazon synthétique", "tonte"),
        ("Installation de clôtures de piscine", "piscine"),
        ("Abri de piscine sur mesure", "piscine"),
    ],
)
def test_un_libelle_exclu_ne_produit_pas_le_metier(libelle: str, metier_a_exclure: str) -> None:
    assert metier_a_exclure not in resoudre_metiers([libelle], AUJOURDHUI).metiers


@pytest.mark.parametrize(
    ("libelles", "metier_attendu"),
    [
        (["Tonte de pelouse"], "tonte"),
        (["Entretien de gazon"], "tonte"),
        (["Tondre les terrains commerciaux"], "tonte"),
        (["Ouverture et fermeture de piscine"], "piscine"),
        (["Entretien de spa et piscine creusée"], "piscine"),
    ],
)
def test_le_vrai_metier_survit_a_l_exclusion(libelles: list[str], metier_attendu: str) -> None:
    """🔴 Le contrôle négatif, et il compte autant que le reste.

    Une exclusion trop large coûterait le métier de vrais tondeurs et de vrais
    pisciniers — le défaut inverse, invisible dans les tests du cas fautif.
    """
    assert metier_attendu in resoudre_metiers(libelles, AUJOURDHUI).metiers


def test_l_exclusion_n_ecrase_pas_les_autres_metiers_du_meme_libelle() -> None:
    """Un libellé exclu pour UN métier peut compter pour un autre.

    « Aménagement paysager avec gazon en rouleau » n'est pas de la tonte, mais
    c'est bien du paysagement.
    """
    metiers = resoudre_metiers(["Aménagement paysager avec gazon en rouleau"], AUJOURDHUI).metiers
    assert "paysagement" in metiers
    assert "tonte" not in metiers


def test_les_exclusions_sont_ecrites_sans_accent() -> None:
    """Elles sont comparées au libellé APRÈS `_sans_accents`.

    Une exclusion accentuée ne matcherait jamais — elle serait morte en
    silence, et le faux positif reviendrait sans que rien ne bronche. C'est le
    même mode de panne que l'ancre morte de `check_statistiques_conformes`.
    """
    for metier, phrases in EXCLUSIONS.items():
        for p in phrases:
            assert p == p.lower(), f"{metier}: {p!r} n'est pas en minuscules"
            assert all(ord(c) < 128 for c in p), (
                f"{metier}: {p!r} porte un accent — elle ne matchera jamais, "
                f"les libellés sont aplatis avant la comparaison"
            )
