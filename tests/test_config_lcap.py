"""La configuration LCAP en faute ne doit JAMAIS tuer un brouillon.

Le piège, trouvé par le conseil de revue du 2026-08-30 et vérifié par
exécution : depuis que le corps ne porte plus de signature (décision William du
même jour), le nom légal et le lien de désabonnement ne vivent QUE dans
`INSTANTLY_CAMPAIGN_FOOTER`. Cette variable vide,
`check_legal_footer` accusait le CORPS d'un manquement venu de
l'environnement :

    verdict `blocked` → `compliance_check_passed = false`
    → le brouillon QUITTE le lot pour toujours (la requête ne reprend que
      les `is.null`)
    → et son contact reste gelé à vie dans la fenêtre WF-4
      (`already_drafted` compte tout message dont le status n'est pas 'failed')

Soit, au premier go-live : 20 contacts brûlés par jour, 255 en deux semaines,
zéro courriel envoyé, et la suite de tests verte du début à la fin.

La règle que ces tests figent : **on refuse la PASSE, pas le MESSAGE.**
"""
from __future__ import annotations

from typing import Any

import pytest

from src.lib import compliance_checks as cc
from src.tools import compliance as comp
from tests.fixtures.corps_ac1 import CORPS_A, SIGNATURE_COMPTE_INSTANTLY


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse, Lévis")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")


async def _passe(**extra: Any) -> comp.ComplianceCheckOut:
    base: dict[str, Any] = dict(
        message_id="msg-1", body=CORPS_A, subject="s", template_used="A",
        research_json={}, social_proof=[], available_slots=[], skip_llm=True,
        track="agence-ia", google_rating=4.8, google_reviews_count=47,
    )
    base.update(extra)
    return await comp.compliance_check(**base)


# ---------------- La detection ----------------

def test_le_pied_de_page_vide_est_une_faute_de_config(monkeypatch: pytest.MonkeyPatch) -> None:
    manquants = cc.mentions_manquantes_dans_la_config("")
    assert manquants
    assert "INSTANTLY_CAMPAIGN_FOOTER" in manquants[0], (
        "le message doit NOMMER la variable à corriger : sans ça, on cherche "
        "le défaut dans la copie pendant des heures"
    )


def test_un_pied_de_page_complet_ne_manque_de_rien() -> None:
    assert cc.mentions_manquantes_dans_la_config(SIGNATURE_COMPTE_INSTANTLY) == []


def test_un_pied_de_page_sans_desabonnement_est_detecte() -> None:
    manquants = cc.mentions_manquantes_dans_la_config("William Couture\ncouture-ia.com")
    assert any("désabonnement" in m for m in manquants)


def test_un_pied_de_page_sans_nom_legal_est_detecte() -> None:
    manquants = cc.mentions_manquantes_dans_la_config(
        "couture-ia.com\nPour te désabonner : https://couture-ia.com/unsubscribe"
    )
    assert any("nom légal" in m for m in manquants)


def test_sans_le_drapeau_la_config_nest_pas_en_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mentions non réduites = le corps est censé tout porter, et
    `check_legal_footer` est alors le bon juge. Le garde-fou de config se tait."""
    monkeypatch.delenv("LCAP_MENTIONS_REDUITES", raising=False)
    assert cc.mentions_manquantes_dans_la_config("") == []


# ---------------- La consequence : la passe, pas le message ----------------

@pytest.mark.asyncio
async def test_la_passe_est_refusee_AVANT_de_juger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", "")

    out = await _passe()

    assert out.verdict == "error", (
        f"verdict={out.verdict!r} — un `blocked` écrirait "
        "compliance_check_passed=false et sortirait le brouillon du lot POUR "
        "TOUJOURS, pour une variable d'environnement vide"
    )
    assert out.error_text and "config_lcap_incomplete" in out.error_text
    assert "INSTANTLY_CAMPAIGN_FOOTER" in out.error_text
    assert out.deterministic_blockers == [], (
        "la passe doit s'arrêter AVANT le layer 1 : si des bloqueurs sont "
        "rendus, c'est que le brouillon a été jugé et donc accusé"
    )


@pytest.mark.asyncio
async def test_le_brouillon_nest_jamais_accuse_dune_faute_de_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le corps est irréprochable ; seule la variable est vide. Le verdict ne
    doit rien dire contre lui."""
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", "")

    out = await _passe()

    assert out.verdict != "blocked"
    assert out.verdict != "needs_revision"
    assert "n'est pas en cause" in out.reasoning


@pytest.mark.asyncio
async def test_la_passe_repart_des_que_la_variable_est_remplie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rien n'ayant été écrit en base, le brouillon revient de lui-même dans le
    lot du lendemain. C'est tout l'intérêt de ne pas l'avoir marqué."""
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)

    out = await _passe()

    assert out.verdict == "approved", out.deterministic_blockers


# ---------------- Le filet de dernier recours reste arme ----------------

def test_check_legal_footer_bloque_toujours_un_corps_nu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Le cas que j'avais SUPPRIMÉ des tests en le remplaçant par une valeur
    remplie, ce qui a rendu le piège invisible. Il est remis ici.

    `check_legal_footer` doit CONTINUER de bloquer un corps sans mentions et
    sans pied de page : c'est le filet de dernier recours si le garde-fou de
    configuration venait à être contourné. Ce qui change, c'est qu'on ne
    l'atteint plus jamais par une variable vide — la passe s'arrête avant.
    """
    r = cc.check_legal_footer(CORPS_A, appended_footer="")
    assert not r.passed
    assert r.severity == "block"
    assert any("company_name" in m for m in r.matches)
    assert any("unsubscribe" in m for m in r.matches)


# ---------------- Le drapeau ABSENT est lui-meme une faute de config ----------------

def test_le_drapeau_absent_est_une_faute_sur_agence_ia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Trouvé le 2026-08-31, en préparant le go-live. Le layer 0 ne couvrait
    que « drapeau POSÉ + pied de page vide ». Le cas PAR DÉFAUT — drapeau
    absent — retombait dans le même désastre par l'autre porte.

    Mesuré : sans le drapeau, tout le reste correctement configuré,
    `check_legal_footer` rend `blocked` parce qu'il cherche l'adresse postale
    dans un corps qui n'en a jamais eu — et `blocked` écrit
    `compliance_check_passed=false`, donc brouillon mort et contact gelé.

    C'est la porte la plus probable : celle qu'on emprunte en OUBLIANT de poser
    une variable sur Railway.
    """
    monkeypatch.delenv("LCAP_MENTIONS_REDUITES", raising=False)
    manquants = cc.mentions_manquantes_dans_la_config(
        SIGNATURE_COMPTE_INSTANTLY, track="agence-ia"
    )
    assert manquants
    assert "LCAP_MENTIONS_REDUITES" in manquants[0], (
        "le message doit NOMMER la variable à poser"
    )


def test_le_drapeau_absent_est_NORMAL_sur_OPT(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contrôle négatif, et il compte : les corps OPT portent LEUR signature.
    L'absence du drapeau y est le comportement correct, pas une faute."""
    monkeypatch.delenv("LCAP_MENTIONS_REDUITES", raising=False)
    assert cc.mentions_manquantes_dans_la_config(
        SIGNATURE_COMPTE_INSTANTLY, track="OPT"
    ) == []


@pytest.mark.asyncio
async def test_sans_le_drapeau_la_passe_sarrete_au_lieu_de_tuer_le_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le verdict doit être `error` (la passe), jamais `blocked` (le message)."""
    monkeypatch.delenv("LCAP_MENTIONS_REDUITES", raising=False)
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)

    out = await _passe()

    assert out.verdict == "error", (
        f"verdict={out.verdict!r} — un `blocked` écrirait passed=false et "
        "gèlerait le contact pour une variable oubliée sur Railway"
    )
    assert "LCAP_MENTIONS_REDUITES" in (out.error_text or "")
