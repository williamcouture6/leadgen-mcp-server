"""Client du render-service (rendu headless à l'escalade). Fail-soft : toute
absence de config / erreur réseau / status error → None (l'appelant garde le statique)."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings


async def fetch_rendered(url: str) -> dict[str, Any] | None:
    """POST {url} au render-service → {html, image_urls} si status ok, sinon None."""
    s = settings()
    if not s.render_service_url:
        return None  # escalade désactivée (pas de service configuré)
    endpoint = s.render_service_url.rstrip("/") + "/render"
    headers = {"Authorization": f"Bearer {s.render_service_token}"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(endpoint, headers=headers, json={"url": url})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if data.get("status") != "ok":
        return None
    return {"html": data.get("html") or "", "image_urls": data.get("image_urls") or []}
