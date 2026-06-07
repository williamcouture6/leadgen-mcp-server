# mcp-server/tests/test_discover_backlog.py
from __future__ import annotations

from typing import Any

import pytest

from src.tools import db as db_tools


def _fake_select_factory(tables: dict[str, list[dict[str, Any]]]):
    captured: dict[str, dict[str, str]] = {}

    async def _fake_select(table: str, *, params: dict[str, str] | None = None):
        captured[table] = params or {}
        return tables.get(table, [])

    return _fake_select, captured


@pytest.mark.asyncio
async def test_list_companies_to_discover_filters(monkeypatch):
    fake, captured = _fake_select_factory({"companies": [{"id": "1", "name": "X"}]})
    monkeypatch.setattr(db_tools.db, "select", fake)

    rows = await db_tools.list_companies_to_discover(limit=5)

    assert rows == [{"id": "1", "name": "X"}]
    p = captured["companies"]
    assert p["track"] == "eq.REACTI"
    assert p["website"] == "is.null"
    assert p["research_json"] == "is.null"
    assert p["status"] == "eq.sourced"


@pytest.mark.asyncio
async def test_research_reacti_no_website_excludes_contactless(monkeypatch):
    # 2 companies REACTI sans website : '1' a un contact, '2' n'en a pas.
    tables = {
        "companies": [
            {"id": "1", "name": "A", "website": None, "google_place_id": "g1"},
            {"id": "2", "name": "B", "website": None, "google_place_id": "g2"},
        ],
        "contacts": [{"company_id": "1"}],
    }
    fake, captured = _fake_select_factory(tables)
    monkeypatch.setattr(db_tools.db, "select", fake)

    rows = await db_tools.list_companies_to_research(
        limit=10, require_website=False, track="REACTI",
    )

    ids = {r["id"] for r in rows}
    assert ids == {"1"}  # '2' sans contact ni website → exclu
    # le filtre statut exclut les terminaux
    assert captured["companies"]["status"] == "not.in.(disqualified,no_web_presence)"


@pytest.mark.asyncio
async def test_research_require_website_true_unchanged(monkeypatch):
    tables = {"companies": [{"id": "1", "name": "A", "website": "https://a.ca",
                             "google_place_id": "g1"}]}
    fake, captured = _fake_select_factory(tables)
    monkeypatch.setattr(db_tools.db, "select", fake)

    rows = await db_tools.list_companies_to_research(
        limit=10, require_website=True, track="OPT",
    )
    assert [r["id"] for r in rows] == ["1"]
    assert captured["companies"]["website"] == "not.is.null"
