"""Tests : le param schema= pose le header PostgREST profile.

Accept-Profile (GET) / Content-Profile (POST/PATCH) ciblent un schéma autre
que `public`. Requis pour atteindre `agence.demo_sites` (P3, Piège 2).
schema=None (défaut) => aucun header profile, zéro régression sur public.
"""
from __future__ import annotations

import httpx
import pytest
import respx


@pytest.fixture(autouse=True)
def _db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


@respx.mock
@pytest.mark.asyncio
async def test_select_sets_accept_profile() -> None:
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/demo_sites").mock(
        return_value=httpx.Response(200, json=[])
    )
    await db.select("demo_sites", params={"select": "id"}, schema="agence")

    assert route.called
    assert route.calls.last.request.headers["Accept-Profile"] == "agence"


@respx.mock
@pytest.mark.asyncio
async def test_insert_sets_content_profile() -> None:
    from src import supabase_client as db

    route = respx.post("https://test.supabase.co/rest/v1/demo_sites").mock(
        return_value=httpx.Response(201, json=[{"id": "x"}])
    )
    await db.insert("demo_sites", {"token": "t"}, schema="agence")

    assert route.called
    assert route.calls.last.request.headers["Content-Profile"] == "agence"


@respx.mock
@pytest.mark.asyncio
async def test_update_sets_content_profile() -> None:
    from src import supabase_client as db

    route = respx.patch("https://test.supabase.co/rest/v1/demo_sites").mock(
        return_value=httpx.Response(200, json=[{"id": "x"}])
    )
    await db.update("demo_sites", {"statut": "envoye"}, filters={"id": "eq.x"}, schema="agence")

    assert route.called
    assert route.calls.last.request.headers["Content-Profile"] == "agence"


@respx.mock
@pytest.mark.asyncio
async def test_no_schema_means_no_profile_header() -> None:
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/messages").mock(
        return_value=httpx.Response(200, json=[])
    )
    await db.select("messages", params={"select": "id"})

    assert route.called
    assert "Accept-Profile" not in route.calls.last.request.headers
