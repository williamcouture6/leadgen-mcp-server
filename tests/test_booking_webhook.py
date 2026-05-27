"""Tests for WF-8 Cal.com booking webhook handler.

Focus areas:

1. HMAC signature verification — must reject:
   - Missing header
   - Empty secret
   - Wrong signature
   - Malformed signature (non-hex)
   And accept:
   - Correct hex signature
   - Signature with `sha256=` prefix (defensive — some providers prepend it)

2. Cal.com payload extraction across all 4 supported triggers
   (BOOKING_CREATED / RESCHEDULED / CANCELLED / MEETING_ENDED) plus the
   unsupported-trigger ignore path. Pin the shape so a refactor that drops
   `uid`/`attendees`/`metadata` extraction breaks the test, not prod.

3. Orchestrator behavior — verify trigger → outcome / conversation.state
   mapping. We stub the DB layer + Slack lib via monkeypatch since these
   are pure routing decisions that shouldn't require Supabase.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest


@pytest.fixture(autouse=True)
def _booking_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub env vars supabase_client reads at import time."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# verify_calcom_signature
# =====================================================================

class TestVerifyCalcomSignature:
    """HMAC verification — defensive against malformed/missing inputs."""

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_rejects_missing_header(self) -> None:
        from src.tools.booking import verify_calcom_signature
        assert verify_calcom_signature(b"{}", None, "secret") is False
        assert verify_calcom_signature(b"{}", "", "secret") is False

    def test_rejects_empty_secret(self) -> None:
        from src.tools.booking import verify_calcom_signature
        assert verify_calcom_signature(b"{}", "abc123", "") is False

    def test_rejects_non_hex_signature(self) -> None:
        from src.tools.booking import verify_calcom_signature
        assert verify_calcom_signature(b"{}", "not-hex-!@#", "secret") is False

    def test_rejects_wrong_signature(self) -> None:
        from src.tools.booking import verify_calcom_signature
        body = b'{"trigger":"BOOKING_CREATED"}'
        wrong = self._sign(body, "OTHER_SECRET")
        assert verify_calcom_signature(body, wrong, "REAL_SECRET") is False

    def test_accepts_correct_signature(self) -> None:
        from src.tools.booking import verify_calcom_signature
        body = b'{"trigger":"BOOKING_CREATED"}'
        sig = self._sign(body, "SECRET")
        assert verify_calcom_signature(body, sig, "SECRET") is True

    def test_accepts_sha256_prefix(self) -> None:
        """Some providers prepend `sha256=` — strip and verify."""
        from src.tools.booking import verify_calcom_signature
        body = b'{"x":1}'
        sig = self._sign(body, "SECRET")
        assert verify_calcom_signature(body, f"sha256={sig}", "SECRET") is True

    def test_case_insensitive_hex(self) -> None:
        """Hex compare should be case-insensitive (Cal.com lowercase, but accept upper)."""
        from src.tools.booking import verify_calcom_signature
        body = b'{"x":1}'
        sig = self._sign(body, "SECRET").upper()
        assert verify_calcom_signature(body, sig, "SECRET") is True


# =====================================================================
# extract_from_calcom_webhook
# =====================================================================

def _booking_created_payload(**overrides) -> dict:
    """Minimum Cal.com BOOKING_CREATED payload (shape v2)."""
    base = {
        "triggerEvent": "BOOKING_CREATED",
        "createdAt": "2026-05-28T17:00:00Z",
        "payload": {
            "uid": "cal-uid-abc-123",
            "title": "30 Min Meeting between William and Anne",
            "type": "30 Min Meeting",
            "startTime": "2026-05-30T18:00:00Z",
            "endTime": "2026-05-30T18:30:00Z",
            "organizer": {
                "email": "william@couture-ia.com",
                "name": "William Couture",
            },
            "attendees": [{
                "email": "anne@clinique-x.com",
                "name": "Anne Tremblay",
            }],
            "status": "ACCEPTED",
            "metadata": {
                "videoCallUrl": "https://meet.google.com/abc-def-ghi",
            },
        },
    }
    base["payload"].update(overrides)
    return base


class TestExtractFromCalcomWebhook:
    def test_extracts_minimal_booking_created(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        out = extract_from_calcom_webhook(_booking_created_payload())
        assert out is not None
        assert out.trigger == "BOOKING_CREATED"
        assert out.external_event_id == "cal-uid-abc-123"
        assert out.attendee_email == "anne@clinique-x.com"
        assert out.attendee_name == "Anne Tremblay"
        assert out.organizer_email == "william@couture-ia.com"
        assert out.start_time_iso == "2026-05-30T18:00:00Z"
        assert out.meeting_url == "https://meet.google.com/abc-def-ghi"
        assert out.event_type_title == "30 Min Meeting"

    def test_lowercases_emails(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        body["payload"]["attendees"][0]["email"] = "ANNE@Clinique-X.COM"
        body["payload"]["organizer"]["email"] = "WILLIAM@Couture-IA.COM"
        out = extract_from_calcom_webhook(body)
        assert out.attendee_email == "anne@clinique-x.com"
        assert out.organizer_email == "william@couture-ia.com"

    def test_returns_none_for_missing_trigger(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        assert extract_from_calcom_webhook({"payload": {"uid": "x"}}) is None

    def test_returns_none_for_missing_payload(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        assert extract_from_calcom_webhook({"triggerEvent": "BOOKING_CREATED"}) is None

    def test_synthesizes_uid_from_bookingid_and_start_when_missing(self) -> None:
        """Fallback uid when Cal.com payload shape doesn't include uid (defensive)."""
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        del body["payload"]["uid"]
        body["payload"]["bookingId"] = 999
        out = extract_from_calcom_webhook(body)
        assert out is not None
        assert "fallback-999" in out.external_event_id
        assert "2026-05-30T18:00:00Z" in out.external_event_id

    def test_returns_none_when_no_uid_and_no_bookingid(self) -> None:
        """No dedup key extractable = refuse (better than silent collision)."""
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        del body["payload"]["uid"]
        assert extract_from_calcom_webhook(body) is None

    def test_extracts_cancellation_reason(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        body["triggerEvent"] = "BOOKING_CANCELLED"
        body["payload"]["cancellationReason"] = "Conflict with another meeting"
        body["payload"]["status"] = "CANCELLED"
        out = extract_from_calcom_webhook(body)
        assert out.trigger == "BOOKING_CANCELLED"
        assert out.cancellation_reason == "Conflict with another meeting"

    def test_ignores_placeholder_location_strings(self) -> None:
        """`integrations:google:meet` is a placeholder, not a URL — skip it."""
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        body["payload"]["metadata"] = {}
        body["payload"]["location"] = "integrations:google:meet"
        out = extract_from_calcom_webhook(body)
        assert out.meeting_url is None

    def test_keeps_http_location_when_metadata_missing(self) -> None:
        from src.tools.booking import extract_from_calcom_webhook
        body = _booking_created_payload()
        body["payload"]["metadata"] = {}
        body["payload"]["location"] = "https://zoom.us/j/123"
        out = extract_from_calcom_webhook(body)
        assert out.meeting_url == "https://zoom.us/j/123"


# =====================================================================
# handle_calcom_booking — orchestrator routing
# =====================================================================

class _StubDB:
    """In-memory stub for supabase_client used by booking.handle_calcom_booking.

    Intercepts select/insert/update calls so we can drive routing logic
    without a real Postgres. Records calls for assertions.
    """

    def __init__(self) -> None:
        self.contacts: list[dict] = []
        self.companies: list[dict] = []
        self.booking_events: list[dict] = []
        self.messages: list[dict] = []
        self.conversations_upserts: list[dict] = []
        self.booking_event_updates: list[tuple[str, dict]] = []

    async def select(self, table: str, *, params: dict | None = None) -> list[dict]:
        params = params or {}
        if table == "contacts":
            email = (params.get("email") or "").removeprefix("eq.")
            if email:
                return [c for c in self.contacts if c["email"] == email][:1]
        if table == "companies":
            cid = (params.get("id") or "").removeprefix("eq.")
            return [c for c in self.companies if c["id"] == cid][:1]
        if table == "booking_events":
            uid = (params.get("external_event_id") or "").removeprefix("eq.")
            return [b for b in self.booking_events if b.get("external_event_id") == uid][:1]
        if table == "messages":
            cid = (params.get("contact_id") or "").removeprefix("eq.")
            return [m for m in self.messages if m["contact_id"] == cid][:1]
        return []

    async def insert(self, table: str, row, *, on_conflict=None, ignore_duplicates=False) -> list[dict]:
        if table == "booking_events":
            row = {**row, "id": f"be-{len(self.booking_events) + 1}"}
            self.booking_events.append(row)
            return [row]
        if table == "conversations":
            self.conversations_upserts.append(row)
            return [{**row, "id": f"cv-{len(self.conversations_upserts)}"}]
        return [{**row, "id": "stub"}]

    async def update(self, table: str, patch: dict, *, filters: dict) -> list[dict]:
        if table == "booking_events":
            beid = filters.get("id", "").removeprefix("eq.")
            self.booking_event_updates.append((beid, patch))
            for b in self.booking_events:
                if b["id"] == beid:
                    b.update(patch)
                    return [b]
        return []


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> _StubDB:
    stub = _StubDB()
    from src import supabase_client as real_db
    monkeypatch.setattr(real_db, "select", stub.select)
    monkeypatch.setattr(real_db, "insert", stub.insert)
    monkeypatch.setattr(real_db, "update", stub.update)
    return stub


@pytest.fixture
def slack_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture Slack notify calls so tests can assert pings happened."""
    calls: list[dict] = []

    async def fake_notify(*, text, blocks=None, context=None):
        calls.append({"text": text, "blocks": blocks, "context": context})
        return True

    from src.lib import slack
    monkeypatch.setattr(slack, "notify", fake_notify)
    return calls


@pytest.mark.asyncio
async def test_booking_created_inserts_row_and_pings_slack(
    stub_db: _StubDB, slack_calls: list[dict]
) -> None:
    """Happy path : nouveau booking → insert booking_events + slack 'booked'."""
    stub_db.contacts.append({
        "id": "c1", "company_id": "co1", "email": "anne@clinique-x.com",
        "first_name": "Anne", "last_name": "Tremblay",
    })
    stub_db.companies.append({"id": "co1", "name": "Clinique X", "city": "Sherbrooke"})

    from src.tools.booking import extract_from_calcom_webhook, handle_calcom_booking
    extracted = extract_from_calcom_webhook(_booking_created_payload())
    out = await handle_calcom_booking(extracted)

    assert out.status == "ok"
    assert out.trigger == "BOOKING_CREATED"
    assert out.contact_id == "c1"
    assert "booking_event_inserted" in out.actions_taken
    assert any("conversation_state=booked" in a for a in out.actions_taken)
    assert "slack_booked" in out.actions_taken

    assert len(stub_db.booking_events) == 1
    be = stub_db.booking_events[0]
    assert be["external_event_id"] == "cal-uid-abc-123"
    assert be["booking_provider"] == "cal.com"
    assert be["meeting_scheduled_for"] == "2026-05-30T18:00:00Z"
    assert be["contact_id"] == "c1"

    assert len(slack_calls) == 1
    assert "anne@clinique-x.com" in str(slack_calls[0])


@pytest.mark.asyncio
async def test_booking_cancelled_marks_outcome_and_pings(
    stub_db: _StubDB, slack_calls: list[dict]
) -> None:
    """BOOKING_CANCELLED → meeting_outcome='cancelled', state='cold', Slack ping."""
    stub_db.contacts.append({
        "id": "c2", "company_id": None, "email": "x@y.com",
        "first_name": "X", "last_name": "Y",
    })
    # Booking already inserted (CREATED came before CANCELLED)
    stub_db.booking_events.append({
        "id": "be-existing", "external_event_id": "uid-1",
        "contact_id": "c2", "booked_at": "2026-05-26T10:00:00Z",
        "meeting_scheduled_for": "2026-05-30T18:00:00Z",
        "meeting_outcome": None,
    })

    body = _booking_created_payload()
    body["triggerEvent"] = "BOOKING_CANCELLED"
    body["payload"]["uid"] = "uid-1"
    body["payload"]["attendees"][0]["email"] = "x@y.com"
    body["payload"]["cancellationReason"] = "Conflict"

    from src.tools.booking import extract_from_calcom_webhook, handle_calcom_booking
    extracted = extract_from_calcom_webhook(body)
    out = await handle_calcom_booking(extracted)

    assert out.status == "ok"
    assert out.trigger == "BOOKING_CANCELLED"
    assert any("conversation_state=cold" in a for a in out.actions_taken)
    assert "slack_cancelled" in out.actions_taken

    assert stub_db.booking_event_updates
    _, patch = stub_db.booking_event_updates[0]
    assert patch["meeting_outcome"] == "cancelled"

    assert len(slack_calls) == 1
    assert "Conflict" in slack_calls[0]["text"]


@pytest.mark.asyncio
async def test_duplicate_booking_created_is_skipped(
    stub_db: _StubDB, slack_calls: list[dict]
) -> None:
    """Retry from Cal.com (same uid, same trigger) → skip, no duplicate Slack ping."""
    stub_db.contacts.append({
        "id": "c1", "company_id": None, "email": "anne@clinique-x.com",
        "first_name": "Anne", "last_name": "T",
    })
    stub_db.booking_events.append({
        "id": "be-1", "external_event_id": "cal-uid-abc-123",
        "contact_id": "c1", "booked_at": "2026-05-26T10:00:00Z",
        "meeting_scheduled_for": "2026-05-30T18:00:00Z",
        "meeting_outcome": None,
    })

    from src.tools.booking import extract_from_calcom_webhook, handle_calcom_booking
    extracted = extract_from_calcom_webhook(_booking_created_payload())
    out = await handle_calcom_booking(extracted)

    assert out.status == "skipped_duplicate"
    assert len(slack_calls) == 0


@pytest.mark.asyncio
async def test_orphan_booking_pings_slack_but_skips_db(
    stub_db: _StubDB, slack_calls: list[dict]
) -> None:
    """Attendee not in DB → still ping Slack so William can react manually."""
    from src.tools.booking import extract_from_calcom_webhook, handle_calcom_booking
    extracted = extract_from_calcom_webhook(_booking_created_payload())
    out = await handle_calcom_booking(extracted)

    assert out.status == "skipped_no_contact"
    assert "orphan_logged" in out.actions_taken
    assert "slack_ping" in out.actions_taken
    assert len(slack_calls) == 1
    assert "introuvable" in slack_calls[0]["text"]
    assert len(stub_db.booking_events) == 0


@pytest.mark.asyncio
async def test_unsupported_trigger_is_ignored(
    stub_db: _StubDB, slack_calls: list[dict]
) -> None:
    """RECORDING_READY etc. → return ignored, no Slack ping, no DB writes."""
    from src.tools.booking import (
        CalcomBookingPayload,
        handle_calcom_booking,
    )
    payload = CalcomBookingPayload(
        trigger="RECORDING_READY",
        external_event_id="uid-x",
        attendee_email="anne@x.com",
    )
    out = await handle_calcom_booking(payload)
    assert out.status == "ignored_unsupported_trigger"
    assert len(slack_calls) == 0
    assert len(stub_db.booking_events) == 0
