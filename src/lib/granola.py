"""Granola public API client — fetch des notes de meeting post-RDV (Part C / WF-9).

API officielle : https://docs.granola.ai/introduction
  - Base URL : https://public-api.granola.ai/v1
  - Auth    : `Authorization: Bearer grn_<KEY>` (env `GRANOLA_API_KEY`)
  - Rate limits : burst 25 req / 5s, soutenu 5 req/s. 429 si excès.
  - Endpoints utilisés :
      GET /notes?created_after=ISO[&cursor=...]
        → {notes: [...], hasMore: bool, cursor: str}
      GET /notes/{not_xxx}?include=transcript
        → note complète (404 si pas encore de summary IA)

Failure-mode : retry sur 429 (avec Retry-After si fourni, sinon backoff) et 5xx.
404 retourne None (note pas encore prête côté Granola — caller re-tente plus tard).
Auth/4xx autre → exception, n8n logguera et passera à la prochaine.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

GRANOLA_BASE_URL = "https://public-api.granola.ai/v1"
GRANOLA_API_KEY_ENV = "GRANOLA_API_KEY"
GRANOLA_TIMEOUT_SECONDS = 30.0


class GranolaError(Exception):
    """Raised when Granola API call fails (auth, 4xx other than 404, persistent 5xx)."""


class GranolaNoteNotReady(Exception):
    """404 sur GET /notes/{id} — la note existe mais n'a pas encore de summary/transcript IA.

    Caller doit re-tenter dans quelques minutes (cf n8n cron WF-9).
    """


def _api_key() -> str:
    key = os.environ.get(GRANOLA_API_KEY_ENV, "").strip()
    if not key:
        raise GranolaError(f"{GRANOLA_API_KEY_ENV} absent")
    return key


def _is_transient_granola_error(exc: BaseException) -> bool:
    """True pour 429 + 5xx + erreurs réseau — éligibles au retry tenacity."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return False


@retry(
    retry=retry_if_exception(_is_transient_granola_error),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Wrapper httpx avec auth + retry sur erreurs transitoires.

    Lève GranolaError sur 401/403/400 (auth/payload cassé) — pas de retry.
    Lève GranolaNoteNotReady sur 404 d'un GET /notes/{id} (cas attendu).
    """
    key = api_key or _api_key()
    url = f"{GRANOLA_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=GRANOLA_TIMEOUT_SECONDS) as client:
        r = await client.request(method, url, headers=headers, params=params or {})
    if r.status_code == 404 and path.startswith("/notes/"):
        raise GranolaNoteNotReady(f"note pas encore prête : {path}")
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Les 429/5xx remontent ici → retry les attrape via _is_transient_granola_error.
        # Les 4xx non-transients (401/403/400) re-raise et sortent du retry.
        if not _is_transient_granola_error(e):
            body = (e.response.text or "")[:300]
            print(f"[granola] {method} {path} → {e.response.status_code} {body}",
                  file=sys.stderr)
            raise GranolaError(
                f"Granola API {e.response.status_code}: {body}"
            ) from e
        raise
    try:
        return r.json()
    except ValueError as e:
        raise GranolaError(f"réponse Granola non-JSON: {r.text[:200]}") from e


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

async def list_notes(
    *,
    created_after: datetime | None = None,
    cursor: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """GET /notes — liste paginée des notes (résumé seulement, pas le transcript)."""
    params: dict[str, str] = {}
    if created_after:
        # ISO 8601 avec timezone (UTC si naive)
        if created_after.tzinfo is None:
            created_after = created_after.replace(tzinfo=timezone.utc)
        params["created_after"] = created_after.isoformat()
    if cursor:
        params["cursor"] = cursor
    return await _request("GET", "/notes", params=params, api_key=api_key)


async def get_note(
    note_id: str,
    *,
    include_transcript: bool = True,
    api_key: str | None = None,
) -> dict[str, Any]:
    """GET /notes/{id} — note complète, transcript inclus par défaut.

    Lève GranolaNoteNotReady (404) si la note existe sans summary/transcript IA.
    """
    params = {"include": "transcript"} if include_transcript else {}
    return await _request("GET", f"/notes/{note_id}", params=params, api_key=api_key)


async def list_notes_paginated(
    *,
    created_after: datetime | None = None,
    max_pages: int = 5,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Itère les pages jusqu'à `max_pages` ou `hasMore=False`. Pour matcher un
    booking on a rarement besoin de plus d'1-2 pages (window de 1-2h).
    """
    all_notes: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = await list_notes(created_after=created_after, cursor=cursor, api_key=api_key)
        notes = page.get("notes") if isinstance(page, dict) else None
        if isinstance(notes, list):
            all_notes.extend(notes)
        if not (isinstance(page, dict) and page.get("hasMore")):
            break
        cursor = page.get("cursor")
        if not cursor:
            break
        # Politesse : 200ms entre pages pour rester loin du 5 req/s
        await asyncio.sleep(0.2)
    return all_notes


# ----------------------------------------------------------------------
# Healthcheck
# ----------------------------------------------------------------------

async def healthcheck(api_key: str | None = None) -> dict[str, Any]:
    """Sanity check : tente un list_notes avec un cap récent. Utile pour
    valider la config Railway sans déclencher de traitement.
    """
    try:
        page = await list_notes(
            created_after=datetime.now(timezone.utc).replace(microsecond=0),
            api_key=api_key,
        )
        return {"ok": True, "notes_count": len(page.get("notes") or [])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
