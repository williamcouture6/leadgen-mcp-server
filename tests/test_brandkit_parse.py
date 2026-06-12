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
