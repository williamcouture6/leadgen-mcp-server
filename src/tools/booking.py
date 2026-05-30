"""Tool `booking` — Cal.com Booking Webhook handler (WF-8).

Reçoit un webhook Cal.com (BOOKING_CREATED / BOOKING_RESCHEDULED /
BOOKING_CANCELLED / MEETING_ENDED), valide la signature HMAC, persiste
l'événement dans `booking_events`, met à jour `conversations.state`, et
envoie un ping Slack avec le détail du RDV.

Auth : HMAC-SHA256 du raw body avec `CALCOM_WEBHOOK_SECRET`. Header
`X-Cal-Signature-256` = signature hex (pas de prefix `sha256=`).
Ref: https://cal.com/docs/core-features/webhooks#webhook-signature

Idempotence : Cal.com peut renvoyer le même webhook plusieurs fois (retries
en cas de timeout). On dédoublonne via `external_event_id` (Cal.com UID).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from .. import supabase_client as db
from ..lib import reacti_tickets
from ..lib import slack as slack_lib

# ----------------------------------------------------------------------
# HMAC validation
# ----------------------------------------------------------------------

def verify_calcom_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Vérifie le header `X-Cal-Signature-256` contre HMAC-SHA256(secret, raw_body).

    Constant-time compare via `hmac.compare_digest`. Returns False sur:
      - header absent / vide
      - secret vide
      - signature mal formée (pas hex)
      - mismatch
    """
    if not signature_header or not secret:
        return False
    sig = signature_header.strip()
    # Cal.com envoie la signature hex brute, mais tolérer un prefix éventuel
    # (`sha256=` vu sur d'autres providers).
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    if not re.fullmatch(r"[0-9a-fA-F]+", sig):
        return False
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig.lower(), expected.lower())


# ----------------------------------------------------------------------
# Payload extraction
# ----------------------------------------------------------------------

# Triggers Cal.com qu'on traite explicitement. Les autres (MEETING_STARTED,
# RECORDING_READY, etc.) sont acceptés mais ignored.
SUPPORTED_TRIGGERS = frozenset({
    "BOOKING_CREATED",
    "BOOKING_RESCHEDULED",
    "BOOKING_CANCELLED",
    "MEETING_ENDED",
})


class CalcomBookingPayload(BaseModel):
    """Représentation normalisée d'un événement booking Cal.com.

    On garde le minimum dont la pipeline a besoin — le raw payload reste
    accessible dans `raw` pour audit / debug ultérieur.
    """
    trigger: str  # BOOKING_CREATED, BOOKING_RESCHEDULED, BOOKING_CANCELLED, MEETING_ENDED
    external_event_id: str  # Cal.com `uid` (stable cross-reschedule)
    title: str | None = None
    event_type_title: str | None = None
    start_time_iso: str | None = None  # ISO 8601
    end_time_iso: str | None = None
    organizer_email: str | None = None
    attendee_email: str | None = None
    attendee_name: str | None = None
    meeting_url: str | None = None  # Google Meet / Zoom link
    status: str | None = None  # ACCEPTED, CANCELLED, etc.
    cancellation_reason: str | None = None
    raw: dict[str, Any] | None = None


def _first_attendee(attendees: Any) -> dict[str, Any]:
    if isinstance(attendees, list) and attendees:
        head = attendees[0]
        if isinstance(head, dict):
            return head
    return {}


def extract_from_calcom_webhook(body: dict[str, Any]) -> CalcomBookingPayload | None:
    """Convertit le payload brut Cal.com en CalcomBookingPayload.

    Cal.com v2 shape:
      {
        "triggerEvent": "BOOKING_CREATED",
        "createdAt": "...",
        "payload": {
          "uid": "abc...", "title": "...", "type": "30 Min Meeting",
          "startTime": "...", "endTime": "...",
          "organizer": {"email": "...", "name": "..."},
          "attendees": [{"email": "...", "name": "..."}],
          "status": "ACCEPTED",
          "metadata": {"videoCallUrl": "https://meet.google.com/..."},
          "location": "https://..." | "integrations:google:meet",
          "cancellationReason": "..."  # sur BOOKING_CANCELLED
        }
      }

    Returns None si trigger inconnu OU pas d'UID extractible (impossible
    de dédup correctement → on refuse).
    """
    trigger = (body.get("triggerEvent") or body.get("trigger") or "").strip()
    if not trigger:
        return None

    p = body.get("payload") or {}
    if not isinstance(p, dict):
        return None

    # UID Cal.com — stable across reschedule (le bookingId change, l'uid non)
    uid = str(
        p.get("uid")
        or p.get("bookingUid")
        or p.get("booking_uid")
        or ""
    ).strip()
    if not uid:
        # Fallback : combiner bookingId + start pour éviter de tout rejeter si
        # Cal.com change le shape. Pas idéal (un reschedule = new row) mais
        # mieux qu'un crash silencieux.
        bid = p.get("bookingId") or p.get("id")
        start = p.get("startTime")
        if bid and start:
            uid = f"fallback-{bid}-{start}"
        else:
            return None

    organizer = p.get("organizer") if isinstance(p.get("organizer"), dict) else {}
    attendee = _first_attendee(p.get("attendees"))

    meeting_url = None
    metadata = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
    if metadata:
        meeting_url = (
            metadata.get("videoCallUrl")
            or metadata.get("hangoutLink")
            or metadata.get("location")
        )
    if not meeting_url:
        loc = p.get("location")
        # Cal.com peut renvoyer "integrations:google:meet" (placeholder) ou un
        # vrai URL — ne garder que les URLs http(s).
        if isinstance(loc, str) and loc.startswith("http"):
            meeting_url = loc

    return CalcomBookingPayload(
        trigger=trigger,
        external_event_id=uid,
        title=p.get("title"),
        event_type_title=p.get("type") or (p.get("eventType") or {}).get("title"),
        start_time_iso=p.get("startTime") or p.get("start"),
        end_time_iso=p.get("endTime") or p.get("end"),
        organizer_email=(organizer.get("email") or "").strip().lower() or None,
        attendee_email=(attendee.get("email") or "").strip().lower() or None,
        attendee_name=attendee.get("name"),
        meeting_url=meeting_url,
        status=p.get("status"),
        cancellation_reason=p.get("cancellationReason") or p.get("cancellation_reason"),
        raw=body,
    )


# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------

async def _find_contact_by_email(email: str) -> dict[str, Any] | None:
    if not email:
        return None
    rows = await db.select(
        "contacts",
        params={
            "select": "id,company_id,first_name,last_name,email",
            "email": f"eq.{email}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _get_company(company_id: str) -> dict[str, Any] | None:
    rows = await db.select(
        "companies",
        params={
            # research_json + industry/google_types alimentent le brief pré-RDV
            # Slack (WF-8) — google_types sert à résoudre la verticale REACTI.
            "select": "id,name,city,icp_segment,industry,google_types,research_json",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _find_booking_event_by_uid(external_event_id: str) -> dict[str, Any] | None:
    if not external_event_id:
        return None
    rows = await db.select(
        "booking_events",
        params={
            "select": "id,contact_id,booked_at,meeting_scheduled_for,meeting_outcome,external_event_id",
            "external_event_id": f"eq.{external_event_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _find_last_outbound_message(contact_id: str) -> dict[str, Any] | None:
    """Trouve le dernier message outbound vers ce contact — utilisé comme
    `triggered_by_message_id`. Sans correspondance, on omet le champ (FK nullable).
    """
    rows = await db.select(
        "messages",
        params={
            "select": "id,campaign_id,sent_at",
            "contact_id": f"eq.{contact_id}",
            "direction": "eq.outbound",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _upsert_conversation_state(
    *,
    contact_id: str,
    campaign_id: str | None,
    state: str,
) -> None:
    """Met à jour conversations.state — non-bloquant si échec.

    `last_direction='inbound'` + `last_channel='email'` : sémantiquement
    le booking est une action du lead déclenchée par notre CTA email.
    Les enums DB (`message_direction`, `channel`) ne tolèrent pas d'autres
    valeurs (pas de 'system'/'calendar').
    """
    row: dict[str, Any] = {
        "contact_id": contact_id,
        "campaign_id": campaign_id,
        "state": state,
        "last_direction": "inbound",
        "last_channel": "email",
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.insert(
            "conversations", row,
            on_conflict="contact_id,campaign_id",
            ignore_duplicates=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[wf8] upsert conversation failed: {e!r}")


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class HandleBookingOut(BaseModel):
    status: Literal[
        "ok",
        "ignored_unsupported_trigger",
        "ignored_no_attendee",
        "skipped_no_contact",
        "skipped_duplicate",
        "error",
    ]
    trigger: str | None = None
    booking_event_id: str | None = None
    contact_id: str | None = None
    actions_taken: list[str] = []
    error_text: str | None = None
    duration_ms: int | None = None


async def handle_calcom_booking(payload: CalcomBookingPayload) -> HandleBookingOut:
    """Orchestrateur principal — persiste l'événement et ping Slack."""
    started = time.monotonic()
    actions: list[str] = []

    if payload.trigger not in SUPPORTED_TRIGGERS:
        return HandleBookingOut(
            status="ignored_unsupported_trigger",
            trigger=payload.trigger,
            actions_taken=["unsupported_trigger"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    if not payload.attendee_email:
        # Sans attendee, impossible de lier à un contact. On accepte mais log.
        return HandleBookingOut(
            status="ignored_no_attendee",
            trigger=payload.trigger,
            actions_taken=["no_attendee_email"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 1) Trouve le contact
    contact = await _find_contact_by_email(payload.attendee_email)
    if not contact:
        # Booking d'un email pas dans notre DB (peut arriver : referral, test
        # manuel, lien partagé). On Slack ping quand même pour ne pas rater l'event.
        await slack_lib.notify(
            text=(
                f"📅 Booking Cal.com pour {payload.attendee_email} — "
                f"contact introuvable en DB (trigger={payload.trigger})"
            ),
            context="wf8_orphan_booking",
            category="alerts",
        )
        return HandleBookingOut(
            status="skipped_no_contact",
            trigger=payload.trigger,
            actions_taken=["orphan_logged", "slack_ping"],
            error_text=f"no contact for {payload.attendee_email}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    contact_id = contact["id"]
    company = await _get_company(contact["company_id"]) if contact.get("company_id") else None

    # 2) Lookup booking_events existant (dédup par UID Cal.com)
    existing = await _find_booking_event_by_uid(payload.external_event_id)

    # 3) Map trigger → outcome / state
    now_iso = datetime.now(timezone.utc).isoformat()
    if payload.trigger == "BOOKING_CREATED":
        booked_at = now_iso
        meeting_outcome = None
        conversation_state = "booked"
    elif payload.trigger == "BOOKING_RESCHEDULED":
        booked_at = (existing or {}).get("booked_at") or now_iso
        meeting_outcome = "rescheduled"
        conversation_state = "booked"
    elif payload.trigger == "BOOKING_CANCELLED":
        booked_at = (existing or {}).get("booked_at")
        meeting_outcome = "cancelled"
        conversation_state = "cold"
    elif payload.trigger == "MEETING_ENDED":
        booked_at = (existing or {}).get("booked_at") or now_iso
        meeting_outcome = "held"
        conversation_state = "booked"
    else:
        # Defensive — déjà filtré par SUPPORTED_TRIGGERS plus haut.
        return HandleBookingOut(
            status="ignored_unsupported_trigger",
            trigger=payload.trigger,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 4) Insert ou update booking_events
    booking_event_id: str | None = None
    if existing:
        # Idempotence : si on a déjà reçu ce trigger précis (même UID, même
        # outcome), skip pour ne pas re-spammer Slack lors d'un retry Cal.com.
        same_outcome = existing.get("meeting_outcome") == meeting_outcome
        same_state_unchanged = (
            payload.trigger == "BOOKING_CREATED" and existing.get("booked_at")
        )
        if same_outcome or same_state_unchanged:
            actions.append("skipped_duplicate_trigger")
            return HandleBookingOut(
                status="skipped_duplicate",
                trigger=payload.trigger,
                booking_event_id=existing["id"],
                contact_id=contact_id,
                actions_taken=actions,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # Update existing row (reschedule / cancellation / meeting ended)
        patch: dict[str, Any] = {}
        if booked_at:
            patch["booked_at"] = booked_at
        if payload.start_time_iso:
            patch["meeting_scheduled_for"] = payload.start_time_iso
        if meeting_outcome:
            patch["meeting_outcome"] = meeting_outcome
        try:
            upd = await db.update(
                "booking_events", patch,
                filters={"id": f"eq.{existing['id']}"},
            )
            booking_event_id = (upd[0]["id"] if upd else existing["id"])
            actions.append("booking_event_updated")
        except Exception as e:  # noqa: BLE001
            return HandleBookingOut(
                status="error",
                trigger=payload.trigger,
                contact_id=contact_id,
                error_text=f"update_booking_event_failed: {e!r}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    else:
        # Insert nouveau row
        last_msg = await _find_last_outbound_message(contact_id)
        row: dict[str, Any] = {
            "contact_id": contact_id,
            "booking_url": payload.meeting_url or "(cal.com webhook — no URL provided)",
            "booking_provider": "cal.com",
            "external_event_id": payload.external_event_id,
        }
        if last_msg:
            row["triggered_by_message_id"] = last_msg["id"]
            if last_msg.get("campaign_id"):
                row["campaign_id"] = last_msg["campaign_id"]
        if booked_at:
            row["booked_at"] = booked_at
        if payload.start_time_iso:
            row["meeting_scheduled_for"] = payload.start_time_iso
        if meeting_outcome:
            row["meeting_outcome"] = meeting_outcome
        try:
            ins = await db.insert("booking_events", row)
            booking_event_id = ins[0]["id"] if ins else None
            actions.append("booking_event_inserted")
        except Exception as e:  # noqa: BLE001
            return HandleBookingOut(
                status="error",
                trigger=payload.trigger,
                contact_id=contact_id,
                error_text=f"insert_booking_event_failed: {e!r}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    # 5) Update conversation state
    last_msg = last_msg if existing is None else await _find_last_outbound_message(contact_id)
    campaign_id = (last_msg or {}).get("campaign_id")
    await _upsert_conversation_state(
        contact_id=contact_id,
        campaign_id=campaign_id,
        state=conversation_state,
    )
    actions.append(f"conversation_state={conversation_state}")

    # 6) Slack ping
    contact_name = (
        f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}"
    ).strip() or payload.attendee_name or payload.attendee_email
    company_name = (company or {}).get("name")

    if payload.trigger == "BOOKING_CREATED":
        # REACTI : si la verticale de la boîte matche la grille, on joint le
        # ticket moyen + commission au brief. None pour un prospect OPT (no-op).
        reacti_ticket = reacti_tickets.ticket_for_company(
            industry=(company or {}).get("industry"),
            google_types=(company or {}).get("google_types"),
        )
        fallback, blocks = slack_lib.build_booked_blocks(
            contact_name=contact_name,
            company_name=company_name,
            contact_email=payload.attendee_email,
            meeting_start_iso=payload.start_time_iso or "(date inconnue)",
            meeting_url=payload.meeting_url,
            event_type=payload.event_type_title,
            research_json=(company or {}).get("research_json"),
            reacti_ticket=reacti_ticket,
        )
        await slack_lib.notify(
            text=fallback, blocks=blocks, context="wf8_booked", category="bookings",
        )
        actions.append("slack_booked")
    elif payload.trigger == "BOOKING_RESCHEDULED":
        await slack_lib.notify(
            text=(
                f"🔄 RDV replanifié — {contact_name}"
                + (f" @ {company_name}" if company_name else "")
                + f" — nouvelle date: {payload.start_time_iso or '?'}"
            ),
            context="wf8_rescheduled",
            category="bookings",
        )
        actions.append("slack_rescheduled")
    elif payload.trigger == "BOOKING_CANCELLED":
        reason_str = f" — raison: {payload.cancellation_reason}" if payload.cancellation_reason else ""
        await slack_lib.notify(
            text=(
                f"❌ RDV annulé — {contact_name}"
                + (f" @ {company_name}" if company_name else "")
                + reason_str
            ),
            context="wf8_cancelled",
            category="bookings",
        )
        actions.append("slack_cancelled")
    elif payload.trigger == "MEETING_ENDED":
        await slack_lib.notify(
            text=(
                f"🏁 RDV terminé — {contact_name}"
                + (f" @ {company_name}" if company_name else "")
            ),
            context="wf8_ended",
            category="bookings",
        )
        actions.append("slack_ended")

    return HandleBookingOut(
        status="ok",
        trigger=payload.trigger,
        booking_event_id=booking_event_id,
        contact_id=contact_id,
        actions_taken=actions,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
