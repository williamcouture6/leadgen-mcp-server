"""Tests for the Instantly wrapper retry behavior.

The intent (documented in lib/instantly.py) is :
  - Retry only on truly transient httpx network errors (timeouts, connection drops).
  - Do NOT retry on 4xx/5xx HTTP status codes (Instantly returned, no point hammering).
  - Surface InstantlyError to the caller in both cases.

The previous code wrapped the call in `try/except httpx.HTTPError → raise InstantlyError`,
which neutralized tenacity (predicate never matched). These tests pin the new behavior.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _instantly_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANTLY_API_KEY", "test-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "test-campaign-id")


class _FakeResponse:
    """Minimal httpx.Response double for status_code + json + text."""

    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


@pytest.mark.asyncio
async def test_add_lead_retries_on_transient_then_succeeds() -> None:
    """Transient httpx error → tenacity retries up to 3 attempts → succeeds."""
    from src.lib import instantly

    calls = {"n": 0}

    async def fake_post(self, url, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("simulated network drop")
        return _FakeResponse(200, {"id": "lead-123"})

    with patch.object(httpx.AsyncClient, "post", fake_post):
        result = await instantly.add_lead_to_campaign(
            email="t@example.com", subject="s", body_text="b",
        )

    assert calls["n"] == 3, f"expected 3 attempts (2 fails + 1 success), got {calls['n']}"
    assert result == {"id": "lead-123"}


@pytest.mark.asyncio
async def test_add_lead_does_not_retry_on_4xx() -> None:
    """4xx → no retry, raise InstantlyError immediately."""
    from src.lib import instantly

    calls = {"n": 0}

    async def fake_post(self, url, *args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(401, {"error": "unauthorized"}, text="unauthorized")

    with patch.object(httpx.AsyncClient, "post", fake_post):
        with pytest.raises(instantly.InstantlyError, match="status 401"):
            await instantly.add_lead_to_campaign(
                email="t@example.com", subject="s", body_text="b",
            )

    assert calls["n"] == 1, f"expected 1 attempt (no retry on 4xx), got {calls['n']}"


@pytest.mark.asyncio
async def test_add_lead_exhausts_retries_then_raises_instantly_error() -> None:
    """Persistent transient error → 3 attempts → InstantlyError (not raw httpx)."""
    from src.lib import instantly

    calls = {"n": 0}

    async def fake_post(self, url, *args, **kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("persistent timeout")

    with patch.object(httpx.AsyncClient, "post", fake_post):
        with pytest.raises(instantly.InstantlyError, match="after retries"):
            await instantly.add_lead_to_campaign(
                email="t@example.com", subject="s", body_text="b",
            )

    assert calls["n"] == 3, f"expected 3 attempts (all fail), got {calls['n']}"


@pytest.mark.asyncio
async def test_get_campaign_retries_on_transient_then_succeeds() -> None:
    """Same retry contract for the GET helper used by /send/healthcheck."""
    from src.lib import instantly

    calls = {"n": 0}

    async def fake_get(self, url, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectTimeout("transient")
        return _FakeResponse(200, {"id": "test-campaign-id", "name": "Test"})

    with patch.object(httpx.AsyncClient, "get", fake_get):
        result = await instantly.get_campaign()

    assert calls["n"] == 2
    assert result["id"] == "test-campaign-id"


@pytest.mark.asyncio
async def test_get_campaign_does_not_retry_on_5xx() -> None:
    """500 from Instantly → no retry (retry would just re-trigger Instantly bug)."""
    from src.lib import instantly

    calls = {"n": 0}

    async def fake_get(self, url, *args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(503, text="instantly under maintenance")

    with patch.object(httpx.AsyncClient, "get", fake_get):
        with pytest.raises(instantly.InstantlyError, match="status 503"):
            await instantly.get_campaign()

    assert calls["n"] == 1
