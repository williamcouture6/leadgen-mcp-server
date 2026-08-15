"""Garde P4.10 dans send_one_message : pas de config produit, pas de courriel.

Contrat épinglé ici :
  - verdict 'ok' -> l'envoi continue.
  - absent / 'a-verifier' / 'refuse' -> skipped_no_site_config, PAS de push,
    message laissé en draft (jamais 'failed').
  - la garde tourne AVANT la frappe démo P3.
  - track OPT -> garde inactive.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("INSTANTLY_API_KEY", "test")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "camp")
    monkeypatch.setenv("WARMUP_END_DATE", "2000-01-01")


def _msg(**over) -> dict:
    base = {
        "id": "m-1", "subject": "S", "body_text": "Allo https://couture-ia.com/demo/T",
        "to_email": "jean@plomberiex.ca", "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "demo_url": "https://couture-ia.com/demo/T", "track": "agence-ia",
        "compliance_notes": None,
    }
    base.update(over)
    return base


def _wire(monkeypatch, *, msg: dict, site_config_rows, read_boom: bool = False):
    """Branche db.select par TABLE (pas en séquence) — la garde ajoute un
    select, un side_effect séquentiel serait fragile.

    Retourne (updates, add_lead_mock) pour les assertions.
    """
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [msg]
        if table == "contacts":
            if params and "status" in (params.get("select") or ""):
                return [{"status": "ready"}]
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1"}]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        if table == "site_configs":
            if read_boom:
                raise RuntimeError("agence not exposed")
            return site_config_rows
        return []

    updates: list[tuple[str, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch))
        return [{}]

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(send.slack, "notify", AsyncMock(return_value=True))
    return updates, add_lead


async def test_verdict_ok_pousse(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[{"verdict": "ok"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    add_lead.assert_awaited_once()


async def test_aucun_config_saute_sans_pousser(monkeypatch) -> None:
    from src.tools import send

    updates, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_no_site_config"
    assert "absent" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()
    # le message reste en draft : jamais marqué failed, il partira plus tard
    assert not any(p.get("status") for _, p in updates)


async def test_a_verifier_saute(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(),
                        site_config_rows=[{"verdict": "a-verifier"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    assert "a-verifier" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()


async def test_refuse_saute(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[{"verdict": "refuse"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    add_lead.assert_not_awaited()


async def test_track_opt_ignore_la_garde(monkeypatch) -> None:
    """OPT est legacy et n'a pas de démo : la garde ne doit pas le bloquer."""
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(track="OPT", demo_url=None),
                        site_config_rows=[])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    add_lead.assert_awaited_once()


async def test_garde_tourne_avant_la_frappe_demo(monkeypatch) -> None:
    """Sans config, on ne crée pas de ligne agence.demo_sites pour rien."""
    from src.tools import send

    _wire(monkeypatch, msg=_msg(demo_url=None, body_text="Allo {{DEMO_URL}}"),
          site_config_rows=[])
    ensure = AsyncMock(return_value="https://couture-ia.com/demo/T")
    monkeypatch.setattr(send, "ensure_demo_site", ensure)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    ensure.assert_not_awaited()
