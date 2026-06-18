"""Tool build_brand_kit — produit companies.brand_kit pour le site démo P4.

Étape on-demand, séparée de research_company. Approche hybride : extraction
déterministe (brandkit_parse) + Google Places + un appel Sonnet (texte + choix
d'images par candidate_id) ; images ré-hébergées dans le bucket brand-assets,
Pexels en fallback.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic
from PIL import Image
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .. import supabase_client as db
from ..config import settings
from ..lib import brandkit_parse as parse
from ..lib import brandkit_assemble as assemble
from ..lib import render_client
from .research import (
    USER_AGENT,
    _clean_text,
    _is_transient_anthropic_error,
    fetch_place_details,
    fetch_site,  # noqa: F401 — encore référencé après merge 2B (suppression différée)
)

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "brand_kit.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_BRANDKIT_TOOL_NAME = "save_brand_kit"

_BRANDKIT_TOOL: dict[str, Any] = {
    "name": _BRANDKIT_TOOL_NAME,
    "description": (
        "Enregistre le brand-kit extrait du site. null/tableau vide si inconnu. "
        "Pour les images, ne renvoie que des candidate_id de la liste fournie."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tagline": {"type": ["string", "null"]},
            "logo_candidate_id": {"type": ["integer", "null"]},
            "hero_candidate_id": {"type": ["integer", "null"]},
            "team_photo_candidate_id": {"type": ["integer", "null"]},
            "gallery": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "before_candidate_id": {"type": ["integer", "null"]},
                        "after_candidate_id": {"type": ["integer", "null"]},
                        "caption": {"type": ["string", "null"]},
                    },
                },
            },
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": ["string", "null"]},
                        "details": {"type": ["string", "null"]},
                        "inclus": {"type": "array", "items": {"type": "string"}},
                        "image_candidate_id": {"type": ["integer", "null"]},
                        "overlay": {"enum": ["dark", "light", None]},
                        "process": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "titre": {"type": "string"},
                                    "texte": {"type": "string"},
                                },
                                "required": ["titre", "texte"],
                            },
                        },
                        "faq": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {"type": "string"},
                                    "reponse": {"type": "string"},
                                },
                                "required": ["question", "reponse"],
                            },
                        },
                    },
                },
            },
            "valeurs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "titre": {"type": "string"},
                        "texte": {"type": "string"},
                        "image_candidate_id": {"type": ["integer", "null"]},
                    },
                },
            },
            "faq": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "reponse": {"type": "string"},
                    },
                },
            },
            "legal": {
                "type": ["object", "null"],
                "properties": {"confidentialite": {"type": ["string", "null"]}},
            },
            "stats": {
                "type": ["object", "null"],
                "properties": {
                    "years_experience": {"type": ["integer", "null"]},
                    "projects": {"type": ["integer", "null"]},
                    "clients": {"type": ["integer", "null"]},
                },
            },
            "service_areas": {"type": "array", "items": {"type": "string"}},
            "team": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nom": {"type": "string"},
                        "role": {"type": ["string", "null"]},
                        "photo_candidate_id": {"type": ["integer", "null"]},
                    },
                },
            },
            "rbq": {"type": ["string", "null"]},
        },
        "required": [],
    },
}


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_brandkit_llm(
    candidates: list[dict[str, Any]],
    page_text: str,
    industry: str | None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 3500,
    service_pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    cand_block = json.dumps(
        [{"id": c["id"], "kind_hint": c.get("kind_hint", "other"), "alt": c.get("alt", "")}
         for c in candidates],
        ensure_ascii=False,
    )
    svc_block = ""
    if service_pages:
        parts = [f"### {p['url']}\n{(p.get('text') or '')[:3000]}" for p in service_pages[:12]]
        svc_block = "## Pages de service (process/faq par service depuis SA page)\n" + "\n\n".join(parts) + "\n\n"
    user = (
        f"## Industrie\n{industry or 'inconnue'}\n\n"
        f"## Candidats images\n{cand_block}\n\n"
        f"{svc_block}"
        f"## Texte des pages\n{page_text[:14000]}\n"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_BRANDKIT_TOOL],
        tool_choice={"type": "tool", "name": _BRANDKIT_TOOL_NAME},
        messages=[{"role": "user", "content": user}],
    )
    block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == _BRANDKIT_TOOL_NAME),
        None,
    )
    if block is not None and isinstance(block.input, dict):
        return block.input
    return {}


_FLEX_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "brand_kit_flex.md"
_FLEX_TOOL_NAME = "save_flex_page"

_FLEX_TOOL: dict[str, Any] = {
    "name": _FLEX_TOOL_NAME,
    "description": (
        "Structure UNE page hors-template en blocs premium. blocs:[] si la page "
        "n'a pas de valeur réelle. Pour les images, ne renvoie que des *_id de la "
        "liste fournie, jamais d'URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": ["string", "null"]},
            "titre": {"type": "string"},
            "eyebrow": {"type": ["string", "null"]},
            "intro": {"type": ["string", "null"]},
            "hero_image_url_id": {"type": ["integer", "null"]},
            "blocs": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"type": "object", "properties": {
                            "type": {"enum": ["titre"]}, "texte": {"type": "string"},
                            "eyebrow": {"type": ["string", "null"]}},
                         "required": ["type", "texte"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["texte"]}, "corps": {"type": "string"}},
                         "required": ["type", "corps"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["liste"]}, "titre": {"type": ["string", "null"]},
                            "items": {"type": "array", "items": {"type": "string"}}},
                         "required": ["type", "items"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["image"]}, "url_id": {"type": ["integer", "null"]},
                            "legende": {"type": ["string", "null"]},
                            "alt": {"type": ["string", "null"]}},
                         "required": ["type"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["galerie"]},
                            "images": {"type": "array", "items": {"type": "object", "properties": {
                                "url_id": {"type": ["integer", "null"]},
                                "legende": {"type": ["string", "null"]}}}}},
                         "required": ["type", "images"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["stats"]},
                            "items": {"type": "array", "items": {"type": "object", "properties": {
                                "valeur": {"type": "string"}, "label": {"type": "string"}},
                                "required": ["valeur", "label"]}}},
                         "required": ["type", "items"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["cta"]}, "titre": {"type": "string"},
                            "texte": {"type": ["string", "null"]}},
                         "required": ["type", "titre"]},
                        {"type": "object", "properties": {
                            "type": {"enum": ["faq"]},
                            "items": {"type": "array", "items": {"type": "object", "properties": {
                                "question": {"type": "string"}, "reponse": {"type": "string"}},
                                "required": ["question", "reponse"]}}},
                         "required": ["type", "items"]},
                    ],
                },
            },
        },
        "required": ["titre", "blocs"],
    },
}


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_flex_llm(
    page_text: str,
    candidates: list[dict[str, Any]],
    industry: str | None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2500,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _FLEX_PROMPT_PATH.read_text(encoding="utf-8")
    cand_block = json.dumps(
        [{"id": c["id"], "kind_hint": c.get("kind_hint", "other"), "alt": c.get("alt", "")}
         for c in candidates],
        ensure_ascii=False,
    )
    user = (
        f"## Industrie\n{industry or 'inconnue'}\n\n"
        f"## Images candidates de cette page\n{cand_block}\n\n"
        f"## Texte de la page\n{page_text[:12000]}\n"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_FLEX_TOOL],
        tool_choice={"type": "tool", "name": _FLEX_TOOL_NAME},
        messages=[{"role": "user", "content": user}],
    )
    block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == _FLEX_TOOL_NAME),
        None,
    )
    if block is not None and isinstance(block.input, dict):
        return block.input
    return {}


_BUCKET = "brand-assets"
_MAX_IMG_BYTES = 5 * 1024 * 1024
_MIN_IMG_SIDE = 200
# Rôles qui exigent une image « assez grande » (un logo/photo d'équipe peut être petit).
_ROLES_NEED_SIZE = {"hero", "stats", "service", "gallery-before", "gallery-after", "valeur"}


def _image_meets_min_side(data: bytes, min_side: int) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001 — bytes non-image
        return False
    return min(w, h) >= min_side


_PEXELS_SEARCH = "https://api.pexels.com/v1/search"

DownloadFn = Callable[[str], Awaitable[tuple[bytes, str]]]
UploadFn = Callable[[str, str, bytes, str], Awaitable[str]]


async def _get_html(client: httpx.AsyncClient, url: str) -> str | None:
    """GET une page → HTML (ou None, fail-soft)."""
    try:
        r = await client.get(url)
        if r.status_code >= 400:
            return None
        return r.text
    except httpx.HTTPError:
        return None


_CRAWL_TYPES = {"home", "service", "equipe", "galerie", "contact", "other"}
_CRAWL_CAP = 25


async def fetch_site_rich(url: str) -> dict[str, Any]:
    """Crawl COMPLET du site + escalade headless des pages faibles → SiteSnapshot.

    Home → discover_links → fetch toutes les pages pertinentes (cap) → rendu headless
    si la page est faible → agrège candidats/head_meta(home)/jsonld(home)/social/rbq/
    page_text + pages[{url,type,text}] + service_pages[] + escalated[]. Fail-soft."""
    head_meta = {"og_image": None, "twitter_image": None, "theme_color": None,
                 "description": None, "icon": None, "apple_touch_icon": None, "icons": []}
    jsonld = dict(parse.EMPTY_JSONLD)
    candidates: list[dict[str, Any]] = []
    social: dict[str, str] = {}
    rbq: str | None = None
    pages: list[dict[str, str]] = []
    service_pages: list[dict[str, str]] = []
    escalated: list[str] = []
    _gallery_pairs_all: list[dict[str, Any]] = []
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9"}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        home_html = await _get_html(client, url)
        if not home_html:
            return {"status": "error", "pages": [], "head_meta": head_meta, "jsonld": jsonld,
                    "social": {}, "rbq": None, "candidates": [], "page_text": "",
                    "service_pages": [], "escalated": [], "gallery_pairs": []}

        # Liste à crawler : home (type 'home') + liens internes pertinents, dédupliqués.
        # Dédup insensible au slash final (évite de re-fetcher la home via href="/").
        targets: list[dict[str, str]] = [{"url": url, "type": "home"}]
        seen = {url.rstrip("/")}
        for link in parse.discover_links(home_html, url, cap=_CRAWL_CAP):
            norm = link["url"].rstrip("/")
            if link["type"] in _CRAWL_TYPES and norm not in seen:
                seen.add(norm)
                targets.append(link)
            if len(targets) >= _CRAWL_CAP:
                break

        for i, tgt in enumerate(targets):
            page_url, page_type = tgt["url"], tgt["type"]
            html = home_html if i == 0 else await _get_html(client, page_url)
            if html is None:
                continue
            # Escalade headless si extraction statique faible.
            if parse.should_escalate(html):
                rendered = await render_client.fetch_rendered(page_url)
                if rendered and rendered.get("html"):
                    html = rendered["html"]
                    escalated.append(page_url)

            text = _clean_text(html)
            page_cands = parse.dedup_and_id(
                parse.extract_image_candidates(html, page_url, where=page_type)
            )
            pages.append({"url": page_url, "type": page_type, "text": text,
                          "candidates": page_cands})
            if page_type == "service":
                service_pages.append({"url": page_url, "text": text})

            if i == 0:  # head meta + JSON-LD = home seulement
                head_meta = parse.extract_head_meta(html, page_url)
                jsonld = parse.parse_jsonld(html, page_url)
            social.update({k: v for k, v in parse.extract_social_links(html).items() if k not in social})
            rbq = rbq or parse.find_rbq(text)
            candidates.extend(parse.extract_image_candidates(html, page_url, where=page_type))
            _gallery_pairs_all.extend(parse.extract_gallery_pairs(html, page_url))

    # og:image / favicon / logo JSON-LD = candidats supplémentaires
    for url_extra, kind in ((head_meta["og_image"], "hero"), (jsonld["logo"], "logo"),
                            (jsonld["image"], "hero"), (head_meta["icon"], "logo")):
        if url_extra:
            candidates.append({"url": url_extra, "kind_hint": kind, "alt": "", "where": "meta"})

    return {
        "status": "ok",
        "pages": pages,
        "head_meta": head_meta,
        "jsonld": jsonld,
        "social": social,
        "rbq": rbq,
        "candidates": parse.dedup_and_id(candidates),
        "page_text": "\n\n".join(p["text"] for p in pages),
        "service_pages": service_pages,
        "escalated": escalated,
        "gallery_pairs": _gallery_pairs_all,
    }


def dominant_color(image_bytes: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 — bytes non-image
        return None
    img = img.resize((32, 32))
    pixels = [p for p in img.getdata() if not (p[0] > 240 and p[1] > 240 and p[2] > 240)]
    if not pixels:
        return None
    n = len(pixels)
    r = sum(p[0] for p in pixels) // n
    g = sum(p[1] for p in pixels) // n
    b = sum(p[2] for p in pixels) // n
    return f"#{r:02x}{g:02x}{b:02x}"


async def _download_image(url: str) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        r = await client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            raise ValueError(f"not an image: {ctype}")
        if len(r.content) > _MAX_IMG_BYTES:
            raise ValueError("image too large")
        return r.content, ctype


def _ext_for(ctype: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
            "image/gif": "gif", "image/svg+xml": "svg"}.get(ctype, "img")


async def _rehost_with_bytes(
    company_id: str,
    role: str,
    src_url: str,
    *,
    download: DownloadFn | None = None,
    upload: UploadFn | None = None,
) -> tuple[str | None, bytes | None]:
    """Download src_url → upload bucket → (URL publique, bytes téléchargés). Fail-soft → (None, None).

    Renvoie aussi les bytes pour éviter un 2e download (ex. couleur dominante du logo)."""
    download = download or _download_image
    upload = upload or db.upload_object
    try:
        data, ctype = await download(src_url)
    except Exception:  # noqa: BLE001
        return None, None
    if role in _ROLES_NEED_SIZE and not _image_meets_min_side(data, _MIN_IMG_SIDE):
        return None, None   # trop petite pour ce rôle → l'appelant tombera sur Pexels
    h = hashlib.sha1(data).hexdigest()[:10]
    path = f"{company_id}/{role}-{h}.{_ext_for(ctype)}"
    try:
        return await upload(_BUCKET, path, data, ctype), data
    except Exception:  # noqa: BLE001
        return None, None


async def rehost_one(
    company_id: str,
    role: str,
    src_url: str,
    *,
    download: DownloadFn | None = None,
    upload: UploadFn | None = None,
) -> str | None:
    """Download src_url → upload bucket brand-assets → URL publique. Fail-soft → None."""
    url, _ = await _rehost_with_bytes(
        company_id, role, src_url, download=download, upload=upload
    )
    return url


async def fetch_facebook_brand(fb_url: str) -> dict[str, Any]:
    """Best-effort : télécharge la page Facebook publique → logo/site/téléphone.

    Facebook = source clé (logo, URL du site quand inconnue, souvent téléphone).
    Fail-soft : toute erreur réseau/HTTP → {} (le kit se construit sans FB)."""
    if not fb_url:
        return {}
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9"}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        try:
            r = await client.get(fb_url)
            r.raise_for_status()
        except httpx.HTTPError:
            return {}
    return parse.parse_facebook_html(r.text)


async def fetch_pexels_image(query: str) -> tuple[bytes, str] | None:
    key = settings().pexels_api_key
    if not key:
        return None
    headers = {"Authorization": key}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            r = await client.get(_PEXELS_SEARCH, headers=headers,
                                 params={"query": query, "orientation": "landscape", "per_page": "1"})
            r.raise_for_status()
            photos = r.json().get("photos") or []
            if not photos:
                return None
            img_url = photos[0]["src"].get("landscape") or photos[0]["src"].get("large")
            ri = await client.get(img_url)
            ri.raise_for_status()
            return ri.content, ri.headers.get("content-type", "image/jpeg").split(";")[0]
        except (httpx.HTTPError, KeyError, ValueError):
            return None


def _pick_colors(head_meta: dict[str, Any], jsonld: dict[str, Any],
                 logo_color: str | None) -> dict[str, Any] | None:
    theme = head_meta.get("theme_color")
    if theme:
        return {"primary": theme, "secondary": None, "_confidence": "high"}
    if logo_color:
        return {"primary": logo_color, "secondary": None, "_confidence": "medium"}
    return None


def _empty_rich() -> dict[str, Any]:
    """Rich vide — site absent OU fetch échoué (dégrade vers un kit Places-only).

    Doit avoir la MÊME forme que le retour de fetch_site_rich (clés additives 2A
    incluses) pour qu'un consommateur en aval ne lève pas KeyError sur ce fallback."""
    return {
        "status": "error",
        "head_meta": {"theme_color": None, "og_image": None, "icon": None,
                      "twitter_image": None, "description": None,
                      "apple_touch_icon": None, "icons": []},
        "jsonld": dict(parse.EMPTY_JSONLD), "social": {}, "rbq": None,
        "candidates": [], "page_text": "", "pages": [],
        "service_pages": [], "escalated": [],
        "gallery_pairs": [],
    }


async def _resolve_card_images(
    company_id: str,
    cards: list[dict[str, Any]] | None,
    by_id: dict[int, str],
    role: str,
    out_field: str,
    id_field: str = "image_candidate_id",
) -> None:
    """Résout id_field (candidate_id) → URL ré-hébergée dans out_field, puis retire l'int.

    Le site consomme l'URL (services→image_url, valeurs→imageUrl, team→photo_url),
    jamais l'id. Mutation en place des cartes du LLM."""
    for card in cards or []:
        cid = card.pop(id_field, None)
        src = by_id.get(cid) if cid is not None else None
        card[out_field] = await rehost_one(company_id, role, src) if src else None


async def _pexels_rehost(company_id: str, role: str, query: str) -> str | None:
    """Pexels(query) → ré-héberge dans brand-assets → URL publique (ou None, fail-soft)."""
    px = await fetch_pexels_image(query)
    if not px:
        return None
    data, ctype = px
    url, _ = await _rehost_with_bytes(
        company_id, role, "pexels", download=_make_static(data, ctype)
    )
    return url


async def _ensure_service_images(
    company_id: str, services: list[dict[str, Any]] | None, industry: str | None
) -> None:
    """Une image par service, sans exception : à défaut de candidat réel, fallback Pexels par service."""
    for svc in services or []:
        if svc.get("image_url"):
            continue
        svc["image_url"] = await _pexels_rehost(
            company_id, "service", assemble.pexels_query_for_service(svc.get("name"), industry)
        )


async def _ensure_stats_image(company_id: str, kit: dict[str, Any], industry: str | None) -> None:
    """Image « cinématographique » de fond pour la bande statistiques (toujours fournie)."""
    img = await _pexels_rehost(company_id, "stats", assemble.pexels_stats_query(industry))
    if not img:
        return
    stats = kit.get("stats") or {}
    stats["image_url"] = img
    kit["stats"] = stats


async def _build_gallery(
    company_id: str, llm: dict[str, Any], by_id: dict[int, str], industry: str | None,
    site_pairs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Galerie avant/après — ordre : site réel → LLM → Pexels."""
    # 1) vraie paire du site (slider avant/après) → ré-hébergée
    for pair in (site_pairs or []):
        b = await rehost_one(company_id, "gallery-before", pair["before_url"])
        a = await rehost_one(company_id, "gallery-after", pair["after_url"])
        if b and a:
            return [{"before_url": b, "after_url": a, "caption": pair.get("caption")}]
    # 2) paire choisie par le LLM (candidate_id)
    for pair in (llm.get("gallery") or []):
        before = by_id.get(pair.get("before_candidate_id"))
        after = by_id.get(pair.get("after_candidate_id"))
        if before and after:
            b = await rehost_one(company_id, "gallery-before", before)
            a = await rehost_one(company_id, "gallery-after", after)
            if b and a:
                return [{"before_url": b, "after_url": a, "caption": pair.get("caption")}]
    # 3) fallback Pexels par métier
    q_before, q_after = assemble.pexels_gallery_queries(industry)
    b = await _pexels_rehost(company_id, "gallery-before", q_before)
    a = await _pexels_rehost(company_id, "gallery-after", q_after)
    if b and a:
        return [{"before_url": b, "after_url": a, "caption": None}]
    return []


async def _resolve_flex_page(
    llm_page: dict[str, Any],
    by_id: dict[int, str],
    industry: str | None,
    company_id: str,
    *,
    rehost: Callable[[str, str], Awaitable[str | None]] | None = None,
    pexels: Callable[[str, str], Awaitable[str | None]] | None = None,
) -> dict[str, Any] | None:
    """Résout les *_id d'images d'UNE page LLM → URL ré-hébergées, RENOMME vers les
    clés du contrat (hero_image_url / url), comble par Pexels (déco) sauf galerie
    (réel only → drop). Renvoie la page finale, ou None si plus aucun bloc."""
    rehost = rehost or (lambda role, src: rehost_one(company_id, role, src))
    pexels = pexels or (lambda role, q: _pexels_rehost(company_id, role, q))
    titre = llm_page.get("titre") or ""
    px_query = f"{industry or ''} {titre}".strip()

    page: dict[str, Any] = {
        "titre": titre,
        "slug": llm_page.get("slug"),
        "eyebrow": llm_page.get("eyebrow"),
        "intro": llm_page.get("intro"),
    }

    # hero de page : id → URL → renommée ; sinon Pexels (déco).
    hero_src = by_id.get(llm_page.get("hero_image_url_id"))
    hero_url = await rehost("flex-hero", hero_src) if hero_src else None
    if not hero_url:
        hero_url = await pexels("flex-hero", px_query)
    if hero_url:
        page["hero_image_url"] = hero_url

    out_blocs: list[dict[str, Any]] = []
    for b in llm_page.get("blocs") or []:
        t = b.get("type")
        if t == "image":
            src = by_id.get(b.get("url_id"))
            url = await rehost("flex-image", src) if src else None
            if not url:
                url = await pexels("flex-image", px_query)  # Pexels déco
            if url:
                out_blocs.append({"type": "image", "url": url,
                                  "legende": b.get("legende"), "alt": b.get("alt")})
        elif t == "galerie":
            imgs: list[dict[str, Any]] = []
            for im in b.get("images") or []:
                src = by_id.get(im.get("url_id"))
                url = await rehost("flex-gallery", src) if src else None  # RÉEL ONLY
                if url:
                    imgs.append({"url": url, "legende": im.get("legende")})
            if imgs:  # galerie sans image réelle → bloc droppé (jamais de Pexels)
                out_blocs.append({"type": "galerie", "images": imgs})
        else:
            out_blocs.append(b)  # titre/texte/liste/stats/cta/faq : pass-through

    if not out_blocs:
        return None
    page["blocs"] = out_blocs
    return {k: v for k, v in page.items() if v is not None}


async def build_brand_kit(company_id: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    rows = await db.select("companies", params={
        "select": "id,name,address,website,industry,google_place_id,brand_kit",
        "id": f"eq.{company_id}", "limit": "1",
    })
    if not rows:
        return {"company_id": company_id, "status": "company_not_found",
                "fields_filled": [], "confidence": {}}
    co = rows[0]
    # Garde anti-clobber : un kit corrigé à la main (_meta.reviewed) n'est jamais réécrit.
    if not assemble.should_write(co.get("brand_kit"), {}):
        return {"company_id": company_id, "status": "skipped_already_reviewed",
                "fields_filled": [], "confidence": {}}

    industry = co.get("industry")
    website = co.get("website")

    # Fetch site + Facebook + Places — fail-soft : une source flaky dégrade le kit, ne l'avorte pas.
    try:
        rich = await fetch_site_rich(website) if website else _empty_rich()
    except Exception:  # noqa: BLE001 — site prospect indispo → kit Places-only
        rich = _empty_rich()

    # Facebook (source clé : logo, URL du site quand inconnue, souvent téléphone).
    fb: dict[str, Any] = {}
    fb_url = (rich.get("social") or {}).get("facebook")
    if fb_url:
        try:
            fb = await fetch_facebook_brand(fb_url)
        except Exception:  # noqa: BLE001
            fb = {}
    # Site inconnu mais trouvé sur Facebook → re-fetch le site pour récupérer ses assets.
    if not website and fb.get("website"):
        website = fb["website"]
        try:
            rich = await fetch_site_rich(website)
        except Exception:  # noqa: BLE001
            pass

    try:
        place = (await fetch_place_details(co["google_place_id"])
                 if co.get("google_place_id") else {})
    except Exception:  # noqa: BLE001
        place = {}

    candidates = rich["candidates"]
    by_id = {c["id"]: c["url"] for c in candidates}
    try:
        llm = _call_brandkit_llm(candidates, rich["page_text"], industry, model=model,
                                 service_pages=rich.get("service_pages"))
    except Exception:  # noqa: BLE001 — LLM indispo (après retry) → kit déterministe seul
        llm = {}

    images: dict[str, str | None] = {}
    logo_color: str | None = None

    # LOGO — choix DÉTERMINISTE (apple-touch/favicon ≥64 / JSON-LD / Facebook / og:image),
    # le LLM ne sert que de dernier recours. La couleur dominante en découle.
    logo_src = parse.pick_logo_url(rich["head_meta"], rich["jsonld"], fb.get("logo"))
    if not logo_src:
        cid = llm.get("logo_candidate_id")
        logo_src = by_id.get(cid) if cid is not None else None
    if logo_src:
        url, data = await _rehost_with_bytes(company_id, "logo", logo_src)
        if url:
            images["logo"] = url
            images["_source_logo"] = "medium"
            if data:
                logo_color = dominant_color(data)

    # HERO / TEAM — choix LLM par candidate_id.
    for role, llm_key in (("hero", "hero_candidate_id"), ("team", "team_photo_candidate_id")):
        cid = llm.get(llm_key)
        src = by_id.get(cid) if cid is not None else None
        if not src:
            continue
        url, _ = await _rehost_with_bytes(company_id, role, src)
        if url:
            images[role] = url
            images[f"_source_{role}"] = "medium"

    # Fallback Pexels pour le hero absent (image la plus visible — requête par industrie).
    if not images.get("hero"):
        hero = await _pexels_rehost(company_id, "hero", assemble.pexels_query_for_industry(industry))
        if hero:
            images["hero"] = hero
            images["_source_hero"] = "low"

    # Cartes services/valeurs/membres d'équipe : candidate_id → URL réelle (jamais l'int brut).
    await _resolve_card_images(company_id, llm.get("services"), by_id, "service", "image_url")
    await _resolve_card_images(company_id, llm.get("valeurs"), by_id, "valeur", "imageUrl")
    await _resolve_card_images(company_id, llm.get("team"), by_id, "team", "photo_url",
                               id_field="photo_candidate_id")

    colors = _pick_colors(rich["head_meta"], rich["jsonld"], logo_color)
    company = {"name": co.get("name"), "address": co.get("address")}
    kit = assemble.assemble_brand_kit(
        place=place, jsonld=rich["jsonld"], head_meta=rich["head_meta"], llm=llm,
        images=images, colors=colors, social=rich["social"], rbq=rich["rbq"],
        company=company, facebook=fb,
    )

    # Garanties d'images (toujours, fallback Pexels par métier) : 1 image/service, fond stats,
    # galerie avant/après — le site démo affiche ces sections en permanence.
    await _ensure_service_images(company_id, kit.get("services"), industry)
    await _ensure_stats_image(company_id, kit, industry)
    gallery = await _build_gallery(company_id, llm, by_id, industry,
                                   site_pairs=rich.get("gallery_pairs"))
    if gallery:
        kit["gallery"] = gallery

    # File de revue : champs douteux/manquants pour correction humaine (Supabase Studio).
    kit["_review"] = assemble.derive_review(kit)
    kit["_meta"]["status"] = "needs_review" if kit["_review"] else "ok"

    # Garde déjà validée (early return) → on écrit directement.
    await db.update("companies", {"brand_kit": kit}, filters={"id": f"eq.{company_id}"})

    fields = [k for k in kit if not k.startswith("_") and k != "confidence"]
    return {"company_id": company_id, "status": "ok",
            "fields_filled": fields, "confidence": kit.get("confidence", {})}


def _make_static(data: bytes, ctype: str) -> DownloadFn:
    async def _dl(_url: str) -> tuple[bytes, str]:
        return data, ctype
    return _dl
