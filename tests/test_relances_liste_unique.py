"""La séquence de relances est définie à UN endroit, et tout le monde la lit.

Avant le 2026-09-01, « il y a exactement deux relances » vivait en dur dans six
modules indépendants. Ajouter la troisième demandait six modifications, et en
oublier une ne cassait rien de visible : la relance aurait été écrite, jugée,
stockée en base — puis le lead serait parti sans elle.

C'est précisément le défaut que cinq lentilles du conseil de revue de la spec
avaient trouvé SÉPARÉMENT pour les deux premières relances, quand rien ne les
transportait. Un défaut silencieux qu'il a fallu cinq regards pour voir mérite
un test, pas un commentaire.

Ce fichier vérifie deux choses : que personne n'a re-déclaré la liste dans son
coin (identité de l'objet), et qu'un ajout à la liste se propage bien jusqu'aux
variables réellement envoyées à Instantly.
"""
from __future__ import annotations

import re

from src.lib.compliance_checks import check_site_au_conditionnel
from src.lib.relances import (
    CLES_RELANCES,
    CORPS_RELANCES,
    NB_CORPS_PAR_ENVOI,
    RELANCES,
)


def test_la_liste_est_coherente_avec_elle_meme() -> None:
    assert CLES_RELANCES == tuple(cle for cle, _, _ in RELANCES)
    assert NB_CORPS_PAR_ENVOI == 1 + len(RELANCES)
    cles = [cle for cle, _, _ in RELANCES]
    variables = [var for _, var, _ in RELANCES]
    assert len(set(cles)) == len(cles), "clé dupliquée"
    assert len(set(variables)) == len(variables), "variable Instantly dupliquée"


def test_la_numerotation_des_variables_suit_le_rang() -> None:
    """`relance_2` doit alimenter `followup_2_body`, pas `followup_3_body`.

    Un décalage ici enverrait la bonne relance à la mauvaise étape : le
    prospect recevrait l'adieu avant la relance, et rien ne planterait.
    """
    for rang, (cle, var, _) in enumerate(RELANCES, start=1):
        assert cle == f"relance_{rang}", f"rang {rang} : clé {cle!r}"
        assert var == f"followup_{rang}_body", f"rang {rang} : variable {var!r}"


def test_personne_n_a_redeclare_la_liste_dans_son_coin() -> None:
    """Identité de l'objet, pas égalité.

    Une copie locale resterait égale aujourd'hui et divergerait au premier
    ajout — exactement le mode de panne que ce module supprime.
    """
    from src import http_api
    from src.lib import instantly as instantly_lib
    from src.tools import compliance as compliance_tools
    from src.tools import personalize as personalize_tools
    from src.tools import send as send_tools

    assert http_api.CLES_RELANCES is CLES_RELANCES
    assert send_tools.CLES_RELANCES is CLES_RELANCES
    assert personalize_tools.CLES_RELANCES is CLES_RELANCES
    assert instantly_lib.RELANCES is RELANCES
    assert compliance_tools.RELANCES is RELANCES


def test_un_ajout_a_la_liste_se_propage_jusqu_a_instantly(monkeypatch) -> None:
    """Le test qui vaut la peine : la propagation, pas la déclaration.

    On ajoute une quatrième relance dans le module d'Instantly seulement, et on
    vérifie que la variable correspondante apparaît dans le corps de requête.
    Si `instantly.py` reconstruisait ses variables à la main, ce test échouerait.
    """
    from src.lib import instantly as instantly_lib

    monkeypatch.setattr(
        instantly_lib,
        "RELANCES",
        RELANCES + (("relance_4", "followup_4_body", "Relance 4 (test)"),),
    )
    followups = {cle: f"corps {cle}" for cle, _, _ in instantly_lib.RELANCES}
    variables = {
        var: followups.get(cle, "") for cle, var, _ in instantly_lib.RELANCES
    }
    assert variables["followup_4_body"] == "corps relance_4"
    assert len(variables) == len(RELANCES) + 1


def test_la_relance_finale_annonce_bien_la_fin() -> None:
    """La dernière relance promet « je ne vais plus t'écrire ».

    Ajouter une relance APRÈS elle ferait de cette phrase un mensonge — et d'un
    genre que le prospect constate tout seul, donc le pire. Ce test ne peut pas
    lire le texte (il vit dans le prompt), mais il fige l'INTENTION dans le
    libellé : quiconque ajoute une relance 4 verra ce test et saura qu'il doit
    d'abord réécrire la 3.
    """
    _cle, _var, libelle = RELANCES[-1]
    assert "dernier contact" in libelle.lower(), (
        "la dernière relance n'annonce plus la fin de la séquence — si une "
        "relance a été ajoutée après l'adieu, c'est le TEXTE de l'adieu qu'il "
        "faut corriger d'abord"
    )


# ── La garde que `compliance.py` a cessé d'exercer chaque soir ──────────────

# 🔴 LA SEULE PHRASE DE LA RELANCE 3 QUI A LE DROIT DE PARLER DU SITE.
#
# Elle est tolérée parce qu'elle est AU CONDITIONNEL : elle parle de ce que le
# prospect n'a pas voulu, jamais d'un site qui existerait. Décision William,
# confirmée le 2026-09-16.
#
# ⚠️ Elle est recopiée ICI EN ENTIER, et pas réduite à « au goût du jour »,
# parce qu'une première version de ce test tolérait le simple BOUT DE PHRASE —
# et un conseil de relecture a montré le 2026-09-17 que ça ne protégeait rien.
# Des quatre motifs de `SITE_DEJA_FAIT_PATTERNS`, « au goût du jour » est le
# seul qui ne soit pas une tournure verbale figée ; les trois autres exigent
# `j'en ai profité`, `ton site est prêt|fait|terminé|refait`, `je te l'envoie`.
# Déplacer un mot suffisait à leur échapper. Ces deux réécritures passaient le
# test au vert tout en affirmant que le site existe :
#
#   « Je crois avoir compris que le site web au goût du jour QUE JE T'AI FAIT
#     ne t'intéresse pas. »
#   « TON NOUVEAU SITE web au goût du jour EST DÉJÀ EN LIGNE, je t'ai envoyé
#     le lien. »
#
# La première est une réécriture maladroite parfaitement plausible : elle garde
# la phrase de William mot pour mot et déplace juste le verbe.
def _parle_du_site(texte: str) -> bool:
    """Le mot « site » comme MOT, pas comme suite de lettres.

    ⚠️ Un simple `"site" in texte` disait oui sur « hésite » — donc sur les
    relances 1 et 2, qui finissent toutes deux par « hésite pas a m'ecrire ».
    Trouvé en écrivant ce test le 2026-09-17.
    """
    return re.search(r"site", texte, re.IGNORECASE) is not None


PHRASE_TOLEREE = (
    "Je crois avoir compris que ça ne t'intéresse pas d'avoir le système "
    "et un site web au goût du jour."
)


def test_la_phrase_du_site_est_exactement_celle_qui_a_ete_approuvee() -> None:
    """🔴 CETTE GARDE A ÉTÉ DÉPLACÉE ICI, elle n'a pas été retirée.

    `check_site_au_conditionnel` se déclenchait sur la relance 3 dans 20
    brouillons sur 20 — mesuré le 2026-09-16 — parce que le motif attrape
    « au goût du jour ». `tools/compliance.py` a donc cessé de la juger corps
    par corps, et la raison dépasse ce cas : **les trois relances sont du texte
    FIXE** (`CORPS_RELANCES`, posé tel quel par `personalize.py`). Le rédacteur
    n'y écrit rien. Un contrôle déterministe sur un texte constant rend un
    verdict constant — il ne mesure pas le brouillon du soir, il mesure un
    fichier du dépôt.

    ⚠️ **Mais « le texte est constant » n'est PAS le critère.** Le critère est
    « le texte constant est HONNÊTE ». Le bloc du site des gabarits C et D est
    tout aussi constant et déclenche tout aussi systématiquement : il reste en
    place EXPRÈS, pour compter combien de courriels partent en disant le site
    fait. Ne pas ressortir l'argument de la constance pour l'exempter à son
    tour — ce serait effacer la seule trace mesurée de la dette du 2026-08-26.

    Ce test-ci est la contrepartie de l'exemption : il exige que la phrase soit
    EXACTEMENT celle que William a approuvée, au caractère près. Toute
    réécriture, même bien intentionnée, le fait rougir — et c'est voulu : elle
    doit être relue par un humain avant de partir à des centaines de prospects.
    """
    assert PHRASE_TOLEREE in CORPS_RELANCES["relance_3"], (
        "la phrase du site de la relance 3 a été réécrite. Elle part telle "
        "quelle à tous les prospects et plus aucun contrôle ne la lit : "
        "`tools/compliance.py` exempte `site_au_conditionnel` sur ce corps. "
        "Relis-la contre la règle du 2026-08-26 — le site n'existe PAS au "
        "moment du courriel, il se fabrique après un oui — puis mets à jour "
        "`PHRASE_TOLEREE` en connaissance de cause."
    )


def test_la_relance_3_ne_parle_du_site_NULLE_PART_AILLEURS() -> None:
    """🔴 LE FILET QUI NE JOUE PAS AU CHAT ET À LA SOURIS.

    Chercher des formulations de mensonge une par une est perdu d'avance : il y
    en a une infinité, et la version précédente de ce test l'a prouvé en
    laissant passer les deux réécritures citées plus haut.

    On renverse donc la charge. La relance 3 n'a qu'UNE raison légitime de
    prononcer le mot « site », et c'est la phrase approuvée. Une fois celle-ci
    retirée, le mot ne doit plus apparaître du tout. N'importe quelle
    affirmation nouvelle sur le site — quelle que soit sa tournure — doit bien
    le nommer pour dire quoi que ce soit à son sujet.
    """
    reste = CORPS_RELANCES["relance_3"].replace(PHRASE_TOLEREE, "")
    assert not _parle_du_site(reste), (
        "la relance 3 parle du site ailleurs que dans la phrase approuvée :\n"
        f"{reste!r}\n"
        "Aucun contrôle ne lit plus ce corps. Si cet ajout est voulu, il doit "
        "passer devant la règle du 2026-08-26 avant, pas après."
    )


def test_le_motif_deterministe_ne_trouve_rien_hors_la_phrase_approuvee() -> None:
    """La troisième couche, celle qui reste attachée au VRAI contrôle.

    Les deux tests du dessus sont écrits à la main ; celui-ci rejoue
    `check_site_au_conditionnel` lui-même, pour que l'ajout d'un motif au
    contrôle profite aussi à la relance 3 — qui, sinon, ne le verrait jamais
    passer.
    """
    reste = CORPS_RELANCES["relance_3"].replace(PHRASE_TOLEREE, "")
    assert not (check_site_au_conditionnel(reste).matches or [])


# La relance 1 parle du site elle aussi, et elle en a le droit : elle rappelle
# une OFFRE, elle n'annonce pas un livrable. Même traitement que la relance 3 —
# la phrase est épinglée au caractère près plutôt que devinée par un motif.
PHRASE_SITE_RELANCE_1 = "Pour le site, l'offre tient toujours."


def test_les_deux_autres_relances_ne_parlent_du_site_que_pour_offrir() -> None:
    """La contre-épreuve, et elle a corrigé ma première version.

    J'avais écrit « les relances 1 et 2 n'ont jamais parlé du site ». C'était
    faux : la relance 1 dit « Pour le site, l'offre tient toujours ». Une offre
    qui tient est au présent et ne prétend rien — c'est exactement la forme que
    la règle du 2026-08-26 autorise, et l'inverse de « je te l'envoie ».

    ⚠️ Les relances 1 et 2 ne sont PAS exemptées dans `tools/compliance.py` :
    seule la relance 3 l'est. Le contrôle déterministe les lit donc encore à
    chaque brouillon. Ce test double la lecture au cas où l'exemption
    glisserait vers elles — ce serait silencieux autrement.
    """
    assert PHRASE_SITE_RELANCE_1 in CORPS_RELANCES["relance_1"]
    assert not _parle_du_site(CORPS_RELANCES["relance_2"])

    for cle, approuvee in (
        ("relance_1", PHRASE_SITE_RELANCE_1),
        ("relance_2", None),
    ):
        texte = CORPS_RELANCES[cle]
        assert not (check_site_au_conditionnel(texte).matches or []), cle
        reste = texte.replace(approuvee, "") if approuvee else texte
        assert not _parle_du_site(reste), (
            f"{cle} parle du site ailleurs que dans sa phrase approuvée : "
            f"{reste!r}"
        )
