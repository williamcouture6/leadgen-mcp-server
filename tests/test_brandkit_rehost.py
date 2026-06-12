import httpx
import respx
import pytest

from src import supabase_client as db


@respx.mock
@pytest.mark.asyncio
async def test_upload_object_returns_public_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    db.settings.cache_clear()
    route = respx.post(
        "https://proj.supabase.co/storage/v1/object/brand-assets/c1/logo-ab.png"
    ).mock(return_value=httpx.Response(200, json={"Key": "brand-assets/c1/logo-ab.png"}))

    url = await db.upload_object("brand-assets", "c1/logo-ab.png", b"\x89PNG", "image/png")

    assert route.called
    assert url == "https://proj.supabase.co/storage/v1/object/public/brand-assets/c1/logo-ab.png"
    assert route.calls.last.request.headers["x-upsert"] == "true"
