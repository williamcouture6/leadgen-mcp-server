"""`industry` COMPLÈTE le classement au lieu de n'être qu'un repli.

🔴 CE QUE ÇA CHANGE, ET POURQUOI (décision William, 2026-09-14).
Jusqu'ici `classer_services` n'appelait `metier_depuis_industry` **jamais** — la
fonction existait, sa docstring nommait même le cas qu'elle devait régler, et
personne ne l'appelait (« le repli existe et n'est appelé par personne »). Deux
entreprises le payaient, et ce sont celles que William a nommées :

  · **Niwa Paysagiste** — `industry = 'paysagiste'`, mais tous ses libellés sont
    du dur (« Trottoirs », « Terrasses sur mesure », « Pavage », « Murets »).
    Seul `pavage` sortait, métier douze-mois qui n'ouvre rien : fenêtre VIDE,
    injoignable pour toujours.
  · **Spray Green** — `industry = 'tonte de gazon'`, mais ses libellés sont tous
    des soins connexes (« Aération de terrain », « Fertilisation », « Contrôle
    de la digitaire »). AUCUN métier reconnu : douze mois par défaut inversé,
    donc démarchée en décembre pour de la pelouse.

📏 POURQUOI C'EST FIABLE, mesuré le 2026-09-14 : `industry` n'est pas du texte
libre deviné. C'est le **mot-clé de sourcing** qui a fait entrer l'entreprise
dans la liste, et il ne prend que **sept valeurs**, toutes des métiers :
`entrepreneur en déneigement`, `paysagiste`, `exterminateur`, `tonte de gazon`,
`tonte de pelouse`, `lavage de vitres`, `entretien de piscine`.

📏 L'IMPACT EST PETIT ET SÛR, simulé sur les 478 fiches qui portent un
`industry` : **462 (97 %) ont déjà ce métier** reconnu par leurs services — rien
ne bouge. **14** changent de fenêtre, et les 14 vont dans le bon sens (4 sortent
du silence, 4 déneigeurs récupèrent leur hiver, 6 quittent le douze-mois pour
leur vraie saison).

🔴 LE SECTEUR NE PREND JAMAIS LE DESSUS SUR LES SERVICES. Il est ajouté avec un
poids de 1 et un rang de dernier arrivé, donc il ne devient **dominant** que
s'il est seul. C'est ce qui protège le LEXIQUE du courriel : pour Niwa, la
fenêtre s'ouvre grâce au secteur, mais le courriel continue de parler de pavé
uni et de trottoirs — ce qu'elle vend vraiment. Inverser ce poids ferait écrire
à un pavageur qu'on veut lui parler de plates-bandes.

🔧 PORTÉE RÉDUITE LE 2026-09-15 : le secteur ne parle plus que si aucun
métier **saisonnier** n'a été trouvé dans les services. Les deux cas nommés
ci-dessus (Niwa, Spray Green) sont préservés — aucun de leurs services ne
donne de saison. Ce qui disparaît, c'est le métier ajouté à une fiche qui
avait déjà sa saison : le rédacteur le nommait, et le juge bloquait. Voir
`classer_services` pour la mesure (9 fiches touchées sur 708, 0 devient
injoignable).

🔴 RIEN N'EST RECLASSÉ EN BASE PAR CE FICHIER. La colonne est un cache : ces
14 fiches ne bougeront qu'au rejeu de `scripts/backfill_metiers.py`, qui est une
décision de William.
"""
from __future__ import annotations

import pytest

from src.lib.metiers import classer_services, colonnes_metiers


# ── Les deux cas nommés par William ────────────────────────────────────────

def test_niwa_le_secteur_ouvre_la_fenetre_sans_voler_la_scene() -> None:
    """Libellés copiés tels quels de la base."""
    services = ["Aménagement complet clé en main", "Trottoirs",
                "Terrasses sur mesure", "Pavage (asphalte et pavés)",
                "Murets", "Marches", "Estimations gratuites et personnalisées"]

    c = classer_services(services, industry="paysagiste")

    assert "paysagement" in c.metiers, "le secteur n'a pas été pris en compte"
    assert "pavage" in c.metiers, "le métier des services a disparu"
    assert c.metiers[0] == "pavage", (
        f"le secteur a volé la première place : {c.metiers}. Le courriel "
        f"parlerait de plates-bandes à un pavageur."
    )
    cols = colonnes_metiers(services, industry="paysagiste")
    assert cols["fenetre_mois"] == [1, 2, 3, 4, 5, 6], cols["fenetre_mois"]


def test_spray_green_le_secteur_ne_fait_que_confirmer() -> None:
    """💀 CE TEST S'APPUYAIT SUR LE SECTEUR IL Y A UNE HEURE — plus maintenant.

    Il vérifiait que `industry = tonte de gazon` sortait Spray Green du
    douze-mois. William a demandé le correctif au BON niveau : « pour Spray
    Green, il faudrait ajouter les mots au dictionnaire ». Ses six libellés sont
    tous des soins de gazon — elle doit être reconnue par ce qu'elle VEND, pas
    rattrapée par son mot-clé de sourcing. Voir
    `tests/test_racines_pelouse_trous.py`.

    Ce que ce test garde, et qui vaut pour 462 fiches sur 478 : quand le secteur
    dit la même chose que les services, il doit être un **NO-OP** complet. S'il
    incrémentait le compte, il ferait basculer le dominant sur une égalité et
    changerait le lexique du courriel de toutes ces entreprises.
    """
    services = ["Aération de terrain", "Fertilisation",
                "Contrôle des mauvaises herbes", "Contrôle de la digitaire",
                "Application de chaux", "Contrôle d'insectes"]

    sans = classer_services(services)
    assert "tonte" in sans.metiers, (
        "les racines de soins de pelouse ont disparu — Spray Green retombe "
        "dans le douze-mois"
    )
    assert sans.source == "services_offered"

    avec = classer_services(services, industry="tonte de gazon")
    assert avec.metiers == sans.metiers
    assert avec.compte == sans.compte, f"{sans.compte} → {avec.compte}"
    assert avec.source == "services_offered"
    assert colonnes_metiers(services, industry="tonte de gazon")["fenetre_mois"] \
        == [1, 2, 3, 4, 5, 6, 7]


# ── Le secteur ne doit jamais dominer les services ─────────────────────────

def test_le_secteur_ne_devient_dominant_que_s_il_est_seul() -> None:
    """🔧 AFFINÉ LE 2026-09-15 — l'intention est INCHANGÉE, sa portée a rétréci.

    La règle de ce fichier était « le secteur complète, sans jamais dominer ».
    Elle laissait le métier du secteur ENTRER dans la liste même quand les
    services avaient déjà donné une saison — et le rédacteur, lui, nomme tous
    les métiers de la liste, pas seulement le dominant. Protéger le dominant ne
    protégeait donc pas le courriel.

    Mesuré le 2026-09-15, premier passage de WF-5 sur la copie AC1 : sur 3
    blocages, 2 venaient de là. Terrassement S.H. ne déclare que du
    terrassement et de l'aménagement paysager ; trouvée par « entrepreneur en
    déneigement », elle recevait le métier, et son courriel lui parlait de
    déneigement. Le juge bloquait pour fait inventé — à raison.

    La règle est maintenant : **le secteur ne parle que si aucun métier
    SAISONNIER n'a été trouvé.** C'est exactement sa raison d'être — ouvrir une
    fenêtre quand il n'y en a aucune. Quand les services en donnent déjà une,
    il n'ajoute plus une information, il en ajoute une fausse.
    """
    seul = classer_services([], industry="paysagiste")
    assert seul.metiers == ("paysagement",)
    assert seul.source == "industry"

    # Les services donnent une SAISON (déneigement) : le secteur se tait.
    saison_trouvee = classer_services(
        ["Déneigement résidentiel", "Déneigement commercial"],
        industry="paysagiste",
    )
    assert saison_trouvee.metiers[0] == "déneigement", saison_trouvee.metiers
    assert "paysagement" not in saison_trouvee.metiers, (
        "le secteur a parlé alors qu'une saison était déjà connue"
    )

    # 🔴 LE CAS NIWA, celui pour lequel le mécanisme existe, et qui continue de
    # marcher. Ses libellés ne donnent que `pavage` — un métier SANS saison,
    # qui n'ouvre aucune fenêtre. Le secteur est alors le seul moyen d'en avoir
    # une, et il la donne SANS voler le dominant : le courriel continue de
    # parler de pavé uni, ce qu'elle vend vraiment.
    sans_saison = classer_services(
        ["Pavage", "Trottoirs", "Terrasses sur mesure"], industry="paysagiste"
    )
    assert "paysagement" in sans_saison.metiers, sans_saison.metiers
    assert sans_saison.metiers[0] == "pavage", (
        "le secteur a volé le dominant, donc le lexique du courriel"
    )


def test_un_secteur_deja_reconnu_ne_compte_pas_deux_fois() -> None:
    """97 % des fiches sont dans ce cas — le secteur doit y être un NO-OP.

    S'il incrémentait le compte, il ferait basculer le dominant sur une égalité
    et changerait le lexique du courriel de 462 entreprises.
    """
    services = ["Aménagement paysager", "Déneigement", "Déneigement commercial"]
    sans = classer_services(services)
    avec = classer_services(services, industry="paysagiste")

    assert avec.metiers == sans.metiers, f"{sans.metiers} → {avec.metiers}"
    assert avec.compte == sans.compte, f"{sans.compte} → {avec.compte}"


# ── L'exigence de `piscine` ────────────────────────────────────────────────

def test_entretien_de_piscine_satisfait_l_exigence() -> None:
    """🔴 SANS CECI, LE SECTEUR `piscine` SERAIT INERTE.

    `piscine` n'ouvre une fenêtre que si un libellé porte un verbe d'entretien.
    Le secteur `entretien de piscine` porte le mot « entretien » dans son propre
    nom : le lire, c'est satisfaire l'exigence. Ne pas le lire donnerait un
    métier ajouté qui n'ouvre rien — un changement invisible, donc pire qu'un
    changement absent.
    """
    c = classer_services([], industry="entretien de piscine")
    assert c.metiers == ("piscine",)
    assert "piscine" in c.exigence_satisfaite
    assert colonnes_metiers([], industry="entretien de piscine")["fenetre_mois"] \
        == [1, 2, 3, 4, 5, 6, 7]


def test_un_constructeur_de_piscines_n_ouvre_toujours_rien() -> None:
    """La contre-épreuve : le secteur ne doit pas servir de passe-droit."""
    c = classer_services(["Installation de piscines creusées"], industry=None)
    assert c.metiers == ("piscine",)
    assert "piscine" not in c.exigence_satisfaite
    assert colonnes_metiers(["Installation de piscines creusées"])["fenetre_mois"] == []


# ── La traçabilité ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "services, industry, attendu",
    [
        (["Déneigement"], None, "services_offered"),
        (["Déneigement"], "entrepreneur en déneigement", "services_offered"),
        (["Pavage"], "paysagiste", "services_offered+industry"),
        ([], "paysagiste", "industry"),
        ([], None, "inconnu"),
        (["Vente de bonbons"], None, "inconnu"),
    ],
)
def test_la_source_dit_d_ou_vient_le_classement(services, industry, attendu) -> None:
    """`metier_source` doit distinguer les quatre origines.

    Sans la valeur `services_offered+industry`, on ne pourrait pas retrouver en
    base les fiches dont la fenêtre dépend du secteur — donc pas mesurer l'effet
    de cette décision, ni la défaire.
    """
    assert classer_services(services, industry=industry).source == attendu


# ── Robustesse ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("industry", [None, "", "   ", "boulangerie", "None"])
def test_un_secteur_vide_ou_inconnu_ne_change_rien(industry) -> None:
    services = ["Déneigement résidentiel"]
    assert classer_services(services, industry=industry).metiers == ("déneigement",)


def test_le_classement_reste_stable_avec_un_secteur() -> None:
    """Le tri dépend de l'ordre d'insertion ; le secteur en ajoute un dernier.

    Si l'insertion passait par un set, l'ordre dépendrait de la randomisation du
    hash de Python — le défaut mesuré en septembre, où le même prospect changeait
    de métier dominant d'un processus à l'autre.
    """
    services = ["Pavage", "Excavation"]
    vus = {classer_services(services, industry="paysagiste").metiers
           for _ in range(50)}
    assert len(vus) == 1, f"classement instable : {vus}"
