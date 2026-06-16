import io

import httpx
import respx
import pytest
from PIL import Image

from src import supabase_client as db
from src.tools import brand_kit as BK


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_dominant_color_hex():
    assert BK.dominant_color(_png_bytes((11, 85, 0))) == "#0b5500"


def test_dominant_color_invalid_bytes_returns_none():
    assert BK.dominant_color(b"not an image") is None


@respx.mock
@pytest.mark.asyncio
async def test_fetch_pexels_image_returns_bytes(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "px")
    from src import config
    config.settings.cache_clear()
    respx.get("https://api.pexels.com/v1/search").mock(
        return_value=httpx.Response(200, json={"photos": [
            {"src": {"landscape": "https://img.pexels/h.jpg"}}]})
    )
    respx.get("https://img.pexels/h.jpg").mock(
        return_value=httpx.Response(200, content=b"JPEGDATA",
                                    headers={"content-type": "image/jpeg"}))

    data, ctype = await BK.fetch_pexels_image("roofing contractor")
    assert data == b"JPEGDATA"
    assert ctype == "image/jpeg"


@pytest.mark.asyncio
async def test_rehost_one_uses_injected_uploader():
    calls = {}
    async def fake_upload(bucket, path, data, content_type):
        calls.update(bucket=bucket, path=path, ctype=content_type)
        return f"https://cdn/{path}"
    async def fake_download(url):
        return (b"PNGDATA", "image/png")

    url = await BK.rehost_one("c1", "logo", "https://x/logo.png",
                              download=fake_download, upload=fake_upload)
    assert url.startswith("https://cdn/c1/logo-")
    assert calls["ctype"] == "image/png"
    assert calls["bucket"] == "brand-assets"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_facebook_brand_parses_public_page():
    respx.get("https://www.facebook.com/blvitres/").mock(
        return_value=httpx.Response(
            200,
            html='<meta property="og:image" content="https://fb/logo.jpg">'
                 '<a href="tel:+15142285119">x</a>',
        )
    )
    fb = await BK.fetch_facebook_brand("https://www.facebook.com/blvitres/")
    assert fb["logo"] == "https://fb/logo.jpg"
    assert fb["phone"] == "+15142285119"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_facebook_brand_failsoft_on_error():
    respx.get("https://www.facebook.com/x/").mock(return_value=httpx.Response(403))
    assert await BK.fetch_facebook_brand("https://www.facebook.com/x/") == {}


def _png_bytes_size(w, h, color=(10, 20, 30)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_image_meets_min_side():
    assert BK._image_meets_min_side(_png_bytes_size(300, 220), 200) is True
    assert BK._image_meets_min_side(_png_bytes_size(64, 64), 200) is False
    assert BK._image_meets_min_side(b"not an image", 200) is False


@pytest.mark.asyncio
async def test_rehost_rejects_small_for_hero(monkeypatch):
    small = _png_bytes_size(50, 50)
    async def fake_download(url):
        return (small, "image/png")
    url, data = await BK._rehost_with_bytes("c1", "hero", "https://x/tiny.png",
                                            download=fake_download, upload=None)
    assert url is None    # rejeté (trop petit pour un hero)


@pytest.mark.asyncio
async def test_rehost_allows_small_for_logo(monkeypatch):
    small = _png_bytes_size(50, 50)
    async def fake_download(url):
        return (small, "image/png")
    async def fake_upload(bucket, path, data, ctype):
        return f"https://cdn/{path}"
    url, data = await BK._rehost_with_bytes("c1", "logo", "https://x/logo.png",
                                            download=fake_download, upload=fake_upload)
    assert url and url.startswith("https://cdn/c1/logo-")   # logo accepté même petit


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
