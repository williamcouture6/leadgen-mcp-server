"""Tests for the WF-7 Instantly /emails list + reply endpoints.

Pins:
1. list_emails uses string enum 'received'/'sent' (NOT int 2/1) — Instantly
   v2 rejects int with FST_ERR_VALIDATION (we hit this 2026-05-26).
2. reply_to_email POSTs to /api/v2/emails/reply with reply_to_uuid + body.
3. 4xx from Instantly surfaces as InstantlyError immediately (no retry).
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _instantly_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANTLY_API_KEY", "test-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "test-campaign-id")


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


# =====================================================================
# list_emails — query params shape
# =====================================================================

@pytest.mark.asyncio
async def test_list_emails_passes_string_enum_received() -> None:
    """email_type must be string 'received', not int — Instantly v2 rejects int."""
    from src.lib import instantly
    captured = {}

    async def fake_get(self, url, *args, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _FakeResponse(200, {"items": []})

    with patch.object(httpx.AsyncClient, "get", fake_get):
        await instantly.list_emails(email_type="received", limit=10)

    assert captured["url"].endswith("/api/v2/emails")
    params = captured["params"]
    assert params["email_type"] == "received", (
        f"email_type must be string, got {params['email_type']!r} ({type(params['email_type']).__name__})"
    )
    assert params["limit"] == 10


@pytest.mark.asyncio
async def test_list_emails_includes_cursor_and_filters() -> None:
    from src.lib import instantly
    captured = {}

    async def fake_get(self, url, *args, **kwargs):
        captured["params"] = kwargs.get("params")
        return _FakeResponse(200, {"items": []})

    with patch.object(httpx.AsyncClient, "get", fake_get):
        await instantly.list_emails(
            email_type="received",
            limit=50,
            starting_after="cursor-abc",
            campaign_id="camp-1",
            eaccount="william@couture-ia.com",
        )

    p = captured["params"]
    assert p["starting_after"] == "cursor-abc"
    assert p["campaign_id"] == "camp-1"
    assert p["eaccount"] == "william@couture-ia.com"


@pytest.mark.asyncio
async def test_list_emails_raises_on_4xx_immediately() -> None:
    """4xx → InstantlyError, no retries waste."""
    from src.lib import instantly
    calls = {"n": 0}

    async def fake_get(self, url, *args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(400, text='{"error":"bad email_type"}')

    with patch.object(httpx.AsyncClient, "get", fake_get):
        with pytest.raises(instantly.InstantlyError, match="status 400"):
            await instantly.list_emails(email_type="received")

    # No retries on 4xx — the predicate filters out non-transient
    # errors at the level we control (httpx.HTTPError is raised only on
    # network/timeout). 4xx is detected via status_code check in lib.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_list_emails_returns_raw_payload() -> None:
    from src.lib import instantly

    async def fake_get(self, url, *args, **kwargs):
        return _FakeResponse(200, {"items": [{"id": "u1"}, {"id": "u2"}]})

    with patch.object(httpx.AsyncClient, "get", fake_get):
        resp = await instantly.list_emails(email_type="received")

    assert "items" in resp
    assert len(resp["items"]) == 2


# =====================================================================
# reply_to_email — POST to /api/v2/emails/reply
# =====================================================================

@pytest.mark.asyncio
async def test_reply_to_email_posts_required_fields() -> None:
    from src.lib import instantly
    captured = {}

    async def fake_post(self, url, *args, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(200, {"id": "sent-reply-123"})

    with patch.object(httpx.AsyncClient, "post", fake_post):
        res = await instantly.reply_to_email(
            reply_to_uuid="inbound-uuid",
            eaccount="william@couture-ia.com",
            subject="Re: votre message",
            body_text="Parfait, on se parle mercredi.",
        )

    assert captured["url"].endswith("/api/v2/emails/reply")
    body = captured["json"]
    assert body["reply_to_uuid"] == "inbound-uuid"
    assert body["eaccount"] == "william@couture-ia.com"
    assert body["subject"] == "Re: votre message"
    assert body["body"]["text"] == "Parfait, on se parle mercredi."
    assert res == {"id": "sent-reply-123"}


@pytest.mark.asyncio
async def test_reply_to_email_includes_html_when_provided() -> None:
    from src.lib import instantly
    captured = {}

    async def fake_post(self, url, *args, **kwargs):
        captured["json"] = kwargs.get("json")
        return _FakeResponse(200, {"id": "ok"})

    with patch.object(httpx.AsyncClient, "post", fake_post):
        await instantly.reply_to_email(
            reply_to_uuid="u1",
            eaccount="w@c.com",
            subject="s",
            body_text="plain",
            body_html="<p>rich</p>",
        )

    assert captured["json"]["body"]["html"] == "<p>rich</p>"


@pytest.mark.asyncio
async def test_reply_to_email_raises_on_4xx() -> None:
    """If Instantly rejects (e.g. eaccount not in workspace), raise."""
    from src.lib import instantly

    async def fake_post(self, url, *args, **kwargs):
        return _FakeResponse(400, text='{"error":"eaccount not found"}')

    with patch.object(httpx.AsyncClient, "post", fake_post):
        with pytest.raises(instantly.InstantlyError, match="status 400"):
            await instantly.reply_to_email(
                reply_to_uuid="u1", eaccount="bad@x.com",
                subject="s", body_text="b",
            )
