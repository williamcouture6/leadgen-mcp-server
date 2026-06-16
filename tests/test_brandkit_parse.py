from src.lib import brandkit_parse as P

HTML_RICH = """
<html><head>
  <meta property="og:image" content="/img/og.png">
  <meta name="theme-color" content="#0B5">
  <meta name="description" content="Rénovation à Laval depuis 1998">
  <link rel="icon" href="https://x.test/favicon.ico">
  <script type="application/ld+json">
  {"@type":"LocalBusiness","name":"Réno Belair","logo":"/logo.png",
   "telephone":"+1 450-555-0192","sameAs":["https://facebook.com/renobelair"],
   "aggregateRating":{"ratingValue":"4.8","reviewCount":"154"}}
  </script>
</head><body></body></html>
"""

def test_extract_head_meta_absolutizes_and_picks_fields():
    m = P.extract_head_meta(HTML_RICH, "https://x.test/")
    assert m["og_image"] == "https://x.test/img/og.png"
    assert m["theme_color"] == "#0B5"
    assert m["description"].startswith("Rénovation")
    assert m["icon"] == "https://x.test/favicon.ico"

def test_parse_jsonld_localbusiness():
    j = P.parse_jsonld(HTML_RICH, "https://x.test/")
    assert j["logo"] == "https://x.test/logo.png"
    assert j["telephone"] == "+1 450-555-0192"
    assert j["same_as"] == ["https://facebook.com/renobelair"]
    assert j["rating"] == 4.8
    assert j["rating_count"] == 154

def test_parse_jsonld_handles_graph_and_missing():
    assert P.parse_jsonld("<html></html>", "https://x.test/") == P.EMPTY_JSONLD

HTML_IMGS = """
<html><body>
  <header><img src="/logo.png" alt="Logo Réno Belair"></header>
  <section class="hero"><img src="https://x.test/hero.jpg" alt="chantier"></section>
  <img src="/team.jpg" alt="notre équipe">
  <footer>
    <a href="https://facebook.com/renobelair">FB</a>
    <a href="https://instagram.com/renobelair">IG</a>
    <a href="tel:+14505550192">Appelez</a>
    Licence RBQ 1234-5678-01
  </footer>
</body></html>
"""

def test_extract_image_candidates_kind_hint():
    cands = P.extract_image_candidates(HTML_IMGS, "https://x.test/")
    urls = {c["url"]: c for c in cands}
    assert urls["https://x.test/logo.png"]["kind_hint"] == "logo"
    assert urls["https://x.test/hero.jpg"]["kind_hint"] == "hero"
    assert any(c["kind_hint"] == "team" for c in cands)

def test_dedup_and_id_assigns_sequential_unique():
    raw = [{"url": "a", "kind_hint": "logo"}, {"url": "a", "kind_hint": "other"},
           {"url": "b", "kind_hint": "hero"}]
    out = P.dedup_and_id(raw)
    assert [c["id"] for c in out] == [0, 1]
    assert [c["url"] for c in out] == ["a", "b"]

def test_extract_social_links():
    s = P.extract_social_links(HTML_IMGS)
    assert s["facebook"] == "https://facebook.com/renobelair"
    assert s["instagram"] == "https://instagram.com/renobelair"

def test_find_rbq():
    assert P.find_rbq("Licence RBQ 1234-5678-01 valide") == "1234-5678-01"
    assert P.find_rbq("aucun numéro ici") is None


# --- Extraction logo déterministe (favicon dimensionné / apple-touch avant og:image) ---

HTML_LOGO = """
<html><head>
  <link rel="icon" href="/cropped-logo-32x32.png" sizes="32x32">
  <link rel="icon" href="/cropped-logo-192x192.png" sizes="192x192">
  <link rel="apple-touch-icon" href="/cropped-logo-180x180.png">
  <meta property="og:image" content="/photo-equipe.jpg">
  <script type="application/ld+json">{"@type":"LocalBusiness","logo":"/jsonld-logo.png"}</script>
</head><body></body></html>
"""


def test_extract_head_meta_collects_icons_and_apple_touch():
    m = P.extract_head_meta(HTML_LOGO, "https://x.test/")
    assert m["apple_touch_icon"] == "https://x.test/cropped-logo-180x180.png"
    sizes = {i["url"]: i["size"] for i in m["icons"]}
    assert sizes["https://x.test/cropped-logo-192x192.png"] == 192
    assert sizes["https://x.test/cropped-logo-32x32.png"] == 32


def test_pick_logo_prefers_apple_touch_over_og_image():
    m = P.extract_head_meta(HTML_LOGO, "https://x.test/")
    j = P.parse_jsonld(HTML_LOGO, "https://x.test/")
    assert P.pick_logo_url(m, j) == "https://x.test/cropped-logo-180x180.png"


def test_pick_logo_falls_back_to_largest_sized_icon():
    m = {"apple_touch_icon": None,
         "icons": [{"url": "https://x/i32.png", "size": 32},
                   {"url": "https://x/i192.png", "size": 192}],
         "og_image": "https://x/photo.jpg", "icon": "https://x/i32.png"}
    assert P.pick_logo_url(m, {"logo": "https://x/jl.png"}) == "https://x/i192.png"


def test_pick_logo_skips_tiny_icons_uses_jsonld_then_fb_then_og():
    m = {"apple_touch_icon": None, "icons": [{"url": "https://x/i16.png", "size": 16}],
         "og_image": "https://x/photo.jpg", "icon": "https://x/i16.png"}
    assert P.pick_logo_url(m, {"logo": "https://x/jl.png"}) == "https://x/jl.png"
    assert P.pick_logo_url(m, {"logo": None}, facebook_logo="https://fb/l.jpg") == "https://fb/l.jpg"
    assert P.pick_logo_url(m, {"logo": None}, facebook_logo=None) == "https://x/photo.jpg"


def test_pick_logo_none_when_nothing():
    m = {"apple_touch_icon": None, "icons": [], "og_image": None, "icon": None}
    assert P.pick_logo_url(m, {"logo": None}) is None


# --- Facebook : logo / site web / téléphone depuis le HTML public de la page ---

FB_HTML = """
<html><head>
  <meta property="og:image" content="https://scontent.fbcdn.net/profile-logo.jpg">
  <meta property="og:title" content="BL Vitres">
</head><body>
  <a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.blvitres.com%2F&h=AT2">Site web</a>
  <a href="tel:+15142285119">Appeler</a>
</body></html>
"""


def test_parse_facebook_html_extracts_logo_website_phone():
    fb = P.parse_facebook_html(FB_HTML)
    assert fb["logo"] == "https://scontent.fbcdn.net/profile-logo.jpg"
    assert fb["website"] == "https://www.blvitres.com/"
    assert fb["phone"] == "+15142285119"


def test_parse_facebook_html_phone_from_json_when_no_tel():
    html = '<html><body>{"__typename":"Page","phone":"+1 514-228-5119"}</body></html>'
    fb = P.parse_facebook_html(html)
    assert fb["phone"] == "+1 514-228-5119"
    assert fb["logo"] is None
    assert fb["website"] is None


def test_parse_facebook_html_empty_is_all_none():
    fb = P.parse_facebook_html("<html></html>")
    assert fb == {"logo": None, "website": None, "phone": None, "hours": None}


def test_classify_page():
    assert P.classify_page("https://x.test/lavage-de-vitres-residentiel/", "Résidentiel") == "service"
    assert P.classify_page("https://x.test/nettoyage-gouttieres/", "") == "service"
    assert P.classify_page("https://x.test/notre-equipe/", "Notre équipe") == "equipe"
    assert P.classify_page("https://x.test/galerie/", "") == "galerie"
    assert P.classify_page("https://x.test/realisations/", "") == "galerie"
    assert P.classify_page("https://x.test/contact/", "Contact") == "contact"
    assert P.classify_page("https://x.test/blog/", "Blogue") == "blog"
    assert P.classify_page("https://x.test/", "Accueil") == "home"
    assert P.classify_page("https://x.test/mentions-legales/", "") == "other"
