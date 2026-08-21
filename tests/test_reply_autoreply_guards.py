"""Tests de la garde WF-7 `_conversation_is_booked` : ne pas régresser une
conversation déjà en RDV (pose le marqueur/statut seulement si pas booked).
Lit la DB → on mocke src.supabase_client.select.

(Les tests du plafond anti-boucle auto-reply sont partis avec la chaîne
composer — pivot tri 2026-08-20, voir test_reply_interested_pivot.py.)
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
