"""Extraction PURE (sans réseau) pour build_brand_kit : head meta, JSON-LD,
candidats images, liens sociaux, RBQ. Testable sur des fixtures HTML."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

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


_SIZE_RE = re.compile(r"(\d{2,4})x(\d{2,4})")


def _icon_size(sizes_attr: str | None, href: str | None) -> int | None:
    """Taille (px, côté) d'une icône depuis l'attribut `sizes` ou le nom de fichier.

    Ex. sizes="192x192" → 192 ; href=".../cropped-logo-180x180.png" → 180."""
    for src in (sizes_attr or "", href or ""):
        m = _SIZE_RE.search(src)
        if m:
            return int(m.group(1))
    return None


def extract_head_meta(html: str, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    def _meta(attr: str, val: str) -> str | None:
        tag = soup.find("meta", attrs={attr: val})
        return tag.get("content") if tag and tag.get("content") else None

    icon = None
    icons: list[dict[str, Any]] = []
    apple_candidates: list[tuple[int, str]] = []  # (size, url) pour apple-touch-icon
    for link in soup.find_all("link", href=True):
        rels = " ".join(link.get("rel", [])).lower()
        if "icon" not in rels:
            continue
        url = _abs(base_url, link["href"])
        if not url:
            continue
        size = _icon_size(link.get("sizes"), link["href"])
        if icon is None:
            icon = link["href"]
        icons.append({"url": url, "size": size})
        if "apple-touch-icon" in rels:
            apple_candidates.append((size or 0, url))

    apple_touch_icon = (
        max(apple_candidates, key=lambda t: t[0])[1] if apple_candidates else None
    )

    return {
        "og_image": _abs(base_url, _meta("property", "og:image") or _meta("name", "og:image")),
        "twitter_image": _abs(base_url, _meta("name", "twitter:image")),
        "theme_color": _meta("name", "theme-color"),
        "description": _meta("name", "description"),
        "icon": _abs(base_url, icon),
        "apple_touch_icon": apple_touch_icon,
        "icons": icons,
    }


def pick_logo_url(
    head_meta: dict[str, Any],
    jsonld: dict[str, Any] | None,
    facebook_logo: str | None = None,
) -> str | None:
    """Choix DÉTERMINISTE du logo (les faits ne dépendent pas du LLM).

    Priorité : apple-touch-icon → plus grande favicon ≥64px → logo JSON-LD →
    logo page Facebook → og:image en DERNIER recours (souvent une photo, pas un logo)."""
    if head_meta.get("apple_touch_icon"):
        return head_meta["apple_touch_icon"]
    sized = [i for i in (head_meta.get("icons") or []) if (i.get("size") or 0) >= 64]
    if sized:
        return max(sized, key=lambda i: i["size"])["url"]
    if jsonld and jsonld.get("logo"):
        return jsonld["logo"]
    if facebook_logo:
        return facebook_logo
    return head_meta.get("og_image")


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


_FB_PHONE_RE = re.compile(r'"phone(?:_?number)?"\s*:\s*"([+\d][\d\s().\-]{6,})"')


# ---------------------------------------------------------------------------
# Découverte et classification de pages internes (Plan 2A)
# ---------------------------------------------------------------------------

# `urlparse` is already imported above as `urlparse`; alias for readability.
_urlparse = urlparse

# Indices d'URL / texte d'ancre (sans accent, minuscule) → type de page.
_PAGE_TYPE_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("equipe", "team", "a-propos", "apropos", "propos", "about", "qui-sommes"), "equipe"),
    (("galerie", "gallery", "realisations", "realisation", "avant-apres", "portfolio", "projets"), "galerie"),
    (("contact", "nous-joindre", "joindre"), "contact"),
    (("blog", "blogue", "actualites", "nouvelles", "articles"), "blog"),
    # 'service' en dernier : c'est le plus large (toute page « métier »).
    (("service", "residentiel", "commercial", "lavage", "nettoyage", "gouttiere",
      "pression", "vitre", "fenetre", "soffite", "renovation", "toiture", "plomberie",
      "electricite", "peinture", "paysagement", "deneigement", "excavation"), "service"),
]


def classify_page(url: str, anchor_text: str = "") -> str:
    """Type d'une page interne d'après son URL + le texte du lien. 'home' pour la racine."""
    path = _urlparse(url).path.strip("/")
    if not path:
        return "home"
    blob = f"{path} {anchor_text}"
    blob = "".join(
        c for c in unicodedata.normalize("NFKD", blob)
        if not unicodedata.combining(c)
    ).lower()
    for needles, kind in _PAGE_TYPE_HINTS:
        if any(n in blob for n in needles):
            return kind
    return "other"


def _same_host(base: str, candidate: str) -> bool:
    try:
        return _urlparse(base).netloc.split(":")[0] == _urlparse(candidate).netloc.split(":")[0]
    except ValueError:
        return False


def discover_links(html: str, base_url: str, cap: int = 25) -> list[dict[str, str]]:
    """Liens internes dédupliqués, classés par type. Exclut externes/mailto/tel/ancres."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = _abs(base_url, href)
        if not url:
            continue
        url = url.split("#")[0]
        if not _same_host(base_url, url) or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "type": classify_page(url, a.get_text(" ", strip=True))})
        if len(out) >= cap:
            break
    return out


# Marqueurs JS-only où l'extraction statique RATE vraiment les images (sliders/
# placeholders SVG injectés par JS). On a écarté wp-block-/elementor-widget/lazyload :
# trop communs/bénins (WordPress Gutenberg partout) → escalades inutiles ; et les
# images lazy `data-src` sont déjà lues (comptées comme réelles ci-dessous).
_JS_MARKERS = ("twentytwenty", "data:image/svg", "swiper-", "owl-carousel")
_MIN_REAL_IMAGES = 3


def should_escalate(html: str) -> bool:
    """True si l'extraction statique de cette page est faible (→ rendu headless)."""
    soup = BeautifulSoup(html, "html.parser")
    real_imgs = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""  # data-src lisible en statique
        if src and not src.startswith("data:"):
            real_imgs += 1
    if real_imgs >= _MIN_REAL_IMAGES:
        return False
    low = html.lower()
    if any(m in low for m in _JS_MARKERS):
        return True
    # peu/pas d'images réelles ET conteneur slider/galerie sans <img>
    if soup.select_one('[class*="twentytwenty"], [class*="slider"], [class*="gallery"], [class*="carousel"]'):
        return True
    return real_imgs == 0


# Conteneurs de slider/figure avant-après les plus courants (WP & plugins).
_BA_SELECTORS = (
    '[class*="twentytwenty"]', '[class*="before-after"]', '[class*="beforeafter"]',
    '[class*="ba-slider"]', '[class*="comparison-slider"]', '[class*="image-comparison"]',
)


def extract_gallery_pairs(html: str, base_url: str) -> list[dict[str, Any]]:
    """Paires avant/après RÉELLES depuis les conteneurs slider (2 premières <img> du nœud).

    Hypothèse (vraie pour twentytwenty et les plugins avant/après) : 1re img = avant,
    2e = après (ordre du DOM)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sel in _BA_SELECTORS:
        for node in soup.select(sel):
            imgs = node.find_all("img")
            srcs = []
            for im in imgs:
                s = im.get("src") or im.get("data-src")
                u = _abs(base_url, s) if s else None
                if u and not u.startswith("data:"):
                    srcs.append(u)
            if len(srcs) >= 2:
                key = (srcs[0], srcs[1])
                if key not in seen:
                    seen.add(key)
                    out.append({"before_url": srcs[0], "after_url": srcs[1], "caption": None})
    return out


def parse_facebook_html(html: str) -> dict[str, Any]:
    """Extraction best-effort depuis le HTML public d'une page Facebook.

    Facebook = source clé (logo + URL du site quand inconnue + souvent téléphone).
    Tout est best-effort : champ absent → None. Jamais d'exception (fail-soft amont)."""
    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", attrs={"property": "og:image"})
    logo = og.get("content") if og and og.get("content") else None

    # Site web : Facebook enrobe les liens sortants dans l.php?u=<url encodée>.
    website = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "l.php" in href and "u=" in href:
            u = parse_qs(urlparse(href).query).get("u", [None])[0]
            if u:
                website = u
                break

    # Téléphone : lien tel: d'abord, sinon clé JSON embarquée.
    phone = None
    tel = soup.find("a", href=re.compile(r"^tel:", re.I))
    if tel and tel.get("href"):
        phone = tel["href"].split(":", 1)[1].strip() or None
    if not phone:
        m = _FB_PHONE_RE.search(html or "")
        if m:
            phone = m.group(1).strip()

    return {"logo": logo, "website": website, "phone": phone, "hours": None}
