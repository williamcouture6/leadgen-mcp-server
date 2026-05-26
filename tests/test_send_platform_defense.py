"""Tests for the 5th defense layer: block sending to platform / big tech
email domains in send.py.

Contract pinned here:
  - @meta.com / @google.com / @facebook.com etc. → skipped_platform_domain,
    message marked failed, no Instantly push.
  - Subdomains (info.meta.com) → blocked too.
  - Legit PME domain (cafefaro.com) → passes through this defense.
  - Defense runs AFTER warmup check, BEFORE suppression list + Instantly push
    (so we always fail closed on the irreversible step).
"""
from __future__ import annotations

import pytest

from src.lib.platform_domains import is_email_on_blocked_domain


# ---------------- Pure helper tests (no async, no DB) ----------------

def test_is_email_blocked_meta() -> None:
    blocked, reason = is_email_on_blocked_domain("ssingh@meta.com")
    assert blocked is True
    assert reason == "meta.com"


def test_is_email_blocked_facebook_host_domain() -> None:
    """facebook.com est dans PLATFORM_DOMAINS_NEVER_USE (donc dans le union)."""
    blocked, reason = is_email_on_blocked_domain("anyone@facebook.com")
    assert blocked is True
    assert reason == "facebook.com"


def test_is_email_blocked_doordash() -> None:
    blocked, _ = is_email_on_blocked_domain("driver@doordash.com")
    assert blocked is True


def test_is_email_blocked_subdomain() -> None:
    """Sous-domaine type info.meta.com doit aussi être bloqué."""
    blocked, reason = is_email_on_blocked_domain("contact@info.meta.com")
    assert blocked is True
    assert "meta.com" in reason


def test_is_email_blocked_case_insensitive() -> None:
    blocked, _ = is_email_on_blocked_domain("UpperCase@META.COM")
    assert blocked is True


def test_is_email_legit_pme_passes() -> None:
    """Un vrai domaine PME ne doit JAMAIS être bloqué."""
    blocked, reason = is_email_on_blocked_domain("mfabi@cafefaro.com")
    assert blocked is False
    assert reason is None


def test_is_email_legit_personal_passes() -> None:
    """Gmail / hotmail / etc. NE doivent PAS être bloqués — beaucoup de
    PME indé QC publient leur email perso sur leur site."""
    for em in [
        "proprio@gmail.com",
        "salon@hotmail.com",
        "boulangerie@videotron.ca",
    ]:
        blocked, _ = is_email_on_blocked_domain(em)
        assert blocked is False, f"{em} should NOT be blocked (legit perso PME)"


def test_is_email_empty_or_malformed() -> None:
    """Email vide / sans @ → not blocked (autres checks de send.py gèrent)."""
    for em in [None, "", "noatsign", "@", "no.at.sign"]:
        blocked, reason = is_email_on_blocked_domain(em)
        assert blocked is False, f"{em!r} should pass through (not our concern)"


def test_email_domains_never_send_contains_meta_and_facebook() -> None:
    """Régression : la liste union doit contenir à la fois la couche
    plateforme (facebook.com) ET la couche big tech (meta.com)."""
    from src.lib.platform_domains import EMAIL_DOMAINS_NEVER_SEND

    assert "meta.com" in EMAIL_DOMAINS_NEVER_SEND
    assert "facebook.com" in EMAIL_DOMAINS_NEVER_SEND
    assert "fb.com" in EMAIL_DOMAINS_NEVER_SEND
    assert "doordash.com" in EMAIL_DOMAINS_NEVER_SEND
    assert "stripe.com" in EMAIL_DOMAINS_NEVER_SEND
    # Sanity: pas de domain perso légit dans la liste
    assert "gmail.com" not in EMAIL_DOMAINS_NEVER_SEND
    assert "hotmail.com" not in EMAIL_DOMAINS_NEVER_SEND


# ---------------- send_one_message integration (mocked DB + Instantly) ----------------

@pytest.fixture
def _send_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bypass warmup gate so we reach the platform check.
    monkeypatch.setenv("WARMUP_END_DATE", "2020-01-01")
    monkeypatch.setenv("INSTANTLY_API_KEY", "test-key")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "test-cid")


@pytest.mark.asyncio
async def test_send_blocks_meta_email_marks_message_failed(
    _send_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bout-à-bout : message destiné à @meta.com → skipped_platform_domain,
    message marqué failed en DB, Instantly jamais appelé."""
    from src.tools import send

    # Stub DB: select returns the draft with to_email @meta.com
    draft_id = "draft-abc"
    db_calls = {"selects": [], "updates": []}

    async def fake_select(table, *, params=None):
        db_calls["selects"].append((table, params))
        if table == "messages":
            return [{
                "id": draft_id,
                "subject": "Test subject",
                "body_text": "Test body",
                "to_email": "ssingh@meta.com",
                "status": "draft",
                "direction": "outbound",
                "compliance_check_passed": True,
                "contact_id": "contact-1",
                "compliance_notes": None,
            }]
        return []

    async def fake_update(table, patch, *, filters):
        db_calls["updates"].append((table, patch, filters))
        return [{}]

    async def fake_add_lead(**kwargs):
        pytest.fail("Instantly add_lead_to_campaign should NOT have been called")

    monkeypatch.setattr(send.db, "select", fake_select)
    monkeypatch.setattr(send.db, "update", fake_update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", fake_add_lead)

    res = await send.send_one_message(send.SendMessageIn(message_id=draft_id))

    assert res.status == "skipped_platform_domain"
    assert "meta.com" in (res.skipped_reason or "")
    # Vérifie qu'on a bien marqué le message 'failed' en DB
    assert any(
        t == "messages" and p.get("status") == "failed"
        for t, p, _ in db_calls["updates"]
    ), "expected DB update marking message as failed"


@pytest.mark.asyncio
async def test_send_allows_legit_pme_email_proceeds_past_defense(
    _send_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Régression : un email PME légit ne doit PAS être bloqué par cette
    couche. On stoppe le test au moment où la défense passe (en faisant
    échouer le fetch contact qui suit) — l'important est de confirmer que
    skipped_platform_domain n'est PAS le statut retourné."""
    from src.tools import send

    async def fake_select(table, *, params=None):
        if table == "messages":
            return [{
                "id": "draft-ok",
                "subject": "Test",
                "body_text": "Test",
                "to_email": "mfabi@cafefaro.com",
                "status": "draft",
                "direction": "outbound",
                "compliance_check_passed": True,
                "contact_id": "contact-1",
                "compliance_notes": None,
            }]
        if table == "contacts":
            return []  # contact_not_found → flow s'arrête là, post-défense
        return []

    async def fake_update(*args, **kwargs):
        return [{}]

    monkeypatch.setattr(send.db, "select", fake_select)
    monkeypatch.setattr(send.db, "update", fake_update)

    res = await send.send_one_message(send.SendMessageIn(message_id="draft-ok"))

    # Confirme qu'on a dépassé la défense platform sans bloquer
    assert res.status != "skipped_platform_domain"
    # Le flow s'arrête sur contact_not_found, mais c'est en aval de notre check
    assert res.status == "error"
    assert res.error_text == "contact_not_found"
