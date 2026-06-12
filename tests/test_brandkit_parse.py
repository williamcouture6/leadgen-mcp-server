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
