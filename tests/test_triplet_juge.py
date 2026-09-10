"""WF-5 juge les TROIS corps, et le verdict est le pire des trois.

🔴 Critère de fin nº3 de la spec du 2026-08-26, dans sa formulation corrigée :
« le critère ne porte pas sur le seul corps de tri — c'est cette formulation-là
qui masquait l'échec des deux relances sur `check_cta_present` ». Un draft
« approuvé » dont deux tiers du contenu n'avaient jamais été regardés.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.tools import compliance as comp
from tests.fixtures.corps_ac1 import (  # noqa: F401
    CORPS_A,
    RELANCE_1,
    RELANCE_2,
    RELANCE_3,
    SIGNATURE_COMPTE_INSTANTLY,
)

# Les TROIS, depuis le 2026-09-01. Une relance absente d'ici est une relance
# que le juge ne voit pas, alors qu'elle part au même prospect.
TRIPLET = {"relance_1": RELANCE_1, "relance_2": RELANCE_2, "relance_3": RELANCE_3}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)


async def _juger(**extra: Any) -> comp.ComplianceCheckOut:
    base: dict[str, Any] = dict(
        message_id="m1", body=CORPS_A, subject="les appels que tu manques",
        template_used="A", research_json={}, social_proof=[], available_slots=[],
        skip_llm=True, track="agence-ia",
        google_rating=4.8, google_reviews_count=47,
        followups=TRIPLET,
    )
    base.update(extra)
    return await comp.compliance_check(**base)


@pytest.mark.asyncio
async def test_le_triplet_complet_sort_approuve() -> None:
    """Le critère nº3 dans son entier : les trois corps ensemble, pas le
    premier tout seul."""
    out = await _juger()
    assert out.verdict == "approved", (
        f"bloqueurs={[b['name'] for b in out.deterministic_blockers]} "
        f"warnings={[w['name'] for w in out.deterministic_warnings]}"
    )


@pytest.mark.asyncio
async def test_une_relance_en_faute_fait_tomber_TOUT_le_verdict() -> None:
    """Le cœur du critère. Le corps de tri est irréprochable ; c'est la relance
    qui pèche, et le draft entier doit être retenu."""
    # ⚠️ La faute doit être un MENSONGE, pas une maladresse de forme.
    # Depuis la décision du 2026-08-31, un mot de vendeur ou un registre mêlé
    # n'a plus le droit de tuer un brouillon : seul ce que le prospect peut
    # VÉRIFIER le peut. « J'ai testé ton formulaire » en est un — il ne l'a
    # jamais fait, et le prospect peut le démentir.
    out = await _juger(
        followups={
            "relance_1": RELANCE_1,
            "relance_2": "Bonjour,\n\nJ'ai testé ton formulaire. Dis-moi.",
        }
    )
    assert out.verdict == "blocked"
    noms = [b["name"] for b in out.deterministic_blockers]
    assert any("relance 2" in b["message"] for b in out.deterministic_blockers), noms


@pytest.mark.asyncio
async def test_letiquette_dit_LEQUEL_des_trois_corps_est_en_faute() -> None:
    """« cta_present » tout court ne dit pas lequel des trois pèche, et c'est
    la première question qu'on se pose en lisant l'alerte du soir."""
    out = await _juger(
        followups={
            "relance_1": "Bonjour,\n\nJ'ai appelé ton bureau hier. Dis-moi.",
            "relance_2": RELANCE_2,
        }
    )
    en_faute = (
        out.deterministic_blockers + out.deterministic_warnings + out.deterministic_infos
    )
    assert any("relance 1" in x["name"] or "relance 1" in x["message"] for x in en_faute)


@pytest.mark.asyncio
async def test_les_relances_sont_jugees_avec_LEUR_gabarit() -> None:
    """Les juger sous le gabarit du corps de tri (180-270 mots) refuserait
    mécaniquement des relances de 97 mots. Chaque corps a ses bornes."""
    out = await _juger()
    longueurs = [
        x for x in out.deterministic_warnings if x["name"].startswith("length")
    ]
    assert not longueurs, longueurs


@pytest.mark.asyncio
async def test_un_draft_sans_relances_reste_jugeable() -> None:
    """La piste OPT n'a pas de relances, et les drafts antérieurs à AC1b non
    plus. Leur absence ne doit rien casser."""
    out = await _juger(followups=None)
    assert out.verdict == "approved"


@pytest.mark.asyncio
async def test_une_relance_vide_est_ignoree_pas_jugee_vide() -> None:
    """Une chaîne vide n'est pas un corps à juger : la faire passer dans
    `run_all` produirait un `length` en échec et un faux refus. Le refus des
    relances manquantes appartient au PUSH."""
    out = await _juger(followups={"relance_1": RELANCE_1, "relance_2": ""})
    assert out.verdict == "approved"


# ---------------- Le juge LLM voit les trois corps ----------------

def test_le_message_du_juge_porte_les_relances() -> None:
    msg = comp._message_utilisateur_juge(
        body=CORPS_A, subject="s", research_json={}, social_proof=[], contact=None,
        google_rating=4.8, google_reviews_count=47, followups=TRIPLET,
    )
    # Les trois, pas deux : une relance absente du message est une relance que
    # personne ne juge, alors qu'elle part au même prospect.
    assert "Relance 1" in msg and "Relance 2" in msg and "Relance 3" in msg
    # Une phrase distinctive de chacune : les libellés seuls prouveraient que
    # l'en-tête est écrit, pas que le CORPS a suivi.
    assert "remettre mon courriel sur le dessus" in msg
    assert "s'est pas encore perdu à travers les autres" in msg
    assert "Je ne vais plus t'écrire" in msg


def test_le_message_du_juge_dit_de_les_juger_AUSSI() -> None:
    """Les mettre sous ses yeux sans le lui dire ne suffit pas : le prompt
    système décrit un « email », au singulier."""
    msg = comp._message_utilisateur_juge(
        body=CORPS_A, subject="s", research_json={}, social_proof=[], contact=None,
        followups=TRIPLET,
    )
    assert "À JUGER AUSSI" in msg


def test_sans_relances_le_bloc_disparait_entierement() -> None:
    msg = comp._message_utilisateur_juge(
        body=CORPS_A, subject="s", research_json={}, social_proof=[], contact=None,
    )
    assert "Relance" not in msg


def test_le_prompt_du_juge_sait_que_les_faits_verifies_font_foi() -> None:
    """Sans ça, il crie à l'invention sur un chiffre exact — le bug 0732d20,
    où il ne voyait pas la fiche contact et flaggait des noms vrais."""
    from pathlib import Path

    prompt = (
        Path(__file__).resolve().parents[1] / "src" / "prompts" / "compliance.md"
    ).read_text(encoding="utf-8")
    assert "FAITS VÉRIFIÉS » EST LA VÉRITÉ" in prompt
    assert "JUGE LES TROIS CORPS" in prompt
    # L'ancienne offre ne doit plus servir d'exemple de formulation légitime.
    assert "je recontacte vos anciens clients" not in prompt
