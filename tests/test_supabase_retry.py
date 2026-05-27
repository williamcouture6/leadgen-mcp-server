"""Tests for supabase_client retry predicate.

The default tenacity predicate retries on ANY exception. We narrowed it to
retry only on transient errors (network + 5xx) so 4xx (NOT NULL violations,
unique violations, FK missing, etc.) surface immediately instead of wasting
3 retries × ~1s = ~6s per request.
"""
from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _make_status_error(code: int) -> httpx.HTTPStatusError:
    """Builds an HTTPStatusError with a fake response carrying status_code."""
    return httpx.HTTPStatusError(
        message=f"{code}", request=None, response=_FakeResp(code),  # type: ignore[arg-type]
    )


# =====================================================================
# Transient errors → retry
# =====================================================================

def test_5xx_is_transient() -> None:
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(_make_status_error(500)) is True
    assert _is_transient_db_error(_make_status_error(502)) is True
    assert _is_transient_db_error(_make_status_error(503)) is True
    assert _is_transient_db_error(_make_status_error(504)) is True


def test_network_errors_are_transient() -> None:
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(httpx.ConnectError("boom")) is True
    assert _is_transient_db_error(httpx.ConnectTimeout("boom")) is True
    assert _is_transient_db_error(httpx.ReadTimeout("boom")) is True
    assert _is_transient_db_error(httpx.WriteTimeout("boom")) is True
    assert _is_transient_db_error(httpx.PoolTimeout("boom")) is True
    assert _is_transient_db_error(httpx.RemoteProtocolError("boom")) is True


# =====================================================================
# Non-transient → no retry
# =====================================================================

def test_4xx_is_not_transient() -> None:
    """4xx is the caller's fault — retrying won't fix it. Surface immediately."""
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(_make_status_error(400)) is False
    assert _is_transient_db_error(_make_status_error(401)) is False
    assert _is_transient_db_error(_make_status_error(403)) is False
    assert _is_transient_db_error(_make_status_error(404)) is False
    # 409 (unique violation) is critical — WF-7 race condition handling
    # relies on this surfacing immediately, not after 3 retries.
    assert _is_transient_db_error(_make_status_error(409)) is False
    assert _is_transient_db_error(_make_status_error(422)) is False


def test_3xx_is_not_transient() -> None:
    """Redirects shouldn't happen on PostgREST, but if they do, don't retry."""
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(_make_status_error(301)) is False
    assert _is_transient_db_error(_make_status_error(302)) is False


def test_2xx_should_not_appear_as_error_but_is_not_transient() -> None:
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(_make_status_error(200)) is False


def test_arbitrary_exception_is_not_transient() -> None:
    """Random exceptions (TypeError, ValueError, etc.) should NOT retry."""
    from src.supabase_client import _is_transient_db_error
    assert _is_transient_db_error(ValueError("bad input")) is False
    assert _is_transient_db_error(TypeError("nope")) is False
    assert _is_transient_db_error(RuntimeError("oops")) is False
