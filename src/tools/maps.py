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
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings
from ..lib.platform_domains import PLATFORM_DOMAINS_NEVER_USE

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
    """Extrait le domain (sans `www.`) d'un URL avec scheme.

    Retourne `None` si l'URL est vide, malformé, OU si le domain est une
    plateforme tierce (Facebook, Instagram, DoorDash, Wix, etc.) — voir
    `lib.platform_domains` pour le pourquoi (bug 2026-05-14 sur emails
    @meta.com insérés pour des cafés).
    """
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return None
        if host in PLATFORM_DOMAINS_NEVER_USE:
            return None
        return host
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


PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"

# Le MÊME jeu de champs que `FIELD_MASK`, mais sans le préfixe `places.` — Text
# Search masque une LISTE (`places.id`), Place Details masque la ressource
# elle-même (`id`). Se tromper de forme rend un 400 dont le message ne dit pas
# lequel des deux est en cause.
# ⚠️ On le DÉRIVE au lieu de le recopier : deux listes de champs finiraient par
# diverger, et la divergence serait invisible — une fiche hydratée sans
# `websiteUri` ne casse rien, elle devient juste éternellement inexploitable.
PLACE_DETAILS_FIELD_MASK = ",".join(
    champ.removeprefix("places.")
    for champ in FIELD_MASK.split(",")
    if champ != "nextPageToken"
)


class PlaceIntrouvable(Exception):
    """Google ne connaît plus ce `place_id`.

    Les identifiants Google PÉRIMENT — la doc recommande de les rafraîchir au
    bout de 12 mois. Un inventaire vieux de plusieurs mois en contiendra
    forcément quelques-uns. C'est une exception à part parce que l'appelant doit
    la traiter autrement qu'une panne : elle est DÉFINITIVE, il ne sert à rien
    de réessayer, alors qu'un 429 ou un 503 méritent une reprise.
    """


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    # ⚠️ On ne rejoue QUE les erreurs de transport. `search_places`, juste en
    # dessous, porte un `@retry` sans ce filtre : il rejoue n'importe quelle
    # exception, 400 compris. Sur une hydratation en lot, rejouer trois fois un
    # identifiant périmé coûte 9 s de backoff par fiche morte.
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
async def _get_place_http(place_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            PLACE_DETAILS_URL + place_id,
            headers={
                "X-Goog-Api-Key": settings().google_places_api_key,
                "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
            },
        )
        if r.status_code == 404:
            raise PlaceIntrouvable(f"404 sur {place_id}: {r.text[:160]}")
        if r.status_code == 400:
            # 🔴 UN 400 N'EST PAS FORCÉMENT UN IDENTIFIANT MORT — ET LES DEUX
            # CAS NE SE PAIENT PAS PAREIL.
            #
            # Ce fichier se contredisait : le commentaire de
            # `PLACE_DETAILS_FIELD_MASK` dit qu'un masque de la mauvaise FORME
            # rend un 400 — donc un 400 causé par NOUS — et le code d'à côté
            # traitait tout 400 comme un identifiant périmé, donc comme
            # définitif. Or `ecartee` est documentée par la 0070 comme « un
            # cache que rien n'invalide » : un masque cassé un matin aurait
            # enterré les 20 fiches du lot, puis 20 de plus le lendemain,
            # récupérables seulement par un `like` SQL à la main.
            #
            # L'asymétrie décide : un faux `echec` sur un identifiant mort coûte
            # une place de file ; un faux `ecartee` sur un lot sain coûte
            # l'inventaire. Dans le doute, on NE écarte PAS.
            corps = r.text[:300]
            if "NOT_FOUND" in corps or "Invalid resource" in corps:
                raise PlaceIntrouvable(f"400 sur {place_id}: {corps[:160]}")
            raise ValueError(f"400 masque/parametre sur {place_id}: {corps[:160]}")
        r.raise_for_status()
        return r.json()


async def get_place(place_id: str) -> PlaceResult:
    """Hydrate UNE entreprise à partir de son identifiant.

    C'est la passe B de l'architecture en deux temps : le balayage découvre des
    identifiants pour 0 $, et on ne paie les détails QUE pour ce qu'on va
    vraiment traiter. Facturé au SKU Place Details Enterprise.

    Lève `PlaceIntrouvable` si Google ne connaît plus l'identifiant.
    """
    return _map_place(await _get_place_http(place_id))


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
