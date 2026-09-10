"""L'énumération ne redit jamais le même métier deux fois.

Décision William du 2026-09-10, en relisant un brouillon réel de Déneigement
J.M : « je vois que tu fais du **déneigement** d'entrées résidentielles, du
**déneigement** sur appel pis du **déneigement** occasionnel ». Le métier trois
fois.

Le code ne peut pas écrire la phrase — c'est le rédacteur qui la compose. Ce
qu'il peut, c'est NOMMER LA SITUATION : lui dire quels libellés partagent un
métier, ce qu'il vient justement de calculer.

🔴 LA LEÇON DE CE FICHIER EST DANS SON PREMIER JET. La garde exigeait d'abord
que TOUS les libellés se ramènent au même métier, et ces tests-là passaient.
Mesurée ensuite sur les 69 vrais brouillons, elle attrapait **1 cas sur 12** :
la répétition vient presque toujours de deux variantes PLUS un service voisin.
Des tests verts sur une règle que personne n'avait comptée.
"""

import datetime

import pytest

from src.tools.personalize import bloc_metiers_resolus, variantes_du_meme_metier

JOUR = datetime.date(2026, 9, 10)
VARIANTES = [
    "Déneigement d'entrées résidentielles",
    "Déneigement sur appel",
    "Déneigement occasionnel",
]
# Déneigement RTM, tel qu'en base : trois variantes PLUS un service voisin.
MIXTE = [
    "Déneigement résidentiel",
    "Déneigement commercial",
    "Déneigement manuel",
    "Épandage d'abrasifs",
]


def _consigne(services, gabarit="C"):
    lignes = bloc_metiers_resolus(services, JOUR, gabarit=gabarit).splitlines()
    return [x.strip() for x in lignes if "ne redis pas" in x or "sortes de «" in x]


def test_la_liste_pure_est_signalee():
    assert _consigne(VARIANTES), "le cas de William lui-même"
    assert variantes_du_meme_metier(VARIANTES, JOUR) == ("déneigement", VARIANTES)


def test_le_cas_mixte_est_signale_lui_aussi():
    """LE test qui a fait réécrire la garde : 11 des 12 cas réels sont ici."""
    metier, libelles = variantes_du_meme_metier(MIXTE, JOUR)
    assert metier == "déneigement"
    assert libelles == MIXTE[:3], "l'épandage n'est pas une sorte de déneigement"


def test_le_reste_est_nomme_pour_que_rien_ne_disparaisse():
    """Sans cette ligne, le rédacteur pourrait laisser tomber le service voisin
    en regroupant — on lui aurait fait perdre un service réel."""
    bloc = bloc_metiers_resolus(MIXTE, JOUR, gabarit="C")
    assert "Épandage d'abrasifs" in bloc
    assert "se rattache" in bloc


def test_deux_metiers_qui_ne_se_repetent_pas_ne_declenchent_rien():
    """LE contrôle négatif : chaque libellé est un métier différent.

    Il n'y a rien à raccourcir, et « plusieurs sortes de déneigement,
    paysagement et tonte » serait faux.
    """
    services = ["Déneigement résidentiel", "Aménagement paysager", "Tonte de pelouse"]
    assert variantes_du_meme_metier(services, JOUR) is None
    assert _consigne(services) == []


@pytest.mark.parametrize("services", [
    None,
    [],
    ["Déneigement résidentiel"],
    ["Déneigement résidentiel", "Vente de sel en vrac"],
])
def test_rien_a_regrouper(services):
    """Un seul libellé reconnu par métier : la forme « autant X que Y » tient."""
    assert variantes_du_meme_metier(services, JOUR) is None
    assert _consigne(services) == []


def test_un_libelle_a_deux_metiers_ne_compte_pour_aucun_groupe():
    """« Déneigement et aménagement paysager » ne peut se ranger sous un seul
    nom sans amputer l'autre."""
    assert variantes_du_meme_metier(
        ["Déneigement et aménagement paysager", "Déneigement résidentiel"], JOUR
    ) is None


def test_le_plus_gros_groupe_gagne():
    """Deux métiers se répètent : c'est celui qui pèse le plus dans la phrase
    qu'on regroupe."""
    metier, libelles = variantes_du_meme_metier([
        "Tonte de pelouse résidentielle",
        "Tonte de pelouse commerciale",
        "Déneigement résidentiel",
        "Déneigement commercial",
        "Déneigement de stationnements",
    ], JOUR)
    assert metier == "déneigement"
    assert len(libelles) == 3


def test_la_consigne_ne_depend_pas_du_gabarit_tire():
    """Elle vaut aussi pour A et B, dont l'ouvreur GÉNÉRÉ décrit les services.

    Rien ici ne recopie une phrase fixe : c'est une observation sur les
    libellés, vraie quel que soit le bras. (Contrairement à la consigne de
    SAISON, tue hors C/D — voir la docstring de `bloc_metiers_resolus`.)
    """
    for gabarit in ("A", "B", "C", "D", None):
        assert _consigne(VARIANTES, gabarit=gabarit), f"gabarit {gabarit}"


def test_le_gabarit_porte_les_deux_formes():
    """Si quelqu'un retire la règle du prompt, la consigne du code parle dans
    le vide : elle renvoie à une forme que le rédacteur ne connaît plus."""
    from pathlib import Path

    # ⚠️ Aplati : le gabarit est mis en colonnes, donc l'exemple se coupe entre
    # deux mots. Chercher la phrase telle quelle échoue sur la mise en forme,
    # pas sur le fond — le même piège que les motifs de conformité, qui
    # s'écrivent tous en `\s+` pour cette raison.
    txt = " ".join(
        Path("src/prompts/reacti/personalize.md").read_text(encoding="utf-8").split()
    )
    assert "plusieurs sortes de déneigement, d'entrées, sur" in txt, (
        "l'exemple concret de William — c'est lui qui enseigne la forme"
    )
    assert "que de la mini-excavation" in txt, "le cas mixte, 11 fois sur 12"
    assert "autant X que Y pis Z" in txt, "la forme multi-métiers reste"
