"""Les trous de dictionnaire qui rendaient des paysagistes injoignables.

🔴 LE DÉFAUT. Trois entreprises portaient une `fenetre_mois` VIDE — joignables
zéro mois sur douze, donc invisibles pour toujours. Deux d'entre elles ont une
adresse valide. Leurs libellés sont pourtant sans ambiguïté : « Pose de
tourbe », « Plantation et jardins », « Transplantation d'arbres ». Aucune racine
ne les appariait, donc seuls `pavage` et `excavation` étaient reconnus — et
ceux-là sont des métiers douze-mois, qui n'ouvrent jamais une séquence (règle
William du 2026-09-02).

📏 LES TROIS RACINES ONT ÉTÉ CHOISIES SUR MESURE, pas à l'intuition. Comptées
sur les libellés réels de la base le 2026-09-13 :

  ✅ `tourbe` (47 libellés)          — « pose de tourbe », « installation de
                                       tourbe ». Aucun faux positif.
  ✅ `plantation` (64)               — « plantation et jardins », « plantation
                                       de plantes ».
  ✅ `transplantation` (dans 41 « arbre ») — nécessaire À PART : l'ancrage
                                       `\\b` fait que `plantation` ne mord PAS
                                       dans « transplantation ».

🔴 DEUX CANDIDATES ONT ÉTÉ ÉCARTÉES, et il faut savoir pourquoi avant de les
rouvrir :

  ❌ `jardin` (48 libellés) attrape du **commerce de détail** — « vente de
     meubles de jardin et pavillons » (un pisciniste), « centre jardin /
     serre », « conseils en jardinage », « jardins pédagogiques » (une firme de
     design en permaculture). Ce ne sont pas des contracteurs de paysagement.
  ❌ `arbre` (41) désigne de l'**arboriculture** (« élagage et abattage »,
     « taille des arbres et arbustes ») — un métier voisin mais distinct, à sa
     propre saison. Et il n'aurait rien changé : toutes les fiches concernées
     portent déjà `tonte`, dont la fenêtre contient celle du paysagement.

✅ LE PIÈGE « amenagement contient menage » N'EN EST PAS UN, vérifié par
exécution : l'appariement est ancré sur un début de mot (`metiers.py:228-231`),
donc `\\bmenage` ne mord pas dans « amenagement ». La racine `amenagement` nue
reste malgré tout écartée — elle attrape « aménagement de sous-sol » et
« aménagement intérieur », qui ne sont pas du paysagement.

🔴 CES RACINES NE RECLASSENT RIEN TANT QUE LE RATTRAPAGE N'EST PAS REJOUÉ.
`companies.fenetre_mois` est un cache que rien n'invalide. Le rejeu de
`scripts/backfill_metiers.py` est une décision de William (2026-09-13) : ne PAS
le lancer de sa propre initiative.
"""
from __future__ import annotations

import pytest

from src.lib.metiers import classer_services, colonnes_metiers


# ── Les libellés qui ne trouvaient personne ────────────────────────────────

@pytest.mark.parametrize(
    "libelle",
    [
        "Pose de tourbe",
        "Installation de tourbe",
        "Pose de pelouse / tourbe",
        "Plantation et jardins",
        "Plantation de plantes",
        "Transplantation d'arbres",
    ],
)
def test_le_libelle_est_reconnu_comme_paysagement(libelle: str) -> None:
    assert "paysagement" in classer_services([libelle]).metiers, (
        f"{libelle!r} n'apparie aucune racine — le trou est rouvert"
    )


def test_transplantation_a_besoin_de_sa_propre_racine() -> None:
    """L'ancrage sur début de mot est ce qui l'impose.

    `plantation` seul NE suffit pas : `\\bplantation` ne mord pas dans
    « transplantation ». Si quelqu'un « simplifie » en retirant la racine
    `transplantation` parce qu'elle a l'air redondante, ce test tombe.
    """
    import re
    assert not re.search(r"\bplantation", "transplantation d'arbres")
    assert "paysagement" in classer_services(["Transplantation d'arbres"]).metiers


# ── Ce que les nouvelles racines NE doivent PAS attraper ───────────────────

@pytest.mark.parametrize(
    "libelle",
    [
        "Vente de meubles de jardin et pavillons",
        "Centre jardin / serre",
        "Conseils en jardinage",
        "Aménagement de sous-sol",
        "Aménagement intérieur",
    ],
)
def test_les_faux_positifs_ecartes_le_restent(libelle: str) -> None:
    """Commerce de détail et aménagement intérieur ne sont pas du paysagement.

    Si ce test tombe, quelqu'un a ajouté `jardin` ou `amenagement` nu. Relire
    la docstring de ce fichier AVANT de le mettre à jour : ces deux racines ont
    été écartées sur mesure, pas par oubli.
    """
    assert "paysagement" not in classer_services([libelle]).metiers, (
        f"{libelle!r} classé en paysagement — ce n'est pas un contracteur de "
        f"paysagement"
    )


def test_amenagement_ne_declenche_pas_menage() -> None:
    """Le piège que le dépôt documentait comme ouvert, et qui ne l'est pas.

    L'appariement est ancré sur un début de mot. `menage` ne mord donc pas dans
    `amenagement`. Ce test épingle la propriété pour qu'on cesse de s'en méfier.
    """
    c = classer_services(["Aménagement paysager résidentiel"])
    assert "paysagement" in c.metiers
    assert "ménage" not in c.metiers


# ── Le cas réel, bout à bout ───────────────────────────────────────────────

def test_les_deux_paysagistes_a_fenetre_vide_rouvrent() -> None:
    """Libellés copiés tels quels de la base le 2026-09-13."""
    vert_autrement = [
        "Plantation et jardins", "Pose de tourbe", "Pavé uni",
        "Excavation", "Installation de piscines creusées",
    ]
    cote_jardin = [
        "Construction (pavé uni, plantation)", "Transplantation d'arbres",
        "Excavation", "Pavage",
    ]
    for nom, services in (("Vert Autrement", vert_autrement),
                          ("Côté Jardin", cote_jardin)):
        cols = colonnes_metiers(services)
        assert cols["fenetre_mois"], f"{nom} : fenêtre toujours vide"
        assert cols["fenetre_mois"] == [1, 2, 3, 4, 5, 6], (
            f"{nom} : attendu la fenêtre du paysagement (janvier→juin), "
            f"obtenu {cols['fenetre_mois']}"
        )


def test_niwa_reste_ferme_et_ce_n_est_PAS_un_trou_de_dictionnaire() -> None:
    """🔴 LE TROISIÈME PAYSAGISTE N'EST PAS RÉPARÉ, ET C'EST VOULU.

    « Niwa Paysagiste » porte `industry = 'paysagiste'` et le mot dans son nom,
    mais ses services sont tous du dur : « Aménagement complet clé en main »,
    « Trottoirs », « Terrasses sur mesure », « Pavage », « Murets »,
    « Marches ». C'est un contracteur de PAVAGE, métier douze-mois, qui
    n'ouvre pas de séquence.

    Son cas relève d'une question de CONCEPTION, pas du dictionnaire :
    `classer_services` n'utilise `industry` qu'en REPLI, quand les services ne
    rendent rien — ici `pavage` matche, donc `industry` est ignoré. Faire
    d'`industry` un COMPLÉMENT plutôt qu'un repli toucherait beaucoup de
    fiches et demande sa propre mesure, et la décision de William.

    Ce test existe pour qu'on ne « répare » pas Niwa par accident en élargissant
    les racines : s'il tombe, c'est qu'une racine trop large est entrée.
    """
    services = ["Aménagement complet clé en main", "Trottoirs",
                "Terrasses sur mesure", "Pavage (asphalte et pavés)",
                "Murets", "Marches"]
    c = classer_services(services, industry="paysagiste")
    assert c.metiers == ("pavage",), f"obtenu {c.metiers}"
    assert colonnes_metiers(services, industry="paysagiste")["fenetre_mois"] == []
