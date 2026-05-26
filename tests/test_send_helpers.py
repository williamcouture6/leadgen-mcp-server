"""Tests for the send.py helpers that govern the daily cap window and
suppression check.

The send pipeline (WF-6) has 3 critical helpers whose semantics MUST
not drift unintentionally :

1. _daily_cap()           — read INSTANTLY_DAILY_CAP env, fall back to 10.
2. _today_start_utc_iso() — start-of-day in America/Toronto (NOT UTC), so
                            "j'ai envoyé 10 aujourd'hui" matches human
                            expectation through DST changes.
3. _is_suppressed()       — block send if email or company domain is in
                            suppression_list (the opt-out / bounce table).

These tests pin the contract. When any of these change, this file fails
and we audit explicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.tools import send


# ---------------- 1. _daily_cap ----------------

def test_daily_cap_defaults_to_10_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INSTANTLY_DAILY_CAP", raising=False)
    assert send._daily_cap() == 10


def test_daily_cap_reads_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANTLY_DAILY_CAP", "25")
    assert send._daily_cap() == 25


def test_daily_cap_falls_back_to_default_on_malformed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INSTANTLY_DAILY_CAP", "not-a-number")
    assert send._daily_cap() == 10


def test_daily_cap_floors_negative_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap négatif = 0 (pas d'envoi). Sanity safe-default."""
    monkeypatch.setenv("INSTANTLY_DAILY_CAP", "-5")
    assert send._daily_cap() == 0


def test_daily_cap_empty_string_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTANTLY_DAILY_CAP", "")
    assert send._daily_cap() == 10


# ---------------- 2. _today_start_utc_iso ----------------

def test_today_start_returns_iso_string() -> None:
    """Format = ISO 8601 UTC parsable par PostgREST."""
    iso = send._today_start_utc_iso()
    # doit être parsable
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None


def test_today_start_is_midnight_in_toronto_timezone() -> None:
    """Le jour démarre à 00:00 Toronto, PAS 00:00 UTC.
    Conséquence : converti UTC, c'est 04:00 ou 05:00 UTC selon DST."""
    iso = send._today_start_utc_iso()
    utc_dt = datetime.fromisoformat(iso)
    # Convertit en local Toronto pour vérif
    local_dt = utc_dt.astimezone(ZoneInfo("America/Toronto"))
    assert local_dt.hour == 0
    assert local_dt.minute == 0
    assert local_dt.second == 0


def test_today_start_handles_dst_summer_period() -> None:
    """En été (EDT, UTC-4), 00:00 Toronto = 04:00 UTC. Le helper doit le gérer."""
    # On ne peut pas mocker date.today facilement dans ce code synchrone, mais
    # on peut au moins vérifier que le résultat est dans une des 2 fenêtres
    # valides (UTC-4 ou UTC-5 selon date du run).
    iso = send._today_start_utc_iso()
    utc_dt = datetime.fromisoformat(iso)
    # H UTC doit être soit 4 (EDT) soit 5 (EST)
    assert utc_dt.hour in (4, 5), f"H UTC inattendu: {utc_dt.hour}"


def test_today_start_constant_within_same_local_day() -> None:
    """Deux appels successifs renvoient la MÊME valeur (pas d'horloge mock,
    juste la conversion start-of-day Toronto)."""
    iso1 = send._today_start_utc_iso()
    iso2 = send._today_start_utc_iso()
    assert iso1 == iso2


# ---------------- 3. _is_suppressed (mocked DB) ----------------

@pytest.mark.asyncio
async def test_is_suppressed_returns_false_when_email_and_domain_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_select(table, *, params=None):
        return []  # rien en DB

    monkeypatch.setattr(send.db, "select", fake_select)
    suppressed, reason = await send._is_suppressed(
        "clean@cafefaro.com", "cafefaro.com"
    )
    assert suppressed is False
    assert reason is None


@pytest.mark.asyncio
async def test_is_suppressed_catches_email_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email exact dans suppression_list → bloqué."""
    async def fake_select(table, *, params=None):
        # Email lookup retourne une row, domain lookup vide
        if params and "email" in params:
            return [{"reason": "opt_out"}]
        return []

    monkeypatch.setattr(send.db, "select", fake_select)
    suppressed, reason = await send._is_suppressed(
        "optedout@cafefaro.com", "cafefaro.com"
    )
    assert suppressed is True
    assert "opt_out" in reason


@pytest.mark.asyncio
async def test_is_suppressed_catches_domain_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain de la company dans suppression_list (spam complaint, ex.) → bloqué
    même si l'email exact n'est pas listé."""
    async def fake_select(table, *, params=None):
        if params and "email" in params:
            return []  # email pas listé
        if params and "domain" in params:
            return [{"reason": "spam_complaint"}]
        return []

    monkeypatch.setattr(send.db, "select", fake_select)
    suppressed, reason = await send._is_suppressed(
        "anyone@badcompany.com", "badcompany.com"
    )
    assert suppressed is True
    assert "spam_complaint" in reason


@pytest.mark.asyncio
async def test_is_suppressed_handles_null_email_or_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pas d'email ni de domain → on ne query rien, OK."""
    calls = {"n": 0}

    async def fake_select(table, *, params=None):
        calls["n"] += 1
        return []

    monkeypatch.setattr(send.db, "select", fake_select)

    suppressed, reason = await send._is_suppressed(None, None)
    assert suppressed is False
    assert reason is None
    assert calls["n"] == 0, "aucune query ne doit être faite quand email+domain absents"


@pytest.mark.asyncio
async def test_is_suppressed_email_takes_precedence_over_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si l'email est suppress, on n'a pas besoin de check le domain
    (court-circuit perf)."""
    domain_called = {"yes": False}

    async def fake_select(table, *, params=None):
        if params and "email" in params:
            return [{"reason": "hard_bounce"}]
        if params and "domain" in params:
            domain_called["yes"] = True
            return [{"reason": "spam_complaint"}]
        return []

    monkeypatch.setattr(send.db, "select", fake_select)
    suppressed, reason = await send._is_suppressed(
        "bounced@cafefaro.com", "cafefaro.com"
    )
    assert suppressed is True
    assert "hard_bounce" in reason
    assert not domain_called["yes"], "domain query inutile si email déjà suppress"


# ---------------- count_pushed_today ----------------

@pytest.mark.asyncio
async def test_count_pushed_today_queries_with_today_start_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vérifie que la query filtre bien sur scheduled_at >= today_start_local
    ET sur status != 'draft' (pour ne compter que les pushes effectifs)."""
    captured = {"params": None}

    async def fake_select(table, *, params=None):
        captured["params"] = params
        return [{"id": "msg-1"}, {"id": "msg-2"}, {"id": "msg-3"}]

    monkeypatch.setattr(send.db, "select", fake_select)
    n = await send.count_pushed_today()
    assert n == 3

    params = captured["params"]
    assert params is not None
    # Filtres essentiels présents
    assert "scheduled_at" in params
    assert params.get("direction") == "eq.outbound"
    assert params.get("status") == "neq.draft"
    # Le filtre scheduled_at est `gte.<iso>` — vérifions que l'iso est UTC
    sched_filter = params["scheduled_at"]
    assert sched_filter.startswith("gte.")
    iso_part = sched_filter[len("gte."):]
    parsed = datetime.fromisoformat(iso_part)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_count_pushed_today_returns_zero_when_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_select(table, *, params=None):
        return []

    monkeypatch.setattr(send.db, "select", fake_select)
    n = await send.count_pushed_today()
    assert n == 0
