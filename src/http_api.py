"""API HTTP REST (FastAPI) — appelée par n8n.

Expose les mêmes fonctions que le serveur MCP (qui reste utilisé en stdio par
Claude Code), mais en routes REST simples pour faciliter l'intégration avec
le node HTTP Request de n8n cloud.

Sécurité : Bearer token statique partagé (`AGENTS_HTTP_TOKEN` dans .env).
À durcir avant un déploiement public (rotation, scopes, etc.).
"""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from .tools import db as db_tools
from .tools import enrich as enrich_tools
from .tools import maps as maps_tools
from .tools import research as research_tools


def _expected_token() -> str | None:
    tok = os.environ.get("AGENTS_HTTP_TOKEN")
    return tok or None


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = _expected_token()
    if not expected:
        # Mode dev sans token : refuse, pour éviter l'oubli avant déploiement.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AGENTS_HTTP_TOKEN non défini côté serveur",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer")
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")


app = FastAPI(title="leadgen-mcp HTTP API", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------------- Sourcing ----------------

@app.get("/sourcing/next-target", dependencies=[Depends(_require_auth)])
async def next_target() -> dict[str, Any] | None:
    t = await db_tools.next_sourcing_target()
    return t.model_dump() if t else None


@app.post("/sourcing/start-run", dependencies=[Depends(_require_auth)])
async def start_run(payload: db_tools.StartRunIn) -> dict[str, Any]:
    return (await db_tools.start_sourcing_run(payload)).model_dump()


@app.post("/sourcing/complete-run", dependencies=[Depends(_require_auth)])
async def complete_run(payload: db_tools.CompleteRunIn) -> dict[str, Any]:
    return await db_tools.complete_sourcing_run(payload)


# ---------------- Companies ----------------

@app.post("/companies/insert", dependencies=[Depends(_require_auth)])
async def insert_company(payload: db_tools.CompanyIn) -> dict[str, Any]:
    return (await db_tools.insert_company(payload)).model_dump()


@app.get("/companies/recent", dependencies=[Depends(_require_auth)])
async def recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db_tools.list_recent_companies(limit=limit)


# ---------------- Maps ----------------

@app.post("/maps/search-places", dependencies=[Depends(_require_auth)])
async def search_places(payload: maps_tools.SearchPlacesIn) -> dict[str, Any]:
    return (await maps_tools.search_places(payload)).model_dump()


# ---------------- Contacts (Phase 1B) ----------------

@app.post("/contacts/insert", dependencies=[Depends(_require_auth)])
async def insert_contact(payload: db_tools.ContactIn) -> dict[str, Any]:
    return (await db_tools.insert_contact(payload)).model_dump()


@app.get("/companies/to-enrich", dependencies=[Depends(_require_auth)])
async def companies_to_enrich(limit: int = 50) -> list[dict[str, Any]]:
    return await db_tools.list_companies_to_enrich(limit=limit)


# ---------------- Enrich (Apollo, Phase 1B) ----------------

@app.post("/enrich/apollo/org", dependencies=[Depends(_require_auth)])
async def enrich_org(payload: enrich_tools.EnrichOrgIn) -> dict[str, Any]:
    return (await enrich_tools.enrich_org(payload)).model_dump()


@app.post("/enrich/apollo/decision-makers", dependencies=[Depends(_require_auth)])
async def enrich_decision_makers(
    payload: enrich_tools.SearchDecisionMakersIn,
) -> dict[str, Any]:
    return (await enrich_tools.search_decision_makers(payload)).model_dump()


@app.post("/enrich/apollo/match", dependencies=[Depends(_require_auth)])
async def enrich_match(payload: enrich_tools.MatchPersonIn) -> dict[str, Any]:
    return (await enrich_tools.match_person(payload)).model_dump()


# ---------------- High-level workflow (WF-1 en un appel) ----------------

class RunWf1In(BaseModel):
    """Lance un pass complet WF-1 côté serveur — pratique pour n8n qui n'a
    qu'à déclencher le cron, le serveur gère le reste."""
    city: str | None = None
    sector: str | None = None
    icp_segment: str | None = None
    max_pages: int = 3
    dry_run: bool = False


class RunWf1Out(BaseModel):
    target: dict[str, Any] | None
    run_id: str | None
    total_results: int
    new_companies_count: int
    duplicates_count: int
    error_text: str | None = None


@app.post("/wf1/run", dependencies=[Depends(_require_auth)], response_model=RunWf1Out)
async def run_wf1(payload: RunWf1In) -> RunWf1Out:
    import asyncio

    # 1) Pick target
    if payload.city and payload.sector and payload.icp_segment:
        city, sector, icp = payload.city, payload.sector, payload.icp_segment
        target_meta = {"city": city, "sector": sector, "icp_segment": icp, "reason": "explicit"}
    else:
        t = await db_tools.next_sourcing_target()
        if not t:
            return RunWf1Out(
                target=None, run_id=None, total_results=0,
                new_companies_count=0, duplicates_count=0,
                error_text="no_target_available",
            )
        city, sector, icp = t.city, t.sector, t.icp_segment
        target_meta = t.model_dump()

    # 2) Start run (sauf dry_run)
    run_id: str | None = None
    if not payload.dry_run:
        run = await db_tools.start_sourcing_run(
            db_tools.StartRunIn(city=city, sector=sector, icp_segment=icp)
        )
        run_id = run.run_id

    page_token: str | None = None
    total_results = 0
    new_count = 0
    dup_count = 0
    error_text: str | None = None

    try:
        for page_num in range(payload.max_pages):
            if page_num > 0:
                if not page_token:
                    break
                await asyncio.sleep(2.5)  # warm-up nextPageToken Google
            out = await maps_tools.search_places(
                maps_tools.SearchPlacesIn(
                    city=city, sector=sector, page_token=page_token, max_results=20
                )
            )
            total_results += len(out.results)

            for p in out.results:
                if payload.dry_run:
                    continue
                res = await db_tools.insert_company(
                    db_tools.CompanyIn(
                        name=p.name,
                        google_place_id=p.google_place_id,
                        address=p.formatted_address,
                        city=p.city or city,
                        postal_code=p.postal_code,
                        latitude=p.latitude,
                        longitude=p.longitude,
                        website=p.website,
                        domain=p.domain,
                        icp_segment=icp,
                        industry=sector,
                        google_types=p.google_types,
                        google_rating=p.google_rating,
                        google_reviews_count=p.google_reviews_count,
                        raw_payload=p.raw_payload,
                    )
                )
                if res.status == "inserted":
                    new_count += 1
                else:
                    dup_count += 1

            page_token = out.next_page_token
            if not page_token:
                break

    except Exception as e:  # noqa: BLE001
        error_text = repr(e)

    if run_id:
        await db_tools.complete_sourcing_run(
            db_tools.CompleteRunIn(
                run_id=run_id,
                status="failed" if error_text else "completed",
                next_page_token=page_token,
                results_count=total_results,
                new_companies_count=new_count,
                duplicates_count=dup_count,
                error_text=error_text,
            )
        )

    return RunWf1Out(
        target=target_meta,
        run_id=run_id,
        total_results=total_results,
        new_companies_count=new_count,
        duplicates_count=dup_count,
        error_text=error_text,
    )


# ---------------- Research (Phase 2 — WF-3) ----------------

@app.get("/companies/to-research", dependencies=[Depends(_require_auth)])
async def companies_to_research(
    limit: int = 20,
    require_website: bool = True,
) -> list[dict[str, Any]]:
    """Companies sans research_json. Utilisé par n8n pour visualiser le backlog."""
    return await db_tools.list_companies_to_research(
        limit=limit, require_website=require_website
    )


class ResearchCompanyByIdIn(BaseModel):
    company_id: str
    model: str = "claude-sonnet-4-6"


class ResearchCompanyByIdOut(BaseModel):
    company_id: str
    status: str  # "ok" | "skipped_no_place_id" | "error"
    research_json: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_text: str | None = None


@app.post(
    "/research/company",
    dependencies=[Depends(_require_auth)],
    response_model=ResearchCompanyByIdOut,
)
async def research_company_by_id(payload: ResearchCompanyByIdIn) -> ResearchCompanyByIdOut:
    """Research d'UNE company. Pratique pour n8n quand on veut traiter
    company-par-company (avec retry par item)."""
    from . import supabase_client as db
    matches = await db.select(
        "companies",
        params={
            "select": "id,google_place_id,website,name",
            "id": f"eq.{payload.company_id}",
            "limit": "1",
        },
    )
    if not matches:
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text="company_not_found",
        )
    co = matches[0]
    if not co.get("google_place_id"):
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="skipped_no_place_id",
        )

    try:
        out = await research_tools.research_company(
            research_tools.ResearchCompanyIn(
                google_place_id=co["google_place_id"],
                website=co.get("website"),
                model=payload.model,
            )
        )
    except Exception as e:  # noqa: BLE001
        # Audit l'échec, sans bloquer
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="research",
                    model=payload.model,
                    company_id=payload.company_id,
                    error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text=repr(e),
        )

    await db_tools.update_company_research(payload.company_id, out.research_json)
    try:
        await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="research",
                model=out.model,
                company_id=payload.company_id,
                input_payload={
                    "google_place_id": co["google_place_id"],
                    "website": co.get("website"),
                },
                output_payload=out.research_json,
                duration_ms=out.duration_ms,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                cache_read_tokens=out.usage.cache_read_input_tokens,
                cache_creation_tokens=out.usage.cache_creation_input_tokens,
            )
        )
    except Exception:  # noqa: BLE001
        pass

    return ResearchCompanyByIdOut(
        company_id=payload.company_id,
        status="ok",
        research_json=out.research_json,
        duration_ms=out.duration_ms,
    )


class RunWf3In(BaseModel):
    """Pass complet WF-3 : prend N companies sans research_json, les traite séquentiellement.

    On reste séquentiel volontairement — l'API Anthropic et Google Places ont des
    rate limits, et un cron quotidien sur 10-20 companies tolère bien 30s par item.
    """
    limit: int = 10
    model: str = "claude-sonnet-4-6"
    require_website: bool = True


class RunWf3Item(BaseModel):
    company_id: str
    name: str | None = None
    status: str
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf3Out(BaseModel):
    processed: int
    succeeded: int
    failed: int
    skipped: int
    items: list[RunWf3Item]


@app.post("/wf3/run", dependencies=[Depends(_require_auth)], response_model=RunWf3Out)
async def run_wf3(payload: RunWf3In) -> RunWf3Out:
    backlog = await db_tools.list_companies_to_research(
        limit=payload.limit, require_website=payload.require_website
    )

    items: list[RunWf3Item] = []
    succeeded = failed = skipped = 0

    for co in backlog:
        try:
            res = await research_company_by_id(
                ResearchCompanyByIdIn(company_id=co["id"], model=payload.model)
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            items.append(RunWf3Item(
                company_id=co["id"], name=co.get("name"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok":
            succeeded += 1
        elif res.status.startswith("skipped"):
            skipped += 1
        else:
            failed += 1
        items.append(RunWf3Item(
            company_id=co["id"], name=co.get("name"),
            status=res.status, duration_ms=res.duration_ms,
            error_text=res.error_text,
        ))

    return RunWf3Out(
        processed=len(items), succeeded=succeeded, failed=failed,
        skipped=skipped, items=items,
    )
