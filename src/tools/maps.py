"""Tool `maps` — Google Places API (New / v1).

On utilise `places:searchText` (POST) car c'est la méthode la plus flexible :
- Accepte une query naturelle ("restaurant in Montréal QC")
- Gère bien les villes québécoises avec accents
- Retourne 20 résultats/page, ~60 résultats max via pagination

Doc : https://developers.google.com/maps/documentation/places/web-service/text-search
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Champs récupérés (impacte la facturation Google — voir SKUs Text Search Pro/Enterprise).
# On reste sur les champs Pro pour éviter le tier Enterprise (plus cher).
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.location",
    "places.websiteUri",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
    "places.nationalPhoneNumber",
    "nextPageToken",
])


class SearchPlacesIn(BaseModel):
    city: str
    sector: str
    page_token: str | None = None
    region_code: str = "CA"
    language_code: str = "fr-CA"
    max_results: int = 20  # 1..20 par page côté API


class PlaceResult(BaseModel):
    google_place_id: str
    name: str
    formatted_address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    website: str | None = None
    domain: str | None = None
    phone: str | None = None
    google_types: list[str] = []
    primary_type: str | None = None
    business_status: str | None = None
    google_rating: float | None = None
    google_reviews_count: int | None = None
    raw_payload: dict[str, Any] | None = None


class SearchPlacesOut(BaseModel):
    results: list[PlaceResult]
    next_page_token: str | None = None


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
        return host.lower().lstrip("www.") or None
    except Exception:  # noqa: BLE001
        return None


def _extract_address_part(components: list[dict[str, Any]] | None, target_type: str) -> str | None:
    if not components:
        return None
    for c in components:
        if target_type in c.get("types", []):
            return c.get("shortText") or c.get("longText")
    return None


def _map_place(p: dict[str, Any]) -> PlaceResult:
    components = p.get("addressComponents")
    location = p.get("location") or {}
    website = p.get("websiteUri")
    return PlaceResult(
        google_place_id=p["id"],
        name=(p.get("displayName") or {}).get("text", ""),
        formatted_address=p.get("formattedAddress"),
        city=_extract_address_part(components, "locality")
        or _extract_address_part(components, "administrative_area_level_3"),
        postal_code=_extract_address_part(components, "postal_code"),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        website=website,
        domain=_domain_from_url(website),
        phone=p.get("nationalPhoneNumber"),
        google_types=p.get("types", []) or [],
        primary_type=p.get("primaryType"),
        business_status=p.get("businessStatus"),
        google_rating=p.get("rating"),
        google_reviews_count=p.get("userRatingCount"),
        raw_payload=p,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def search_places(payload: SearchPlacesIn) -> SearchPlacesOut:
    body: dict[str, Any] = {
        "textQuery": f"{payload.sector} in {payload.city}, Québec, Canada",
        "regionCode": payload.region_code,
        "languageCode": payload.language_code,
        "pageSize": max(1, min(20, payload.max_results)),
    }
    if payload.page_token:
        body["pageToken"] = payload.page_token

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings().google_places_api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(PLACES_SEARCH_URL, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    places = [_map_place(p) for p in data.get("places", [])]
    return SearchPlacesOut(results=places, next_page_token=data.get("nextPageToken"))
