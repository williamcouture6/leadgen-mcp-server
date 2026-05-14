"""Tool `enrich` — Apollo (Phase 1B, validable après purchase Basic).

Apollo expose plusieurs endpoints utiles :
  - POST organizations/enrich   : enrichit une entreprise par domain (~1 credit)
  - POST mixed_people/search    : liste les personnes (filtres org + titles)
  - POST people/match           : matche/enrichit 1 personne par nom + org

Plan de cache (table enrichment_cache, expires 90j) :
  - clé "apollo:org:<domain>"        → payload organizations/enrich
  - clé "apollo:people:<org_id>:<roles_hash>" → payload mixed_people/search
  - clé "apollo:match:<email>"      → payload people/match

⚠️  Le plan Apollo Basic en trial bloque mixed_people/search et people/match (403).
Phase 1B sera lancée après purchase (~20 mai 2026).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from .. import supabase_client as db
from ..config import settings

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"

# Titres ciblés pour décideur PME (en EN, Apollo normalise FR-QC vers EN)
DEFAULT_DECISION_MAKER_TITLES: list[str] = [
    "owner",
    "founder",
    "co-founder",
    "ceo",
    "president",
    "general manager",
    "managing director",
    "operations manager",
    "vp operations",
    "director",
]

CACHE_TTL_DAYS = 90


# ----------------------------------------------------------------------
# Cache helpers
# ----------------------------------------------------------------------

async def _cache_get(provider: str, key: str) -> dict[str, Any] | None:
    rows = await db.select(
        "enrichment_cache",
        params={
            "select": "payload,expires_at",
            "provider": f"eq.{provider}",
            "key": f"eq.{key}",
            "limit": "1",
        },
    )
    if not rows:
        return None
    if rows[0]["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None
    return rows[0]["payload"]


async def _cache_put(provider: str, key: str, payload: dict[str, Any]) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)).isoformat()
    await db.insert(
        "enrichment_cache",
        {
            "provider": provider,
            "key": key,
            "payload": payload,
            "expires_at": expires,
        },
        on_conflict="provider,key",
    )


def _apollo_headers() -> dict[str, str]:
    api_key = settings().apollo_api_key
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY manquant")
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    }


# ----------------------------------------------------------------------
# 1) organizations/enrich (fonctionne sur trial)
# ----------------------------------------------------------------------

class EnrichOrgIn(BaseModel):
    domain: str


class EnrichOrgOut(BaseModel):
    domain: str
    organization_id: str | None = None
    name: str | None = None
    industry: str | None = None
    estimated_num_employees: int | None = None
    website_url: str | None = None
    linkedin_url: str | None = None
    raw: dict[str, Any] | None = None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def enrich_org(payload: EnrichOrgIn) -> EnrichOrgOut:
    key = payload.domain.lower().strip()
    cached = await _cache_get("apollo_org", key)
    if cached:
        return _org_from_payload(payload.domain, cached)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{APOLLO_BASE_URL}/organizations/enrich",
            headers=_apollo_headers(),
            params={"domain": payload.domain},
        )
        r.raise_for_status()
        data = r.json()

    await _cache_put("apollo_org", key, data)
    return _org_from_payload(payload.domain, data)


def _org_from_payload(domain: str, data: dict[str, Any]) -> EnrichOrgOut:
    org = (data or {}).get("organization") or {}
    return EnrichOrgOut(
        domain=domain,
        organization_id=org.get("id"),
        name=org.get("name"),
        industry=org.get("industry"),
        estimated_num_employees=org.get("estimated_num_employees"),
        website_url=org.get("website_url"),
        linkedin_url=org.get("linkedin_url"),
        raw=data,
    )


# ----------------------------------------------------------------------
# 2) mixed_people/search (paid plan only — Phase 1B après purchase)
# ----------------------------------------------------------------------

class SearchDecisionMakersIn(BaseModel):
    organization_id: str | None = None
    organization_name: str | None = None
    titles: list[str] = Field(default_factory=lambda: DEFAULT_DECISION_MAKER_TITLES.copy())
    per_page: int = 5


class PersonHit(BaseModel):
    apollo_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


class SearchDecisionMakersOut(BaseModel):
    people: list[PersonHit]
    raw: dict[str, Any] | None = None


def _roles_hash(titles: list[str]) -> str:
    norm = ",".join(sorted(t.lower().strip() for t in titles))
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def search_decision_makers(payload: SearchDecisionMakersIn) -> SearchDecisionMakersOut:
    if not payload.organization_id and not payload.organization_name:
        raise ValueError("organization_id ou organization_name requis")

    cache_key_parts = [
        payload.organization_id or payload.organization_name or "",
        _roles_hash(payload.titles),
        str(payload.per_page),
    ]
    cache_key = ":".join(cache_key_parts)
    cached = await _cache_get("apollo_people", cache_key)
    if cached:
        return _people_from_payload(cached)

    body: dict[str, Any] = {
        "person_titles": payload.titles,
        "per_page": payload.per_page,
        "page": 1,
    }
    if payload.organization_id:
        body["organization_ids"] = [payload.organization_id]
    elif payload.organization_name:
        body["q_organization_name"] = payload.organization_name

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Apollo a déprécié `mixed_people/search` pour les API callers (2025+).
        # Le nouvel endpoint `mixed_people/api_search` accepte le même body.
        r = await client.post(
            f"{APOLLO_BASE_URL}/mixed_people/api_search",
            headers=_apollo_headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    await _cache_put("apollo_people", cache_key, data)
    return _people_from_payload(data)


def _people_from_payload(data: dict[str, Any]) -> SearchDecisionMakersOut:
    hits: list[PersonHit] = []
    for p in (data or {}).get("people", []) or []:
        hits.append(
            PersonHit(
                apollo_id=p.get("id"),
                first_name=p.get("first_name"),
                last_name=p.get("last_name"),
                title=p.get("title"),
                seniority=p.get("seniority"),
                email=p.get("email"),
                email_status=p.get("email_status"),
                phone=(p.get("phone_numbers") or [{}])[0].get("sanitized_number")
                if p.get("phone_numbers")
                else None,
                linkedin_url=p.get("linkedin_url"),
            )
        )
    return SearchDecisionMakersOut(people=hits, raw=data)


# ----------------------------------------------------------------------
# 3) people/match (paid plan only — enrichir 1 personne avec email vérifié)
# ----------------------------------------------------------------------

class MatchPersonIn(BaseModel):
    # Apollo Basic obfusque last_name dans mixed_people/api_search (ex: "Ro***i").
    # Quand on a l'apollo_id de la search, le passer ici contourne le besoin de last_name.
    apollo_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    organization_name: str | None = None
    domain: str | None = None
    reveal_personal_emails: bool = False  # mettre True consomme crédits


class MatchPersonOut(BaseModel):
    matched: bool
    apollo_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    raw: dict[str, Any] | None = None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def match_person(payload: MatchPersonIn) -> MatchPersonOut:
    if not payload.apollo_id and not (payload.first_name and payload.last_name):
        raise ValueError("apollo_id requis OU first_name + last_name")

    if payload.apollo_id:
        cache_key = f"id:{payload.apollo_id}"
    else:
        cache_key = f"{payload.first_name}|{payload.last_name}|{payload.organization_name or payload.domain or ''}".lower()
    cached = await _cache_get("apollo_match", cache_key)
    if cached:
        return _match_from_payload(cached)

    body: dict[str, Any] = {
        "reveal_personal_emails": payload.reveal_personal_emails,
    }
    if payload.apollo_id:
        body["id"] = payload.apollo_id
    else:
        body["first_name"] = payload.first_name
        body["last_name"] = payload.last_name
        if payload.organization_name:
            body["organization_name"] = payload.organization_name
        if payload.domain:
            body["domain"] = payload.domain

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{APOLLO_BASE_URL}/people/match",
            headers=_apollo_headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    await _cache_put("apollo_match", cache_key, data)
    return _match_from_payload(data)


def _match_from_payload(data: dict[str, Any]) -> MatchPersonOut:
    person = (data or {}).get("person") or {}
    if not person:
        return MatchPersonOut(matched=False, raw=data)
    return MatchPersonOut(
        matched=True,
        apollo_id=person.get("id"),
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        email=person.get("email"),
        email_status=person.get("email_status"),
        phone=(person.get("phone_numbers") or [{}])[0].get("sanitized_number")
        if person.get("phone_numbers")
        else None,
        title=person.get("title"),
        linkedin_url=person.get("linkedin_url"),
        raw=data,
    )
