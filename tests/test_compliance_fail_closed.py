"""WF-5 fail-closed : un courriel non inspecté n'est pas un courriel approuvé.

Le trou mesuré : l'appel au juge LLM est enveloppé dans un `except` qui
transforme la panne en `{"error": ...}`. La cascade de verdict ne testait que
`send_decision`, absente d'un dict d'erreur — les deux branches de refus
étaient donc sautées et on tombait sur le `else` final, `approved`. Le cron de
conformité étant quotidien, quelques minutes d'indisponibilité chez Anthropic
suffisaient à approuver le lot du jour, et l'alerte (qui compte
`needs_revision + blocked`) restait muette : le résumé du soir affichait
« refusés : 0 ».

Les corps servent ici avec `track="agence-ia"` ET `template_used="A"` : c'est
la seule combinaison qui laisse le layer 1 totalement vert (0 bloqueur,
0 warning) sur CORPS_A. Sans ça le test serait FAUX-VERT — soit `registre`
bloque avant le juge, soit `length` pose un warning qui produit
`needs_revision` par un chemin qui n'a rien à voir avec la panne.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.tools import compliance as comp
from tests.fixtures.corps_ac1 import CORPS_A, SIGNATURE_COMPTE_INSTANTLY


@pytest.fixture(autouse=True)
def _env_layer1_vert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise les gates d'environnement du layer 1 (fail-closed par défaut).

    L'environnement reproduit la PRODUCTION depuis la décision du 2026-08-30 :
    le corps ne porte plus de signature, et `INSTANTLY_CAMPAIGN_FOOTER` porte
    la signature du compte d'envoi. Sans `LCAP_MENTIONS_REDUITES`, l'adresse
    postale manquerait et `legal_footer` bloquerait avant le juge — le test
    prouverait alors le fail-closed du footer au lieu de celui du juge.
    """
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)


def _juge_qui_tombe(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("Anthropic overloaded")


async def _check(**extra: Any) -> comp.ComplianceCheckOut:
    base: dict[str, Any] = dict(
        message_id="msg-1",
        body=CORPS_A,
        subject="Une question",
        template_used="A",
        research_json={},
        social_proof=[],
        available_slots=[],
    )
    base.update(extra)
    return await comp.compliance_check(**base)


# ---------------- 1. Panne du juge → non_juge, jamais approved ----------------

@pytest.mark.asyncio
async def test_panne_du_juge_ne_donne_jamais_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comp, "_llm_judge", _juge_qui_tombe)

    out = await _check(track="agence-ia", tentatives=0)

    assert out.verdict == "non_juge", (
        f"juge en panne → verdict={out.verdict!r} ; un courriel que le juge "
        "n'a jamais inspecté ne peut pas être approuvé"
    )
    assert out.send_decision == "DO_NOT_SEND"
    # Le layer 1 est vert : la preuve que le refus vient bien de la panne et
    # non d'un warning déterministe qui traînerait.
    assert out.deterministic_blockers == []
    assert out.deterministic_warnings == []
    assert out.llm_judge is not None and out.llm_judge.get("error")
    # `compliance_notes` se lit à l'œil nu : il ne doit pas rassurer.
    assert "Aucune violation" not in out.reasoning
    assert "NON inspect" in out.reasoning
    assert "llm_error" in comp.format_compliance_notes(out)


# ---------------- 2. 3e tentative → vrai refus (garde anti-boucle) ----------------

@pytest.mark.asyncio
async def test_troisieme_tentative_devient_un_vrai_refus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un corps qui fait tomber le juge tous les jours doit finir par crier.

    `non_juge` laisse `compliance_check_passed` à NULL, donc le draft revient
    de lui-même dans le lot du lendemain. Sans plafond, il tournerait en rond
    à l'infini en silence.
    """
    monkeypatch.setattr(comp, "_llm_judge", _juge_qui_tombe)

    out = await _check(track="agence-ia", tentatives=2)

    assert out.verdict == "needs_revision", (
        f"3e tentative → verdict={out.verdict!r} ; un échec permanent doit "
        "sortir du lot, pas y rester"
    )
    assert out.send_decision == "REVIEW_THEN_SEND"


@pytest.mark.asyncio
async def test_deuxieme_tentative_reste_non_juge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frontière du plafond : 2e tentative (tentatives=1) réessaie encore."""
    monkeypatch.setattr(comp, "_llm_judge", _juge_qui_tombe)

    out = await _check(track="agence-ia", tentatives=1)

    assert out.verdict == "non_juge"


@pytest.mark.asyncio
async def test_tentatives_none_ne_fait_pas_planter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`compliance_tentatives` absent du SELECT rend None côté appelant.

    `None >= 2` lève un TypeError en Python 3. Le juge tomberait alors dans le
    `except` de l'appelant HTTP, le verdict ne serait jamais persisté, et le
    draft repartirait pour un tour — exactement la boucle qu'on ferme ici.
    """
    monkeypatch.setattr(comp, "_llm_judge", _juge_qui_tombe)

    out = await _check(track="agence-ia", tentatives=None)

    assert out.verdict == "non_juge"


# ---------------- 3. Le track est transmis au layer 1 ----------------

@pytest.mark.asyncio
async def test_track_transmis_aux_checks_deterministes() -> None:
    """CORPS_A tutoie ; sans le track, `check_registre` retombe sur `vous`."""
    out = await _check(track="agence-ia", skip_llm=True)

    noms = [b["name"] for b in out.deterministic_blockers]
    assert "registre" not in noms, (
        f"le track n'atteint pas run_all — bloqueurs={noms}"
    )


@pytest.mark.asyncio
async def test_sans_track_le_registre_bloque_bien() -> None:
    """Contrôle négatif : sans lui, l'assertion ci-dessus ne prouverait rien."""
    out = await _check(skip_llm=True)

    noms = [b["name"] for b in out.deterministic_blockers]
    assert "registre" in noms
    assert out.verdict == "blocked"


# ---------------- skip_llm n'est PAS une panne ----------------

@pytest.mark.asyncio
async def test_skip_llm_nest_pas_confondu_avec_une_panne() -> None:
    """`skip_llm=True` laisse `llm_verdict` à None → comportement historique."""
    out = await _check(track="agence-ia", skip_llm=True)

    assert out.verdict == "approved"
    assert out.send_decision == "SEND"
    assert out.llm_judge is None


@pytest.mark.asyncio
async def test_verdict_sans_error_ni_send_decision_reste_historique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un dict du juge sans `error` ni `send_decision` n'est pas une panne."""
    monkeypatch.setattr(
        comp, "_llm_judge",
        lambda *a, **k: {"reasoning_one_line": "rien à signaler"},
    )

    out = await _check(track="agence-ia", tentatives=0)

    assert out.verdict == "approved"
    assert out.send_decision == "SEND"
    assert out.reasoning == "rien à signaler"
