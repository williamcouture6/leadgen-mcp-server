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
