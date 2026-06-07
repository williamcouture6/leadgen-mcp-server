# mcp-server/tests/test_discover_endpoint.py
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.tools import reacti_discover as rd


class _LLMResult:
    def __init__(self, discovery: dict[str, Any]) -> None:
        self.discovery = discovery
        self.model = "claude-sonnet-4-6"
        self.usage = rd.DiscoveryUsage(input_tokens=1, output_tokens=1)


async def _noop(*a, **k):
    return None


@pytest.fixture
def patch_company(monkeypatch):
    """Stub db.select pour retourner UNE company REACTI sans site."""
    async def _select(table, *, params=None):
        if table == "companies":
            return [{
                "id": "c1", "name": "Déneige X", "city": "Sherbrooke",
                "address": "1 rue X", "raw_payload": {"nationalPhoneNumber": "819-555"},
                "website": None, "track": "REACTI",
            }]
        return []
    monkeypatch.setattr(http_api.db_tools.db, "select", _select)


@pytest.mark.asyncio
async def test_discover_found_inserts_and_backfills(monkeypatch, patch_company):
    discovery = {
        "found": True, "discovered_url": "https://facebook.com/x",
        "page_kind": "facebook",
        "emails": [{"email": "info@x.ca", "kind": "generic",
                    "source_url": "https://facebook.com/x/about",
                    "published_on_own_page": True}],
        "confidence": "high", "match_reasoning": "ok",
    }
    monkeypatch.setattr(rd, "_call_discovery_llm",
                        lambda **kw: _LLMResult(discovery))

    inserted: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []

    async def _insert_contact(payload):
        inserted.append(payload.model_dump())
        return http_api.db_tools.InsertContactOut(status="inserted", contact_id="ct1")

    async def _update(table, patch, *, filters):
        updated.append({"table": table, "patch": patch})
        return [{}]

    monkeypatch.setattr(http_api.db_tools, "insert_contact", _insert_contact)
    monkeypatch.setattr(http_api.db_tools.db, "update", _update)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", _noop)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )

    assert out.status == "found"
    assert out.contacts_inserted == 1
    assert inserted[0]["email"] == "info@x.ca"
    assert inserted[0]["email_verification_source"] == "reacti_discovery_own_page"
    # website backfillé
    assert any(u["patch"].get("website") == "https://facebook.com/x" for u in updated)


@pytest.mark.asyncio
async def test_discover_not_found_marks_no_web_presence(monkeypatch, patch_company):
    monkeypatch.setattr(rd, "_call_discovery_llm",
                        lambda **kw: _LLMResult(dict(rd._EMPTY_DISCOVERY)))
    updated: list[dict[str, Any]] = []

    async def _update(table, patch, *, filters):
        updated.append(patch)
        return [{}]

    monkeypatch.setattr(http_api.db_tools.db, "update", _update)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", _noop)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )
    assert out.status == "no_web_presence"
    assert any(p.get("status") == "no_web_presence" for p in updated)
