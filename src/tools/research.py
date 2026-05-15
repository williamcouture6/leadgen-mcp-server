"""Tool `research` — Research Agent (WF-3).

Produit un `research_json` structuré pour une company à partir de :
  1. Google Places Details (re-fetch pour inclure les reviews — le FieldMask de WF-1
     n'inclut PAS `reviews` pour économiser les crédits)
  2. Scrape léger du site web (homepage + jusqu'à 2 pages "à propos/contact/services")
  3. Appel Claude Sonnet avec le prompt système de `src/prompts/research.md`

Le prompt système est marqué `cache_control: ephemeral` pour profiter du prompt
caching (~90% de réduction sur les tokens système après le 1er appel).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from anthropic import Anthropic
from anthropic import APIStatusError, RateLimitError, APIConnectionError
from bs4 import BeautifulSoup
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings

# ----------------------------------------------------------------------
# Google Places Details (avec reviews)
# ----------------------------------------------------------------------

PLACES_BASE = "https://places.googleapis.com/v1"

# Mêmes champs que dans le proto CLI (agents/lib/places.py) — inclut `reviews`.
PLACE_DETAILS_FIELD_MASK = ",".join([
    "id",
    "displayName",
    "formattedAddress",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "businessStatus",
    "regularOpeningHours",
    "primaryType",
    "types",
    "reviews",
    "googleMapsUri",
])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def fetch_place_details(google_place_id: str) -> dict[str, Any]:
    headers = {
        "X-Goog-Api-Key": settings().google_places_api_key,
        "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{PLACES_BASE}/places/{google_place_id}",
            headers=headers,
            params={"languageCode": "fr"},
        )
        r.raise_for_status()
        return r.json()


# ----------------------------------------------------------------------
# Website scraper (port de agents/lib/scraper.py — version async)
# ----------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; CoutureIA-Research/0.1; +https://couture-ia.com)"

PAGE_HINTS = (
    "propos", "about", "equipe", "team", "service", "contact", "tarif", "pricing",
)

TECH_KEYWORDS = (
    "chatbot", "intelligence artificielle", " ia ", "ai ", "automatisation",
    "agence numérique", "agence numerique", "powered by", "built with",
    "hubspot", "salesforce", "intercom", "drift", "zendesk",
)

# Email scraping (fallback pour les PME indépendantes non couvertes par Apollo).
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
EMAIL_BLOCKLIST_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse", "security", "webmaster", "spam",
}
EMAIL_BLOCKLIST_DOMAINS = {
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com",
    "googlegroups.com", "example.com", "domain.com",
}
EMAIL_GENERIC_LOCAL = {
    "info", "contact", "hello", "bonjour", "allo", "salut",
    "sales", "ventes", "vente", "admin", "marketing", "support", "service",
    "accueil", "reservation", "reservations", "booking", "commande",
    "commandes", "office", "general", "general-info", "direction",
}
# PME indépendantes publient souvent l'email perso du proprio sur leur site
# (ex: salons, traiteurs, micro-restos). On les accepte SEULEMENT si le local
# matche un pattern nominatif (≥2 segments alpha ou un seul token de ≥6 lettres).
EMAIL_PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "hotmail.ca", "hotmail.fr",
    "outlook.com", "outlook.fr", "live.com", "live.ca",
    "yahoo.com", "yahoo.ca", "yahoo.fr",
    "icloud.com", "me.com", "videotron.ca", "sympatico.ca",
    "bellnet.ca", "rogers.com",
}


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _classify_email(local: str) -> str:
    """Renvoie 'nominative' | 'generic' | 'other'.

    - nominative : ressemble à prénom.nom@, p.nom@, prenomnom@ (≥2 segments alpha
      séparés par '.', '-' ou '_', ou un seul token alpha de 6+ chars sans chiffres).
    - generic : info@, contact@, ventes@, etc.
    - other : tout le reste (chiffres, codes courts).
    """
    local_low = local.lower()
    if local_low in EMAIL_GENERIC_LOCAL:
        return "generic"
    parts = re.split(r"[._\-]", local_low)
    alpha_parts = [p for p in parts if p.isalpha() and len(p) >= 2]
    if len(alpha_parts) >= 2:
        return "nominative"
    if len(alpha_parts) == 1 and len(alpha_parts[0]) >= 6:
        return "nominative"
    return "other"


def _extract_emails_from_html(html: str, base_url: str) -> list[dict[str, str]]:
    """Extrait les emails d'un HTML. Filtre blocklist + emails hors-domaine.

    Garde uniquement les emails dont le domaine == base_domain (ou sous-domaine).
    Évite de scraper les emails d'autres sites mentionnés (partenaires, etc.).
    Retourne [{email, local, domain, kind}] dédupliqué.
    """
    base_dom = _domain_of(base_url)
    seen: dict[str, dict[str, str]] = {}
    soup = BeautifulSoup(html, "html.parser")

    candidates: list[str] = list(EMAIL_REGEX.findall(html))
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr:
                candidates.append(addr)

    for raw in candidates:
        addr = raw.strip().strip(".,;:<>()[]\"'").lower()
        if "@" not in addr:
            continue
        local, _, dom = addr.partition("@")
        if not local or not dom:
            continue
        if local in EMAIL_BLOCKLIST_LOCAL:
            continue
        if dom in EMAIL_BLOCKLIST_DOMAINS:
            continue
        kind = _classify_email(local)
        is_same_domain = bool(base_dom) and (dom == base_dom or dom.endswith("." + base_dom))
        is_personal_nominative = dom in EMAIL_PERSONAL_DOMAINS and kind == "nominative"
        if not (is_same_domain or is_personal_nominative):
            continue
        if addr in seen:
            continue
        seen[addr] = {
            "email": addr,
            "local": local,
            "domain": dom,
            "kind": kind,
        }
    return list(seen.values())


def _clean_text(html: str, max_chars: int = 8000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _same_host(base: str, candidate: str) -> bool:
    try:
        return urlparse(base).netloc.split(":")[0] == urlparse(candidate).netloc.split(":")[0]
    except ValueError:
        return False


async def fetch_site(url: str, max_pages: int = 3, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch homepage + up to (max_pages-1) linked internal pages.

    Returns: {url, status, pages: [{url, text}], tech_keyword_hits: [str]}
    """
    out: dict[str, Any] = {
        "url": url, "status": "unknown", "pages": [],
        "tech_keyword_hits": [], "emails_found": [],
    }
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5"}
    emails_by_addr: dict[str, dict[str, str]] = {}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            out["status"] = f"error: {type(e).__name__}"
            return out

        out["status"] = f"http_{r.status_code}"
        if r.status_code >= 400:
            return out

        home_text = _clean_text(r.text)
        out["pages"].append({"url": str(r.url), "text": home_text})
        for em in _extract_emails_from_html(r.text, str(r.url)):
            em["source_url"] = str(r.url)
            emails_by_addr.setdefault(em["email"], em)

        soup = BeautifulSoup(r.text, "html.parser")
        candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(str(r.url), a["href"])
            if not _same_host(str(r.url), href):
                continue
            if any(h in href.lower() or h in a.get_text(" ", strip=True).lower() for h in PAGE_HINTS):
                if href not in candidates and href != str(r.url):
                    candidates.append(href)
            if len(candidates) >= max_pages - 1:
                break

        for href in candidates:
            try:
                rp = await client.get(href)
                if rp.status_code < 400:
                    out["pages"].append({"url": str(rp.url), "text": _clean_text(rp.text)})
                    for em in _extract_emails_from_html(rp.text, str(rp.url)):
                        em["source_url"] = str(rp.url)
                        emails_by_addr.setdefault(em["email"], em)
            except httpx.HTTPError:
                continue

    haystack = " ".join(p["text"].lower() for p in out["pages"])
    out["tech_keyword_hits"] = [kw.strip() for kw in TECH_KEYWORDS if kw in haystack]
    out["emails_found"] = list(emails_by_addr.values())
    return out


# ----------------------------------------------------------------------
# Formatting helpers (réutilisés du proto)
# ----------------------------------------------------------------------

def _format_place_for_llm(place: dict[str, Any]) -> str:
    lines = [
        f"name: {place.get('displayName', {}).get('text', '')}",
        f"address: {place.get('formattedAddress', '')}",
        f"phone: {place.get('internationalPhoneNumber', '')}",
        f"website: {place.get('websiteUri', '')}",
        f"rating: {place.get('rating', '?')} ({place.get('userRatingCount', 0)} reviews)",
        f"business_status: {place.get('businessStatus', '')}",
        f"primary_type: {place.get('primaryType', '')}",
        f"types: {', '.join(place.get('types', []))}",
        f"google_maps_uri: {place.get('googleMapsUri', '')}",
    ]
    reviews = place.get("reviews", []) or []
    if reviews:
        lines.append("")
        lines.append("recent_reviews:")
        for rv in reviews[:5]:
            text = (rv.get("text") or {}).get("text", "") or (rv.get("originalText") or {}).get("text", "")
            lines.append(
                f"  - rating={rv.get('rating')} when={rv.get('relativePublishTimeDescription', '')}: "
                f"{text[:600]}"
            )
    return "\n".join(lines)


def _format_site_for_llm(site: dict[str, Any]) -> str:
    status = site.get("status", "unknown")
    if str(status).startswith("error") or status == "unknown":
        return f"website_status: {status}\nwebsite_text: (unavailable)"
    parts = [f"website_status: {status}"]
    hits = site.get("tech_keyword_hits") or []
    parts.append(f"tech_keyword_hits: {', '.join(hits) if hits else '(none)'}")
    for page in site.get("pages", []):
        parts.append(f"\n--- {page['url']} ---\n{page['text']}")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# LLM call
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "research.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text[:300]}")
    return json.loads(match.group(0))


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class LLMResult(BaseModel):
    research_json: dict[str, Any]
    model: str
    usage: LLMUsage


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    """True si l'erreur Anthropic est transitoire et mérite un retry.

    Catch surtout les 529 OverloadedError + 429 RateLimitError + erreurs réseau.
    Apollo et Anthropic émettent des 529 pendant les pics de charge globaux —
    on retry avec backoff au lieu de laisser la company en `status='error'`.
    """
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        # 529 = Overloaded, 503 = Service Unavailable, 502 = Bad Gateway, 504 = Gateway Timeout
        return status in (502, 503, 504, 529)
    # OverloadedError (sous-classe d'APIStatusError dans SDK récents) attrapé via APIStatusError.
    return False


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_llm(
    place_block: str,
    site_block: str,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2000,
) -> LLMResult:
    """Synchronous Anthropic call. Wrapped via `asyncio.to_thread` from the endpoint.

    Retry avec backoff exponentiel sur les erreurs transitoires Anthropic
    (529 Overloaded, 429 Rate Limit, 502/503/504 gateway, erreurs réseau).
    5 tentatives au total, attente 4→8→16→32→60s entre essais. Couvre les
    pics de charge globaux de l'API Anthropic qui durent typiquement <2 min.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        "## Google Places data\n"
        f"{place_block}\n\n"
        "## Website scrape\n"
        f"{site_block}\n"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return LLMResult(
        research_json=_parse_json(text),
        model=model,
        usage=LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


# ----------------------------------------------------------------------
# Public API — un seul point d'entrée
# ----------------------------------------------------------------------

class ResearchCompanyIn(BaseModel):
    google_place_id: str
    website: str | None = None
    model: str = _DEFAULT_MODEL


class ResearchCompanyOut(BaseModel):
    research_json: dict[str, Any]
    model: str
    duration_ms: int
    usage: LLMUsage
    place_status: str
    site_status: str
    tech_keyword_hits: list[str]
    emails_found: list[dict[str, Any]] = []  # [{email, local, domain, kind, source_url}]


async def research_company(payload: ResearchCompanyIn) -> ResearchCompanyOut:
    import asyncio

    started = time.monotonic()

    place = await fetch_place_details(payload.google_place_id)
    website = payload.website or place.get("websiteUri")
    if website:
        site = await fetch_site(website)
    else:
        site = {"status": "no_website", "pages": [], "tech_keyword_hits": []}

    place_block = _format_place_for_llm(place)
    site_block = _format_site_for_llm(site)

    llm_result = await asyncio.to_thread(_call_llm, place_block, site_block, payload.model)

    return ResearchCompanyOut(
        research_json=llm_result.research_json,
        model=llm_result.model,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage=llm_result.usage,
        place_status="ok",
        site_status=site.get("status", "unknown"),
        tech_keyword_hits=site.get("tech_keyword_hits", []),
        emails_found=site.get("emails_found", []),
    )
