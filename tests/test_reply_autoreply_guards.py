"""Tests des gardes auto-reply WF-7 (audit) :
  - _conversation_is_booked : ne pas auto-répondre à un lead déjà en RDV.
  - _count_prior_auto_replies : plafond anti-boucle.
Les deux lisent la DB → on mocke src.supabase_client.select.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


@pytest.mark.asyncio
async def test_conversation_is_booked_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import supabase_client
    from src.tools import reply

    captured: dict = {}

    async def fake_select(table, params=None):
        captured["table"] = table
        captured["params"] = params
        return [{"id": "conv-1"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._conversation_is_booked("contact-1") is True
    assert captured["table"] == "conversations"
    assert captured["params"]["contact_id"] == "eq.contact-1"
    assert captured["params"]["state"] == "eq.booked"


@pytest.mark.asyncio
async def test_conversation_is_booked_false_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import supabase_client
    from src.tools import reply

    async def fake_select(table, params=None):
        return []

    monkeypatch.setattr(supabase_client, "select", fake_select)
    assert await reply._conversation_is_booked("contact-1") is False


@pytest.mark.asyncio
async def test_conversation_is_booked_none_contact_no_db() -> None:
    from src.tools import reply
    # contact_id None → False sans toucher la DB
    assert await reply._conversation_is_booked(None) is False


@pytest.mark.asyncio
async def test_conversation_is_booked_failopen_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import supabase_client
    from src.tools import reply

    async def boom(table, params=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(supabase_client, "select", boom)
    # fail-open : erreur lecture → False (ne bloque pas le flux normal)
    assert await reply._conversation_is_booked("c1") is False


@pytest.mark.asyncio
async def test_count_prior_auto_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import supabase_client
    from src.tools import reply

    captured: dict = {}

    async def fake_select(table, params=None):
        captured["params"] = params
        return [{"id": "1"}, {"id": "2"}, {"id": "3"}]

    monkeypatch.setattr(supabase_client, "select", fake_select)
    n = await reply._count_prior_auto_replies("c1")
    assert n == 3
    assert captured["params"]["direction"] == "eq.outbound"
    assert captured["params"]["compliance_notes"].startswith("like.auto_reply_to_interested")


@pytest.mark.asyncio
async def test_count_prior_auto_replies_none_contact() -> None:
    from src.tools import reply
    assert await reply._count_prior_auto_replies(None) == 0


def test_cap_constant_is_high_safety_net() -> None:
    """Le plafond est un filet de sécurité, pas un limiteur de conversation
    normale — donc volontairement >= 5."""
    from src.tools import reply
    assert reply.MAX_AUTO_REPLIES_PER_CONVERSATION >= 5
