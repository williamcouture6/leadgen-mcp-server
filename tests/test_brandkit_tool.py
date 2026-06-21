from typing import Any
import pytest
from src.tools import brand_kit as BK


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 5
    output_tokens = 9
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = _Usage()


class _Messages:
    def __init__(self, resp):
        self._resp = resp
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._resp


class _Client:
    def __init__(self, resp):
        self.messages = _Messages(resp)


def test_tool_schema_has_expected_keys():
    props = BK._BRANDKIT_TOOL["input_schema"]["properties"]
    for k in ("tagline", "logo_candidate_id", "hero_candidate_id",
              "team_photo_candidate_id", "services", "valeurs", "faq",
              "legal", "stats", "service_areas", "team", "rbq", "gallery"):
        assert k in props, f"clé manquante: {k}"


def test_tool_schema_gallery_has_before_after_candidate_ids():
    item = BK._BRANDKIT_TOOL["input_schema"]["properties"]["gallery"]["items"]["properties"]
    assert "before_candidate_id" in item
    assert "after_candidate_id" in item
    assert "caption" in item


def test_call_llm_forces_tool_and_returns_input(monkeypatch):
    expected = {"tagline": "Rénovation clé en main", "logo_candidate_id": 0,
                "services": [], "valeurs": [], "faq": []}
    client = _Client(_Resp([_Block(type="tool_use", name="save_brand_kit", input=expected)]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(BK, "Anthropic", lambda api_key: client)

    out = BK._call_brandkit_llm([{"id": 0, "url": "u", "kind_hint": "logo"}], "page text", "toiture")

    assert out == expected
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "save_brand_kit"}


def test_call_llm_requests_enough_tokens_for_multiservice(monkeypatch):
    # 7 services × (details + process + faq) ne tient pas dans 3500 → le champ services
    # (émis tard) se faisait tronquer. On exige une marge confortable.
    client = _Client(_Resp([_Block(type="tool_use", name="save_brand_kit", input={})]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(BK, "Anthropic", lambda api_key: client)
    BK._call_brandkit_llm([{"id": 0, "url": "u", "kind_hint": "logo"}], "page text", "toiture")
    assert client.messages.last_kwargs["max_tokens"] >= 8000


def test_pick_colors_prefers_css_palette_with_secondary():
    # palette CSS de marque (Elementor) prime sur theme-color/logo + fournit le secondaire
    c = BK._pick_colors({"theme_color": "#111111"}, {}, "#999999",
                        css_colors={"primary": "#00a6c0", "secondary": "#0e2f3a"})
    assert c["primary"] == "#00a6c0"
    assert c["secondary"] == "#0e2f3a"
    assert c["_confidence"] == "high"


def test_pick_colors_falls_back_to_logo_when_no_palette_no_theme():
    c = BK._pick_colors({"theme_color": None}, {}, "#00a6c0", css_colors={})
    assert c["primary"] == "#00a6c0"
    assert c["secondary"] is None
    assert c["_confidence"] == "medium"


@pytest.mark.asyncio
async def test_build_brand_kit_orchestrates(monkeypatch):
    # company en DB (nom + adresse requis pour la vérif du match Places)
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        assert table == "companies"
        return [{"id": "c1", "name": "Réno Belair",
                 "address": "10 rue Principale, Laval, QC H1G 4P1",
                 "website": "https://x.test", "industry": "toiture",
                 "google_place_id": "place1", "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    async def fake_rich(url):
        return {"head_meta": {"theme_color": "#0B5", "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": None, "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD},
                "social": {"facebook": "https://facebook.com/x"},
                "rbq": "1234-5678-01",
                "candidates": [{"id": 0, "url": "https://x/logo.png", "kind_hint": "logo", "alt": ""}],
                "page_text": "Réno Belair, toiture à Laval",
                "service_pages": [], "gallery_pairs": []}
    async def fake_place(pid):
        return {"displayName": {"text": "Réno Belair"},
                "formattedAddress": "10 rue Principale, Laval, QC H1G 4P1, Canada",
                "internationalPhoneNumber": "+1 450-555-0192", "reviews": []}
    def fake_llm(cands, text, industry, **kw):
        return {"tagline": "Toiture clé en main", "logo_candidate_id": 0,
                "services": [], "valeurs": [], "faq": []}
    async def fake_fb(url):
        return {}
    async def fake_rehost(cid, role, url, **kw):
        return f"https://cdn/{cid}/{role}.png"
    async def fake_rehost_bytes(cid, role, url, **kw):
        return (f"https://cdn/{cid}/{role}.png", b"x")
    async def fake_pexels(query):
        return None

    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "fetch_place_details", fake_place)
    monkeypatch.setattr(BK, "fetch_facebook_brand", fake_fb)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "_rehost_with_bytes", fake_rehost_bytes)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)

    out = await BK.build_brand_kit("c1")

    assert out["status"] == "ok"
    kit = written["patch"]["brand_kit"]
    assert kit["phone"] == "+1 450-555-0192"          # Places (match nom+adresse OK)
    assert kit["tagline"] == "Toiture clé en main"
    assert kit["logo_url"] == "https://cdn/c1/logo.png"
    assert kit["rbq"] == "1234-5678-01"
    assert "logo_url" in out["fields_filled"]


@pytest.mark.asyncio
async def test_build_brand_kit_uses_footer_service_areas(monkeypatch):
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "name": "Réno Belair",
                 "address": "10 rue Principale, Laval, QC H1G 4P1",
                 "website": "https://x.test", "industry": "toiture",
                 "google_place_id": None, "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    areas = ["Terrebonne", "Mascouche", "Laval", "Brossard", "Longueuil", "Mirabel"]
    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": None, "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [], "page_text": "x", "service_pages": [],
                "gallery_pairs": [], "pages": [], "service_areas": areas}
    def fake_llm(cands, text, industry, **kw):
        return {"service_areas": ["Montréal"]}  # LLM sort une liste partielle/erronée
    async def fake_pexels(query):
        return None
    async def fake_rehost(cid, role, url, **kw):
        return None
    async def fake_rehost_bytes(cid, role, url, **kw):
        return (None, None)

    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "_rehost_with_bytes", fake_rehost_bytes)

    await BK.build_brand_kit("c1")
    kit = written["patch"]["brand_kit"]
    assert kit["service_areas"] == areas
    assert kit["confidence"]["service_areas"] == "high"


@pytest.mark.asyncio
async def test_build_brand_kit_does_not_clobber_services_with_empty(monkeypatch):
    # Build pauvre (LLM sans services) ne doit PAS effacer les services déjà en place.
    existing = {"services": [{"name": "Lavage de vitres", "image_url": "https://cdn/s.jpg"}],
                "_meta": {"reviewed": False}}
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "name": "BL Vitres", "address": "x, Laval, QC H1G 4P1",
                 "website": "https://x.test", "industry": "lavage de vitres",
                 "google_place_id": None, "brand_kit": existing}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": None, "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [], "page_text": "x", "service_pages": [],
                "gallery_pairs": [], "pages": [], "service_areas": []}
    def fake_llm(cands, text, industry, **kw):
        return {"tagline": "Slogan frais"}  # build pauvre : aucun service
    async def fake_pexels(query):
        return None
    async def fake_rehost(cid, role, url, **kw):
        return None
    async def fake_rehost_bytes(cid, role, url, **kw):
        return (None, None)
    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "_rehost_with_bytes", fake_rehost_bytes)

    await BK.build_brand_kit("c1")
    kit = written["patch"]["brand_kit"]
    assert kit["services"] == existing["services"]              # services repris, pas effacés
    assert kit["tagline"] == "Slogan frais"                     # le bon nouveau contenu reste
    assert any(r["field"] == "services" for r in kit["_review"])  # tracé pour revue


@pytest.mark.asyncio
async def test_build_brand_kit_fills_generic_process_and_faq(monkeypatch):
    # Pages de service sans étapes/FAQ → fallback générique (process par service + FAQ
    # générale avec vrais secteurs), pour que la démo ne montre pas de sections vides.
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "name": "BL Vitres", "address": "x, Laval, QC H1G 4P1",
                 "website": "https://x.test", "industry": "lavage de vitres",
                 "google_place_id": None, "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    areas = ["Laval", "Brossard", "Montréal", "Longueuil", "Mirabel", "Boucherville"]
    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": None, "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [], "page_text": "x", "service_pages": [],
                "gallery_pairs": [], "pages": [], "service_areas": areas}
    def fake_llm(cands, text, industry, **kw):
        return {"tagline": "x", "services": [{"name": "Lavage de vitres"},
                                             {"name": "Nettoyage de gouttières"}]}
    async def fake_pexels(query):
        return None
    async def fake_rehost(cid, role, url, **kw):
        return None
    async def fake_rehost_bytes(cid, role, url, **kw):
        return (None, None)
    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "_rehost_with_bytes", fake_rehost_bytes)

    await BK.build_brand_kit("c1")
    kit = written["patch"]["brand_kit"]
    svcs = {s["name"]: s for s in kit["services"]}
    assert svcs["Lavage de vitres"]["process"]          # étapes remplies
    assert svcs["Nettoyage de gouttières"]["process"]
    assert kit["faq"]                                    # FAQ générale remplie
    assert any("Laval" in q["reponse"] for q in kit["faq"])  # vrais secteurs injectés


@pytest.mark.asyncio
async def test_build_brand_kit_guarantees_images(monkeypatch):
    # Chaque service a une image, stats.image_url et gallery TOUJOURS fournis (fallback Pexels),
    # logo déterministe via apple-touch-icon.
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "name": "BL Vitres", "address": "11233 Av X, Montréal, QC H1G 4P1",
                 "website": "https://x.test", "industry": "lavage de vitres",
                 "google_place_id": None, "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": "https://x/apple-180.png", "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [{"id": 0, "url": "https://x/svc.jpg", "kind_hint": "other", "alt": ""}],
                "page_text": "lavage de vitres"}
    def fake_llm(cands, text, industry, **kw):
        return {"services": [{"name": "Lavage de vitres", "image_candidate_id": 0},
                             {"name": "Nettoyage de gouttières"}],
                "valeurs": [], "faq": [], "stats": {}}
    async def fake_fb(url):
        return {}
    async def fake_rehost(cid, role, src, **kw):
        return f"https://cdn/{cid}/{role}.jpg"
    async def fake_rehost_bytes(cid, role, src, **kw):
        return (f"https://cdn/{cid}/{role}.jpg", b"img")
    async def fake_pexels(query):
        return (b"img", "image/jpeg")   # Pexels répond pour toute requête

    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "fetch_facebook_brand", fake_fb)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "_rehost_with_bytes", fake_rehost_bytes)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)

    await BK.build_brand_kit("c1")
    kit = written["patch"]["brand_kit"]

    # logo déterministe (apple-touch-icon, pas un candidat LLM)
    assert kit["logo_url"] == "https://cdn/c1/logo.jpg"
    # 1 image par service, sans exception (2e service via fallback Pexels)
    assert len(kit["services"]) == 2
    assert all(s.get("image_url") for s in kit["services"])
    # bande statistiques : image cinématographique de fond
    assert kit["stats"]["image_url"]
    # galerie avant/après toujours présente
    assert kit["gallery"] and kit["gallery"][0]["before_url"] and kit["gallery"][0]["after_url"]


@pytest.mark.asyncio
async def test_build_brand_kit_resolves_service_image_ids(monkeypatch):
    # Régression C1 : un image_candidate_id de service doit devenir une URL réelle
    # dans le kit persisté (jamais l'int brut).
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "website": "https://x.test", "industry": "toiture",
                 "google_place_id": None, "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [{"id": 7, "url": "https://x/salle-bain.jpg",
                                "kind_hint": "other", "alt": ""}],
                "page_text": "services"}
    def fake_llm(cands, text, industry, **kw):
        return {"services": [{"name": "Salles de bain", "image_candidate_id": 7}],
                "team": [{"nom": "Kevin B.", "role": "Proprio", "photo_candidate_id": 7}],
                "valeurs": []}
    async def fake_rehost(cid, role, src, **kw):
        return f"https://cdn/{cid}/{role}.jpg"
    async def fake_pexels(query):
        return None
    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)

    await BK.build_brand_kit("c1")

    kit = written["patch"]["brand_kit"]
    svc = kit["services"][0]
    assert svc["image_url"] == "https://cdn/c1/service.jpg"
    assert "image_candidate_id" not in svc
    # team : photo_candidate_id résolu en photo_url (jamais l'int brut).
    member = kit["team"][0]
    assert member["photo_url"] == "https://cdn/c1/team.jpg"
    assert "photo_candidate_id" not in member


@pytest.mark.asyncio
async def test_build_brand_kit_sets_review_and_status(monkeypatch):
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return []   # pas de verrou de revue → on build
        return [{"id": "c1", "name": "X", "address": "Y", "website": "https://x.test",
                 "industry": "lavage de vitres", "google_place_id": None, "brand_kit": None}]
    written = {}
    async def fake_update(table, patch, **kw):
        written.update(patch=patch)
        return [patch]
    monkeypatch.setattr(BK.db, "select", fake_select)
    monkeypatch.setattr(BK.db, "update", fake_update)

    async def fake_rich(url):
        return {"head_meta": {"theme_color": None, "og_image": None, "icon": None,
                              "twitter_image": None, "description": None,
                              "apple_touch_icon": None, "icons": []},
                "jsonld": {**BK.parse.EMPTY_JSONLD}, "social": {}, "rbq": None,
                "candidates": [], "page_text": "x", "service_pages": [], "gallery_pairs": []}
    def fake_llm(cands, text, industry, **kw):
        return {"services": [], "valeurs": [], "faq": [], "stats": {}}
    async def fake_fb(url):
        return {}
    async def fake_pexels(query):
        return None
    monkeypatch.setattr(BK, "fetch_site_rich", fake_rich)
    monkeypatch.setattr(BK, "fetch_facebook_brand", fake_fb)
    monkeypatch.setattr(BK, "_call_brandkit_llm", fake_llm)
    monkeypatch.setattr(BK, "fetch_pexels_image", fake_pexels)

    out = await BK.build_brand_kit("c1")
    kit = written["patch"]["brand_kit"]
    assert "_review" in kit and isinstance(kit["_review"], list)
    assert kit["_review"]                                   # logo absent, etc. → non vide
    assert kit["_meta"]["status"] == "needs_review"
    assert out["status"] == "ok"                            # l'appel synchrone reste "ok"


@pytest.mark.asyncio
async def test_build_brand_kit_skips_when_review_approved(monkeypatch):
    # Verrou : une row de revue 'approved' bloque le re-build (kit préservé).
    async def fake_select(table, **kw):
        if table == "brand_kit_reviews":
            return [{"status": "approved"}]
        return [{"id": "c1", "name": "X", "address": "a", "website": "w",
                 "industry": "i", "google_place_id": "p", "brand_kit": {"_meta": {}}}]
    monkeypatch.setattr(BK.db, "select", fake_select)
    out = await BK.build_brand_kit("c1")
    assert out["status"] == "skipped_already_reviewed"


@pytest.mark.asyncio
async def test_is_brandkit_approved_true_false(monkeypatch):
    async def approved(table, **kw):
        return [{"status": "approved"}]
    async def none(table, **kw):
        return []
    monkeypatch.setattr(BK.db, "select", approved)
    assert await BK._is_brandkit_approved("c1") is True
    monkeypatch.setattr(BK.db, "select", none)
    assert await BK._is_brandkit_approved("c1") is False


@pytest.mark.asyncio
async def test_is_brandkit_approved_queries_agence_schema(monkeypatch):
    captured: dict = {}
    async def fake_select(table, **kw):
        captured["table"] = table
        captured.update(kw)
        return []
    monkeypatch.setattr(BK.db, "select", fake_select)
    await BK._is_brandkit_approved("c1")
    assert captured["table"] == "brand_kit_reviews"
    assert captured["schema"] == "agence"
    assert captured["params"]["status"] == "eq.approved"


@pytest.mark.asyncio
async def test_is_brandkit_approved_fail_closed_on_error(monkeypatch):
    # PostgREST indispo (après retries) → on REFUSE de ré-écrire (fail-closed), pas de crash.
    async def boom(table, **kw):
        raise RuntimeError("postgrest down")
    monkeypatch.setattr(BK.db, "select", boom)
    assert await BK._is_brandkit_approved("c1") is True


@pytest.mark.asyncio
async def test_build_gallery_prefers_real_site_pair(monkeypatch):
    async def fake_rehost(cid, role, src, **kw):
        return f"https://cdn/{role}.jpg"
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    out = await BK._build_gallery(
        "c1", llm={}, by_id={}, industry="lavage de vitres",
        site_pairs=[{"before_url": "https://x/av.jpg", "after_url": "https://x/ap.jpg", "caption": None}],
    )
    assert out and out[0]["before_url"] == "https://cdn/gallery-before.jpg"
    assert out[0]["after_url"] == "https://cdn/gallery-after.jpg"


@pytest.mark.asyncio
async def test_build_gallery_site_half_pair_falls_through(monkeypatch):
    # 1re image OK, 2e échoue → jamais de demi-paire ; on tombe sur le fallback.
    async def fake_rehost(cid, role, src, **kw):
        return None if role == "gallery-after" else f"https://cdn/{role}.jpg"
    monkeypatch.setattr(BK, "rehost_one", fake_rehost)
    async def no_pexels(query):
        return None
    monkeypatch.setattr(BK, "fetch_pexels_image", no_pexels)
    out = await BK._build_gallery(
        "c1", llm={}, by_id={}, industry="lavage de vitres",
        site_pairs=[{"before_url": "https://x/av.jpg", "after_url": "https://x/ap.jpg", "caption": None}],
    )
    assert out == []   # demi-paire rejetée ; LLM vide + Pexels None → galerie vide


def test_call_llm_includes_service_pages(monkeypatch):
    captured = {}
    class _Msg:
        def create(self, **kw):
            captured.update(kw)
            return _Resp([_Block(type="tool_use", name="save_brand_kit", input={})])
    class _Cli:
        messages = _Msg()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(BK, "Anthropic", lambda api_key: _Cli())

    BK._call_brandkit_llm(
        [{"id": 0, "url": "u", "kind_hint": "other"}],
        "texte global", "lavage de vitres",
        service_pages=[{"url": "https://x/lavage/", "text": "Détail du lavage résidentiel"}],
    )
    user_msg = captured["messages"][0]["content"]
    assert "Détail du lavage résidentiel" in user_msg
    assert "lavage/" in user_msg


def test_service_schema_has_process_and_faq():
    svc = BK._BRANDKIT_TOOL["input_schema"]["properties"]["services"]["items"]["properties"]
    assert "process" in svc
    assert "faq" in svc
    proc = svc["process"]["items"]["properties"]
    assert "titre" in proc and "texte" in proc
    faq = svc["faq"]["items"]["properties"]
    assert "question" in faq and "reponse" in faq   # reponse sans accent (contrat)
