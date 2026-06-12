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
from .research import (
    USER_AGENT,
    _is_transient_anthropic_error,
    fetch_place_details,
    fetch_site,
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
    max_tokens: int = 2500,
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
    user = (
        f"## Industrie\n{industry or 'inconnue'}\n\n"
        f"## Candidats images\n{cand_block}\n\n"
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


_BUCKET = "brand-assets"
_MAX_IMG_BYTES = 5 * 1024 * 1024
_PEXELS_SEARCH = "https://api.pexels.com/v1/search"

DownloadFn = Callable[[str], Awaitable[tuple[bytes, str]]]
UploadFn = Callable[[str, str, bytes, str], Awaitable[str]]


async def fetch_site_rich(url: str) -> dict[str, Any]:
    """Comme research.fetch_site mais conserve le HTML brut + extrait head/JSON-LD/candidats.

    Réutilise fetch_site pour le crawl multi-pages (texte + emails), puis re-fetch
    le HTML brut de chaque page pour l'extraction d'assets (fetch_site renvoie le
    texte nettoyé, pas le HTML)."""
    base = await fetch_site(url)
    pages = base.get("pages", [])
    head_meta = {"og_image": None, "twitter_image": None, "theme_color": None,
                 "description": None, "icon": None}
    jsonld = dict(parse.EMPTY_JSONLD)
    candidates: list[dict[str, Any]] = []
    social: dict[str, str] = {}
    rbq: str | None = None
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9"}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        for i, page in enumerate(pages):
            page_url = page["url"]
            try:
                r = await client.get(page_url)
                html = r.text
            except httpx.HTTPError:
                continue
            if i == 0:  # homepage = source des head meta + JSON-LD + couleurs
                head_meta = parse.extract_head_meta(html, page_url)
                jsonld = parse.parse_jsonld(html, page_url)
            social.update({k: v for k, v in parse.extract_social_links(html).items() if k not in social})
            rbq = rbq or parse.find_rbq(page["text"])
            candidates.extend(parse.extract_image_candidates(html, page_url, where=f"page{i}"))

    # og:image / favicon / logo JSON-LD = candidats supplémentaires
    for url_extra, kind in ((head_meta["og_image"], "hero"), (jsonld["logo"], "logo"),
                            (jsonld["image"], "hero"), (head_meta["icon"], "logo")):
        if url_extra:
            candidates.append({"url": url_extra, "kind_hint": kind, "alt": "", "where": "meta"})

    return {
        "status": base.get("status"),
        "pages": pages,
        "head_meta": head_meta,
        "jsonld": jsonld,
        "social": social,
        "rbq": rbq,
        "candidates": parse.dedup_and_id(candidates),
        "page_text": "\n\n".join(p["text"] for p in pages),
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


_ROLE_FIELDS = {"logo": "logo_candidate_id", "hero": "hero_candidate_id",
                "team": "team_photo_candidate_id"}


def _pick_colors(head_meta: dict[str, Any], jsonld: dict[str, Any],
                 logo_color: str | None) -> dict[str, Any] | None:
    theme = head_meta.get("theme_color")
    if theme:
        return {"primary": theme, "secondary": None, "_confidence": "high"}
    if logo_color:
        return {"primary": logo_color, "secondary": None, "_confidence": "medium"}
    return None


def _empty_rich() -> dict[str, Any]:
    """Rich vide — site absent OU fetch échoué (dégrade vers un kit Places-only)."""
    return {
        "head_meta": {"theme_color": None, "og_image": None, "icon": None,
                      "twitter_image": None, "description": None},
        "jsonld": dict(parse.EMPTY_JSONLD), "social": {}, "rbq": None,
        "candidates": [], "page_text": "",
    }


async def _resolve_card_images(
    company_id: str,
    cards: list[dict[str, Any]] | None,
    by_id: dict[int, str],
    role: str,
    out_field: str,
) -> None:
    """Résout image_candidate_id → URL ré-hébergée dans out_field, puis retire l'int.

    Le site consomme l'URL (services→image_url, valeurs→imageUrl), jamais l'id.
    Mutation en place des cartes du LLM."""
    for card in cards or []:
        cid = card.pop("image_candidate_id", None)
        src = by_id.get(cid) if cid is not None else None
        card[out_field] = await rehost_one(company_id, role, src) if src else None


async def build_brand_kit(company_id: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    rows = await db.select("companies", params={
        "select": "id,website,industry,google_place_id,brand_kit",
        "id": f"eq.{company_id}", "limit": "1",
    })
    if not rows:
        return {"company_id": company_id, "status": "company_not_found",
                "fields_filled": [], "confidence": {}}
    co = rows[0]
    # Garde anti-clobber : un kit corrigé à la main (_meta.reviewed) n'est jamais réécrit.
    # (should_write ignore son 2e argument ; {} = sentinelle.) Validé une seule fois ici.
    if not assemble.should_write(co.get("brand_kit"), {}):
        return {"company_id": company_id, "status": "skipped_already_reviewed",
                "fields_filled": [], "confidence": {}}

    # Fetch site + Places — fail-soft : une source flaky dégrade le kit, ne l'avorte pas.
    website = co.get("website")
    try:
        rich = await fetch_site_rich(website) if website else _empty_rich()
    except Exception:  # noqa: BLE001 — site prospect indispo → kit Places-only
        rich = _empty_rich()
    try:
        place = (await fetch_place_details(co["google_place_id"])
                 if co.get("google_place_id") else {})
    except Exception:  # noqa: BLE001
        place = {}

    candidates = rich["candidates"]
    by_id = {c["id"]: c["url"] for c in candidates}
    try:
        llm = _call_brandkit_llm(candidates, rich["page_text"], co.get("industry"), model=model)
    except Exception:  # noqa: BLE001 — LLM indispo (après retry) → kit déterministe seul
        llm = {}

    # Images top-level (logo/hero/team) : candidate_id → URL ré-hébergée.
    images: dict[str, str | None] = {}
    logo_color: str | None = None
    for role, llm_key in _ROLE_FIELDS.items():
        cid = llm.get(llm_key)
        src = by_id.get(cid) if cid is not None else None
        if not src:
            continue
        url, data = await _rehost_with_bytes(company_id, role, src)
        if not url:
            continue
        images[role] = url
        images[f"_source_{role}"] = "medium"
        if role == "logo" and data:  # couleur dérivée des bytes déjà téléchargés (pas de 2e fetch)
            logo_color = dominant_color(data)

    # Fallback Pexels pour le hero absent (image la plus visible de la démo).
    if not images.get("hero"):
        px = await fetch_pexels_image(assemble.pexels_query_for_industry(co.get("industry")))
        if px:
            data, ctype = px
            url, _ = await _rehost_with_bytes(
                company_id, "hero", "pexels", download=_make_static(data, ctype)
            )
            if url:
                images["hero"] = url
                images["_source_hero"] = "low"

    # Images des cartes services/valeurs : candidate_id → URL réelle (jamais l'int brut).
    await _resolve_card_images(company_id, llm.get("services"), by_id, "service", "image_url")
    await _resolve_card_images(company_id, llm.get("valeurs"), by_id, "valeur", "imageUrl")

    colors = _pick_colors(rich["head_meta"], rich["jsonld"], logo_color)
    kit = assemble.assemble_brand_kit(
        place=place, jsonld=rich["jsonld"], head_meta=rich["head_meta"], llm=llm,
        images=images, colors=colors, social=rich["social"], rbq=rich["rbq"],
    )

    # Garde déjà validée (early return) → on écrit directement.
    await db.update("companies", {"brand_kit": kit}, filters={"id": f"eq.{company_id}"})

    fields = [k for k in kit if not k.startswith("_") and k != "confidence"]
    return {"company_id": company_id, "status": "ok",
            "fields_filled": fields, "confidence": kit.get("confidence", {})}


def _make_static(data: bytes, ctype: str) -> DownloadFn:
    async def _dl(_url: str) -> tuple[bytes, str]:
        return data, ctype
    return _dl
