"""Le chiffre écrit doit être celui qui a été décidé.

CE QUE CETTE GARDE NE FAIT PAS, ET C'EST IMPORTANT DE LE LIRE EN PREMIER.

Elle ne dit rien sur la VÉRITÉ des chiffres de la relance 2. Le « 78 % des
clients signent avec la première compagnie qui répond » n'a aucune source
primaire ; le « 21 fois » existe mais mesure la qualification d'un lead, pas la
rétention. Les deux ont été présentés à William avec ces éléments le
2026-09-01, et il a tranché « on garde ». Le détail des sources est dans
`compliance_checks.STATISTIQUES_APPROUVEES` et dans le plan AC1b.

Ce que cette garde protège est la seule chose qui reste défendable : que le
chiffre qui part soit celui qui a été DÉCIDÉ, et pas une dérive du modèle. Un
21 devenu 210 n'est plus la décision de personne — c'est une hallucination que
le prospect peut vérifier, donc la catégorie que William a gardée fatale.

D'où `block`, et d'où le fait que ce fichier teste surtout des MUTATIONS : une
garde qui laisse passer les bonnes valeurs sans jamais refuser les mauvaises ne
garde rien.
"""
from __future__ import annotations

import pytest

from src.lib import compliance_checks as cc

# 🔴 IMPORTÉE, PAS RECOPIÉE — corrigé le 2026-09-01 sur trouvaille du conseil.
#
# Ce fichier portait 18 lignes de relance 2 recopiées à la main. Le test qui se
# décrit comme « le plus important du fichier » — celui qui vérifie qu'aucune
# ancre n'est morte — interrogeait donc une COPIE. Le jour où la vraie relance 2
# est reformulée, l'ancre cesse de mordre, « absent = conforme » rend le check
# vert, et le test écrit contre ce scénario reste vert lui aussi : il lisait
# l'ancien texte.
#
# C'est le défaut consolidé ailleurs le même jour (trois copies devenues une).
# Il en restait une, précisément dans le fichier écrit pour s'en protéger.
from src.lib.relances import CORPS_RELANCES  # noqa: E402

RELANCE_2 = CORPS_RELANCES["relance_2"]


def test_les_valeurs_decidees_passent() -> None:
    assert cc.check_statistiques_conformes(RELANCE_2).passed


def test_aucune_ancre_ne_doit_etre_morte() -> None:
    """🔴 LE TEST LE PLUS IMPORTANT DU FICHIER.

    « Absent = conforme » est voulu — tous les corps ne portent pas de
    statistique — mais ça rend une ancre MORTE indistinguable d'un texte sain :
    dans les deux cas le check est vert.

    Le cas s'est produit le 2026-09-01, le jour même. William a réécrit
    « comparé à un lead qui ATTEND 30 minutes » en « comparé à celles qui
    RÉPONDENT en 30 minutes ». L'ancre `attend (\\d+) minutes` cessait de
    matcher : la garde du 30 serait devenue silencieuse, et rien — ni les
    tests, ni la suite complète — ne l'aurait signalé.

    Ce test est le seul filet. Il échoue à la prochaine réécriture de la copie
    qui déplacerait une ancre, et c'est exactement ce qu'on veut : c'est un
    rappel de mettre à jour `STATISTIQUES_APPROUVEES`, pas une nuisance.
    """
    import re

    for nom, (motif, attendu, quoi) in cc.STATISTIQUES_APPROUVEES.items():
        trouves = re.findall(motif, RELANCE_2, flags=re.IGNORECASE)
        assert trouves, (
            f"ancre MORTE : le motif de « {quoi} » ({nom}) ne trouve plus rien dans "
            f"la relance 2. La copie a été réécrite sans que le motif suive — la "
            f"garde de ce chiffre ne protège plus rien, en silence."
        )
        assert attendu in trouves, (
            f"l'ancre de « {quoi} » ({nom}) trouve {trouves} mais pas la valeur "
            f"décidée {attendu!r}"
        )


def test_un_corps_sans_statistique_passe() -> None:
    """Absent = conforme. Le courriel principal et la relance 1 n'en portent pas."""
    assert cc.check_statistiques_conformes(
        "Bonjour,\n\nJuste un suivi rapide. Dis-moi si ça t'intéresse."
    ).passed


@pytest.mark.parametrize(
    ("avant", "apres", "quoi"),
    [
        ("21 fois", "210 fois", "le multiplicateur"),
        ("21 fois", "12 fois", "le multiplicateur, chiffres inversés"),
        ("78 %", "87 %", "la part du premier répondant, chiffres inversés"),
        ("moins de 5 minutes", "moins de 2 minutes", "le délai court"),
        ("répondent en 30 minutes", "répondent en 60 minutes", "le délai long"),
    ],
)
def test_une_derive_est_bloquee(avant: str, apres: str, quoi: str) -> None:
    """Chaque chiffre est gardé SÉPARÉMENT.

    Un seul motif qui couvrirait « la phrase » laisserait dériver les trois
    autres nombres sans rien dire.
    """
    corps = RELANCE_2.replace(avant, apres)
    assert corps != RELANCE_2, f"la mutation n'a rien changé — ancre morte pour {quoi}"
    resultat = cc.check_statistiques_conformes(corps)
    assert not resultat.passed, f"dérive non détectée : {quoi}"
    assert resultat.severity == "block", "une dérive doit TUER le brouillon, pas l'annoter"


def test_le_message_dit_la_valeur_attendue_et_la_valeur_trouvee() -> None:
    """Sans les deux valeurs, la note de conformité oblige à rouvrir le code."""
    resultat = cc.check_statistiques_conformes(RELANCE_2.replace("21 fois", "210 fois"))
    joint = " ".join(resultat.matches)
    assert "21" in joint and "210" in joint


def test_l_espace_avant_le_pourcent_est_indifferent() -> None:
    """« 78 % » et « 78% » sont la même valeur.

    L'espace insécable français est une variation de TYPOGRAPHIE. Bloquer
    là-dessus tuerait des brouillons pour un caractère invisible — exactement le
    défaut de l'apostrophe courbe raconté dans `_find_matches`.
    """
    assert cc.check_statistiques_conformes(RELANCE_2.replace("78 %", "78%")).passed
    assert cc.check_statistiques_conformes(RELANCE_2.replace("78 %", "78 %")).passed


def test_les_60_secondes_du_gabarit_D_ne_sont_pas_confondues() -> None:
    """« en moins de 60 secondes » décrit le SERVICE, pas la statistique.

    Le motif du délai court exige « minutes ». S'il attrapait les secondes, le
    4e paragraphe de D bloquerait sur chaque envoi.
    """
    assert cc.check_statistiques_conformes(
        "Je monte des systèmes qui répondent en moins de 60 secondes à tout ce qui rentre."
    ).passed


def test_la_garde_regarde_aussi_sous_la_signature() -> None:
    """Contrairement aux autres checks, le corps N'EST PAS coupé à la signature.

    Une statistique glissée sous la ligne de séparation partirait quand même
    chez le prospect.
    """
    corps = "Bonjour,\n\nUn suivi.\n\n—\nWilliam — 210 fois plus de clients"
    assert not cc.check_statistiques_conformes(corps).passed


def test_le_check_est_dans_run_all() -> None:
    """Une garde absente de `run_all` est une garde morte."""
    noms = {r.name for r in cc.run_all(RELANCE_2, 0, template="RELANCE", track="agence-ia")}
    assert "statistiques_conformes" in noms


def test_la_relance_2_tient_dans_les_bornes() -> None:
    """121 mots pour 145 de plafond.

    La borne a été portée de 120 à 145 le 2026-09-01 : la relance est un texte
    FIXE, donc une borne trop basse aurait posé une remarque sur chaque envoi
    pour toujours, ce qui apprend à ignorer les remarques.
    """
    resultat = cc.check_length(RELANCE_2, template="RELANCE", track="agence-ia")
    assert resultat.passed, resultat.message
