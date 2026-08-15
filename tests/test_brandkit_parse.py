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


# --- Téléphone : les PME publient rarement `telephone` au 1er niveau d'un @type canonique.
# Deux cas réels vus chez Déneigement J. Lauzon (2026-08-14) : le numéro vit sous
# `contactPoint`, et un 2e nœud le porte au 1er niveau mais avec un @type maison
# (« Déneigement commercial, industriel et institutionnel ») hors schema.org.

def test_parse_jsonld_telephone_from_contact_point():
    html = ('<script type="application/ld+json">'
            '{"@type":"Organization","name":"Lauzon","url":"https://x.test",'
            '"contactPoint":[{"@type":"ContactPoint","telephone":"450 627-0961",'
            '"contactType":"customer support"}]}</script>')
    assert P.parse_jsonld(html, "https://x.test/")["telephone"] == "450 627-0961"


def test_parse_jsonld_accepts_custom_business_type_of_the_site_itself():
    """@type maison mais nœud identifié comme le site (url/@id du domaine) → on lit ses
    faits (téléphone, adresse, image) au lieu de jeter le nœud entier."""
    html = ('<script type="application/ld+json">'
            '{"@type":"Déneigement commercial, industriel et institutionnel",'
            '"name":"Lauzon","url":"https://x.test","@id":"https://x.test",'
            '"telephone":"450 627-0961","image":"https://x.test/photo.png",'
            '"address":{"@type":"PostalAddress","streetAddress":"889, rang St-Antoine",'
            '"addressLocality":"Laval","postalCode":"H7R6E8"}}</script>')
    j = P.parse_jsonld(html, "https://x.test/")
    assert j["telephone"] == "450 627-0961"
    assert j["address"] == "889, rang St-Antoine, Laval, H7R6E8"
    assert j["image"] == "https://x.test/photo.png"


def test_parse_jsonld_ignores_custom_type_node_of_another_domain():
    """Un nœud @type inconnu qui ne pointe PAS vers le site (widget, annuaire tiers)
    reste ignoré — sinon on importerait le téléphone d'un autre commerce."""
    html = ('<script type="application/ld+json">'
            '{"@type":"WidgetTiers","url":"https://autre.test",'
            '"telephone":"418 000-0000"}</script>')
    assert P.parse_jsonld(html, "https://x.test/")["telephone"] is None


def test_parse_jsonld_ignores_structural_nodes_even_on_the_site():
    """Fil d'Ariane / article : même domaine, mais leur `image` n'est pas la marque."""
    html = ('<script type="application/ld+json">'
            '{"@type":"BlogPosting","url":"https://x.test/blogue/neige",'
            '"image":"https://x.test/vignette-article.jpg"}</script>')
    assert P.parse_jsonld(html, "https://x.test/")["image"] is None

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


HTML_SOCIAL_WIDE = """
<html><body><footer>
  <a href="https://www.youtube.com/@renobelair">YT</a>
  <a href="https://www.tiktok.com/@renobelair">TT</a>
  <a href="https://twitter.com/renobelair">Tweet</a>
  <a href="https://www.linkedin.com/company/renobelair">IN</a>
  <a href="https://box.com/share/xyz">Boîte (piège: contient x.com)</a>
</footer></body></html>
"""


def test_extract_social_links_widened_platforms():
    s = P.extract_social_links(HTML_SOCIAL_WIDE)
    assert s["youtube"] == "https://www.youtube.com/@renobelair"
    assert s["tiktok"] == "https://www.tiktok.com/@renobelair"
    assert s["linkedin"] == "https://www.linkedin.com/company/renobelair"
    # twitter.com normalisé vers la plateforme 'x'
    assert s["x"] == "https://twitter.com/renobelair"
    # « box.com » ne doit PAS matcher x.com (netloc exact, pas sous-chaîne)
    assert "box.com" not in " ".join(s.values())


def test_extract_social_links_maps_google_and_x_com():
    html = ('<a href="https://x.com/reno">X</a>'
            '<a href="https://www.google.com/maps/place/Reno/@45,-73">Maps</a>'
            '<a href="https://www.google.com/search?q=reno">Search (pas social)</a>')
    s = P.extract_social_links(html)
    assert s["x"] == "https://x.com/reno"
    assert s["google"] == "https://www.google.com/maps/place/Reno/@45,-73"
    # google.com hors /maps n'est pas un lien social → première occurrence 'google' garde Maps
    assert "search" not in s["google"]


def test_social_platform_rejects_junk_and_relative():
    assert P._social_platform("#") is None
    assert P._social_platform("javascript:void(0)") is None
    assert P._social_platform("/contact") is None
    assert P._social_platform("") is None
    assert P._social_platform("https://facebook.com/x") == "facebook"


def test_merge_social_links_unions_anchor_sameas_and_google():
    anchor = {"facebook": "https://facebook.com/reno"}
    same_as = ["https://www.instagram.com/reno", "https://twitter.com/reno",
               "https://facebook.com/AUTRE"]  # doublon plateforme → ignoré
    out = P.merge_social_links(anchor, same_as,
                               google_maps_uri="https://maps.google.com/?cid=9")
    assert out["facebook"] == "https://facebook.com/reno"   # ancre prime sur sameAs
    assert out["instagram"] == "https://www.instagram.com/reno"
    assert out["x"] == "https://twitter.com/reno"           # sameAs mergé + normalisé
    assert out["google"] == "https://maps.google.com/?cid=9"


def test_merge_social_links_google_not_overwritten_if_present():
    out = P.merge_social_links({"google": "https://g.page/reno"}, [],
                               google_maps_uri="https://maps.google.com/?cid=1")
    assert out["google"] == "https://g.page/reno"  # ancre google conservée

def test_find_rbq():
    assert P.find_rbq("Licence RBQ 1234-5678-01 valide") == "1234-5678-01"
    assert P.find_rbq("aucun numéro ici") is None


# --- Secteurs desservis : liste de toponymes du footer (souvent « Ville | Ville | … ») ---

HTML_AREAS = """
<html><body>
  <nav><a>Accueil</a> | <a>Services</a> | <a>Contact</a></nav>
  <main><p>Lavage de vitres à Montréal, Rive-Nord et Rive-Sud.</p></main>
  <footer>
    <div class="elementor-widget-container">
      <p class="elementor-heading-title">Terrebonne | Mascouche | Blainville | Lorraine |
       Rosemère | Mirabel | Sainte-Thérèse | Boisbriand | Repentigny | L'Assomption |
       Lanaudière | Rive-Nord | Montréal | Rivière-des-Prairies | Pointe-aux-Trembles |
       Outremont | Anjou | Ahuntsic | Laval | Montréal Nord | Longueuil | Boucherville |
       Varennes | Brossard | Saint-Lambert | Saint-Hubert | Rive-Sud | Montérégie</p>
    </div>
    <a href="https://facebook.com/x">FB</a>
  </footer>
</body></html>
"""


def test_extract_service_areas_from_footer_pipe_list():
    areas = P.extract_service_areas(HTML_AREAS)
    assert areas[0] == "Terrebonne"
    assert "Montréal" in areas and "Montréal Nord" in areas
    assert "L'Assomption" in areas
    assert "Sainte-Thérèse" in areas
    assert "Montérégie" in areas
    assert len(areas) == 28


def test_extract_service_areas_ignores_short_nav_list():
    html = "<footer><p>Accueil | Services | Blogue | Contact</p></footer>"
    assert P.extract_service_areas(html) == []


def test_extract_service_areas_empty_when_absent():
    assert P.extract_service_areas("<html><body><p>Bonjour le monde</p></body></html>") == []


def test_extract_service_areas_dedups_preserving_order():
    html = "<footer><p>Laval | Laval | Brossard | Longueuil | Laval | Boucherville | Varennes | Mirabel</p></footer>"
    areas = P.extract_service_areas(html)
    assert areas == ["Laval", "Brossard", "Longueuil", "Boucherville", "Varennes", "Mirabel"]


# --- Couleurs de marque depuis la palette CSS globale (Elementor) ---

HTML_CSS_COLORS = (
    "<html><head><style>:root{"
    "--e-global-color-primary:#00A6C0;--e-global-color-secondary:#0E2F3A;"
    "--e-global-color-accent:#FFFFFF;--e-global-color-text:#000000;}</style>"
    "</head><body></body></html>"
)


def test_extract_css_colors_elementor_palette():
    c = P.extract_css_colors(HTML_CSS_COLORS)
    assert c["primary"] == "#00a6c0"      # vraie couleur de marque (cyan)
    assert c["secondary"] == "#0e2f3a"    # vrai secondaire (navy)


def test_extract_css_colors_rejects_white_or_grey_primary():
    html = "<style>--e-global-color-primary:#FFFFFF;--e-global-color-secondary:#808285;</style>"
    assert P.extract_css_colors(html) == {}  # blanc/gris = pas une couleur de marque


def test_extract_css_colors_absent_returns_empty():
    assert P.extract_css_colors("<html><body>rien</body></html>") == {}


# --- Couleurs : repli par fréquence pour les thèmes sans variables de palette (Divi, etc.) ---
# Un thème WordPress non-Elementor n'expose aucune variable `--e-global-color-*` : la
# couleur de marque est simplement CELLE QUI REVIENT le plus dans le CSS inline. Sans ce
# repli, `_pick_colors` tombait sur la couleur dominante du logo (vert #466f4b sur un site
# dont toute l'identité est orange) — cas Déneigement J. Lauzon, 2026-08-14.
HTML_CSS_DIVI = (
    "<html><head><style>"
    ".et_pb_button{background:#F9AA12;border-color:#f9aa12;color:#ffffff}"
    "a:hover{color:#f9aa12}.et_pb_toggle_title:before{color:#f9aa12}"
    ".footer{background:#36454F;color:#F2F2F2}.footer a{color:#36454f}"
    "body{color:#595959;background:#ffffff}"
    "</style><style>"
    ".has-vivid-red-color{color:#cf2e2e}.has-luminous-vivid-orange-color{color:#ff6900}"
    ".has-pale-pink-color{color:#f78da7}.has-vivid-cyan-blue-color{color:#0693e3}"
    "</style></head><body></body></html>"
)


def test_extract_css_colors_frequency_fallback_picks_dominant_brand_color():
    c = P.extract_css_colors(HTML_CSS_DIVI)
    assert c["primary"] == "#f9aa12"     # l'orange de marque (le plus fréquent)
    assert c["secondary"] == "#36454f"   # l'ardoise du footer
    assert c["_source"] == "frequency"   # repli, pas une palette déclarée


def test_extract_css_colors_frequency_ignores_wordpress_default_palette():
    """Les couleurs du thème Gutenberg par défaut (une occurrence chacune) ne sont pas
    la marque : sans ce garde-fou, un site sobre hériterait du rouge/orange de WordPress."""
    html = ("<style>.has-vivid-red-color{color:#cf2e2e}"
            ".has-luminous-vivid-orange-color{color:#ff6900}"
            ".has-vivid-purple-color{color:#9b51e0}</style>")
    assert P.extract_css_colors(html) == {}


def test_extract_css_colors_frequency_ignores_near_duplicate_shade_as_secondary():
    """#f9ab12 n'est qu'une nuance de #f9aa12 : le secondaire doit être une AUTRE couleur."""
    html = ("<style>a{color:#f9aa12}b{color:#f9aa12}i{color:#f9aa12}"
            "u{color:#f9ab12}s{color:#f9ab12}</style>")
    c = P.extract_css_colors(html)
    assert c["primary"] == "#f9aa12"
    assert c.get("secondary") is None


def test_extract_css_colors_frequency_reads_rgb_notation():
    html = ("<style>.a{color:rgb(249,170,18)}.b{background:rgba(249, 170, 18, 1)}"
            ".c{border-color:rgb(249,170,18)}.d{box-shadow:0 0 2px rgba(0,0,0,.1)}</style>")
    assert P.extract_css_colors(html)["primary"] == "#f9aa12"


def test_extract_css_colors_prefers_declared_palette_over_frequency():
    """Une palette déclarée (Elementor) reste la source autoritative même si une autre
    couleur revient plus souvent dans le CSS."""
    html = (HTML_CSS_COLORS.replace("</head>", "")
            + "<style>.x{color:#f9aa12}.y{color:#f9aa12}.z{color:#f9aa12}"
              ".w{color:#f9aa12}</style></head><body></body></html>")
    c = P.extract_css_colors(html)
    assert c["primary"] == "#00a6c0"
    assert c["_source"] == "css_vars"


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


HTML_NAV = """
<html><body><nav>
  <a href="/lavage-de-vitres-residentiel/">Résidentiel</a>
  <a href="/nettoyage-gouttieres/">Gouttières</a>
  <a href="/notre-equipe/">Notre équipe</a>
  <a href="/contact/">Contact</a>
  <a href="https://facebook.com/x">FB</a>
  <a href="/lavage-de-vitres-residentiel/">Résidentiel (doublon)</a>
  <a href="mailto:info@x.test">Courriel</a>
</nav></body></html>
"""


def test_discover_links_internal_classified_deduped():
    links = P.discover_links(HTML_NAV, "https://x.test/")
    by_url = {l["url"]: l["type"] for l in links}
    assert by_url["https://x.test/lavage-de-vitres-residentiel/"] == "service"
    assert by_url["https://x.test/nettoyage-gouttieres/"] == "service"
    assert by_url["https://x.test/notre-equipe/"] == "equipe"
    assert by_url["https://x.test/contact/"] == "contact"
    # externe (facebook) et mailto exclus ; doublon dédupliqué
    assert all("facebook.com" not in u for u in by_url)
    assert all(not u.startswith("mailto:") for u in by_url)
    assert sum(1 for l in links if "residentiel" in l["url"]) == 1


def test_discover_links_respects_cap():
    many = "".join(f'<a href="/service-{i}/">S{i}</a>' for i in range(40))
    links = P.discover_links(f"<nav>{many}</nav>", "https://x.test/", cap=25)
    assert len(links) <= 25


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


def test_classify_page_routes_slot_pages():
    assert P.classify_page("https://x.test/faq/") == "faq"
    assert P.classify_page("https://x.test/foire-aux-questions/") == "faq"
    assert P.classify_page("https://x.test/avis/") == "avis"
    assert P.classify_page("https://x.test/temoignages/") == "avis"
    assert P.classify_page("https://x.test/nos-valeurs/") == "valeurs"
    # une page hors-slot reste 'other'
    assert P.classify_page("https://x.test/financement/") == "other"


def test_select_flex_candidates_filters_and_caps():
    pages = [
        {"url": "https://x.test/", "type": "home", "text": "x" * 500},
        {"url": "https://x.test/lavage/", "type": "service", "text": "x" * 500},
        {"url": "https://x.test/faq/", "type": "faq", "text": "x" * 500},
        {"url": "https://x.test/panier/", "type": "other", "text": "x" * 500},
        {"url": "https://x.test/connexion/", "type": "other", "text": "x" * 500},
        {"url": "https://x.test/en/financing/", "type": "other", "text": "x" * 500},
        {"url": "https://x.test/maigre/", "type": "other", "text": "trop court"},
        {"url": "https://x.test/financement/", "type": "other", "text": "F" * 800},
        {"url": "https://x.test/garanties/", "type": "other", "text": "G" * 400},
        {"url": "https://x.test/certifications/", "type": "other", "text": "C" * 600},
    ]
    out = P.select_flex_candidates(pages, cap=2, min_text=200)
    urls = [p["url"] for p in out]
    # slots (home/service/faq), junk (panier/connexion), langue (/en/), maigre -> exclus
    assert "https://x.test/panier/" not in urls
    assert "https://x.test/faq/" not in urls
    assert "https://x.test/en/financing/" not in urls
    assert "https://x.test/maigre/" not in urls
    # tri richesse + cap 2 -> financement (800) puis certifications (600)
    assert urls == ["https://x.test/financement/", "https://x.test/certifications/"]


def test_should_escalate_weak_pages():
    # Page riche (assez d'images réelles) → pas d'escalade
    rich = "<html><body>" + "".join(f'<img src="/p{i}.jpg">' for i in range(6)) + "</body></html>"
    assert P.should_escalate(rich) is False

    # Marqueur JS-only (placeholder SVG data:) + peu d'images → escalade
    weak = '<html><body><img src="data:image/svg+xml,PHN2Zz48L3N2Zz4="></body></html>'
    assert P.should_escalate(weak) is True

    # Conteneur slider sans <img> → escalade
    slider = '<html><body><div class="twentytwenty-container"></div></body></html>'
    assert P.should_escalate(slider) is True

    # Page vide → escalade
    assert P.should_escalate("<html><body></body></html>") is True


def test_should_escalate_short_circuits_on_real_images():
    # ≥3 vraies images → PAS d'escalade, même si un marqueur slider est présent.
    html = ("<html><body>"
            + "".join(f'<img src="/p{i}.jpg">' for i in range(3))
            + '<div class="twentytwenty-container"></div></body></html>')
    assert P.should_escalate(html) is False


def test_should_escalate_counts_data_src_as_real():
    # Images lazy-load (data-src) lisibles en statique → comptées réelles → pas d'escalade.
    html = ("<html><body>"
            + "".join(f'<img data-src="/p{i}.jpg">' for i in range(3))
            + "</body></html>")
    assert P.should_escalate(html) is False


HTML_BA = """
<html><body>
  <div class="twentytwenty-container">
    <img src="/avant1.jpg" alt="Avant">
    <img src="/apres1.jpg" alt="Après">
  </div>
  <figure class="before-after">
    <img src="/b2.jpg"><img src="/a2.jpg">
  </figure>
</body></html>
"""


def test_extract_gallery_pairs():
    pairs = P.extract_gallery_pairs(HTML_BA, "https://x.test/")
    assert {"before_url": "https://x.test/avant1.jpg",
            "after_url": "https://x.test/apres1.jpg", "caption": None} in pairs
    assert any(p["before_url"].endswith("/b2.jpg") and p["after_url"].endswith("/a2.jpg") for p in pairs)


def test_extract_gallery_pairs_none():
    assert P.extract_gallery_pairs("<html><body><img src='/x.jpg'></body></html>", "https://x.test/") == []
