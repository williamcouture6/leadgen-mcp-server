"""Extraction PURE (sans réseau) pour build_brand_kit : head meta, JSON-LD,
candidats images, liens sociaux, RBQ. Testable sur des fixtures HTML."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

EMPTY_JSONLD: dict[str, Any] = {
    "logo": None, "telephone": None, "address": None,
    "opening_hours": [], "same_as": [], "rating": None,
    "rating_count": None, "image": None,
}

_JSONLD_TYPES = {
    "localbusiness", "organization", "professionalservice",
    "homeandconstructionbusiness", "generalcontractor", "plumber",
    "electrician", "roofingcontractor", "hvacbusiness", "store",
}


def _abs(base: str, url: str | None) -> str | None:
    if not url:
        return None
    return urljoin(base, url.strip())


def extract_head_meta(html: str, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    def _meta(attr: str, val: str) -> str | None:
        tag = soup.find("meta", attrs={attr: val})
        return tag.get("content") if tag and tag.get("content") else None

    icon = None
    for link in soup.find_all("link", href=True):
        rels = " ".join(link.get("rel", [])).lower()
        if "icon" in rels:
            icon = link["href"]
            break

    return {
        "og_image": _abs(base_url, _meta("property", "og:image") or _meta("name", "og:image")),
        "twitter_image": _abs(base_url, _meta("name", "twitter:image")),
        "theme_color": _meta("name", "theme-color"),
        "description": _meta("name", "description"),
        "icon": _abs(base_url, icon),
    }


def _iter_jsonld_objects(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if "@graph" in node and isinstance(node["@graph"], list):
                out.extend(n for n in node["@graph"] if isinstance(n, dict))
            else:
                out.append(node)
    return out


def _type_matches(node: dict[str, Any]) -> bool:
    t = node.get("@type")
    types = [t] if isinstance(t, str) else (t or [])
    return any(isinstance(x, str) and x.lower() in _JSONLD_TYPES for x in types)


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_jsonld(html: str, base_url: str) -> dict[str, Any]:
    result = dict(EMPTY_JSONLD)
    result["opening_hours"] = []
    result["same_as"] = []
    for node in _iter_jsonld_objects(html):
        if not _type_matches(node):
            continue
        logo = node.get("logo")
        if isinstance(logo, dict):
            logo = logo.get("url")
        if logo and not result["logo"]:
            result["logo"] = _abs(base_url, logo)
        img = node.get("image")
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, list):
            img = img[0] if img else None
        if img and not result["image"]:
            result["image"] = _abs(base_url, img)
        if node.get("telephone") and not result["telephone"]:
            result["telephone"] = str(node["telephone"]).strip()
        same = node.get("sameAs")
        if isinstance(same, str):
            same = [same]
        if isinstance(same, list):
            result["same_as"].extend(s for s in same if isinstance(s, str))
        oh = node.get("openingHours") or node.get("openingHoursSpecification")
        if isinstance(oh, str):
            result["opening_hours"].append(oh)
        elif isinstance(oh, list):
            result["opening_hours"].extend(str(x) for x in oh)
        rating = node.get("aggregateRating")
        if isinstance(rating, dict):
            result["rating"] = result["rating"] or _as_float(rating.get("ratingValue"))
            result["rating_count"] = result["rating_count"] or _as_int(
                rating.get("reviewCount") or rating.get("ratingCount")
            )
        addr = node.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("postalCode")]
            result["address"] = result["address"] or ", ".join(p for p in parts if p)
        elif isinstance(addr, str) and not result["address"]:
            result["address"] = addr
    result["same_as"] = list(dict.fromkeys(result["same_as"]))
    return result


RBQ_RE = re.compile(r"\b(\d{4}-\d{4}-\d{2})\b")

_SOCIAL_HOSTS = {
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "linkedin": "linkedin.com",
    "google": "g.page",
}


def _img_kind_hint(src: str, alt: str, in_header: bool, in_hero: bool) -> str:
    blob = f"{src} {alt}".lower()
    if in_header or "logo" in blob or "favicon" in blob:
        return "logo"
    if in_hero or "hero" in blob or "banner" in blob or "banniere" in blob:
        return "hero"
    if "team" in blob or "equipe" in blob or "équipe" in blob or "staff" in blob:
        return "team"
    if "before" in blob or "avant" in blob or "after" in blob or "apres" in blob:
        return "gallery"
    return "other"


def extract_image_candidates(html: str, base_url: str, where: str = "other") -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    header_ancestors = set(soup.find_all("header"))
    hero_nodes = set(soup.select('[class*="hero"], [class*="banner"], [id*="hero"]'))
    out: list[dict[str, Any]] = []
    first = True
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        url = _abs(base_url, src)
        if not url:
            continue
        alt = (img.get("alt") or "").strip()
        in_header = any(h in header_ancestors for h in img.parents)
        in_hero = first or any(h in hero_nodes for h in img.parents)
        out.append({
            "url": url,
            "kind_hint": _img_kind_hint(src, alt, in_header, in_hero),
            "alt": alt,
            "where": where,
        })
        first = False
    return out


def dedup_and_id(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for c in cands:
        if c["url"] not in seen:
            seen[c["url"]] = dict(c)
    out = list(seen.values())
    for i, c in enumerate(out):
        c["id"] = i
    return out


def extract_social_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        for key, host in _SOCIAL_HOSTS.items():
            if host in low and key not in found:
                found[key] = href
    return found


def find_rbq(text: str) -> str | None:
    m = RBQ_RE.search(text or "")
    return m.group(1) if m else None
