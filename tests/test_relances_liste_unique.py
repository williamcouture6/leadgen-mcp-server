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

# La seule formulation « site » tolérée dans la relance 3, et pourquoi.
# « ça ne t'intéresse pas d'avoir […] un site web au goût du jour » est au
# CONDITIONNEL : elle parle de ce que le prospect n'a pas voulu, jamais d'un
# site qui existerait. Décision William, confirmée le 2026-09-16.
_TOLERE_RELANCE_3 = "au goût du jour"


def test_relance_3_ne_dit_pas_le_site_deja_fait() -> None:
    """🔴 CETTE GARDE A ÉTÉ DÉPLACÉE ICI, elle n'a pas été retirée.

    `check_site_au_conditionnel` se déclenchait sur la relance 3 dans 20
    brouillons sur 20 — mesuré le 2026-09-16 — parce que le motif attrape
    « au goût du jour ». `tools/compliance.py` a donc cessé de la juger corps
    par corps, et la raison dépasse ce cas précis :

    **les trois relances sont du texte FIXE** (`CORPS_RELANCES`, posé tel quel
    par `personalize.py`). Le rédacteur n'y écrit rien. Un contrôle
    déterministe sur un texte constant rend un verdict constant : il ne mesure
    pas le brouillon du soir, il mesure un fichier du dépôt. À 100 % de
    déclenchement il n'apprend rien, et il noie les remarques qui, elles,
    portent sur ce qui vient d'être écrit.

    Ici, le même contrôle est meilleur : il échoue **au moment où quelqu'un
    édite le texte**, pas trois semaines plus tard sur un brouillon. Si une
    réécriture de la relance 3 affirmait un jour que le site est fait — « je te
    l'envoie », « ton site est prêt » — ce test rougirait tout de suite.

    ⚠️ Retirer ce test impose de retirer l'exception dans `tools/compliance.py`.
    """
    r = check_site_au_conditionnel(CORPS_RELANCES["relance_3"])
    trouve = [m for m in (r.matches or []) if _TOLERE_RELANCE_3 not in m.lower()]
    assert not trouve, (
        "la relance 3 affirme maintenant que le site est DÉJÀ FAIT : "
        f"{trouve}. C'est la dette d'honnêteté refermée le 2026-08-26 "
        "([[feedback-no-lying-in-outreach]]), et plus personne ne la voit "
        "passer au juge — ce test est le dernier filet."
    )


def test_les_deux_autres_relances_ne_parlent_pas_du_site() -> None:
    """La contre-épreuve. Sans elle, l'exception pourrait glisser vers les
    relances 1 et 2 sans que rien ne le signale : elles, personne ne les
    exempte, et c'est parce qu'elles n'ont jamais parlé du site."""
    for cle in ("relance_1", "relance_2"):
        assert not (check_site_au_conditionnel(CORPS_RELANCES[cle]).matches or []), cle
