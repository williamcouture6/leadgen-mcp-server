"""PT1 — la branche `interested` de WF-7 après le pivot tri :
garde désabonnement AVANT toute écriture, marqueur interested_at idempotent,
plus aucune chaîne auto-reply/composer."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


async def test_suppressed_par_statut_contact(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        assert table == "contacts"
        return [{"status": "opted_out"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is True


async def test_suppressed_par_liste(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        if table == "contacts":
            return [{"status": "replied"}]
        assert table == "suppression_list"
        return [{"reason": "opt_out"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is True


async def test_pas_suppressed(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None, **kw):
        return [{"status": "contacted"}] if table == "contacts" else []

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is False


async def test_fail_open_sur_erreur_de_lecture(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def boom(table, params=None, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(supabase_client, "select", boom)
    # Fail-open assumé : un ping de trop (William arbitre) vaut mieux qu'un
    # hot lead silencieusement perdu sur une panne de lecture.
    assert await reply._interested_lead_is_suppressed("ct-1", "a@b.ca") is False


async def test_marqueur_pose_avec_filtre_idempotent(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    captured: dict = {}

    async def fake_update(table, patch, filters=None, **kw):
        captured["table"] = table
        captured["patch"] = patch
        captured["filters"] = filters
        return [{}]

    monkeypatch.setattr(supabase_client, "update", fake_update)
    await reply._mark_contact_interested("ct-1")
    assert captured["table"] == "contacts"
    assert "interested_at" in captured["patch"]
    assert captured["filters"]["id"] == "eq.ct-1"
    assert captured["filters"]["interested_at"] == "is.null"


async def test_marqueur_sans_contact_id_ne_lit_pas_la_base(monkeypatch):
    from src import supabase_client
    from src.tools import reply

    async def boom(*a, **kw):
        raise AssertionError("aucun appel DB attendu")

    monkeypatch.setattr(supabase_client, "update", boom)
    await reply._mark_contact_interested(None)  # ne lève pas


def test_chaine_composer_retiree():
    from src.tools import reply

    for symbole in (
        "_call_composer",
        "_count_prior_auto_replies",
        "AUTO_REPLY_CONFIDENCE_THRESHOLD",
        "MAX_AUTO_REPLIES_PER_CONVERSATION",
    ):
        assert not hasattr(reply, symbole), symbole


def test_prompt_composer_supprime():
    from pathlib import Path

    from src.tools import reply

    # Ancré sur le module (pas le cwd) — robuste peu importe d'où pytest tourne.
    prompts = Path(reply.__file__).resolve().parents[1] / "prompts"
    assert not (prompts / "reply_compose.md").exists()
    assert (prompts / "reply_classifier.md").exists()


def test_hot_lead_blocks_nouvelle_signature():
    from src.lib import slack

    fallback, blocks = slack.build_hot_lead_blocks(
        contact_name="Jean Roy",
        company_name="Plomberie X",
        contact_email="jean@plomberiex.ca",
        reply_preview="Oui, montrez-moi ça",
        confidence=0.91,
        track="agence-ia",
        website="https://plomberiex.ca",
    )
    joined = str(blocks)
    assert "produire le site" in fallback.lower() or "produire le site" in joined.lower()
    assert "plomberiex.ca" in joined
    assert "Auto-reply" not in joined
