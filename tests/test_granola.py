"""Tests for the Granola API client (Part C / WF-9).

Intent :
  - Lever GranolaError si clé absente (échec rapide en config).
  - Lever GranolaNoteNotReady sur 404 GET /notes/{id} (note pas encore prête).
  - Retry tenacity sur 429 / 5xx / erreurs réseau (4 essais max, backoff).
  - Pas de retry sur 4xx autres (401/403/400) — raise GranolaError immédiat.

Tous les appels HTTP sont mockés via monkey-patch de httpx.AsyncClient.request.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest


@pytest.fixture(autouse=True)
def _granola_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_test_key")


class _FakeResp:
    """httpx.Response minimal pour status_code + json + text + raise_for_status."""
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = body
        self.text = text or (json.dumps(body) if body else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://public-api.granola.ai/v1/notes")
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=req,
                response=httpx.Response(self.status_code, text=self.text),
            )


def _patch_request(monkeypatch: pytest.MonkeyPatch, responder):
    """Monkey-patch httpx.AsyncClient.request avec un async callable
    `responder(method, url, **kwargs)` qui retourne un _FakeResp.
    """
    async def fake_request(self, method, url, *args, **kwargs):
        return responder(method, url, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


# ---------------------------------------------------------------------
# Config & auth
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    from src.lib.granola import GranolaError, list_notes
    with pytest.raises(GranolaError):
        await list_notes()


@pytest.mark.asyncio
async def test_list_notes_sends_bearer_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def responder(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        captured["params"] = kwargs.get("params") or {}
        return _FakeResp(200, {"notes": [{"id": "not_1"}], "hasMore": False, "cursor": None})

    _patch_request(monkeypatch, responder)
    from src.lib.granola import list_notes
    out = await list_notes(created_after=datetime(2026, 5, 28, tzinfo=timezone.utc))
    assert out["notes"][0]["id"] == "not_1"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/notes")
    assert captured["headers"]["Authorization"] == "Bearer grn_test_key"
    assert "created_after" in captured["params"]


# ---------------------------------------------------------------------
# 404 → note pas prête
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_note_404_raises_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_request(monkeypatch, lambda m, u, **kw: _FakeResp(404, text="not found"))
    from src.lib.granola import GranolaNoteNotReady, get_note
    with pytest.raises(GranolaNoteNotReady):
        await get_note("not_xyz")


# ---------------------------------------------------------------------
# 401 → pas de retry, GranolaError immédiat
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_401_raises_granola_error_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def responder(method, url, **kw):
        calls["n"] += 1
        return _FakeResp(401, text="bad token")

    _patch_request(monkeypatch, responder)
    from src.lib.granola import GranolaError, list_notes
    with pytest.raises(GranolaError):
        await list_notes()
    # 401 = pas de retry tenacity (pas transient)
    assert calls["n"] == 1


# ---------------------------------------------------------------------
# 429 → retry tenacity, succès après 2 tentatives
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Court-circuite tenacity wait pour que le test reste rapide
    import src.lib.granola as gr
    monkeypatch.setattr(gr._request.retry, "wait", lambda *a, **k: 0)

    calls = {"n": 0}

    def responder(method, url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResp(429, text="rate limit")
        return _FakeResp(200, {"notes": [], "hasMore": False})

    _patch_request(monkeypatch, responder)
    out = await gr.list_notes()
    assert out == {"notes": [], "hasMore": False}
    assert calls["n"] == 3


# ---------------------------------------------------------------------
# Pagination (list_notes_paginated)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_stops_when_hasmore_false(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        _FakeResp(200, {"notes": [{"id": "n1"}, {"id": "n2"}], "hasMore": True, "cursor": "c1"}),
        _FakeResp(200, {"notes": [{"id": "n3"}], "hasMore": False, "cursor": None}),
    ]
    idx = {"i": 0}

    def responder(method, url, **kw):
        r = pages[idx["i"]]
        idx["i"] += 1
        return r

    _patch_request(monkeypatch, responder)
    from src.lib.granola import list_notes_paginated
    all_notes = await list_notes_paginated(max_pages=5)
    assert [n["id"] for n in all_notes] == ["n1", "n2", "n3"]
    assert idx["i"] == 2
