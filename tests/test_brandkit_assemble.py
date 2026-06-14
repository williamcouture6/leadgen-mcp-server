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


def test_pexels_query_for_industry_window_cleaning():
    # « lavage de vitres » doit donner une requête pertinente (pas le défaut rénovation).
    q = A.pexels_query_for_industry("lavage de vitres")
    assert "window" in q.lower()
    # tolérant aux accents / casse
    assert A.pexels_query_for_industry("Lavage de Vitres") == q


def test_pexels_stats_query_is_industry_specific():
    s = A.pexels_stats_query("lavage de vitres")
    assert "window" in s.lower()
    # défaut non vide pour industrie inconnue
    assert A.pexels_stats_query(None)
    assert A.pexels_stats_query("industrie inconnue")


def test_pexels_gallery_queries_returns_distinct_before_after():
    before, after = A.pexels_gallery_queries("lavage de vitres")
    assert "dirty" in before.lower()
    assert "clean" in after.lower()
    # toujours une paire, même industrie inconnue, et before != after
    b2, a2 = A.pexels_gallery_queries(None)
    assert b2 and a2 and b2 != a2


def test_pexels_query_for_service_uses_name_keywords():
    assert A.pexels_query_for_service("Nettoyage de gouttières", "lavage de vitres") == "gutter cleaning"
    assert A.pexels_query_for_service("Lavage à pression", "lavage de vitres") == "pressure washing house exterior"
    # nom de service non reconnu → défaut « service » de l'industrie
    assert A.pexels_query_for_service("Forfait spécial", "lavage de vitres") == "window cleaning"
    # service inconnu + industrie inconnue → défaut générique
    assert A.pexels_query_for_service("Truc", None) == "home renovation contractor"

def test_should_write_rules():
    assert A.should_write(None, {"_meta": {"reviewed": False}}) is True
    assert A.should_write({"_meta": {"reviewed": False}}, {}) is True
    assert A.should_write({"_meta": {"reviewed": True}}, {}) is False

_EMPTY_JSONLD = {"same_as": [], "telephone": None, "rating": None, "rating_count": None,
                 "logo": None, "opening_hours": [], "address": None, "image": None}
_EMPTY_HEAD = {"theme_color": None, "og_image": None, "twitter_image": None,
               "description": None, "icon": None, "apple_touch_icon": None, "icons": []}
_EMPTY_LLM = {"tagline": None, "services": [], "valeurs": [], "faq": [], "stats": {},
              "service_areas": [], "team": [], "rbq": None, "legal": {}}


def test_places_match_ok_name_and_postal():
    place = {"displayName": {"text": "BL Vitres"},
             "formattedAddress": "11233 Av. Jules-Paul-Tardivel, Montréal, QC H1G 4P1, Canada"}
    company = {"name": "BL Vitres Centre-Est",
               "address": "11233 Av. Jules-Paul-Tardivel, Montréal, QC H1G 4P1"}
    assert A.places_match_ok(place, company) is True


def test_places_match_fails_on_wrong_name():
    place = {"displayName": {"text": "Garage Pneus Plus"},
             "formattedAddress": "99 Rue X, Laval, QC H7A 1B2"}
    company = {"name": "BL Vitres Centre-Est",
               "address": "11233 Av. Jules-Paul-Tardivel, Montréal, QC H1G 4P1"}
    assert A.places_match_ok(place, company) is False


def test_places_match_none_company_is_compat_true():
    assert A.places_match_ok({"displayName": {"text": "X"}}, None) is True


def test_places_match_returns_name_and_addr_flags():
    place = {"displayName": {"text": "BL Vitres Centre-Est"},
             "formattedAddress": "11233 Av X, Montréal, QC H1G 4P1"}
    # bon nom, code postal divergent (annuaire ≠ Google)
    assert A.places_match(place, {"name": "BL Vitres Centre-Est",
                                  "address": "9481 Rue De Martigny, QC H1Z 2P1"}) == (True, False)
    # bon nom + bon code postal
    assert A.places_match(place, {"name": "BL Vitres Centre-Est",
                                  "address": "11233 Av X, Montréal, QC H1G 4P1"}) == (True, True)
    # mauvais commerce
    assert A.places_match(place, {"name": "Garage Pneus Plus",
                                  "address": "99 Rue Z, Laval, QC H7A 1B2"})[0] is False


def test_assemble_keeps_hours_on_name_match_addr_diff_but_medium():
    # Nom concorde (bonne entreprise) mais adresse diverge → on GARDE les heures Google Maps
    # (source autoritative) en abaissant la confidence, jamais les écarter.
    place = {"displayName": {"text": "BL Vitres Centre-Est"},
             "formattedAddress": "9481 Rue De Martigny, Montréal, QC H1Z 2P1",
             "internationalPhoneNumber": "+1 514-228-5119",
             "regularOpeningHours": {"weekdayDescriptions": ["lundi: 09:00 – 19:00"]},
             "reviews": []}
    kit = A.assemble_brand_kit(
        place=place, jsonld=dict(_EMPTY_JSONLD), head_meta=dict(_EMPTY_HEAD),
        llm=dict(_EMPTY_LLM), images={}, colors=None, social={}, rbq=None,
        company={"name": "BL Vitres Centre-Est", "address": "11233 Av X, Montréal, QC H1G 4P1"},
    )
    assert kit["hours"] == "lundi: 09:00 – 19:00"
    assert kit["confidence"]["hours"] == "medium"
    assert kit["phone"] == "+1 514-228-5119"
    assert kit["confidence"]["phone"] == "medium"


def test_assemble_drops_places_facts_on_mismatch():
    place = {"displayName": {"text": "Autre Commerce"},
             "formattedAddress": "1 rue Z, Québec, QC G1A 1A1",
             "internationalPhoneNumber": "+1 418-000-0000",
             "googleMapsUri": "https://maps.google.com/?cid=9",
             "regularOpeningHours": {"weekdayDescriptions": ["lundi: 09:00 – 17:00"]},
             "reviews": [{"rating": 5, "text": {"text": "x"},
                          "authorAttribution": {"displayName": "A"}}]}
    kit = A.assemble_brand_kit(
        place=place, jsonld=dict(_EMPTY_JSONLD), head_meta=dict(_EMPTY_HEAD),
        llm=dict(_EMPTY_LLM), images={}, colors=None, social={}, rbq=None,
        company={"name": "BL Vitres", "address": "11233 Av X, Montréal, QC H1G 4P1"},
    )
    assert "hours" not in kit                  # mismatch → heures Places écartées (None purgé)
    assert "phone" not in kit                  # idem téléphone (pas d'autre source)
    assert kit["reviews"] == []                # avis du mauvais commerce écartés
    assert kit["confidence"].get("hours") is None


def test_assemble_keeps_places_hours_when_match_ok():
    place = {"displayName": {"text": "BL Vitres"},
             "formattedAddress": "11233 Av X, Montréal, QC H1G 4P1, Canada",
             "internationalPhoneNumber": "+1 514-228-5119",
             "regularOpeningHours": {"weekdayDescriptions": ["lundi: 09:00 – 19:00"]},
             "reviews": []}
    kit = A.assemble_brand_kit(
        place=place, jsonld=dict(_EMPTY_JSONLD), head_meta=dict(_EMPTY_HEAD),
        llm=dict(_EMPTY_LLM), images={}, colors=None, social={}, rbq=None,
        company={"name": "BL Vitres Centre-Est", "address": "11233 Av X, Montréal, QC H1G 4P1"},
    )
    assert kit["hours"] == "lundi: 09:00 – 19:00"
    assert kit["phone"] == "+1 514-228-5119"
    assert kit["confidence"]["hours"] == "high"


def test_assemble_phone_falls_back_to_facebook():
    kit = A.assemble_brand_kit(
        place={}, jsonld=dict(_EMPTY_JSONLD), head_meta=dict(_EMPTY_HEAD),
        llm=dict(_EMPTY_LLM), images={}, colors=None, social={}, rbq=None,
        company={"name": "X", "address": "Y"}, facebook={"phone": "+1 514-555-0000"},
    )
    assert kit["phone"] == "+1 514-555-0000"
    assert kit["confidence"]["phone"] == "medium"


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
