"""Le prompt du juge ne doit jamais citer notre propre copie comme exemple à refuser.

🔴 LE DÉFAUT, TROUVÉ DEUX FOIS EN UNE JOURNÉE, DANS LE MÊME FICHIER.

Le matin : `compliance.md` §3 donnait « 78% des leads quittent en 60 minutes »
comme exemple canonique de statistique à faire reformuler — soit le chiffre
même que la relance 2 emploie.

Le soir, trouvé par le conseil : §1ter donnait « j'en ai profité pour te le
créer » comme exemple canonique de mensonge vérifiable à signaler — soit le
pied de page de C et D, au mot près : « J'en ai aussi profité pour te refaire
un site web au goût du jour ».

Le second était plus cher que le premier. La couche déterministe laisse passer
(`check_site_au_conditionnel` est en `info` depuis la décision de William), le
juge lit le corps, applique §1ter à la lettre et rend `DO_NOT_SEND`. Le
brouillon passe à `compliance_check_passed=false` et sort du lot POUR TOUJOURS
— la requête ne reprend que les `is.null`. Avec `template_choice="ABCD"`, un
contact sur deux reçoit C ou D : **~128 des 255 contacts gelés à vie, en
silence, la suite de tests verte du début à la fin.**

Ces tests ne valent que ce que vaut l'obéissance d'un modèle. Ce qu'ils
protègent, c'est que le prompt cesse de se contredire — parce que quand deux
consignes se contredisent, c'est la plus concrète qui l'emporte, et un exemple
littéral est la chose la plus concrète d'un prompt.
"""
from __future__ import annotations

import pytest

from src.tools.compliance import _PROMPT_PATH

PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Les deux formulations réellement expédiées, lues depuis les fixtures pour que
# ce test suive la copie au lieu de figer un souvenir d'elle.
from tests.fixtures.corps_ac1 import CORPS_C, CORPS_C_SANS_SITE  # noqa: E402


def _phrase_du_site(corps: str) -> str:
    """La ligne du bloc du site, telle qu'elle part."""
    for para in corps.split("\n\n"):
        if "profité pour te" in para:
            return " ".join(para.split())
    raise AssertionError("le bloc du site a disparu du corps — la copie a changé")


def test_la_permission_est_dans_la_liste_ne_pas_re_checker() -> None:
    entete = "## Ce que les checks déterministes ont déjà couvert (NE PAS RE-CHECKER)"
    section = PROMPT.split(entete, 1)[1].split("\n## ", 1)[0]
    assert "gabarits C et D" in section, (
        "la permission du bloc du site n'est pas dans la section que le juge lit "
        "comme « ce n'est pas ton travail »"
    )


def test_le_paragraphe_1ter_ne_cite_plus_notre_propre_pied_de_page() -> None:
    """🔴 Le cœur du défaut.

    Une permission ajoutée ailleurs ne suffit pas si l'exemple reste : c'est la
    leçon écrite dans le commit e5743b6 du matin même, à propos du 78 %.
    """
    interdit = "j'en ai profité pour te le créer"
    for ligne in PROMPT.splitlines():
        if interdit not in ligne:
            continue
        assert "ne pas le remettre" in ligne.lower(), (
            f"l'exemple qui cite notre propre pied de page est revenu comme "
            f"consigne : {ligne.strip()[:140]}"
        )


@pytest.mark.parametrize(
    ("nom", "corps"), [("avec site", CORPS_C), ("sans site", CORPS_C_SANS_SITE)]
)
def test_la_formulation_expediee_est_couverte_par_la_permission(nom: str, corps: str) -> None:
    """Le prompt doit nommer le verbe RÉELLEMENT employé.

    « refaire » et « faire » sont deux formulations distinctes selon que
    l'entreprise a un site. Une permission qui n'en couvre qu'une laisse
    l'autre exposée — et c'est la variante sans site, celle des 97 entreprises
    les plus faciles à convaincre, qui serait tombée.
    """
    phrase = _phrase_du_site(corps)
    verbe = "refaire" if "refaire" in phrase else "faire"
    assert f"te {verbe} un site web au goût du jour" in PROMPT, (
        f"la variante « {nom} » ({verbe}) n'est pas nommée dans le prompt du juge"
    )


def test_la_permission_dit_que_le_deterministe_le_detecte_deja() -> None:
    """Sans ça, la permission ressemble à un blanc-seing.

    Elle doit dire que la formulation EST détectée et comptée, simplement en
    `info` — c'est ce qui rend la décision de William mesurable et réversible
    au lieu d'invisible.
    """
    assert "check_site_au_conditionnel" in PROMPT
    assert "info" in PROMPT


def test_A_et_B_restent_au_conditionnel() -> None:
    """La permission est NOMMÉE, pas générale.

    Si un corps A se mettait à dire le site fait, ce serait bien une violation.
    Le prompt doit garder la distinction, sinon la permission de C et D devient
    une permission pour tout le monde.
    """
    assert "A et B, eux, restent AU CONDITIONNEL" in PROMPT
