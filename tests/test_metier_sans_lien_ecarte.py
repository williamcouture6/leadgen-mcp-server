"""Une entreprise dont la SEULE reconnaissance est un métier sans saison n'est pas contactée.

🔴 LA RÈGLE, ET POURQUOI ELLE N'EST PAS CELLE QU'ON CROIT.

Décision William du 2026-09-02 : « les compagnies comme Niwa Paysagiste qui
n'ont de reconnu qu'un métier sans réel lien, et qui est 12 mois sur 12, on doit
faire en sorte qu'elles ne soient pas contactées. »

Le cas réel : « Niwa Paysagiste », sourcée sur le mot-clé `paysagiste`, dont la
fiche `services_offered` ne contient aucun libellé où la racine `paysag`
apparaît. La seule chose reconnue est « pavage » — un métier sans saison, donc
avec une fenêtre ouverte douze mois sur douze. Elle passait le filtre toute
l'année, sur un métier qui n'est pas le sien.

⚠️ CE N'EST PAS LE MÊME CAS QUE « AUCUN MÉTIER RECONNU », et c'est la
distinction que ce fichier existe pour figer :

    aucun métier reconnu  → JOIGNABLE toute l'année (défaut inversé, garde-fou
                            nº2 de la spec). On n'a rien, et on le sait.
    un métier sans saison → ÉCARTÉE. On a quelque chose, et c'est faux.

Une reconnaissance fausse est pire qu'une absence de reconnaissance, parce
qu'elle a l'air d'une information. Traiter les deux pareil — dans un sens ou
dans l'autre — est l'erreur que ces tests empêchent.

💀 LES DEUX EXEMPLES D'ORIGINE NE SONT PLUS DES EXEMPLES (2026-09-14).

« Niwa Paysagiste » et « Aménagement Côté Jardin » portent toutes deux
`industry = paysagiste`. Ce secteur était IGNORÉ jusqu'au 2026-09-14 ; William a
renversé cette décision — « comme il a paysagiste dans son secteur, il doit être
traité comme telle ». Les deux sont donc désormais reconnues `paysagement` et
JOIGNABLES de janvier à juin.

🔴 LA RÈGLE CI-DESSUS N'EST PAS TOUCHÉE POUR AUTANT, et c'est tout l'objet de ce
fichier. Elle s'applique maintenant aux fiches dont **ni les services ni le
secteur** ne disent un métier saisonnier. Les fixtures ont été remplacées en
conséquence ; `test_le_secteur_sauve_les_fiches_qu_il_peut` tient l'autre bord.

⚠️ NE PAS « RÉPARER » CE FICHIER en lui rendant ses anciennes fixtures : elles
passeraient désormais pour la mauvaise raison.
"""
from __future__ import annotations

import datetime

import pytest

from src.lib.metiers import SAISONS, resoudre_metiers
from src.tools.db import fenetre_saisonniere_ouverte

JANVIER = datetime.date(2027, 1, 20)   # toutes les saisons ouvertes ou presque
SEPTEMBRE = datetime.date(2026, 9, 2)  # seul le déneigement est ouvert

# 💀 Les deux fiches réelles qui ont déclenché la décision du 2026-09-02 —
# « Niwa Paysagiste » et « Aménagement Côté Jardin », toutes deux
# `industry = paysagiste` — ne servent plus ici : leur secteur les sauve depuis
# le 2026-09-14. Elles sont tenues par
# `test_le_secteur_sauve_les_fiches_qu_il_peut`, plus bas.
#
# Ce qu'il fallait à leur place : des fiches dont NI les services NI le secteur
# ne nomment un métier saisonnier. C'est le cas qui reste, et c'est celui que la
# règle vise.
PAVEUR_SANS_SECTEUR = {
    "name": "Pavage Untel",
    # Pas d'`industry` du tout : le cas le plus fréquent hors sourcing Places.
    "research_json": {"services_offered": ["Pose de pavé uni"]},
}
EXCAVATEUR_SECTEUR_MUET = {
    "name": "Excavation Machin",
    # Un secteur QUI N'APPARIE AUCUNE RACINE : il ne peut rien sauver.
    "industry": "entrepreneur général",
    "research_json": {"services_offered": ["Excavation résidentielle", "Pavage de stationnement"]},
}


@pytest.mark.parametrize(
    "fiche", [PAVEUR_SANS_SECTEUR, EXCAVATEUR_SECTEUR_MUET],
    ids=["paveur_sans_secteur", "excavateur_secteur_muet"],
)
@pytest.mark.parametrize("quand", [SEPTEMBRE, JANVIER], ids=["septembre", "janvier"])
def test_un_metier_sans_saison_seul_n_ouvre_jamais(fiche: dict, quand: datetime.date) -> None:
    """Écartée TOUTE L'ANNÉE, pas seulement hors saison.

    C'est le point : un métier douze mois sur douze est ouvert en janvier aussi.
    Ne tester qu'en septembre laisserait passer la règle inverse.
    """
    assert not fenetre_saisonniere_ouverte(fiche, track="agence-ia", aujourdhui=quand), (
        f"{fiche['name']} est joignable alors que sa seule reconnaissance est "
        f"un métier sans saison"
    )


@pytest.mark.parametrize("quand", [SEPTEMBRE, JANVIER], ids=["septembre", "janvier"])
def test_aucun_metier_reconnu_n_est_plus_joignable(quand: datetime.date) -> None:
    """🔴 RENVERSEMENT — décision William du 2026-09-14.

    CE TEST AFFIRMAIT L'INVERSE JUSQU'À CETTE DATE, et ce n'était pas un
    oubli : la spec du 2026-08-27 §3 posait un « défaut inversé » volontaire —
    faute de savoir quand est le bon mois, on n'en interdisait aucun. Le
    raisonnement tenait : se taire faute de savoir aurait fait disparaître en
    silence toute entreprise que le dictionnaire ne sait pas classer.

    CE QUI A CHANGÉ. Mesuré le 2026-09-14 sur les 457 fiches joignables : les 5
    entreprises sans métier reconnu portaient `fenetre_mois = [1..12]`, donc
    elles étaient les SEULES joignables douze mois sur douze, pendant que toutes
    les autres attendaient leur saison. En septembre, où seul le déneigement
    est ouvert, elles passaient quand même.

    Ce que ça a produit : « Conception Perma-Nourricière » a reçu un courriel
    froid. Onze services listés — design en permaculture, agroforesterie,
    parcs comestibles municipaux, jardins pédagogiques, aide aux subventions —
    et aucun qui soit un métier du catalogue. Ce n'est pas un contracteur de
    services résidentiels.

    LA RÈGLE EST DONC LA MÊME QUE CELLE DE NIWA, étendue au cas qu'elle avait
    laissé ouvert. William, 2026-09-02 : « les compagnies qui n'ont de reconnu
    qu'un métier sans réel lien, et qui est 12 mois sur 12, on doit faire en
    sorte qu'elles ne soient pas contactées. » Le cas « un métier faux » était
    couvert ; le cas « aucun métier » ne l'était pas. Il l'est maintenant :

        aucun métier reconnu  → ÉCARTÉE. On n'a rien, et écrire sans savoir à
                                qui on écrit, c'est deviner.
        un métier sans saison → ÉCARTÉE. On a quelque chose, et c'est faux.

    ⚠️ LE DÉFAUT INVERSÉ DE `lib/metiers` N'EST PAS TOUCHÉ. `fenetre_mois`
    continue de rendre les douze mois sur l'inconnu — c'est une réponse à
    « quel est le bon moment ? », pas à « a-t-on le droit d'écrire ? ». Seule
    la seconde question change de réponse ici. Les inverser toutes les deux
    casserait le sens de la colonne.

    ⚠️ ET LE SILENCE NE DOIT PAS ÊTRE INVISIBLE — c'était l'objection de la
    spec, et elle reste valable. Ces entreprises doivent apparaître dans
    `agence.v_pourquoi_pas_de_courriel` avec leur raison.
    """
    inconnue = {
        "name": "Services Généraux Machin",
        # 💀 Portait `industry = paysagiste` jusqu'au 2026-09-14. Depuis que le
        # secteur complète le classement, ce secteur-là la sauverait — et le
        # test mesurerait le contraire de ce qu'il annonce. Un secteur qui
        # n'apparie aucune racine est ce qu'il faut ici.
        "industry": "entrepreneur général",
        "research_json": {"services_offered": ["Consultation", "Forfaits sur mesure"]},
    }
    assert not resoudre_metiers(
        inconnue["research_json"]["services_offered"], quand
    ).metiers, "la fiche d'exemple ne doit apparier AUCUN métier"
    assert not fenetre_saisonniere_ouverte(inconnue, track="agence-ia", aujourdhui=quand), (
        "une entreprise dont aucun metier n'est reconnu ne doit plus etre "
        "demarchee (decision William 2026-09-14)"
    )


def test_la_fiche_reelle_qui_a_declenche_le_renversement() -> None:
    """Le cas concret, garde-fou contre un retour en arrière par distraction.

    « Conception Perma-Nourricière » a ONZE services et 24 avis à 5,0 — donc
    tout ce qu'il faut pour écrire un beau courriel. Ce n'est pas la pauvreté
    de la fiche qui l'écarte, c'est qu'aucun de ses libellés n'est un métier du
    catalogue. Une fiche riche et hors cible est exactement le cas qu'un test
    sur une fiche vide laisserait passer.
    """
    perma = {
        "name": "Conception Perma-Nourricière",
        # 💀 En base elle porte `industry = paysagiste`, et depuis le 2026-09-14
        # ce secteur la rendrait joignable de janvier à juin. C'est une
        # CONSÉQUENCE ASSUMÉE du renversement de William, pas un oubli : une
        # firme de design en permaculture sourcée sur le mot-clé « paysagiste »
        # entre dans la file comme paysagiste.
        # ⚠️ Ce test-ci garde le secteur muet pour continuer d'exercer la règle
        # qu'il vise — « une fiche riche mais sans métier du catalogue est
        # écartée ». Le cas réel, lui, est tenu par
        # `test_le_secteur_sauve_les_fiches_qu_il_peut`.
        "industry": "entrepreneur général",
        "google_rating": 5.0,
        "google_reviews_count": 24,
        "research_json": {"services_offered": [
            "Design en permaculture (plans 2D et 3D photoréalistes)",
            "Forêts nourricières résidentielles",
            "Agroforesterie et sylvopastoralisme (terres agricoles)",
            "Aménagements municipaux (parcs comestibles, forêts communautaires)",
            "Aménagements éducatifs (jardins pédagogiques, cours d'école)",
            "Analyse de sol",
            "Aide aux demandes de subventions",
        ]},
    }
    assert not fenetre_saisonniere_ouverte(perma, track="agence-ia", aujourdhui=SEPTEMBRE)
    assert not fenetre_saisonniere_ouverte(perma, track="agence-ia", aujourdhui=JANVIER)


def test_le_secteur_sauve_les_fiches_qu_il_peut() -> None:
    """🔧 L'AUTRE BORD DE LA RÈGLE — décision William du 2026-09-14.

    « Comme il a paysagiste dans son secteur, il doit être traité comme telle. »

    Les deux fiches réelles qui avaient motivé la règle inverse le 2026-09-02
    sont désormais joignables **en saison**, parce que leur mot-clé de sourcing
    dit un métier que leurs libellés taisent. Ce n'est pas un contournement de
    la règle : la règle écarte les fiches qu'on ne sait pas nommer, et le
    secteur les nomme.

    ⚠️ EN SAISON SEULEMENT. Le paysagement ouvre de janvier à juin : ces fiches
    restent écartées en septembre. Si ce test passait aux deux dates, c'est que
    le secteur aurait été traité comme un laissez-passer au lieu d'un métier.
    """
    niwa = {
        "name": "Niwa Paysagiste",
        "industry": "paysagiste",
        "research_json": {"services_offered": ["Pose de pavé uni"]},
    }
    cote_jardin = {
        "name": "Aménagement Côté Jardin Inc.",
        "industry": "paysagiste",
        "research_json": {"services_offered": ["Excavation résidentielle",
                                               "Pavage de stationnement"]},
    }
    for fiche in (niwa, cote_jardin):
        assert fenetre_saisonniere_ouverte(fiche, track="agence-ia", aujourdhui=JANVIER), (
            f"{fiche['name']} : le secteur ne la sauve pas — elle reste "
            f"injoignable pour toujours"
        )
        assert not fenetre_saisonniere_ouverte(
            fiche, track="agence-ia", aujourdhui=SEPTEMBRE
        ), (
            f"{fiche['name']} : joignable en septembre alors que le paysagement "
            f"ferme en juin — le secteur sert de laissez-passer au lieu de métier"
        )


def test_la_piste_opt_n_est_pas_touchee_par_le_renversement() -> None:
    """⚠️ OPT est gelée et ses métiers (dentiste, physio) n'apparient RIEN.

    Appliquer le renversement à OPT y écarterait tout le monde en silence —
    c'est déjà la raison pour laquelle le filtre entier s'arrête à la première
    ligne quand la piste n'est pas `agence-ia`. Ce test tient cette sortie.
    """
    dentiste = {
        "name": "Clinique Dentaire Untel",
        "research_json": {"services_offered": ["Détartrage", "Couronnes"]},
    }
    assert not resoudre_metiers(
        dentiste["research_json"]["services_offered"], SEPTEMBRE
    ).metiers
    assert fenetre_saisonniere_ouverte(dentiste, track="OPT", aujourdhui=SEPTEMBRE)


def test_un_metier_sans_saison_EN_PLUS_d_un_saisonnier_ne_gene_pas() -> None:
    """Le pavage d'un déneigeur ne l'empêche pas d'être contacté.

    La règle vise le cas où le métier sans saison est la SEULE reconnaissance,
    jamais sa présence en secondaire — sinon on écarterait les 79 entreprises
    mixtes, qui sont le cœur de la liste.
    """
    mixte = {
        "name": "Déneigement et Pavage Untel",
        "industry": "entrepreneur en déneigement",
        "research_json": {"services_offered": ["Déneigement résidentiel", "Pavage"]},
    }
    assert fenetre_saisonniere_ouverte(mixte, track="agence-ia", aujourdhui=SEPTEMBRE)


def test_le_secteur_complete_le_classement_sans_dominer() -> None:
    """💀 CE TEST DISAIT L'INVERSE JUSQU'AU 2026-09-14.

    Il s'appelait `test_le_repli_sur_industry_reste_debranche` et gelait la
    décision du 2026-09-02 : « si la seule chose qu'on reconnaît d'un
    paysagiste est pavage, notre donnée sur lui est mauvaise — on ne devine pas
    son métier depuis le mot-clé de sourcing ».

    🔧 William a renversé cette décision le 2026-09-14, sur le cas Niwa :
    « comme il a paysagiste dans son secteur, il doit être traité comme telle ».

    Ce qui a changé entre les deux dates, et qui justifie le renversement :
    `industry` a été MESURÉ. Ce n'est pas du texte deviné — c'est le mot-clé
    Google Places qui a fait entrer l'entreprise dans la liste, il ne prend que
    sept valeurs, toutes des métiers, et sur les 478 fiches qui en portent un,
    **462 (97 %) ont déjà ce métier reconnu par leurs services**. Le secteur
    n'invente donc rien dans 97 % des cas ; il comble un trou dans les 3 %
    restants.

    ⚠️ L'objection d'origine reste vraie et reste tenue par
    `test_le_secteur_ne_vole_jamais_la_scene` : le courriel doit parler de ce
    que l'entreprise fait VRAIMENT. C'est pour ça que le secteur entre avec un
    poids de 1 et un rang de dernier arrivé — il ouvre la fenêtre, il ne prend
    jamais la parole.
    """
    sans = resoudre_metiers(["Pose de pavé uni"], JANVIER)
    avec = resoudre_metiers(["Pose de pavé uni"], JANVIER, industry="paysagiste")

    assert sans.metiers == ("pavage",)
    assert "paysagement" in avec.metiers, (
        "le secteur n'est plus lu — Niwa redevient injoignable pour toujours"
    )
    assert avec.metiers[0] == "pavage", (
        f"le secteur a volé la première place : {avec.metiers}. Le courriel "
        f"parlerait de plates-bandes à un pavageur."
    )


def test_la_regle_porte_sur_SAISONS_pas_sur_une_liste_figee() -> None:
    """Le jour où un métier reçoit une date, il devient capable d'ouvrir.

    C'est ce qui est arrivé à la piscine le 2026-09-02 : `SAISONS["piscine"] =
    (5, 1)`, sa fenêtre devient février → juillet, et un piscinier pur devient
    joignable pendant cette fenêtre — sans qu'une seule autre ligne de code
    change. Si la règle avait été écrite avec une liste de métiers interdits en
    dur, il aurait fallu penser à l'en retirer, et personne ne l'aurait fait.

    🔧 Ce test a ÉCHOUÉ le jour où la date a été posée, et il avait raison : il
    affirmait « dans SAISONS ⇒ joignable en janvier ». C'est faux — janvier est
    HORS de la fenêtre février-juillet. La bonne formulation ne teste pas un
    mois arbitraire, elle teste la fenêtre elle-même.
    """
    piscinier = {
        "name": "Piscines Untel",
        "industry": "entretien de piscine",
        "research_json": {"services_offered": ["Ouverture et fermeture de piscine"]},
    }
    assert "piscine" in SAISONS, "la piscine a perdu sa date"

    avril = datetime.date(2027, 4, 10)      # dans la fenêtre
    septembre = datetime.date(2026, 9, 10)  # hors fenêtre
    assert fenetre_saisonniere_ouverte(piscinier, track="agence-ia", aujourdhui=avril)
    assert not fenetre_saisonniere_ouverte(piscinier, track="agence-ia", aujourdhui=septembre)


def test_la_fenetre_de_la_piscine_encadre_l_ouverture() -> None:
    """Janvier à juillet — 4 mois avant le 1er mai, 2 après.

    La date d'ouverture vient d'un fait technique, pas de la météo : l'eau qui
    atteint 12 °C laisse partir les algues, donc on ouvre AVANT. C'est ce qui la
    rend documentable, comme le 15 novembre du déneigement.

    🔧 La fenêtre est passée de février→juillet à janvier→juillet le 2026-09-04
    (décision William, 4 mois avant au lieu de 3). Le mois qui compte est le
    dernier : **la piscine et la tonte sont les deux seules à couvrir juillet**.
    Les ramener à 1 mois après avait rendu juillet mort — 3 leads joignables sur
    260 — et `test_les_douze_mois_ont_quelqu_un` l'avait attrapé.
    """
    from src.lib.metiers import fenetre_mois

    assert SAISONS["piscine"] == (5, 1)
    assert fenetre_mois("piscine") == frozenset({1, 2, 3, 4, 5, 6, 7})
