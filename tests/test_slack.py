"""Tests for the Slack Incoming Webhook helper.

The library MUST be fail-safe: if SLACK_WEBHOOK_URL is unset, every notify
call is a silent no-op (returns False) and never raises. The pipeline should
never break because Slack is down or unconfigured.

We also pin the Block Kit format shapes used by WF-7 hot-lead/review pings
and WF-8 booked pings, so they don't accidentally lose required fields on
refactor.
"""
from __future__ import annotations

import os

import httpx
import pytest


@pytest.fixture(autouse=True)
def _clear_slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_BOOKINGS", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_LEADS", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_ALERTS", raising=False)


# =====================================================================
# notify_sync — no webhook configured
# =====================================================================

def test_notify_sync_returns_false_when_no_webhook_configured() -> None:
    """Silent no-op when env var missing — pipeline must not break."""
    from src.lib.slack import notify_sync
    assert notify_sync(text="anything", context="t") is False


def test_notify_sync_returns_false_when_webhook_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "   ")
    from src.lib.slack import notify_sync
    assert notify_sync(text="anything") is False


# =====================================================================
# notify_sync — webhook configured
# =====================================================================

def test_notify_sync_posts_to_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When configured, notify POSTs JSON to the webhook URL."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test/abc")
    calls = {"posts": []}

    class _FakeResp:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        calls["posts"].append({"url": url, "json": json})
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    from src.lib.slack import notify_sync
    ok = notify_sync(text="hi", context="t")
    assert ok is True
    assert len(calls["posts"]) == 1
    assert calls["posts"][0]["url"] == "https://hooks.slack.com/test/abc"
    assert calls["posts"][0]["json"] == {"text": "hi"}


def test_notify_sync_swallows_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack returning 500 should NOT raise — return False so pipeline continues."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test/abc")

    class _FakeResp:
        status_code = 500
        text = "internal error"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp())
    from src.lib.slack import notify_sync
    assert notify_sync(text="hi") is False


def test_notify_sync_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network exception → no raise, return False."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test/abc")

    def boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "post", boom)
    from src.lib.slack import notify_sync
    assert notify_sync(text="hi") is False


# =====================================================================
# build_*_blocks — pin the Block Kit shapes used by WF-7 / WF-8
# =====================================================================

def test_build_hot_lead_blocks_returns_fallback_and_blocks() -> None:
    from src.lib.slack import build_hot_lead_blocks
    fb, blocks = build_hot_lead_blocks(
        contact_name="Anne T.",
        company_name="Clinique X",
        contact_email="anne@x.com",
        reply_preview="Oui dispo mercredi",
        auto_reply_sent=True,
        confidence=0.92,
    )
    assert isinstance(fb, str) and len(fb) > 0
    assert "Anne T." in fb
    assert "Clinique X" in fb
    # 4 blocks expected: header, fields, status section, preview
    assert len(blocks) == 4
    assert blocks[0]["type"] == "header"


def test_build_hot_lead_blocks_truncates_long_preview() -> None:
    """Preview > 400 chars must be truncated so Slack message doesn't break."""
    from src.lib.slack import build_hot_lead_blocks
    long_text = "x" * 1000
    fb, blocks = build_hot_lead_blocks(
        contact_name="A", company_name="B", contact_email="a@b.com",
        reply_preview=long_text, auto_reply_sent=False, confidence=None,
    )
    preview_block = blocks[-1]
    preview_text = preview_block["text"]["text"]
    # Must contain ellipsis marker for truncation
    assert "…" in preview_text


def test_build_review_blocks_includes_category_and_reasoning() -> None:
    from src.lib.slack import build_review_blocks
    fb, blocks = build_review_blocks(
        contact_name="Anne",
        company_name="Co",
        contact_email="a@b.com",
        category="other",
        confidence=0.4,
        reasoning="ambiguous reply",
        reply_preview="hmm peut etre",
    )
    body = " ".join(str(b) for b in blocks)
    assert "other" in body
    assert "ambiguous reply" in body
    assert "40%" in body  # confidence formatted as percent


def test_build_booked_blocks_includes_meeting_url_when_provided() -> None:
    from src.lib.slack import build_booked_blocks
    fb, blocks = build_booked_blocks(
        contact_name="Anne",
        company_name="Co X",
        contact_email="anne@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
        meeting_url="https://cal.com/ev/abc",
        event_type="25 min discovery",
    )
    body = " ".join(str(b) for b in blocks)
    assert "anne@x.com" in body
    assert "https://cal.com/ev/abc" in body
    assert "25 min discovery" in body


def test_build_booked_blocks_works_without_optional_fields() -> None:
    """Minimum required: contact name + meeting start. Rest is optional."""
    from src.lib.slack import build_booked_blocks
    fb, blocks = build_booked_blocks(
        contact_name="Anne",
        company_name=None,
        contact_email=None,
        meeting_start_iso="2026-05-28T18:00:00Z",
    )
    assert "Anne" in fb
    assert isinstance(blocks, list) and len(blocks) >= 1


# =====================================================================
# Category routing — SLACK_WEBHOOK_BOOKINGS / LEADS / ALERTS
# =====================================================================

def _capture_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch httpx.post to record where notify_sync sent its payload."""
    calls: list[dict] = []

    class _FakeResp:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_category_routes_to_dedicated_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """When SLACK_WEBHOOK_BOOKINGS is set, category='bookings' uses it."""
    monkeypatch.setenv("SLACK_WEBHOOK_BOOKINGS", "https://hooks.slack.com/booked")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/legacy")
    calls = _capture_posts(monkeypatch)
    from src.lib.slack import notify_sync
    assert notify_sync(text="hi", category="bookings") is True
    assert calls[0]["url"] == "https://hooks.slack.com/booked"


def test_category_falls_back_to_legacy_when_specific_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """If SLACK_WEBHOOK_LEADS is missing, category='leads' falls back to SLACK_WEBHOOK_URL."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/legacy")
    calls = _capture_posts(monkeypatch)
    from src.lib.slack import notify_sync
    assert notify_sync(text="hi", category="leads") is True
    assert calls[0]["url"] == "https://hooks.slack.com/legacy"


def test_no_category_uses_legacy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backwards-compat : calls without category= still go to SLACK_WEBHOOK_URL."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/legacy")
    monkeypatch.setenv("SLACK_WEBHOOK_BOOKINGS", "https://hooks.slack.com/booked")
    calls = _capture_posts(monkeypatch)
    from src.lib.slack import notify_sync
    assert notify_sync(text="hi") is True
    assert calls[0]["url"] == "https://hooks.slack.com/legacy"


def test_three_categories_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """bookings/leads/alerts each route to their own webhook URL."""
    monkeypatch.setenv("SLACK_WEBHOOK_BOOKINGS", "https://hooks.slack.com/b")
    monkeypatch.setenv("SLACK_WEBHOOK_LEADS", "https://hooks.slack.com/l")
    monkeypatch.setenv("SLACK_WEBHOOK_ALERTS", "https://hooks.slack.com/a")
    calls = _capture_posts(monkeypatch)
    from src.lib.slack import notify_sync
    notify_sync(text="x", category="bookings")
    notify_sync(text="y", category="leads")
    notify_sync(text="z", category="alerts")
    assert [c["url"] for c in calls] == [
        "https://hooks.slack.com/b",
        "https://hooks.slack.com/l",
        "https://hooks.slack.com/a",
    ]


def test_no_config_at_all_is_silent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars set + category= → still silent no-op, no crash."""
    calls = _capture_posts(monkeypatch)
    from src.lib.slack import notify_sync
    assert notify_sync(text="x", category="bookings") is False
    assert calls == []
