"""Client HTTP léger pour PostgREST (Supabase) avec service_role.

On évite supabase-py pour rester async-native et léger. Toutes les écritures
passent par service_role : RLS est bypassé, donc on est responsable de la
sécurité côté code (le MCP server n'est jamais exposé à l'extérieur sans auth).
"""
from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import settings


def _headers() -> dict[str, str]:
    s = settings()
    return {
        "apikey": s.supabase_service_role_key,
        "Authorization": f"Bearer {s.supabase_service_role_key}",
        "Content-Type": "application/json",
        # Prefer return=representation : on récupère la ligne insérée/maj
        "Prefer": "return=representation",
    }


def _rest_url(path: str) -> str:
    base = settings().supabase_url.rstrip("/")
    return f"{base}/rest/v1/{path.lstrip('/')}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def select(
    table: str,
    *,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(_rest_url(table), headers=_headers(), params=params or {})
        r.raise_for_status()
        return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def insert(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    on_conflict: str | None = None,
    ignore_duplicates: bool = False,
) -> list[dict[str, Any]]:
    headers = _headers()
    params: dict[str, str] = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
        if ignore_duplicates:
            headers["Prefer"] = "return=representation,resolution=ignore-duplicates"
        else:
            headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    body = row if isinstance(row, list) else [row]
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(_rest_url(table), headers=headers, params=params, json=body)
        r.raise_for_status()
        return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def update(
    table: str,
    patch: dict[str, Any],
    *,
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.patch(
            _rest_url(table), headers=_headers(), params=filters, json=patch
        )
        r.raise_for_status()
        return r.json()


async def rpc(name: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(_rest_url(f"rpc/{name}"), headers=_headers(), json=args)
        r.raise_for_status()
        return r.json()
