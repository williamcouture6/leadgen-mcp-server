"""Tests WF-6 sync-status (audit #5 — sync statut/bounce Instantly).

Couvre :
1. classify_lead_outcome — mapping best-effort multi-shape (bounce/unsub/reply/sent/pending/not_found).
2. sync_send_status — flip messages + suppression sur bounce/unsub, dry_run, lead not_found.
3. get_lead — GET /leads/{id}, 404 -> None, autre 4xx -> InstantlyError.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("INSTANTLY_API_KEY", "test-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "test-campaign-id")


# =====================================================================
# classify_lead_outcome
# =====================================================================

@pytest.mark.parametrize(
    "lead,expected",
    [
        (None, "not_found"),
        ({"is_bounced": True}, "bounced"),
        ({"bounced": 1}, "bounced"),
        ({"email_bounced_count": 1}, "bounced"),
        ({"status_text": "Bounced"}, "bounced"),
        ({"status": -1}, "bounced"),
        ({"status": -3}, "bounced"),
        ({"status": -2}, "unsubscribed"),
        ({"is_unsubscribed": True}, "unsubscribed"),
        ({"status_summary": "lead unsubscribed"}, "unsubscribed"),
        ({"email_reply_count": 2}, "replied"),
        ({"email_sent_count": 1}, "sent"),
        ({"sent_count": 3}, "sent"),
        ({"status": 3}, "sent"),       # Completed
        ({"status": 1}, "pending"),    # Active, rien envoye encore
        ({}, "pending"),
    ],
)
def test_classify_lead_outcome(lead, expected) -> None:
    from src.tools.send_status import classify_lead_outcome
    assert classify_lead_outcome(lead) == expected


def test_classify_bounce_wins_over_sent() -> None:
    """Un lead avec un email envoye PUIS bounce doit etre classe 'bounced'."""
    from src.tools.send_status import classify_lead_outcome
    assert classify_lead_outcome({"email_sent_count": 1, "is_bounced": True}) == "bounced"


# =====================================================================
# sync_send_status — flow
# =====================================================================

def _msg(mid: str, lead_id: str, email: str, contact_id: str | None = None) -> dict:
    return {
        "id": mid, "provider_message_id": lead_id, "to_email": email,
        "contact_id": contact_id, "status": "queued", "sent_at": None,
    }


class _Spy:
    """Capture les appels db.update + add_to_suppression."""
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict, dict]] = []
        self.suppressions: list[dict] = []


@pytest.fixture
def _patch_io(monkeypatch: pytest.MonkeyPatch):
    """Patch db.select/update + instantly.get_lead + db_tools.add_to_suppression.

    Le caller fournit `queued` (rows messages) et `leads` (lead_id -> dict|None).
    """
    from src import supabase_client
    from src.lib import instantly
    from src.tools import db as db_tools

    spy = _Spy()
    state: dict = {"queued": [], "leads": {}}

    async def fake_select(table, *, params=None):
        assert table == "messages"
        return list(state["queued"])

    async def fake_update(table, patch_, *, filters):
        spy.updates.append((table, patch_, filters))
        return [{}]

    async def fake_get_lead(lead_id):
        return state["leads"].get(lead_id)

    async def fake_suppress(*, email=None, domain=None, reason="manual", source=None, notes=None):
        spy.suppressions.append({"email": email, "reason": reason, "source": source})
        return True

    monkeypatch.setattr(supabase_client, "select", fake_select)
    monkeypatch.setattr(supabase_client, "update", fake_update)
    monkeypatch.setattr(instantly, "get_lead", fake_get_lead)
    monkeypatch.setattr(db_tools, "add_to_suppression", fake_suppress)
    return spy, state


@pytest.mark.asyncio
async def test_sync_flips_sent(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "a@x.com")]
    state["leads"] = {"lead1": {"email_sent_count": 1}}

    out = await sync_send_status(SyncStatusIn())

    assert out.flipped_sent == 1
    assert out.processed == 1
    assert spy.updates and spy.updates[0][1]["status"] == "sent"
    assert "sent_at" in spy.updates[0][1]
    assert not spy.suppressions


@pytest.mark.asyncio
async def test_sync_bounce_suppresses(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "dead@x.com")]
    state["leads"] = {"lead1": {"is_bounced": True}}

    out = await sync_send_status(SyncStatusIn())

    assert out.flipped_bounced == 1
    assert out.suppressed == 1
    assert spy.updates[0][1]["status"] == "bounced"
    assert "bounced_at" in spy.updates[0][1]
    assert spy.suppressions[0] == {"email": "dead@x.com", "reason": "hard_bounce", "source": "instantly_sync"}


@pytest.mark.asyncio
async def test_sync_unsubscribe_suppresses_and_opts_out_contact(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "u@x.com", contact_id="c1")]
    state["leads"] = {"lead1": {"status": -2}}

    out = await sync_send_status(SyncStatusIn())

    assert out.unsubscribed == 1
    assert out.suppressed == 1
    statuses = [p["status"] for (_, p, _) in spy.updates]
    assert "sent" in statuses
    assert any(f.get("id") == "eq.c1" and p.get("status") == "opted_out" for (_, p, f) in spy.updates)
    assert spy.suppressions[0]["reason"] == "opt_out"


@pytest.mark.asyncio
async def test_sync_pending_does_nothing(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "a@x.com")]
    state["leads"] = {"lead1": {"status": 1}}  # Active, rien envoye

    out = await sync_send_status(SyncStatusIn())

    assert out.still_pending == 1
    assert not spy.updates
    assert not spy.suppressions


@pytest.mark.asyncio
async def test_sync_not_found_does_nothing(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "missing-lead", "a@x.com")]
    state["leads"] = {}  # get_lead retourne None

    out = await sync_send_status(SyncStatusIn())

    assert out.not_found == 1
    assert not spy.updates


@pytest.mark.asyncio
async def test_sync_dry_run_writes_nothing(_patch_io) -> None:
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "dead@x.com")]
    state["leads"] = {"lead1": {"is_bounced": True}}

    out = await sync_send_status(SyncStatusIn(dry_run=True))

    assert out.dry_run is True
    assert out.flipped_bounced == 1   # compte
    assert not spy.updates            # mais rien ecrit
    assert not spy.suppressions


@pytest.mark.asyncio
async def test_sync_instantly_error_counts_as_error(_patch_io, monkeypatch) -> None:
    from src.lib import instantly
    from src.tools.send_status import SyncStatusIn, sync_send_status
    spy, state = _patch_io
    state["queued"] = [_msg("m1", "lead1", "a@x.com")]

    async def boom(lead_id):
        raise instantly.InstantlyError("boom")
    monkeypatch.setattr(instantly, "get_lead", boom)

    out = await sync_send_status(SyncStatusIn())
    assert out.errors == 1
    assert not spy.updates


# =====================================================================
# get_lead — endpoint Instantly
# =====================================================================

class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.mark.asyncio
async def test_get_lead_ok() -> None:
    from src.lib import instantly
    captured = {}

    async def fake_get(self, url, *args, **kwargs):
        captured["url"] = url
        return _FakeResponse(200, {"id": "lead1", "status": 3})

    with patch.object(httpx.AsyncClient, "get", fake_get):
        lead = await instantly.get_lead("lead1")

    assert captured["url"].endswith("/api/v2/leads/lead1")
    assert lead["status"] == 3


@pytest.mark.asyncio
async def test_get_lead_404_returns_none() -> None:
    from src.lib import instantly

    async def fake_get(self, url, *args, **kwargs):
        return _FakeResponse(404, text="not found")

    with patch.object(httpx.AsyncClient, "get", fake_get):
        lead = await instantly.get_lead("missing")
    assert lead is None


@pytest.mark.asyncio
async def test_get_lead_4xx_raises() -> None:
    from src.lib import instantly

    async def fake_get(self, url, *args, **kwargs):
        return _FakeResponse(400, text='{"error":"bad"}')

    with patch.object(httpx.AsyncClient, "get", fake_get):
        with pytest.raises(instantly.InstantlyError, match="status 400"):
            await instantly.get_lead("x")


@pytest.mark.asyncio
async def test_get_lead_empty_id_raises() -> None:
    from src.lib import instantly
    with pytest.raises(instantly.InstantlyError, match="lead_id vide"):
        await instantly.get_lead("")
