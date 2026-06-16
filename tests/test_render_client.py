import httpx
import respx
import pytest

from src.lib import render_client


@respx.mock
@pytest.mark.asyncio
async def test_fetch_rendered_ok(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_URL", "https://render.test")
    monkeypatch.setenv("RENDER_SERVICE_TOKEN", "tok")
    from src import config
    config.settings.cache_clear()
    route = respx.post("https://render.test/render").mock(
        return_value=httpx.Response(200, json={
            "url": "https://x.test", "html": "<h1>ok</h1>",
            "image_urls": ["https://x.test/a.jpg"], "status": "ok", "error": None})
    )
    out = await render_client.fetch_rendered("https://x.test")
    assert out == {"html": "<h1>ok</h1>", "image_urls": ["https://x.test/a.jpg"]}
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_rendered_status_error_returns_none(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_URL", "https://render.test")
    monkeypatch.setenv("RENDER_SERVICE_TOKEN", "tok")
    from src import config
    config.settings.cache_clear()
    respx.post("https://render.test/render").mock(
        return_value=httpx.Response(200, json={"url": "x", "html": "", "image_urls": [],
                                               "status": "error", "error": "boom"})
    )
    assert await render_client.fetch_rendered("https://x.test") is None


@pytest.mark.asyncio
async def test_fetch_rendered_disabled_when_no_url(monkeypatch):
    monkeypatch.delenv("RENDER_SERVICE_URL", raising=False)
    from src import config
    config.settings.cache_clear()
    assert await render_client.fetch_rendered("https://x.test") is None


@respx.mock
@pytest.mark.asyncio
async def test_fetch_rendered_http_error_returns_none(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_URL", "https://render.test")
    monkeypatch.setenv("RENDER_SERVICE_TOKEN", "tok")
    from src import config
    config.settings.cache_clear()
    respx.post("https://render.test/render").mock(return_value=httpx.Response(502))
    assert await render_client.fetch_rendered("https://x.test") is None
