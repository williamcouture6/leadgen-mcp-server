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


def test_hours_from_jsonld_mo_fr():
    h = A.hours_from_jsonld(["Mo-Fr 08:00-17:00", "Sa 09:00-12:00"])
    assert h.startswith("lundi: 08:00 – 17:00")
    assert "samedi: 09:00 – 12:00" in h
    assert "dimanche: Fermé" in h


def test_hours_from_jsonld_empty():
    assert A.hours_from_jsonld([]) is None
    assert A.hours_from_jsonld(["bidon"]) is None


def test_hours_from_jsonld_wraps_week():
    # Plage à cheval sur la semaine (Fr-Mo) → ven, sam, dim ET lun couverts.
    h = A.hours_from_jsonld(["Fr-Mo 10:00-14:00"])
    for jour in ("vendredi", "samedi", "dimanche", "lundi"):
        assert f"{jour}: 10:00 – 14:00" in h
    assert "mardi: Fermé" in h


def test_derive_review_flags():
    kit = {
        "hours": "lundi: 09:00 – 17:00", "phone": "+1 514-000-0000",
        "team": [{"nom": "Kevin", "photo_url": "u"}, {"nom": "Sam", "photo_url": None}],
        "confidence": {"hours": "medium", "phone": "high", "hero_image_url": "low",
                       "colors": "medium"},
        "hero_image_url": "u",
    }
    review = A.derive_review(kit)
    fields = {r["field"] for r in review}
    assert "hours" in fields          # confiance medium → à vérifier
    assert "phone" not in fields      # high → pas de flag
    assert "logo_url" in fields       # absent
    assert "team" in fields           # 1 membre sans photo
    assert "hero_image_url" in fields # confiance low (image de banque)
    assert "colors" in fields         # dérivée (medium)


def test_derive_review_clean_kit():
    kit = {"hours": "x", "phone": "y", "logo_url": "l",
           "confidence": {"hours": "high", "phone": "high"}}
    assert A.derive_review(kit) == []


def test_assemble_hours_jsonld_fallback_when_no_places():
    # Pas d'heures Places (place vide) mais openingHours JSON-LD → heures de secours, low.
    kit = A.assemble_brand_kit(
        place={}, jsonld={**_EMPTY_JSONLD, "opening_hours": ["Mo-Fr 08:00-17:00"]},
        head_meta=dict(_EMPTY_HEAD), llm=dict(_EMPTY_LLM),
        images={}, colors=None, social={}, rbq=None,
        company={"name": "X", "address": "Y"},
    )
    assert kit["hours"].startswith("lundi: 08:00 – 17:00")
    assert kit["confidence"]["hours"] == "low"


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
    assert kit["_meta"]["build_version"] == A.BUILD_VERSION


def _bare_assemble_kwargs(**over):
    base = dict(
        place={"reviews": []},
        jsonld={"same_as": [], "telephone": None, "rating": None, "rating_count": None,
                "logo": None, "opening_hours": [], "address": None, "image": None},
        head_meta={"theme_color": None, "og_image": None, "twitter_image": None,
                   "description": None, "icon": None},
        llm={}, images={}, colors=None, social={}, rbq=None,
    )
    base.update(over)
    return base


def test_assemble_service_areas_deterministic_overrides_llm():
    areas = ["Terrebonne", "Mascouche", "Laval", "Brossard", "Longueuil", "Mirabel"]
    kit = A.assemble_brand_kit(**_bare_assemble_kwargs(
        llm={"service_areas": ["Montréal", "Rive-Nord", "Rive-Sud"]},
        service_areas=areas,
    ))
    assert kit["service_areas"] == areas
    assert kit["confidence"]["service_areas"] == "high"


def test_assemble_service_areas_falls_back_to_llm_when_no_deterministic():
    kit = A.assemble_brand_kit(**_bare_assemble_kwargs(llm={"service_areas": ["Laval"]}))
    assert kit["service_areas"] == ["Laval"]
    assert kit["confidence"]["service_areas"] == "medium"


def test_preserve_nonempty_carries_over_when_new_empty():
    existing = {"services": [{"name": "Lavage de vitres"}], "team": [{"nom": "Kevin"}]}
    new = {"services": [], "tagline": "Nouveau slogan"}
    out, carried = A.preserve_nonempty(existing, new)
    assert out["services"] == [{"name": "Lavage de vitres"}]  # repris (new vide)
    assert out["team"] == [{"nom": "Kevin"}]                   # repris (new absent)
    assert out["tagline"] == "Nouveau slogan"                  # new conservé
    assert set(carried) == {"services", "team"}


def test_preserve_nonempty_keeps_new_when_present():
    out, carried = A.preserve_nonempty(
        {"services": [{"name": "Vieux"}]}, {"services": [{"name": "Nouveau"}]})
    assert out["services"] == [{"name": "Nouveau"}]
    assert carried == []


def test_preserve_nonempty_no_existing_returns_new():
    out, carried = A.preserve_nonempty(None, {"services": []})
    assert out == {"services": []}
    assert carried == []


def test_generic_process_window_cleaning():
    steps = A.generic_process_for_service("Lavage de vitres")
    assert len(steps) >= 3 and all("titre" in s and "texte" in s for s in steps)
    joined = " ".join(s["texte"] for s in steps).lower()
    assert "osmos" in joined or "raclette" in joined


def test_generic_process_gutter():
    steps = A.generic_process_for_service("Nettoyage de gouttières")
    joined = " ".join(s["texte"] for s in steps).lower()
    assert "descente" in joined


def test_generic_process_default_for_unknown_service():
    steps = A.generic_process_for_service("Comptabilité fiscale")
    assert steps[0]["titre"] == "Soumission gratuite"


def test_generic_home_service_faq_injects_real_areas():
    faq = A.generic_home_service_faq(["Laval", "Brossard", "Montréal"])
    assert all("question" in q and "reponse" in q for q in faq)
    regions = [q for q in faq if "gion" in q["question"].lower()]  # 'région(s)'
    assert regions and "Laval" in regions[0]["reponse"]


def test_generic_home_service_faq_default_no_areas():
    faq = A.generic_home_service_faq()
    assert len(faq) >= 2 and all(q["question"] and q["reponse"] for q in faq)


def test_finalize_flex_pages_slugify_reserved_dedupe():
    pages = [
        {"titre": "Financement Maison", "blocs": [{"type": "texte", "corps": "a"}]},
        {"slug": "Contact", "titre": "Nous joindre", "blocs": [{"type": "texte", "corps": "b"}]},  # réservé -> drop
        {"titre": "Financement Maison", "blocs": [{"type": "texte", "corps": "c"}]},  # doublon slug -> drop
        {"titre": "Garanties & Assurances", "blocs": [{"type": "liste", "items": ["x"]}]},
    ]
    out = A.finalize_flex_pages(pages)
    slugs = [p["slug"] for p in out]
    assert slugs == ["financement-maison", "garanties-assurances"]
    assert all(p["nav"] is True for p in out)
    # le 1er gagne sur le doublon
    assert out[0]["blocs"][0]["corps"] == "a"


def test_pexels_profile_matches_paysagiste_variants():
    # BUG : industry='paysagiste' tombait sur le défaut « home renovation » (image salle de bain).
    for ind in ["paysagiste", "Paysagiste", "aménagement paysager", "entretien paysager", "paysagement"]:
        q = A.pexels_query_for_industry(ind).lower()
        assert ("landscap" in q) or ("garden" in q), (ind, q)
    assert "renovation" not in A.pexels_query_for_industry("paysagiste").lower()


def test_pexels_query_for_service_landscaping_keywords():
    f = lambda n: A.pexels_query_for_service(n, "paysagiste").lower()
    assert "lawn" in f("Tonte de pelouse")
    assert "hedge" in f("Taille / Rabattage de haies et d'arbustes")
    assert "aeration" in f("Aération / Déchaumage")
    # services distincts → requêtes distinctes (sinon même image)
    assert f("Tonte de pelouse") != f("Terreautage et Ensemencement")
    # service paysagiste sans mot-clé reconnu → requête paysagement (jamais rénovation)
    g = f("Ouverture / Fermeture de terrains")
    assert ("landscap" in g) or ("garden" in g) or ("lawn" in g)
    assert "renovation" not in g


def test_pick_index_deterministic_varies_by_seed_and_safe():
    assert A.pick_index(10, "x|hero|q") == A.pick_index(10, "x|hero|q")   # déterministe
    idxs = {A.pick_index(10, f"company{i}|hero|landscaping crew") for i in range(8)}
    assert len(idxs) >= 2                          # variété entre compagnies
    assert 0 <= A.pick_index(10, "z") < 10
    assert A.pick_index(0, "z") == 0               # garde : aucune photo
    assert A.pick_index(1, "z") == 0


def test_derive_review_flags_verbatim_flex_pages():
    kit = {
        "confidence": {},
        "pages": [
            {"slug": "financement", "blocs": [{"type": "stats",
              "items": [{"valeur": "0 %", "label": "intérêt 12 mois"}]}]},
            {"slug": "equipe-bis", "blocs": [{"type": "texte", "corps": "Service de 8 h à 17 h."}]},
            {"slug": "tarifs", "blocs": [{"type": "texte", "corps": "Dès 199 $ la visite."}]},
        ],
    }
    review = A.derive_review(kit)
    reasons = {r["field"]: r["reason"] for r in review}
    assert "pages:financement" in reasons   # bloc stats
    assert "pages:tarifs" in reasons         # contient un prix ($)
    assert "pages:equipe-bis" not in reasons # ni stats ni prix
