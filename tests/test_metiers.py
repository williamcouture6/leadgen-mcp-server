"""La résolution des métiers — les cas que la spec nomme, un par un.

Chaque test porte le cas mesuré en production qui l'a fait exister. Un test qui
casse ici veut dire qu'un lead va se faire parler du mauvais métier, ou qu'une
entreprise joignable devient invisible.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

RACINE_PROJET = Path(__file__).resolve().parents[1]

from src.lib.metiers import (
    SAISONS,
    TOUS_LES_MOIS,
    fenetre_mois,
    resoudre_metiers,
)

# Les mois de référence des exemples de la spec.
OCTOBRE = date(2026, 10, 15)
DECEMBRE = date(2026, 12, 10)
FEVRIER = date(2027, 2, 10)
AOUT = date(2026, 8, 20)

# Les fiches réelles citées par la spec.
MV_PAYSAGISTE = ["aménagement paysager", "déneigement résidentiel"]
BRILLE_O_MAX = ["lavage de vitres", "déneigement commercial"]
HERBOFLEURS = ["tonte de pelouse", "aménagement paysager"]
AMG_NEIGE = ["déneigement"]


# ---------------- 1. Les fenêtres, telles que la spec les fixe ----------------
# Arrondi généreux : un mois compte dès que la fenêtre le touche. La spec donne
# les cinq ensembles explicitement — ce test les pinne pour qu'un changement de
# la règle des 3/2 mois ne les déplace pas en silence.

@pytest.mark.parametrize(
    "metier,attendu",
    [
        ("déneigement", {8, 9, 10, 11, 12, 1}),
        ("paysagement", {1, 2, 3, 4, 5, 6}),
        ("lavage de vitres", {1, 2, 3, 4, 5, 6}),
        ("extermination", {1, 2, 3, 4, 5, 6}),
        ("tonte", {2, 3, 4, 5, 6, 7}),
    ],
)
def test_les_fenetres_sont_celles_de_la_spec(metier: str, attendu: set[int]) -> None:
    assert set(fenetre_mois(metier)) == attendu


def test_les_douze_mois_ont_quelqu_un() -> None:
    """Conséquence de l'arrondi généreux : le « trou du 1er juillet au 14 août »
    de la v3 disparaît. Juillet est couvert par la tonte, août par le
    déneigement. Si ce test casse, une période de l'année devient morte."""
    couverts: set[int] = set()
    for metier in SAISONS:
        couverts |= set(fenetre_mois(metier))
    assert couverts == set(TOUS_LES_MOIS)


def test_un_metier_sans_saison_documentee_ouvre_tous_les_mois() -> None:
    """Défaut inversé. Sur l'optimisation on autorise par défaut : un métier
    inconnu ne doit jamais faire disparaître un lead."""
    assert set(fenetre_mois("toiture")) == set(TOUS_LES_MOIS)
    assert set(fenetre_mois("métier inventé")) == set(TOUS_LES_MOIS)


# ---------------- 2. Le choix de la scène ----------------

def test_mv_paysagiste_en_octobre_parle_de_deneigement() -> None:
    r = resoudre_metiers(MV_PAYSAGISTE, OCTOBRE)
    assert r.scene == "déneigement"
    assert r.autres == ("paysagement",)


def test_mv_paysagiste_en_fevrier_parle_de_paysagement() -> None:
    r = resoudre_metiers(MV_PAYSAGISTE, FEVRIER)
    assert r.scene == "paysagement"
    assert r.autres == ("déneigement",)


def test_mv_paysagiste_en_decembre_parle_de_deneigement() -> None:
    """Sa seule fenêtre ouverte en décembre."""
    r = resoudre_metiers(MV_PAYSAGISTE, DECEMBRE)
    assert r.scene == "déneigement"
    assert r.fenetre_ouverte == ("déneigement",)


def test_brille_o_max_en_decembre_ne_parle_PAS_de_lavage_de_vitres() -> None:
    """🔴 Le test qui prouve la clause « dont la fenêtre est ouverte ».

    Sans elle, mesuré : 76 des 113 joignables en décembre se font parler d'un
    métier hors saison. Brille-O-Max est joignable en décembre UNIQUEMENT parce
    que sa fenêtre déneigement est ouverte — et la règle sans clause lui parlait
    de lavage de vitres, parce que la prochaine saison de vitres (1er avril)
    arrive avant la prochaine saison de neige (15 novembre).

    Les deux seuls exemples travaillés de la version précédente (octobre et
    février) étaient exactement les deux mois où la règle tombait juste par
    hasard : ce cas-ci est celui qui la met à l'épreuve.
    """
    r = resoudre_metiers(BRILLE_O_MAX, DECEMBRE)
    assert r.scene == "déneigement", (
        "en décembre, la prochaine saison de vitres (1er avril) arrive AVANT "
        "celle de la neige (15 novembre) : sans la clause de fenêtre ouverte, "
        "on lui parlerait de lavage de vitres en plein hiver"
    )
    assert r.joignable


def test_herbofleurs_en_octobre_nest_pas_joignable() -> None:
    """Deux métiers d'été, deux fenêtres fermées d'août à décembre. Le prédicat
    est la négation de la règle QUI, pas une sous-catégorie : mesuré, 23 à 27
    entreprises MULTI-métiers sont écartées chaque mois, pas seulement des
    mono-métiers comme le disait la version précédente."""
    r = resoudre_metiers(HERBOFLEURS, OCTOBRE)
    assert not r.joignable
    assert r.scene is None
    assert r.fenetre_ouverte == ()


def test_herbofleurs_en_fevrier_est_joignable() -> None:
    r = resoudre_metiers(HERBOFLEURS, FEVRIER)
    assert r.joignable
    assert r.scene == "paysagement"


# ---------------- 3. L'appariement généreux ----------------

@pytest.mark.parametrize(
    "libelle",
    [
        "Déneigement commercial",
        "transport de neige",
        "déneigement de toiture",
        "DENEIGEMENT RESIDENTIEL",
        "deneigement sans accent",
        "Souffleuse à neige",
    ],
)
def test_les_variantes_de_deneigement_tombent_toutes_dans_deneigement(libelle: str) -> None:
    """Rater son déneigement coûte sa fenêtre entière ; l'inclure à tort coûte
    un courriel mal placé. On inclut dans le doute."""
    r = resoudre_metiers([libelle], DECEMBRE)
    assert "déneigement" in r.metiers, libelle


def test_amenagement_ne_declenche_PAS_menage() -> None:
    """🔴 Défaut trouvé par ce test avant la production.

    « aménagement » CONTIENT « ménage ». Avec un appariement par sous-chaîne,
    tout paysagiste devenait aussi un service de ménage — métier sans saison
    documentée, donc ouvert les douze mois par le défaut inversé. Un paysagiste
    pur serait devenu joignable en octobre, ce que toute la règle saisonnière
    existe pour empêcher. « Généreux » veut dire préfixe d'un MOT, pas
    n'importe où dans la chaîne.
    """
    r = resoudre_metiers(["aménagement paysager"], OCTOBRE)
    assert r.metiers == ("paysagement",)
    assert not r.joignable, "un paysagiste pur n'est pas joignable en octobre"


def test_deneigement_de_toiture_ne_declenche_PAS_toiture() -> None:
    """🔴 Deuxième défaut du même genre. La spec nomme elle-même « déneigement
    de toiture » comme un libellé de DÉNEIGEMENT. Sans écrasement, il faisait
    aussi naître « toiture », sans saison documentée, donc douze mois ouverts —
    et une entreprise de déneigement pur recevait un courriel en juillet."""
    r = resoudre_metiers(["déneigement de toiture"], DECEMBRE)
    assert r.metiers == ("déneigement",)


def test_un_deneigeur_pur_nest_pas_joignable_en_juillet() -> None:
    """Le test de bout en bout des deux défauts ci-dessus : quelle que soit la
    façon dont ils reviendraient, celui-ci casse."""
    juillet = date(2027, 7, 10)
    for services in (["déneigement"], ["déneigement de toiture"], ["déneigement", "déglaçage"]):
        assert not resoudre_metiers(services, juillet).joignable, services


def test_piscine_creusee_nest_PAS_de_lexcavation() -> None:
    """🔴 Troisième collision de la même famille, trouvée par le conseil du
    2026-08-30. La racine « creus » appariait « piscine CREUSÉE », le terme
    standard au Québec pour une piscine enterrée. Mesuré en base : 20 des 22
    libellés contenant « creus » sont des piscines, 2 seulement de la vraie
    excavation.

    Le dégât était double : un métier fantôme (donc `deuxieme_temps_obligatoire`
    sur une entreprise mono-métier), et excavation n'ayant pas de saison
    documentée, elle ouvrait les douze mois à une entreprise saisonnière.
    """
    for libelle in (
        "Installation de piscines creusées",
        "Vente de piscines creusées",
        "installation de piscine creusee",
    ):
        r = resoudre_metiers([libelle], date(2026, 6, 15))
        assert r.metiers == ("piscine",), f"{libelle!r} → {r.metiers}"
        assert not r.deuxieme_temps_obligatoire


def test_la_vraie_excavation_est_toujours_reconnue() -> None:
    """Le contrôle négatif : resserrer la racine ne doit pas faire disparaître
    les 2 vrais libellés d'excavation de la base."""
    for libelle in ("Creusage de tranchées", "Creusage de puits sec", "Excavation résidentielle"):
        assert "excavation" in resoudre_metiers([libelle], date(2026, 6, 15)).metiers, libelle


@pytest.mark.parametrize(
    "libelle,metier_attendu,combien_en_base",
    [
        ("Aménagement des plates-bandes", "paysagement", 23),
        ("Sarclage des plates-bandes", "paysagement", 23),
        ("Lavage à pression de patio", "lavage de vitres", 34),
        ("Construction de murs de soutènement", "paysagement", 19),
    ],
)
def test_les_trous_du_dictionnaire_sont_bouches(
    libelle: str, metier_attendu: str, combien_en_base: int
) -> None:
    """Les trous que la spec §3 avait nommés et mesurés, et que la première
    version du dictionnaire n'avait pas bouchés.

    ⚠️ « plate-bande » au SINGULIER apparaît dans **0 fiche** de production,
    « plates-bandes » dans 23 : la racine d'origine était du code mort.
    Le classement de « lavage à pression » (→ vitres) et « murs de soutènement »
    (→ paysagement) est une décision de William du 2026-08-30.
    """
    r = resoudre_metiers([libelle], date(2027, 2, 10))
    assert metier_attendu in r.metiers, f"{libelle!r} → {r.metiers}"


def test_herbofleurs_avec_ses_VRAIS_libelles() -> None:
    """Le cas qui prouve que les trous coûtaient cher. Herbofleurs a
    l'horticulture pour métier, et ses quatre premiers services sont des
    plates-bandes. Avant le correctif elle se résolvait en `('tonte',)` —
    mono-métier — donc elle recevait en février un ouvreur 100 % tonte et
    AUCUN 2ᵉ temps, sans que rien ne le signale."""
    services = [
        "Aménagement des plates-bandes",
        "Sarclage des plates-bandes",
        "Plantation d'arbustes et végétaux",
        "Tonte de pelouse",
    ]
    r = resoudre_metiers(services, FEVRIER)
    assert r.dominant == "paysagement", r.metiers
    assert r.deuxieme_temps_obligatoire
    assert "tonte" in r.autres


def test_les_accents_manquants_ne_font_pas_rater_une_fenetre() -> None:
    avec = resoudre_metiers(["aménagement paysager"], FEVRIER)
    sans = resoudre_metiers(["amenagement paysager"], FEVRIER)
    assert avec.metiers == sans.metiers == ("paysagement",)


# ---------------- 4. Le défaut inversé ----------------

def test_aucun_metier_reconnu_reste_joignable_toute_lannee() -> None:
    """Un lead non classable est protégé par le défaut inversé et reçoit un
    ouvreur générique. Il ne disparaît jamais."""
    for jour in (OCTOBRE, DECEMBRE, FEVRIER, AOUT):
        r = resoudre_metiers(["réparation de clôtures", "bricolage divers"], jour)
        assert r.joignable, jour
        assert r.scene is None
        assert r.source == "inconnu"
        assert not r.deuxieme_temps_obligatoire


@pytest.mark.parametrize("vide", [None, [], ["", "   "]])
def test_services_offered_vide_ne_plante_pas(vide: list[str] | None) -> None:
    r = resoudre_metiers(vide, OCTOBRE)
    assert r.joignable and r.metiers == () and r.source == "inconnu"


# ---------------- 5. Le 2e temps, et sa formulation ----------------

def test_mono_metier_na_pas_de_deuxieme_temps() -> None:
    """Mesuré sur A.M.G. Neige : 203 mots, dans la borne, sans 2ᵉ temps."""
    r = resoudre_metiers(AMG_NEIGE, DECEMBRE)
    assert not r.deuxieme_temps_obligatoire
    assert r.autres == ()


def test_hiver_plus_ete_donne_pour_le_reste_de_lannee() -> None:
    """76 boîtes. Le contraste temporel est vrai : les saisons sont opposées."""
    r = resoudre_metiers(MV_PAYSAGISTE, OCTOBRE)
    assert r.deuxieme_temps_obligatoire
    assert not r.meme_saison


def test_ete_seulement_donne_tu_fais_aussi() -> None:
    """34 boîtes. « Pour le reste de l'année » serait FAUX ici : la tonte et le
    paysagement, c'est le même été."""
    r = resoudre_metiers(HERBOFLEURS, FEVRIER)
    assert r.deuxieme_temps_obligatoire
    assert r.meme_saison


def test_un_metier_sans_saison_documentee_ne_ment_jamais_sur_la_saison() -> None:
    """🔴 Le défaut de `meme_saison` est True, et ce n'est pas de la paresse.

    « Tu fais aussi X » ne fait AUCUNE affirmation temporelle et ne peut pas
    être faux. « Pour le reste de l'année » affirme un contraste et devient un
    mensonge si on se trompe. Sur une affirmation que le lecteur peut vérifier
    d'un coup d'œil, on choisit la forme qui ne peut pas mentir.
    """
    r = resoudre_metiers(["déneigement", "toiture"], DECEMBRE)
    assert r.meme_saison, (
        "toiture n'a pas de saison documentée : on ne doit PAS affirmer qu'elle "
        "occupe le reste de l'année"
    )


def test_la_scene_minoritaire_declenche_le_nommage_du_dominant() -> None:
    """Règle mesurable de la §3 : si la scène pèse ≤ 25 % des libellés, le
    2ᵉ temps est obligatoire ET nomme le dominant. C'est le cas que l'ENAP
    décrit — 14 déneigeurs sur 21 tirent 25 % ou moins de leur revenu du
    déneigement, donc leur parler 100 % neige, c'est leur parler de leur métier
    d'appoint."""
    services = [
        "déneigement",
        "aménagement paysager",
        "plate-bandes",
        "haies",
        "pavé uni",
    ]
    r = resoudre_metiers(services, DECEMBRE)
    assert r.scene == "déneigement"
    assert r.scene_est_minoritaire
    assert r.dominant == "paysagement"
    assert r.autres[0] == "paysagement", "le dominant vient en tête de l'énumération"


def test_la_scene_majoritaire_ne_declenche_pas_le_nommage() -> None:
    r = resoudre_metiers(["déneigement", "déneigement de toiture", "haies"], DECEMBRE)
    assert r.scene == "déneigement"
    assert not r.scene_est_minoritaire


# ---------------- 6. Le dominant gouverne le lexique, pas la scène ----------------

def test_le_dominant_est_independant_de_la_scene() -> None:
    """🔴 Le cas qui a fait trancher la spec : un laveur de vitres démarché en
    août à propos de la neige. Le lexique suit le DOMINANT (vitres → « le nombre
    d'étages »), la scène suit la saison (déneigement). Confondre les deux lui
    demanderait la grandeur de son entrée de garage alors qu'il lave des vitres
    commerciales onze mois par année."""
    services = [
        "lavage de vitres commercial",
        "lavage de vitres résidentiel",
        "nettoyage de fenêtres",
        "déneigement",
    ]
    r = resoudre_metiers(services, AOUT)
    assert r.scene == "déneigement", "en août, seule la fenêtre du déneigement est ouverte"
    assert r.dominant == "lavage de vitres", "3 libellés contre 1"


def test_lordre_des_metiers_suit_le_nombre_de_libelles() -> None:
    services = ["tonte", "pelouse", "gazon", "déneigement", "haies"]
    r = resoudre_metiers(services, FEVRIER)
    assert r.metiers[0] == "tonte"
    assert r.dominant == "tonte"


def test_la_resolution_est_rejouable() -> None:
    """Déterminisme DANS UN processus.

    ⚠️ Ce test ne suffit PAS, et c'est important de le dire ici : le hash de
    Python est tiré une fois au démarrage du processus, donc une dépendance à
    l'ordre d'itération d'un set y est parfaitement stable. Voir le test
    suivant, qui est le seul à pouvoir la voir."""
    services = ["déneigement", "tonte", "haies", "excavation"]
    premier = resoudre_metiers(services, OCTOBRE)
    for _ in range(5):
        assert resoudre_metiers(services, OCTOBRE) == premier


@pytest.mark.parametrize(
    "services",
    [
        ["Installation de piscines creusées"],
        ["déneigement et aménagement paysager"],
        ["lavage de vitres", "déneigement"],
        ["tonte", "haies", "pavage"],
    ],
)
def test_le_dominant_est_stable_entre_processus(services: list[str]) -> None:
    """🔴 Le défaut que le conseil du 2026-08-30 a trouvé, et que rien d'autre
    ne pouvait voir.

    `apparies` est un SET de chaînes. Son ordre d'itération dépend de la
    randomisation du hash, qui change à chaque PROCESSUS. Il décidait l'ordre
    d'insertion dans `compte`, donc la rupture d'égalité du tri stable, donc
    `dominant` — qui gouverne le LEXIQUE.

    Mesuré avant correctif : Piscines Élégance rendait `dominant='piscine'`
    sous PYTHONHASHSEED=1 et `dominant='excavation'` sous PYTHONHASHSEED=0.
    L'installateur de piscines se faisait demander « l'accès au terrain, ce
    qu'il y a à creuser » un envoi sur deux, sans qu'aucun test ne bronche.

    Il FAUT des processus séparés : un test en un seul processus est
    structurellement aveugle à cette classe de défaut.
    """
    import json
    import subprocess
    import sys

    programme = (
        "import sys, json; sys.path.insert(0, '.');"
        "from src.lib.metiers import resoudre_metiers;"
        "from datetime import date;"
        "r = resoudre_metiers(json.loads(sys.argv[1]), date(2026, 6, 15));"
        "print(json.dumps([r.dominant, list(r.metiers), r.scene]))"
    )
    vus = set()
    for seed in ("0", "1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        res = subprocess.run(
            [sys.executable, "-c", programme, json.dumps(services)],
            capture_output=True, text=True, env=env, cwd=str(RACINE_PROJET),
        )
        assert res.returncode == 0, res.stderr
        vus.add(res.stdout.strip())

    assert len(vus) == 1, (
        f"{len(vus)} résultats différents selon PYTHONHASHSEED pour {services} : {vus}"
    )
