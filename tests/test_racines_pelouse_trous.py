"""Les soins de pelouse manquaient au dictionnaire de `tonte`.

🔴 LE CAS (décision William, 2026-09-14). « Spray Green - Traitement de
pelouse » ne portait AUCUN métier reconnu, donc douze mois par défaut inversé —
elle aurait été démarchée en décembre pour de la pelouse. Pourtant ses six
libellés sont tous des soins de gazon :

    Aération de terrain · Fertilisation · Contrôle des mauvaises herbes
    Contrôle de la digitaire · Application de chaux · Contrôle d'insectes

Le dictionnaire de `tonte` ne connaissait que l'acte de couper (`tonte`,
`pelouse`, `gazon`, `tondre`). Tout l'entretien AUTOUR de la coupe lui
échappait. Comme William l'a dit : « les services affichés sont tous des
services connexes à la tonte de pelouse, la déduction est facile à faire ».

📏 CHAQUE RACINE A ÉTÉ MESURÉE, et le juge a été le SECTEUR DE SOURCING dans
lequel le mot apparaît — un mot de pelouse ne doit se voir que chez des
entreprises de pelouse. Comptes du 2026-09-14 :

    digitaire      4 libellés — secteur « tonte de gazon » SEUL
    herbicide      2          — « tonte de gazon » seul
    chaux          5          — paysagiste, tonte
    engrais       10          — paysagiste, tonte
    terreautage   10          — paysagiste, tonte
    ensemencement 16          — déneigement, paysagiste, tonte
    mauvaises h.  23          — + exterminateur
    aeration      44          — déneigement, paysagiste, tonte
    fertilisation 55          — déneigement, paysagiste, tonte

Les déneigeurs y figurent parce qu'au Québec le déneigeur d'hiver est le
paysagiste d'été — ce n'est pas du bruit, c'est la même entreprise.

📏 IMPACT MESURÉ : **9 fiches** gagneraient `tonte`, dont **une seule** n'avait
aucun métier (Spray Green). Les 8 autres en ont déjà, donc `tonte` ne fait
qu'élargir une union qui recouvre déjà l'essentiel. Aucune fiche ne perd de mois.

⚠️ `chaux` EST LA SEULE À SURVEILLER. Elle est propre aujourd'hui, mais c'est
une mesure sur un catalogue de sept secteurs, tous extérieurs/résidentiels. Le
jour où le sourcing ajoutera de la maçonnerie, « chaux hydraulique » et
« mortier de chaux » la feront basculer. Le rattrapage la relèvera ; c'est
`EXCLUSIONS` qui sera la porte de sortie.

🔴 RIEN N'EST RECLASSÉ EN BASE. La colonne est un cache : ces 9 fiches ne
bougeront qu'au rejeu de `scripts/backfill_metiers.py`, décision de William.
"""
from __future__ import annotations

import pytest

from src.lib.metiers import classer_services, colonnes_metiers


@pytest.mark.parametrize(
    "libelle",
    [
        "Aération de terrain",
        "Fertilisation",
        "Contrôle des mauvaises herbes",
        "Contrôle de la digitaire",
        "Application de chaux",
        "Épandage d'engrais",
        "Ensemencement",
        "Terreautage",
        "Application d'herbicide",
    ],
)
def test_le_soin_de_pelouse_est_reconnu_comme_tonte(libelle: str) -> None:
    assert "tonte" in classer_services([libelle]).metiers, (
        f"{libelle!r} n'apparie aucune racine — le trou est rouvert"
    )


def test_spray_green_sort_du_douze_mois_par_ses_seuls_services() -> None:
    """🔴 LE CŒUR DE LA DÉCISION : sans AUCUNE aide du secteur.

    Ce test passe `industry=None` exprès. Si un jour quelqu'un retire les
    racines en se disant « le secteur suffit », ce test tombe — et c'est voulu :
    le secteur est un filet, pas la règle. Une entreprise doit être reconnue par
    ce qu'elle VEND.
    """
    services = ["Aération de terrain", "Fertilisation",
                "Contrôle des mauvaises herbes", "Contrôle de la digitaire",
                "Application de chaux", "Contrôle d'insectes"]

    c = classer_services(services, industry=None)
    assert "tonte" in c.metiers, f"obtenu {c.metiers}"

    cols = colonnes_metiers(services, industry=None)
    assert cols["fenetre_mois"] == [1, 2, 3, 4, 5, 6, 7], cols["fenetre_mois"]
    assert cols["metier_source"] == "services_offered", (
        "elle doit être reconnue par ses SERVICES, pas par son secteur"
    )


def test_la_tonte_reste_dominante_chez_un_vrai_tondeur() -> None:
    """Les racines ajoutées ne doivent pas noyer l'acte principal.

    Elles comptent toutes pour le MÊME métier (`tonte`), donc un tondeur qui
    vend aussi de la fertilisation voit son compte monter — pas se diluer.
    """
    c = classer_services(["Tonte de pelouse", "Fertilisation", "Aération"])
    assert c.metiers[0] == "tonte"
    assert c.compte["tonte"] == 3


@pytest.mark.parametrize(
    "libelle",
    ["Déneigement de toiture", "Lavage de vitres", "Installation de piscine",
     "Pavage d'entrée", "Entretien ménager commercial"],
)
def test_les_metiers_voisins_ne_basculent_pas_en_tonte(libelle: str) -> None:
    assert "tonte" not in classer_services([libelle]).metiers


def test_chaux_est_la_racine_a_surveiller() -> None:
    """Épingle le risque connu, pour qu'il soit trouvable quand il mordra.

    `chaux` est propre sur le catalogue actuel (sept secteurs, tous
    extérieurs/résidentiels). En maçonnerie, « mortier de chaux » et « chaux
    hydraulique » la feraient basculer. Si le sourcing ajoute ce secteur un
    jour, la sortie est `EXCLUSIONS`, pas le retrait de la racine — d'autres
    fiches en dépendent.
    """
    assert "tonte" in classer_services(["Application de chaux"]).metiers
    assert "tonte" in classer_services(["Chaux dolomitique pour pelouse"]).metiers
