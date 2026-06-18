import pytest

from src.tools import brand_kit as BK


def test_flex_tool_schema_has_8_block_types():
    schema = BK._FLEX_TOOL["input_schema"]
    blocs = schema["properties"]["blocs"]["items"]
    # union fermée via oneOf, chacun discriminé par 'type'
    type_enums = set()
    for variant in blocs["oneOf"]:
        type_enums |= set(variant["properties"]["type"]["enum"])
    assert type_enums == {
        "titre", "texte", "liste", "image", "galerie", "stats", "cta", "faq",
    }
    # aucune clé d'URL libre : seuls des *_id pour les images
    dumped = str(schema)
    assert "url_id" in dumped
    assert "'url'" not in dumped  # pas de champ url libre dans le schéma d'entrée


@pytest.mark.asyncio
async def test_resolve_flex_page_renames_resolves_pexels_and_drops_stock_gallery():
    by_id = {1: "https://x.test/real-hero.jpg", 2: "https://x.test/real-img.jpg"}

    async def fake_rehost(role, src):
        return f"https://cdn/{role}/{src.rsplit('/', 1)[-1]}"

    async def fake_pexels(role, query):
        return f"https://cdn/{role}/pexels.jpg"

    llm_page = {
        "titre": "Financement",
        "hero_image_url_id": 1,
        "blocs": [
            {"type": "image", "url_id": 2, "legende": "plan"},          # réel -> url
            {"type": "image", "url_id": None, "legende": "déco"},        # aucun -> Pexels
            {"type": "galerie", "images": [{"url_id": None}]},           # aucun réel -> DROP
            {"type": "texte", "corps": "Sans intérêt 12 mois."},
        ],
    }
    out = await BK._resolve_flex_page(
        llm_page, by_id, "construction", "Financement",
        rehost=fake_rehost, pexels=fake_pexels,
    )

    # hero : id résolu + clé renommée (plus de hero_image_url_id)
    assert out["hero_image_url"] == "https://cdn/flex-hero/real-hero.jpg"
    assert "hero_image_url_id" not in out

    img_blocs = [b for b in out["blocs"] if b["type"] == "image"]
    assert img_blocs[0]["url"] == "https://cdn/flex-image/real-img.jpg"
    assert "url_id" not in img_blocs[0]
    assert img_blocs[1]["url"] == "https://cdn/flex-image/pexels.jpg"   # Pexels comble

    # galerie sans image réelle -> bloc absent (zéro Pexels)
    assert all(b["type"] != "galerie" for b in out["blocs"])
    # texte intact
    assert any(b["type"] == "texte" for b in out["blocs"])


@pytest.mark.asyncio
async def test_resolve_flex_page_empty_blocs_returns_none():
    out = await BK._resolve_flex_page(
        {"titre": "Vide", "blocs": []}, {}, "construction", "Vide",
        rehost=None, pexels=None,
    )
    assert out is None


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_build_flex_pages_end_to_end(monkeypatch):
    pages = [
        {"url": "https://x.test/financement/", "type": "other", "text": "F" * 600,
         "candidates": [{"id": 1, "url": "https://x.test/f.jpg", "kind_hint": "other", "alt": ""}]},
        {"url": "https://x.test/panier/", "type": "other", "text": "P" * 600, "candidates": []},
        {"url": "https://x.test/lavage/", "type": "service", "text": "S" * 600, "candidates": []},
    ]

    def fake_call(page_text, candidates, industry, model="m"):
        return {"titre": "Financement", "blocs": [
            {"type": "image", "url_id": 1, "legende": "plan"},
            {"type": "stats", "items": [{"valeur": "0 %", "label": "intérêt"}]}]}

    monkeypatch.setattr(BK, "_call_flex_llm", fake_call)

    async def fake_rehost(role, src):
        return f"https://cdn/{role}.jpg"

    out = await BK._build_flex_pages(
        "c1", pages, "construction", model="m",
        rehost_factory=lambda cid: fake_rehost,
        pexels_factory=lambda cid: (lambda role, q: _async_none()),
    )
    # seule la page 'financement' survit (service + panier filtrés)
    assert len(out) == 1
    assert out[0]["slug"] == "financement"
    assert out[0]["nav"] is True
    assert out[0]["blocs"][0]["url"] == "https://cdn/flex-image.jpg"
