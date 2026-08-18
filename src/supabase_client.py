"""Client HTTP léger pour PostgREST (Supabase) avec service_role.

On évite supabase-py pour rester async-native et léger. Toutes les écritures
passent par service_role : RLS est bypassé, donc on est responsable de la
sécurité côté code (le MCP server n'est jamais exposé à l'extérieur sans auth).
"""
from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import settings


# Retry uniquement sur les erreurs vraiment transitoires :
# - Network errors (connect timeout, read timeout, etc.)
# - 5xx serveur temporairement indisponible
#
# Les 4xx (400/401/403/404/409/422/...) ne doivent JAMAIS retry — c'est une
# erreur côté caller (mauvais payload, unique violation, FK manquante).
# Sans ce predicate, tenacity retry 3 fois sur un NOT NULL violation, gaspille
# ~6s + 3 connexions PostgREST pour rien.
_TRANSIENT_HTTPX_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _is_transient_db_error(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_HTTPX_ERRORS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (500, 502, 503, 504)
    return False


_RETRY_KW = dict(
    retry=retry_if_exception(_is_transient_db_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    reraise=True,
)


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


@retry(**_RETRY_KW)
async def select(
    table: str,
    *,
    params: dict[str, str] | None = None,
    schema: str | None = None,
) -> list[dict[str, Any]]:
    headers = _headers()
    if schema:
        headers["Accept-Profile"] = schema
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(_rest_url(table), headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()


# PostgREST plafonne TOUTE réponse à `max-rows`, réglé à 1000 sur ce projet
# (mesuré le 2026-08-17 : la vue v_pourquoi_pas_de_courriel a 1895 lignes, un
# GET sans filtre en rend exactement 1000). Le plafond est serveur : `limit=5000`
# ne le lève pas. Rien ne signale la troncature — pas d'erreur, pas d'en-tête —
# donc `len(await select(...))` rend 1000 et ment sans prévenir.
#
# D'où les deux primitives ci-dessous. Règle : dès qu'une relation PEUT dépasser
# 1000 lignes, ne jamais compter avec `len(select(...))`.
#   - besoin d'un NOMBRE  → count()      : exact, zéro ligne transférée
#   - besoin des LIGNES   → select_all() : pagine par tranches ordonnées
#
# Les agrégats côté serveur (`select=motif,count()`), qui auraient évité la
# pagination, sont désactivés sur ce projet : PGRST123 « Use of aggregate
# functions is not allowed ».
_PAGE_MAX = 1000


def _total_depuis_content_range(header: str | None) -> int:
    """`*/1895` ou `0-999/1895` → 1895.

    Lève plutôt que de rendre 0 : un compte faux et silencieux est exactement
    le défaut que ces primitives existent pour fermer."""
    total = (header or "").rpartition("/")[2]
    if not total.isdigit():
        raise RuntimeError(
            f"Content-Range illisible ({header!r}) — compte impossible. "
            "Vérifier que la requête porte bien Prefer: count=exact."
        )
    return int(total)


@retry(**_RETRY_KW)
async def count(
    table: str,
    *,
    params: dict[str, str] | None = None,
    schema: str | None = None,
) -> int:
    """Compte EXACT côté serveur, sans ramener de lignes (Prefer: count=exact).

    Le total arrive dans l'en-tête Content-Range ; `limit=0` évite de payer le
    transfert des lignes. Insensible au plafond max-rows, contrairement à
    `len(await select(...))`."""
    headers = _headers()
    headers["Prefer"] = "count=exact"
    if schema:
        headers["Accept-Profile"] = schema
    q = {**(params or {}), "limit": "0"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(_rest_url(table), headers=headers, params=q)
        r.raise_for_status()
        return _total_depuis_content_range(r.headers.get("Content-Range"))


async def select_all(
    table: str,
    *,
    order: str,
    params: dict[str, str] | None = None,
    page_size: int = _PAGE_MAX,
    schema: str | None = None,
) -> list[dict[str, Any]]:
    """Toutes les lignes, tranche par tranche, sans se faire couper à 1000.

    `order` est OBLIGATOIRE et doit porter sur une colonne unique et stable
    (`id`, `company_id`) : sans ORDER BY, deux requêtes offset successives
    peuvent sauter ou dupliquer des lignes, l'ordre des lignes n'étant garanti
    par rien côté Postgres.

    Chaque tranche passe par `select()`, donc garde ses retries : une panne
    réseau sur la tranche 2 ne refait pas la tranche 1."""
    # Une tranche plus grande que le plafond serveur serait un piège : la
    # réponse reviendrait coupée à 1000, donc « plus courte que demandé », et
    # la boucle conclurait à tort qu'elle a tout lu.
    page_size = max(1, min(page_size, _PAGE_MAX))
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await select(
            table,
            params={
                **(params or {}),
                "order": order,
                "limit": str(page_size),
                "offset": str(offset),
            },
            schema=schema,
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


@retry(**_RETRY_KW)
async def insert(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    on_conflict: str | None = None,
    ignore_duplicates: bool = False,
    schema: str | None = None,
) -> list[dict[str, Any]]:
    headers = _headers()
    if schema:
        headers["Content-Profile"] = schema
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


@retry(**_RETRY_KW)
async def update(
    table: str,
    patch: dict[str, Any],
    *,
    filters: dict[str, str],
    schema: str | None = None,
) -> list[dict[str, Any]]:
    headers = _headers()
    if schema:
        headers["Content-Profile"] = schema
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.patch(
            _rest_url(table), headers=headers, params=filters, json=patch
        )
        r.raise_for_status()
        return r.json()


async def rpc(name: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(_rest_url(f"rpc/{name}"), headers=_headers(), json=args)
        r.raise_for_status()
        return r.json()


def _storage_url(path: str) -> str:
    base = settings().supabase_url.rstrip("/")
    return f"{base}/storage/v1/{path.lstrip('/')}"


@retry(**_RETRY_KW)
async def upload_object(bucket: str, path: str, data: bytes, content_type: str) -> str:
    """Upload binaire dans Supabase Storage (service_role) → retourne l'URL publique.

    x-upsert: true => ré-héberger la même clé est idempotent (overwrite)."""
    s = settings()
    headers = {
        "apikey": s.supabase_service_role_key,
        "Authorization": f"Bearer {s.supabase_service_role_key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            _storage_url(f"object/{bucket}/{path}"), headers=headers, content=data
        )
        r.raise_for_status()
    base = s.supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{bucket}/{path}"
