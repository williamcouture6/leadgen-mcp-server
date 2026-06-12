"""Assemblage PUR du brand_kit : mappers Google Places, merge final,
garde anti-clobber, requête Pexels. Sans réseau — testable directement."""
from __future__ import annotations

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


# Mapping industrie (companies.industry) → requête Pexels (EN, meilleurs résultats).
_PEXELS_QUERIES = {
    "toiture": "roofing contractor",
    "roofing": "roofing contractor",
    "plomberie": "plumber working",
    "plumbing": "plumber working",
    "electricite": "electrician working",
    "électricité": "electrician working",
    "renovation": "home renovation contractor",
    "rénovation": "home renovation contractor",
    "cvac": "hvac technician",
    "hvac": "hvac technician",
    "paysagement": "landscaping crew",
    "excavation": "excavation construction site",
    "deneigement": "snow removal truck",
    "peinture": "house painter",
}
_PEXELS_DEFAULT = "home renovation contractor"


def pexels_query_for_industry(industry: str | None) -> str:
    if not industry:
        return _PEXELS_DEFAULT
    key = industry.strip().lower()
    return _PEXELS_QUERIES.get(key, _PEXELS_DEFAULT)


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
) -> dict[str, Any]:
    confidence: dict[str, str] = {}
    kit: dict[str, Any] = {}

    # Déterministe (high) — Places / JSON-LD / extraction
    kit["phone"] = phone_from_places(place) or jsonld.get("telephone")
    kit["hours"] = hours_from_places(place)
    kit["reviews"] = reviews_from_places(place)
    kit["reviews_url"] = reviews_url_from_places(place)
    kit["social"] = social or None
    kit["rbq"] = rbq or llm.get("rbq")
    for f in ("phone", "hours", "reviews", "reviews_url", "social", "rbq"):
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
