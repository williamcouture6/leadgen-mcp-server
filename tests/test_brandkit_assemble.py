from src.lib import brandkit_assemble as A

PLACE = {
    "internationalPhoneNumber": "+1 450-555-0192",
    "googleMapsUri": "https://maps.google.com/?cid=123",
    "regularOpeningHours": {"weekdayDescriptions": ["lundi: 08:00–18:00", "mardi: 08:00–18:00"]},
    "reviews": [
        {"rating": 5, "relativePublishTimeDescription": "il y a 3 jours",
         "text": {"text": "Travail impeccable"},
         "authorAttribution": {"displayName": "Marie L.", "photoUri": "https://x.test/a.png"}},
    ],
}

def test_reviews_from_places():
    r = A.reviews_from_places(PLACE)
    assert r == [{"author": "Marie L.", "rating": 5, "quote": "Travail impeccable",
                  "date": "il y a 3 jours", "avatar_url": "https://x.test/a.png", "source": "google"}]

def test_reviews_from_places_empty():
    assert A.reviews_from_places({}) == []

def test_phone_and_url_and_hours():
    assert A.phone_from_places(PLACE) == "+1 450-555-0192"
    assert A.reviews_url_from_places(PLACE) == "https://maps.google.com/?cid=123"
    assert A.hours_from_places(PLACE) == "lundi: 08:00–18:00 · mardi: 08:00–18:00"
    assert A.hours_from_places({}) is None

def test_pexels_query_for_industry():
    assert A.pexels_query_for_industry("toiture") == "roofing contractor"
    assert A.pexels_query_for_industry("plomberie") == "plumber working"
    assert A.pexels_query_for_industry(None) == "home renovation contractor"

def test_should_write_rules():
    assert A.should_write(None, {"_meta": {"reviewed": False}}) is True
    assert A.should_write({"_meta": {"reviewed": False}}, {}) is True
    assert A.should_write({"_meta": {"reviewed": True}}, {}) is False

def test_assemble_brand_kit_places_wins_and_confidence():
    kit = A.assemble_brand_kit(
        place={"internationalPhoneNumber": "+1 450-555-0192", "reviews": []},
        jsonld={"same_as": ["https://facebook.com/x"], "telephone": None,
                "rating": None, "rating_count": None, "logo": None,
                "opening_hours": [], "address": None, "image": None},
        head_meta={"theme_color": "#0B5", "og_image": None, "twitter_image": None,
                   "description": None, "icon": None},
        llm={"tagline": "Rénovation clé en main", "services": [], "valeurs": [],
             "faq": [], "stats": {}, "service_areas": ["Laval"], "team": [],
             "rbq": None, "legal": {}},
        images={"logo": "https://cdn/logo.png", "hero": "https://cdn/hero.jpg"},
        colors={"primary": "#0B5", "secondary": None},
        social={"facebook": "https://facebook.com/x"},
        rbq="1234-5678-01",
    )
    assert kit["phone"] == "+1 450-555-0192"
    assert kit["tagline"] == "Rénovation clé en main"
    assert kit["logo_url"] == "https://cdn/logo.png"
    assert kit["colors"] == {"primary": "#0B5", "secondary": None}
    assert kit["rbq"] == "1234-5678-01"
    assert kit["confidence"]["phone"] == "high"
    assert kit["confidence"]["tagline"] == "medium"
    assert kit["_meta"]["reviewed"] is False
    assert kit["_meta"]["build_version"] == A.BUILD_VERSION
