"""Tool `db` — accès Supabase pour le pipeline.

Phase 1 (sourcing) :
- next_sourcing_target : trouve le prochain (city, sector) à scraper (cooldown 30j)
- start_sourcing_run : crée une trace de pass (status=running)
- complete_sourcing_run : marque completed/failed avec métriques
- insert_company : insert avec dédup 3 clés (google_place_id, neq, dedup_key)
- list_recent_companies : pour vérif manuelle après WF-1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import supabase_client as db


# ----------------------------------------------------------------------
# Catalogue de cibles (city, sector) — ordre de priorité
# ----------------------------------------------------------------------
# Aligné avec docs/icp-playbooks.md. Pour MVP on reste sur 3 segments × top villes.
# Le sector correspond à un keyword Google Places (`type` ou `keyword`).

DEFAULT_CITIES: list[str] = [
    "Montréal", "Québec", "Laval", "Gatineau", "Longueuil",
    "Sherbrooke", "Saguenay", "Lévis", "Trois-Rivières", "Terrebonne",
]

# 3 segments ICP × keywords Google Places
SECTOR_CATALOG: dict[str, list[str]] = {
    "commerce_local": [
        "restaurant",
        "cafe",
        "bakery",
        "beauty_salon",
        "hair_care",
        "gym",
        "spa",
        "auto_repair",
        "florist",
        "pet_store",
    ],
    "services_pro": [
        "lawyer",
        "accountant",
        "real_estate_agency",
        "insurance_agency",
        "dentist",
        "physiotherapist",
        "veterinary_care",
        "travel_agency",
    ],
    "manufacturier": [
        "manufacturer",
        "wholesaler",
        "industrial",
        "metal_fabrication",
        "food_processing",
    ],
}

COOLDOWN_DAYS = 30


def _all_targets() -> list[tuple[str, str, str]]:
    """Retourne la liste complète (city, sector, icp_segment) dans l'ordre de priorité."""
    targets: list[tuple[str, str, str]] = []
    for city in DEFAULT_CITIES:
        for icp, sectors in SECTOR_CATALOG.items():
            for sector in sectors:
                targets.append((city, sector, icp))
    return targets


# ----------------------------------------------------------------------
# Schémas Pydantic (input/output des tools)
# ----------------------------------------------------------------------

class NextTargetOut(BaseModel):
    city: str
    sector: str
    icp_segment: str
    reason: Literal["never_scraped", "cooldown_expired"]


class StartRunIn(BaseModel):
    city: str
    sector: str
    icp_segment: str
    search_query: str | None = None


class StartRunOut(BaseModel):
    run_id: str
    started_at: str


class CompleteRunIn(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    next_page_token: str | None = None
    results_count: int = 0
    new_companies_count: int = 0
    duplicates_count: int = 0
    error_text: str | None = None


class CompanyIn(BaseModel):
    name: str
    google_place_id: str | None = None
    address: str | None = None
    city: str | None = None
    region: str = "QC"
    postal_code: str | None = None
    country: str = "CA"
    latitude: float | None = None
    longitude: float | None = None
    website: str | None = None
    domain: str | None = None
    icp_segment: str | None = None
    industry: str | None = None
    google_types: list[str] = Field(default_factory=list)
    estimated_employees: int | None = None
    google_rating: float | None = None
    google_reviews_count: int | None = None
    source: str = "google_places"
    raw_payload: dict[str, Any] | None = None


class InsertCompanyOut(BaseModel):
    status: Literal["inserted", "duplicate"]
    company_id: str | None = None
    dedup_reason: str | None = None


# ----------------------------------------------------------------------
# Logique
# ----------------------------------------------------------------------

async def next_sourcing_target() -> NextTargetOut | None:
    """Retourne la prochaine cible (city, sector) à scraper, ou None si rien à faire.

    Stratégie :
    1. On itère le catalogue dans l'ordre de priorité.
    2. Pour chaque (city, sector), on regarde le `max(created_at)` dans sourcing_runs.
    3. Si jamais scrapé OU >30j → on retourne cette cible.
    4. Sinon, on passe à la suivante.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)).isoformat()

    # On charge toutes les runs récentes en une seule query pour éviter N requêtes.
    recent = await db.select(
        "sourcing_runs",
        params={
            "select": "city,sector,created_at",
            "created_at": f"gte.{cutoff}",
            "order": "created_at.desc",
        },
    )
    recent_keys = {(r["city"], r["sector"]) for r in recent}

    for city, sector, icp in _all_targets():
        if (city, sector) in recent_keys:
            continue
        reason: Literal["never_scraped", "cooldown_expired"] = (
            "cooldown_expired" if recent else "never_scraped"
        )
        return NextTargetOut(city=city, sector=sector, icp_segment=icp, reason=reason)
    return None


async def start_sourcing_run(payload: StartRunIn) -> StartRunOut:
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.insert(
        "sourcing_runs",
        {
            "city": payload.city,
            "sector": payload.sector,
            "icp_segment": payload.icp_segment,
            "search_query": payload.search_query
            or f"{payload.sector} in {payload.city}",
            "status": "running",
            "started_at": now,
        },
    )
    row = rows[0]
    return StartRunOut(run_id=row["id"], started_at=row["started_at"])


async def complete_sourcing_run(payload: CompleteRunIn) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "status": payload.status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results_count": payload.results_count,
        "new_companies_count": payload.new_companies_count,
        "duplicates_count": payload.duplicates_count,
    }
    if payload.next_page_token is not None:
        patch["next_page_token"] = payload.next_page_token
    if payload.error_text:
        patch["error_text"] = payload.error_text
    rows = await db.update("sourcing_runs", patch, filters={"id": f"eq.{payload.run_id}"})
    return {"updated": len(rows)}


async def insert_company(payload: CompanyIn) -> InsertCompanyOut:
    """Insert avec dédup. Retourne 'duplicate' si une clé conflit."""
    # Pré-check sur les 2 clés business (google_place_id, neq).
    if payload.google_place_id:
        existing = await db.select(
            "companies",
            params={
                "select": "id",
                "google_place_id": f"eq.{payload.google_place_id}",
                "limit": "1",
            },
        )
        if existing:
            return InsertCompanyOut(
                status="duplicate",
                company_id=existing[0]["id"],
                dedup_reason="google_place_id",
            )

    row = payload.model_dump(exclude_none=False)
    # status par défaut = 'sourced' (défini dans la migration)
    try:
        rows = await db.insert("companies", row)
    except Exception as e:  # noqa: BLE001
        # PostgREST renvoie 409 sur conflit unique (dedup_key). On l'attrape grossièrement
        # et on relit la company existante via dedup_key.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower() or "409" in msg:
            # Re-fetch par nom + ville + postal pour récupérer l'id existant
            existing = await db.select(
                "companies",
                params={
                    "select": "id",
                    "name": f"eq.{payload.name}",
                    "city": f"eq.{payload.city or ''}",
                    "limit": "1",
                },
            )
            if existing:
                return InsertCompanyOut(
                    status="duplicate",
                    company_id=existing[0]["id"],
                    dedup_reason="dedup_key",
                )
        raise
    return InsertCompanyOut(status="inserted", company_id=rows[0]["id"])


async def list_recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db.select(
        "companies",
        params={
            "select": "id,name,city,icp_segment,status,created_at,google_rating",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


# ----------------------------------------------------------------------
# Contacts (Phase 1B — utilisé par WF-2 après enrichissement Apollo)
# ----------------------------------------------------------------------

class ContactIn(BaseModel):
    company_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_verified: bool = False
    email_verification_source: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    title: str | None = None
    seniority: str | None = None
    is_decision_maker: bool = False
    source: str = "apollo"
    raw_payload: dict[str, Any] | None = None


class InsertContactOut(BaseModel):
    status: Literal["inserted", "duplicate", "skipped_no_email"]
    contact_id: str | None = None


async def insert_contact(payload: ContactIn) -> InsertContactOut:
    """Insert contact, dédup sur (company_id, email)."""
    if not payload.email:
        return InsertContactOut(status="skipped_no_email")

    existing = await db.select(
        "contacts",
        params={
            "select": "id",
            "company_id": f"eq.{payload.company_id}",
            "email": f"eq.{payload.email}",
            "limit": "1",
        },
    )
    if existing:
        return InsertContactOut(status="duplicate", contact_id=existing[0]["id"])

    row = payload.model_dump(exclude_none=False)
    rows = await db.insert("contacts", row)
    return InsertContactOut(status="inserted", contact_id=rows[0]["id"])


async def mark_company_enriched(company_id: str, status: str = "enriched") -> dict[str, Any]:
    return {
        "updated": len(
            await db.update(
                "companies",
                {"status": status, "last_enriched_at": datetime.now(timezone.utc).isoformat()},
                filters={"id": f"eq.{company_id}"},
            )
        )
    }


async def list_companies_to_enrich(limit: int = 50) -> list[dict[str, Any]]:
    """Companies status='sourced' à passer dans WF-2."""
    return await db.select(
        "companies",
        params={
            "select": "id,name,domain,website,city,icp_segment,industry",
            "status": "eq.sourced",
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )
