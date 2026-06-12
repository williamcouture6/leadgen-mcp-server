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
