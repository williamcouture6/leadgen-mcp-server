"""Assemblage PUR du brand_kit : mappers Google Places, merge final,
garde anti-clobber, requête Pexels. Sans réseau — testable directement."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

BUILD_VERSION = "1"


def reviews_from_places(place: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rv in (place.get("reviews") or []):
        text = (rv.get("text") or {}).get("text") or (rv.get("originalText") or {}).get("text")
        if not text:
            continue
        author = (rv.get("authorAttribution") or {}).get("displayName")
        avatar = (rv.get("authorAttribution") or {}).get("photoUri")
        out.append({
            "author": author,
            "rating": rv.get("rating"),
            "quote": text,
            "date": rv.get("relativePublishTimeDescription"),
            "avatar_url": avatar,
            "source": "google",
        })
    return out


def phone_from_places(place: dict[str, Any]) -> str | None:
    return place.get("internationalPhoneNumber") or None


def reviews_url_from_places(place: dict[str, Any]) -> str | None:
    return place.get("googleMapsUri") or None


def hours_from_places(place: dict[str, Any]) -> str | None:
    desc = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    return " · ".join(desc) if desc else None


# ----------------------------------------------------------------------
# Vérification du match Google Places (EXACTITUDE des faits) — un mauvais
# match = heures/adresse/avis d'un AUTRE commerce. On ne tire des faits de
# Places que si nom + adresse concordent avec la company.
# ----------------------------------------------------------------------

_POSTAL_RE = re.compile(r"[A-Za-z]\d[A-Za-z]\s*\d[A-Za-z]\d")
_GENERIC_NAME_TOKENS = {
    "inc", "enr", "ltee", "ltd", "llc", "co", "service", "services", "les", "des",
    "du", "de", "la", "le", "et", "and", "the",
}


def _tokens(s: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", _norm_industry(s)) if len(t) >= 2}


def _name_match(company_name: str | None, place_name: str | None) -> bool:
    a = _tokens(company_name) - _GENERIC_NAME_TOKENS
    b = _tokens(place_name) - _GENERIC_NAME_TOKENS
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.5


def _postal(s: str | None) -> str | None:
    m = _POSTAL_RE.search(s or "")
    return m.group(0).replace(" ", "").lower() if m else None


def _address_match(company_addr: str | None, place_addr: str | None) -> bool:
    if not company_addr or not place_addr:
        return False
    pa, pb = _postal(company_addr), _postal(place_addr)
    if pa and pb:
        return pa == pb
    # à défaut de code postal : au moins un token significatif commun (ville, rue).
    return bool((_tokens(company_addr) & _tokens(place_addr)) - _GENERIC_NAME_TOKENS)


def places_match_ok(place: dict[str, Any] | None, company: dict[str, Any] | None) -> bool:
    """True si le résultat Places est bien la company (nom + adresse concordent).

    `company` None → rien à vérifier → True (compat). Sinon nom ET adresse requis."""
    if not place or not company:
        return True
    name_ok = _name_match(company.get("name"), (place.get("displayName") or {}).get("text"))
    addr_ok = _address_match(company.get("address"), place.get("formattedAddress"))
    return name_ok and addr_ok


_PEXELS_DEFAULT = "home renovation contractor"


def _norm_industry(s: str | None) -> str:
    """minuscule + sans accents + trim — pour matcher industrie/nom de service FR."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


# Profil Pexels par industrie (requêtes EN — meilleurs résultats), une requête par rôle :
#   hero    = grande image héros représentative du métier
#   stats   = plan « cinématographique » (fond de la bande statistiques)
#   gallery = paire (avant « sale/abîmé », après « propre/fini »)
#   service = défaut pour une carte service sans candidat réel
_INDUSTRY_PROFILES: dict[str, dict[str, Any]] = {
    "lavage de vitres": {
        "hero": "professional window cleaner washing glass",
        "stats": "window cleaner high-rise building",
        "gallery": ("dirty grimy window glass", "sparkling clean shiny window"),
        "service": "window cleaning",
    },
    "toiture": {
        "hero": "roofing contractor",
        "stats": "roofer working on roof",
        "gallery": ("damaged old roof shingles", "new finished roof shingles"),
        "service": "roof repair",
    },
    "plomberie": {
        "hero": "plumber working",
        "stats": "plumber fixing pipes",
        "gallery": ("leaking rusty pipe", "new clean plumbing pipes"),
        "service": "plumbing repair",
    },
    "electricite": {
        "hero": "electrician working",
        "stats": "electrician electrical panel",
        "gallery": ("messy old electrical wiring", "tidy new electrical panel"),
        "service": "electrical work",
    },
    "renovation": {
        "hero": "home renovation contractor",
        "stats": "home renovation construction site",
        "gallery": ("old worn room before renovation", "modern renovated room"),
        "service": "home renovation",
    },
    "cvac": {
        "hero": "hvac technician",
        "stats": "hvac technician air conditioning",
        "gallery": ("old dirty air conditioner unit", "new clean hvac unit"),
        "service": "hvac installation",
    },
    "paysagement": {
        "hero": "landscaping crew",
        "stats": "landscaped garden backyard",
        "gallery": ("overgrown messy yard", "manicured landscaped garden"),
        "service": "landscaping",
    },
    "excavation": {
        "hero": "excavation construction site",
        "stats": "excavator construction site",
        "gallery": ("dirt construction site", "finished graded building lot"),
        "service": "excavation",
    },
    "deneigement": {
        "hero": "snow removal truck",
        "stats": "snow plow truck winter",
        "gallery": ("driveway covered in deep snow", "cleared plowed driveway"),
        "service": "snow removal",
    },
    "peinture": {
        "hero": "house painter",
        "stats": "painter painting wall",
        "gallery": ("old peeling paint wall", "freshly painted smooth wall"),
        "service": "house painting",
    },
}

_DEFAULT_PROFILE: dict[str, Any] = {
    "hero": _PEXELS_DEFAULT,
    "stats": "home renovation construction site",
    "gallery": ("old worn house exterior", "renovated modern house exterior"),
    "service": _PEXELS_DEFAULT,
}

# Synonymes (anglais / variantes) → clé canonique du profil.
_INDUSTRY_ALIASES = {
    "roofing": "toiture",
    "couvreur": "toiture",
    "plumbing": "plomberie",
    "plumber": "plomberie",
    "electrician": "electricite",
    "electrical": "electricite",
    "hvac": "cvac",
    "window cleaning": "lavage de vitres",
    "lavage de fenetres": "lavage de vitres",
    "nettoyage de vitres": "lavage de vitres",
}

# Mot-clé du nom de service (normalisé, sans accent) → requête Pexels spécifique.
# Ordre important : du plus spécifique au plus générique.
_SERVICE_KEYWORDS: list[tuple[str, str]] = [
    ("goutti", "gutter cleaning"),                       # gouttière(s)
    ("soffite", "soffit and fascia cleaning"),
    ("pression", "pressure washing house exterior"),
    ("apres-construction", "post construction cleaning"),
    ("apres construction", "post construction cleaning"),
    ("construction", "post construction cleaning"),
    ("vitre", "window cleaning"),
    ("fenetre", "window cleaning"),
    ("copropriet", "commercial building window cleaning"),
    ("commercial", "commercial building cleaning"),
    ("toiture", "roof repair"),
    ("plomb", "plumbing repair"),
    ("electr", "electrical work"),
    ("peinture", "house painting"),
    ("paysage", "landscaping"),
    ("deneig", "snow removal"),
    ("excavation", "excavation site"),
]


def _profile(industry: str | None) -> dict[str, Any]:
    n = _norm_industry(industry)
    n = _INDUSTRY_ALIASES.get(n, n)
    return _INDUSTRY_PROFILES.get(n, _DEFAULT_PROFILE)


def pexels_query_for_industry(industry: str | None) -> str:
    """Requête héros (grande image du métier)."""
    return _profile(industry)["hero"]


def pexels_stats_query(industry: str | None) -> str:
    """Requête « cinématographique » pour le fond de la bande statistiques."""
    return _profile(industry)["stats"]


def pexels_gallery_queries(industry: str | None) -> tuple[str, str]:
    """Paire (avant, après) pour la galerie avant/après du métier."""
    return _profile(industry)["gallery"]


def pexels_query_for_service(service_name: str | None, industry: str | None) -> str:
    """Requête spécifique à un service (par mots-clés du nom) ; défaut = service de l'industrie."""
    n = _norm_industry(service_name)
    for sub, query in _SERVICE_KEYWORDS:
        if sub in n:
            return query
    return _profile(industry)["service"]


def should_write(existing: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    """Garde anti-clobber : ne jamais écraser un brand_kit corrigé à la main."""
    if not existing:
        return True
    return (existing.get("_meta") or {}).get("reviewed") is not True


def _conf(value: Any, level: str, acc: dict[str, str], field: str) -> None:
    if value:
        acc[field] = level


def assemble_brand_kit(
    *,
    place: dict[str, Any],
    jsonld: dict[str, Any],
    head_meta: dict[str, Any],
    llm: dict[str, Any],
    images: dict[str, str | None],
    colors: dict[str, Any] | None,
    social: dict[str, str],
    rbq: str | None,
    company: dict[str, Any] | None = None,
    facebook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    confidence: dict[str, str] = {}
    kit: dict[str, Any] = {}
    facebook = facebook or {}

    # Faits Places (heures/avis/téléphone/adresse) UNIQUEMENT si le match est confirmé.
    match_ok = places_match_ok(place, company)

    # Téléphone : Places (si match) → JSON-LD → Facebook.
    places_phone = phone_from_places(place) if match_ok else None
    kit["phone"] = places_phone or jsonld.get("telephone") or facebook.get("phone")
    if kit["phone"]:
        confidence["phone"] = "high" if places_phone else "medium"

    # Heures : seulement Places (bon format FR localisé) et seulement si match confirmé.
    # En cas de doute → vide + pas de confidence (jamais deviner les heures).
    kit["hours"] = hours_from_places(place) if match_ok else None
    if kit["hours"]:
        confidence["hours"] = "high"

    # Avis / lien avis : écartés si le match échoue (avis d'un autre commerce).
    kit["reviews"] = reviews_from_places(place) if match_ok else []
    kit["reviews_url"] = reviews_url_from_places(place) if match_ok else None
    kit["social"] = social or None
    kit["rbq"] = rbq or llm.get("rbq")
    for f in ("reviews", "reviews_url", "social", "rbq"):
        _conf(kit.get(f), "high", confidence, f)

    # Couleurs (high si theme-color/JSON-LD, medium si dérivé du logo) — décidé en amont.
    if colors and colors.get("primary"):
        kit["colors"] = {"primary": colors.get("primary"), "secondary": colors.get("secondary")}
        confidence["colors"] = colors.get("_confidence", "medium")

    # Images ré-hébergées (medium ; low si Pexels — porté par images["_source_*"])
    src_map = {k: v for k, v in images.items() if not k.startswith("_")}
    for role, url in src_map.items():
        if not url:
            continue
        field = {"logo": "logo_url", "hero": "hero_image_url", "team": "team_photo_url"}.get(role)
        if field:
            kit[field] = url
            confidence[field] = images.get(f"_source_{role}", "medium")

    # LLM (medium) — texte + structures
    for f in ("tagline", "team", "faq", "legal", "valeurs", "stats",
              "services", "service_areas"):
        val = llm.get(f)
        if val:
            kit[f] = val
            confidence[f] = "medium"

    kit["confidence"] = confidence
    kit["_meta"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reviewed": False,
        "source": "mixed",
        "build_version": BUILD_VERSION,
    }
    # purge des clés None de premier niveau (sauf structures voulues)
    return {k: v for k, v in kit.items() if v is not None}
