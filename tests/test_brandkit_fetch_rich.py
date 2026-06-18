import pytest
from src.tools import brand_kit as BK


HOME = """
<html><head>
  <meta name="theme-color" content="#0B5">
  <link rel="apple-touch-icon" href="/logo-180.png">
</head><body>
  <nav>
    <a href="/lavage-de-vitres-residentiel/">Résidentiel</a>
    <a href="/notre-equipe/">Notre équipe</a>
    <a href="/contact/">Contact</a>
  </nav>
  <img src="/home1.jpg"><img src="/home2.jpg"><img src="/home3.jpg">
  <a href="https://facebook.com/blvitres">FB</a>
</body></html>
"""
SERVICE = ('<html><body><h1>Lavage résidentiel</h1>'
           '<img src="/s1.jpg"><img src="/s2.jpg"><img src="/s3.jpg">'
           'Nous lavons vos vitres.</body></html>')
EQUIPE = ('<html><body><div class="twentytwenty-container"></div>'  # faible → escalade
          'Kevin Bouvier</body></html>')
RENDERED_EQUIPE = ('<html><body><img src="/kevin.jpg"><img src="/sam.jpg">'
                   '<img src="/x.jpg">Notre équipe rendue</body></html>')


@pytest.mark.asyncio
async def test_fetch_site_rich_crawls_all_and_escalates(monkeypatch):
    fetched = {
        "https://x.test/": HOME,
        "https://x.test/lavage-de-vitres-residentiel/": SERVICE,
        "https://x.test/notre-equipe/": EQUIPE,
        "https://x.test/contact/": "<html><body>Contact 514-555-0000</body></html>",
    }

    async def fake_get_html(client, url):
        return fetched.get(url)
    monkeypatch.setattr(BK, "_get_html", fake_get_html)

    async def fake_rendered(url):
        # Render service ne renvoie du HTML que pour la page équipe (qui a un vrai conteneur JS).
        if url == "https://x.test/notre-equipe/":
            return {"html": RENDERED_EQUIPE, "image_urls": []}
        return None  # Autres pages faibles (vides) : le render service ne renvoie rien.
    monkeypatch.setattr(BK.render_client, "fetch_rendered", fake_rendered)

    rich = await BK.fetch_site_rich("https://x.test/")

    types = {p["url"]: p["type"] for p in rich["pages"]}
    assert types["https://x.test/lavage-de-vitres-residentiel/"] == "service"
    assert types["https://x.test/notre-equipe/"] == "equipe"
    # la home a fourni head_meta + social facebook
    assert rich["head_meta"]["theme_color"] == "#0B5"
    assert rich["social"].get("facebook") == "https://facebook.com/blvitres"
    # page équipe escaladée → son HTML rendu a alimenté les candidats (kevin.jpg)
    assert any(c["url"].endswith("/kevin.jpg") for c in rich["candidates"])
    assert "https://x.test/notre-equipe/" in rich["escalated"]
    # service_pages contient la page de service avec son texte
    sp = {p["url"]: p["text"] for p in rich["service_pages"]}
    assert "lavons vos vitres" in sp["https://x.test/lavage-de-vitres-residentiel/"].lower()


@pytest.mark.asyncio
async def test_fetch_site_rich_failsoft_on_home_error(monkeypatch):
    async def fake_get_html(client, url):
        return None
    monkeypatch.setattr(BK, "_get_html", fake_get_html)
    rich = await BK.fetch_site_rich("https://x.test/")
    assert rich["pages"] == []
    assert rich["candidates"] == []
    assert rich["page_text"] == ""


@pytest.mark.asyncio
async def test_fetch_site_rich_crawls_other_pages_with_per_page_candidates(monkeypatch):
    home = """<html><body>
      <a href="/financement/">Financement</a>
      <img src="/home1.jpg"><img src="/home2.jpg"><img src="/home3.jpg">
    </body></html>"""
    fin = """<html><body><h1>Financement</h1>
      <img src="/fin-a.jpg" alt="plan"><img src="/fin-b.jpg" alt="taux">
      <img src="/fin-c.jpg" alt="agent"></body></html>"""
    fetched = {
        "https://flex.test/": home,
        "https://flex.test/financement/": fin,
    }

    async def fake_get_html(client, url):
        return fetched.get(url)
    monkeypatch.setattr(BK, "_get_html", fake_get_html)

    async def fake_rendered(url):
        return None
    monkeypatch.setattr(BK.render_client, "fetch_rendered", fake_rendered)

    rich = await BK.fetch_site_rich("https://flex.test/")
    fin_page = next(p for p in rich["pages"] if p["url"].endswith("/financement/"))
    assert fin_page["type"] == "other"
    # candidats id'd PORTÉS PAR LA PAGE (pas seulement le pool global)
    cand_urls = {c["url"] for c in fin_page["candidates"]}
    assert "https://flex.test/fin-a.jpg" in cand_urls
    assert all("id" in c for c in fin_page["candidates"])
