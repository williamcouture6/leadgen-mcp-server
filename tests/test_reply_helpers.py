"""Tests for WF-7 reply handler pure helpers.

Focus on the helpers that have logic that could silently regress and cause
real damage to the classifier or to the auto-reply flow:

1. strip_quote_and_signature — keeps the LLM from "seeing" our own cold email
   in the reply quote, plus trims signatures that pollute classification.
2. html_to_text — used when a reply has only HTML body; bad output here =
   bad classification (model thinks `<p>` is content).
3. extract_from_instantly_email_list_item — defensive extraction across the
   shape variations Instantly v2 /emails returns (string vs list-of-string
   vs list-of-dict vs single-dict for from/to fields).
4. extract_from_instantly_webhook — same defensive extraction for the
   reply_received webhook payload, plus the synthetic-id collision guard.

These are pure functions — no DB/LLM/network mocking needed.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _reply_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the env vars reply.py reads at import time (config indirection)."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# strip_quote_and_signature
# =====================================================================

class TestStripQuoteAndSignature:
    """Quote and signature stripping — defensive against many reply formats."""

    def test_empty_input_returns_empty(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        assert strip_quote_and_signature("") == ""
        assert strip_quote_and_signature("\n\n\n") == ""

    def test_no_quote_no_signature_returns_body_intact(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = "Oui ca minteresse, dispo mercredi."
        assert strip_quote_and_signature(body) == body

    def test_strips_gmail_style_quote_header_french(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Oui ca minteresse.\n\n"
            "Le 25 mai 2026, William Couture a écrit :\n"
            "> Bonjour Anne,\n> j'ai vu que votre clinique..."
        )
        assert strip_quote_and_signature(body) == "Oui ca minteresse."

    def test_strips_outlook_style_original_message(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Pas pour nous.\n\n"
            "----- Original Message -----\n"
            "From: William\n"
            "Bonjour..."
        )
        assert strip_quote_and_signature(body) == "Pas pour nous."

    def test_strips_quoted_lines(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Pas intéressé merci.\n\n"
            "> Bonjour Pierre,\n"
            "> votre clinique..."
        )
        assert strip_quote_and_signature(body) == "Pas intéressé merci."

    def test_strips_rfc_3676_signature(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = "Oui je suis dispo.\n\n--\nWilliam Couture\nDirecteur"
        assert strip_quote_and_signature(body) == "Oui je suis dispo."

    def test_strips_cordialement_signature(self) -> None:
        """The 'Cordialement,' lead-in cuts at the start of the signature."""
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Oui ca minteresse.\n\n"
            "Cordialement,\n"
            "Pierre Tremblay\n"
            "Directeur, Clinique Tremblay\n"
            "514-555-1234"
        )
        assert strip_quote_and_signature(body) == "Oui ca minteresse."

    def test_strips_merci_signature(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = "Pas dispo cette semaine.\n\nMerci,\nPierre"
        assert strip_quote_and_signature(body) == "Pas dispo cette semaine."

    def test_strips_sent_from_iphone(self) -> None:
        from src.tools.reply import strip_quote_and_signature
        body = "Oui interesse.\n\nSent from my iPhone"
        assert strip_quote_and_signature(body) == "Oui interesse."

    def test_strips_tail_signature_with_phone(self) -> None:
        """1-5 short trailing lines with a phone number = probable signature."""
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Pas pour nous merci.\n\n"
            "Pierre Tremblay\n"
            "514-555-1234"
        )
        # Should trim the 2-line tail signature (contains phone)
        assert strip_quote_and_signature(body) == "Pas pour nous merci."

    def test_does_not_strip_legitimate_content_with_phone(self) -> None:
        """A phone number in the middle of the message is not a signature."""
        from src.tools.reply import strip_quote_and_signature
        body = (
            "Bonjour, mon numero est le 514-555-1234 si vous voulez "
            "discuter plus en detail."
        )
        # Body has no trailing blank line, so no signature trim should occur.
        result = strip_quote_and_signature(body)
        assert "514-555-1234" in result

    def test_preserves_body_when_signature_is_too_long(self) -> None:
        """Tail block with lines > 80 chars is not treated as signature."""
        from src.tools.reply import strip_quote_and_signature
        long_tail = "x" * 90  # > 80 chars threshold
        body = f"Pas interesse.\n\n{long_tail}\n514-555-1234"
        # Long line means we don't trim — preserve everything
        result = strip_quote_and_signature(body)
        assert long_tail in result


# =====================================================================
# html_to_text
# =====================================================================

class TestHtmlToText:
    """HTML body → plain text for classifier consumption."""

    def test_empty_returns_empty(self) -> None:
        from src.tools.reply import html_to_text
        assert html_to_text("") == ""

    def test_strips_simple_tags(self) -> None:
        from src.tools.reply import html_to_text
        assert html_to_text("<p>Hello</p>") == "Hello"

    def test_converts_br_to_newline(self) -> None:
        from src.tools.reply import html_to_text
        assert html_to_text("Line 1<br>Line 2<br/>Line 3") == "Line 1\nLine 2\nLine 3"

    def test_decodes_html_entities(self) -> None:
        from src.tools.reply import html_to_text
        assert "M & M" in html_to_text("M &amp; M")
        assert "ça" in html_to_text("&ccedil;a")

    def test_strips_script_block(self) -> None:
        from src.tools.reply import html_to_text
        body = "Hello <script>alert(1)</script> World"
        # Multiple spaces around the stripped tag are collapsed to single
        assert html_to_text(body) == "Hello World"

    def test_strips_style_block(self) -> None:
        from src.tools.reply import html_to_text
        body = "Hi <style>.x{color:red}</style> there"
        assert html_to_text(body) == "Hi there"

    def test_collapses_multiple_newlines(self) -> None:
        from src.tools.reply import html_to_text
        body = "<p>A</p><p>B</p><p>C</p>"
        result = html_to_text(body)
        # Multiple blank lines collapsed to max one blank
        assert "\n\n\n" not in result
        assert "A" in result and "B" in result and "C" in result

    def test_real_world_outlook_reply(self) -> None:
        from src.tools.reply import html_to_text
        body = (
            '<div dir="ltr"><p>Bonjour William,</p>'
            "<p>Oui &ccedil;a m'int&eacute;resse, dispo mercredi.</p>"
            "<p>Cordialement,<br>Pierre</p></div>"
        )
        result = html_to_text(body)
        assert "Bonjour William" in result
        assert "ça m'intéresse" in result
        assert "Cordialement" in result
        # No raw HTML left
        assert "<" not in result and ">" not in result


# =====================================================================
# extract_from_instantly_email_list_item — poll path
# =====================================================================

class TestExtractFromInstantlyEmailListItem:
    """Defensive extraction from GET /api/v2/emails items.

    Instantly's API shape varies — sometimes string, sometimes list, sometimes
    dict for from/to addresses. We accept all of them.
    """

    def test_returns_none_on_missing_id(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {"from_email": "a@b.com", "body": "hi"}
        assert extract_from_instantly_email_list_item(item) is None

    def test_returns_none_on_missing_from(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {"id": "abc", "body": "hi"}
        assert extract_from_instantly_email_list_item(item) is None

    def test_returns_none_on_missing_body(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {"id": "abc", "from_email": "a@b.com"}
        assert extract_from_instantly_email_list_item(item) is None

    def test_extracts_basic_email(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "uuid-123",
            "from_email": "Anne@Example.COM",
            "subject": "Re: votre message",
            "body_text": "Oui ca minteresse",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.lead_email == "anne@example.com"  # lowercased
        assert res.provider_message_id_inbound == "uuid-123"
        assert res.reply_body_text == "Oui ca minteresse"

    def test_from_address_as_list_of_strings(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_address_email_list": ["foo@bar.com"],
            "body": "hello",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.lead_email == "foo@bar.com"

    def test_from_address_as_list_of_dicts(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_address_email_list": [{"address": "Pierre@Test.com", "name": "Pierre"}],
            "body": "hi",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.lead_email == "pierre@test.com"

    def test_from_address_as_single_dict(self) -> None:
        """Audit fix #12: handle Instantly returning single-dict (not list)."""
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_address": {"address": "Single@Dict.com", "name": "S"},
            "body": "hi",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.lead_email == "single@dict.com"

    def test_body_as_dict_with_text_and_html(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_email": "x@y.com",
            "body": {"text": "plain text body", "html": "<p>html body</p>"},
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.reply_body_text == "plain text body"
        assert res.reply_body_html == "<p>html body</p>"

    def test_html_only_body_gets_converted_to_text(self) -> None:
        """Audit fix #10: when body has only HTML, derive text for classifier."""
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_email": "x@y.com",
            "body_html": "<p>Oui dispo</p>",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert "Oui dispo" in res.reply_body_text
        assert "<p>" not in res.reply_body_text
        assert res.reply_body_html == "<p>Oui dispo</p>"

    def test_parent_id_extracted_from_alternative_fields(self) -> None:
        from src.tools.reply import extract_from_instantly_email_list_item
        item = {
            "id": "u1",
            "from_email": "x@y.com",
            "body": "hi",
            "in_reply_to_uuid": "parent-uuid",
        }
        res = extract_from_instantly_email_list_item(item)
        assert res is not None
        assert res.provider_message_id_parent == "parent-uuid"


# =====================================================================
# extract_from_instantly_webhook — webhook path (dormant)
# =====================================================================

class TestExtractFromInstantlyWebhook:

    def test_ignores_non_reply_events(self) -> None:
        from src.tools.reply import extract_from_instantly_webhook
        assert extract_from_instantly_webhook({"event_type": "email_sent"}) is None
        assert extract_from_instantly_webhook({"event_type": "email_opened"}) is None

    def test_accepts_reply_received(self) -> None:
        from src.tools.reply import extract_from_instantly_webhook
        body = {
            "event_type": "reply_received",
            "lead_email": "anne@cliniquex.com",
            "email_subject": "Re: votre message",
            "email_text_body": "Oui ca minteresse",
            "reply_uuid": "uuid-1",
            "in_reply_to_uuid": "parent-uuid",
            "email_account": "william@couture-ia.com",
        }
        res = extract_from_instantly_webhook(body)
        assert res is not None
        assert res.lead_email == "anne@cliniquex.com"
        assert res.provider_message_id_inbound == "uuid-1"
        assert res.provider_message_id_parent == "parent-uuid"

    def test_synthetic_id_includes_random_suffix(self) -> None:
        """Audit fix #13: synthetic ID must include token_hex to avoid
        collisions when 2 webhooks arrive in the same second."""
        from src.tools.reply import extract_from_instantly_webhook
        body = {
            "event_type": "reply_received",
            "lead_email": "x@y.com",
            "email_text_body": "hi",
            # no reply_uuid → synthetic generated
        }
        res1 = extract_from_instantly_webhook(body)
        res2 = extract_from_instantly_webhook(body)
        assert res1 is not None and res2 is not None
        assert res1.provider_message_id_inbound != res2.provider_message_id_inbound, (
            "synthetic IDs should differ even when called back-to-back"
        )
        assert "synthetic-" in res1.provider_message_id_inbound

    def test_html_only_webhook_body_gets_text(self) -> None:
        from src.tools.reply import extract_from_instantly_webhook
        body = {
            "event_type": "reply_received",
            "lead_email": "x@y.com",
            "email_html_body": "<p>Oui</p>",
            "reply_uuid": "u1",
        }
        res = extract_from_instantly_webhook(body)
        assert res is not None
        assert "Oui" in res.reply_body_text
        assert "<p>" not in res.reply_body_text
