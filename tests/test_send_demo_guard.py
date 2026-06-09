"""Garde demo au send (P3) : aucun email agence-ia ne part sans lien démo.

- demo_url manquant + retry OK -> lien injecté, push continue.
- retry échoue -> skipped_no_demo, PAS de push.
- track OPT -> garde inactive.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("INSTANTLY_API_KEY", "test")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "camp")
    # neutralise le warmup gate (sinon skip avant la garde demo)
    monkeypatch.setenv("WARMUP_END_DATE", "2000-01-01")


def _msg(**over) -> dict:
    base = {
        "id": "m-1", "subject": "S", "body_text": "Allo {{DEMO_URL}}",
        "to_email": "jean@plomberiex.ca", "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "demo_url": None, "track": "agence-ia", "compliance_notes": None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_retry_success_injects_and_pushes(monkeypatch) -> None:
    from src.tools import send

    # select renvoie le message (1er appel) puis contact, puis company
    selects = [
        [_msg()],
        [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
          "email": "jean@plomberiex.ca", "company_id": "co-1"}],
        [{"name": "Plomberie X", "domain": "plomberiex.ca"}],
        [{"status": "ready"}],  # status lookup côté side-effect contact
    ]
    monkeypatch.setattr(send.db, "select", AsyncMock(side_effect=selects))
    updated = {}
    async def _update(table, patch, **kw):
        updated.setdefault(table, []).append(patch)
        return [{}]
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send, "ensure_demo_site",
                        AsyncMock(return_value="https://couture-ia.com/demo/TOK"))
    monkeypatch.setattr(send, "inject_demo_link",
                        lambda b, u: b.replace("{{DEMO_URL}}", u))
    pushed = {}
    async def _add_lead(**kw):
        pushed.update(kw)
        return {"id": "lead-1"}
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", _add_lead)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, None)))

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    # le body poussé contient le vrai lien, pas le placeholder
    assert "https://couture-ia.com/demo/TOK" in pushed["body_text"]
    assert "{{DEMO_URL}}" not in pushed["body_text"]


@pytest.mark.asyncio
async def test_retry_failure_skips_no_demo(monkeypatch) -> None:
    from src.tools import send

    selects = [
        [_msg()],
        [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
          "email": "jean@plomberiex.ca", "company_id": "co-1"}],
        [{"name": "Plomberie X", "domain": "plomberiex.ca"}],
    ]
    monkeypatch.setattr(send.db, "select", AsyncMock(side_effect=selects))
    monkeypatch.setattr(send.db, "update", AsyncMock(return_value=[{}]))

    async def _boom(*a, **k):
        raise RuntimeError("agence not exposed")
    monkeypatch.setattr(send, "ensure_demo_site", _boom)
    add = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, None)))

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_demo"
    add.assert_not_called()  # JAMAIS de push sans lien


@pytest.mark.asyncio
async def test_opt_track_guard_inactive(monkeypatch) -> None:
    from src.tools import send

    selects = [
        [_msg(track="OPT", body_text="Pas de placeholder", demo_url=None)],
        [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
          "email": "jean@plomberiex.ca", "company_id": "co-1"}],
        [{"name": "Plomberie X", "domain": "plomberiex.ca"}],
        [{"status": "ready"}],
    ]
    monkeypatch.setattr(send.db, "select", AsyncMock(side_effect=selects))
    monkeypatch.setattr(send.db, "update", AsyncMock(return_value=[{}]))
    ensure = AsyncMock(return_value="https://x/demo/T")
    monkeypatch.setattr(send, "ensure_demo_site", ensure)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign",
                        AsyncMock(return_value={"id": "lead-1"}))
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, None)))

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    ensure.assert_not_called()  # OPT n'a pas de démo
