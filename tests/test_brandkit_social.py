import httpx
import respx
import pytest

from src.tools import brand_kit as BK


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_keeps_live_link():
    respx.head("https://facebook.com/reno").mock(return_value=httpx.Response(200))
    valid, dead = await BK.validate_social({"facebook": "https://facebook.com/reno"})
    assert valid == {"facebook": "https://facebook.com/reno"}
    assert dead == []


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_head_refused_falls_back_to_get():
    # Beaucoup de CDN refusent HEAD (405) → repli GET ; 200 → gardé.
    respx.head("https://instagram.com/reno").mock(return_value=httpx.Response(405))
    respx.get("https://instagram.com/reno").mock(return_value=httpx.Response(200))
    valid, dead = await BK.validate_social({"instagram": "https://instagram.com/reno"})
    assert valid == {"instagram": "https://instagram.com/reno"}
    assert dead == []


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_keeps_bot_blocked_403():
    # Facebook/Instagram/X renvoient souvent 403 aux robots — le profil EXISTE, on GARDE
    # (le but est d'attraper les liens morts `#`/404, pas les blocages anti-bot).
    respx.head("https://facebook.com/reno").mock(return_value=httpx.Response(403))
    respx.get("https://facebook.com/reno").mock(return_value=httpx.Response(403))
    valid, dead = await BK.validate_social({"facebook": "https://facebook.com/reno"})
    assert valid == {"facebook": "https://facebook.com/reno"}
    assert dead == []


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_keeps_linkedin_999_bot_block():
    # LinkedIn renvoie 999 aux robots — profil valide, on GARDE.
    respx.head("https://linkedin.com/company/reno").mock(return_value=httpx.Response(999))
    respx.get("https://linkedin.com/company/reno").mock(return_value=httpx.Response(999))
    valid, dead = await BK.validate_social({"linkedin": "https://linkedin.com/company/reno"})
    assert valid == {"linkedin": "https://linkedin.com/company/reno"}
    assert dead == []


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_drops_404():
    respx.head("https://facebook.com/dead").mock(return_value=httpx.Response(404))
    respx.get("https://facebook.com/dead").mock(return_value=httpx.Response(404))
    valid, dead = await BK.validate_social({"facebook": "https://facebook.com/dead"})
    assert valid == {}
    assert dead == ["facebook"]


@pytest.mark.asyncio
async def test_validate_social_drops_anchor_and_wrong_domain_without_network():
    # `#` (ancre interne) et mauvais domaine (url facebook sous la clé instagram) → drop
    # AVANT tout réseau (aucune route respx nécessaire = preuve qu'aucune requête n'est faite).
    valid, dead = await BK.validate_social({
        "facebook": "#",
        "instagram": "https://facebook.com/notinsta",
    })
    assert valid == {}
    assert set(dead) == {"facebook", "instagram"}


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_drops_on_exception_without_crashing():
    # Exception réseau sur un hôte → drop, jamais de crash du build.
    respx.head("https://x.com/reno").mock(side_effect=httpx.ConnectError("boom"))
    valid, dead = await BK.validate_social({"x": "https://x.com/reno"})
    assert valid == {}
    assert dead == ["x"]


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_drops_redirect_to_wrong_host():
    # Redirection finale vers un hôte hors-plateforme (parking/vente de domaine) → drop
    # malgré un 200 final, car l'URL finale ne matche plus la plateforme.
    respx.head("https://tiktok.com/@reno").mock(
        return_value=httpx.Response(301, headers={"Location": "https://parking.example/x"}))
    respx.head("https://parking.example/x").mock(return_value=httpx.Response(200))
    valid, dead = await BK.validate_social({"tiktok": "https://tiktok.com/@reno"})
    assert valid == {}
    assert dead == ["tiktok"]


@respx.mock
@pytest.mark.asyncio
async def test_validate_social_mixed_valid_and_dead():
    respx.head("https://facebook.com/ok").mock(return_value=httpx.Response(200))
    respx.head("https://instagram.com/gone").mock(return_value=httpx.Response(404))
    respx.get("https://instagram.com/gone").mock(return_value=httpx.Response(404))
    valid, dead = await BK.validate_social({
        "facebook": "https://facebook.com/ok",
        "instagram": "https://instagram.com/gone",
        "x": "#",
    })
    assert valid == {"facebook": "https://facebook.com/ok"}
    assert set(dead) == {"instagram", "x"}
