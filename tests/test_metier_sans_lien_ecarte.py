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

⚠️ Un repli sur `industry` existe (`metier_depuis_industry`) et rendrait Niwa
joignable en lui redonnant `paysagement` depuis son mot-clé de sourcing. Il est
DÉBRANCHÉ volontairement. La spec du 2026-08-27 le prévoit pourtant — d'où le
test qui vérifie qu'il le reste.
"""
from __future__ import annotations

import datetime

import pytest

from src.lib.metiers import SAISONS, resoudre_metiers
from src.tools.db import fenetre_saisonniere_ouverte

JANVIER = datetime.date(2027, 1, 20)   # toutes les saisons ouvertes ou presque
SEPTEMBRE = datetime.date(2026, 9, 2)  # seul le déneigement est ouvert

# Les deux fiches réelles qui ont déclenché la décision.
NIWA = {
    "name": "Niwa Paysagiste",
    "industry": "paysagiste",
    "research_json": {"services_offered": ["Pose de pavé uni"]},
}
COTE_JARDIN = {
    "name": "Aménagement Côté Jardin Inc.",
    "industry": "paysagiste",
    "research_json": {"services_offered": ["Excavation résidentielle", "Pavage de stationnement"]},
}


@pytest.mark.parametrize("fiche", [NIWA, COTE_JARDIN], ids=["niwa", "cote_jardin"])
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
def test_aucun_metier_reconnu_reste_joignable(quand: datetime.date) -> None:
    """🔴 Le contrôle négatif, et il porte toute la distinction.

    Une fiche dont AUCUN métier n'est reconnu tombe sur le défaut inversé et
    reste joignable. Si ce test échoue en même temps que le précédent passe,
    c'est que la règle a été appliquée trop largement — et le silence serait
    invisible : ces entreprises disparaîtraient de la file sans que rien ne
    l'annonce.
    """
    inconnue = {
        "name": "Services Généraux Machin",
        "industry": "paysagiste",
        "research_json": {"services_offered": ["Consultation", "Forfaits sur mesure"]},
    }
    assert not resoudre_metiers(
        inconnue["research_json"]["services_offered"], quand
    ).metiers, "la fiche d'exemple ne doit apparier AUCUN métier"
    assert fenetre_saisonniere_ouverte(inconnue, track="agence-ia", aujourdhui=quand)


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


def test_le_repli_sur_industry_reste_debranche() -> None:
    """🔴 La spec du 2026-08-27 le PRÉVOIT — d'où ce test.

    Elle décrit `metier_source` comme « services_offered · industry (repli) ·
    inconnu ». Le repli a été écrit le 2026-09-02, mesuré, puis débranché le
    jour même sur décision de William. Quelqu'un le rebranchera en croyant
    réparer un oubli de la spec ; ce test le lui dira.

    Ce qu'on vérifie : passer `industry` ne change RIEN au résultat.
    """
    sans = resoudre_metiers(["Pose de pavé uni"], JANVIER)
    avec = resoudre_metiers(["Pose de pavé uni"], JANVIER, industry="paysagiste")
    assert sans.metiers == avec.metiers == ("pavage",), (
        "le repli sur `industry` a été rebranché — Niwa redevient joignable. "
        "Décision William du 2026-09-02 : ces fiches ne sont pas contactées, "
        "la correction est en amont dans WF-3."
    )
    assert "paysagement" not in avec.metiers


def test_la_regle_porte_sur_SAISONS_pas_sur_une_liste_figee() -> None:
    """Le jour où un métier reçoit une date, il devient capable d'ouvrir.

    C'est ce qui doit arriver à la piscine. Si la règle était écrite avec une
    liste de métiers interdits en dur, ajouter une saison ne suffirait pas — il
    faudrait penser à retirer le métier de la liste, et personne ne le ferait.
    """
    piscinier = {
        "name": "Piscines Untel",
        "industry": "entretien de piscine",
        "research_json": {"services_offered": ["Ouverture et fermeture de piscine"]},
    }
    joignable = fenetre_saisonniere_ouverte(piscinier, track="agence-ia", aujourdhui=JANVIER)
    assert joignable is ("piscine" in SAISONS), (
        "la joignabilité d'un piscinier doit suivre la présence de `piscine` "
        "dans SAISONS, sans autre changement de code"
    )
