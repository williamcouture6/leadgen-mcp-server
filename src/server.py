"""Entrypoint FastMCP — expose les tools du pipeline lead gen.

Lancement :
    python -m src.server                            # stdio (Claude Code, mcp-inspector)
    python -m src.server --transport http --port 8765  # HTTP/SSE (n8n MCP node)
"""
from __future__ import annotations

import argparse
from typing import Literal

from fastmcp import FastMCP

from .tools import db as db_tools
from .tools import enrich as enrich_tools
from .tools import maps as maps_tools

mcp: FastMCP = FastMCP(
    name="leadgen-mcp",
    instructions=(
        "Serveur MCP du pipeline lead gen PME QC. "
        "Tools disponibles : db.* (Supabase), maps.* (Google Places). "
        "Toutes les écritures DB passent par service_role, donc traiter avec soin."
    ),
)


# ----------------------------------------------------------------------
# Tools db.*
# ----------------------------------------------------------------------

@mcp.tool(name="db_next_sourcing_target")
async def next_sourcing_target() -> dict | None:
    """Retourne la prochaine cible (city, sector, icp_segment) à scraper.

    Itère le catalogue ICP × top villes QC et applique un cooldown de 30 jours
    via la table `sourcing_runs`. Retourne `None` si toutes les cibles sont
    en cooldown.
    """
    target = await db_tools.next_sourcing_target()
    return target.model_dump() if target else None


@mcp.tool(name="db_start_sourcing_run")
async def start_sourcing_run(
    city: str,
    sector: str,
    icp_segment: str,
    search_query: str | None = None,
) -> dict:
    """Crée un nouveau `sourcing_runs` en status='running'."""
    out = await db_tools.start_sourcing_run(
        db_tools.StartRunIn(
            city=city, sector=sector, icp_segment=icp_segment, search_query=search_query
        )
    )
    return out.model_dump()


@mcp.tool(name="db_complete_sourcing_run")
async def complete_sourcing_run(
    run_id: str,
    status: Literal["completed", "failed"],
    next_page_token: str | None = None,
    results_count: int = 0,
    new_companies_count: int = 0,
    duplicates_count: int = 0,
    error_text: str | None = None,
) -> dict:
    """Marque un sourcing_run terminé avec les métriques."""
    return await db_tools.complete_sourcing_run(
        db_tools.CompleteRunIn(
            run_id=run_id,
            status=status,
            next_page_token=next_page_token,
            results_count=results_count,
            new_companies_count=new_companies_count,
            duplicates_count=duplicates_count,
            error_text=error_text,
        )
    )


@mcp.tool(name="db_insert_company")
async def insert_company(payload: dict) -> dict:
    """Insert une company avec dédup. Renvoie status='inserted' ou 'duplicate'."""
    out = await db_tools.insert_company(db_tools.CompanyIn(**payload))
    return out.model_dump()


@mcp.tool(name="db_list_recent_companies")
async def list_recent_companies(limit: int = 20) -> list[dict]:
    """Liste les companies récentes (vérif manuelle après WF-1)."""
    return await db_tools.list_recent_companies(limit=limit)


# ----------------------------------------------------------------------
# Tools maps.*
# ----------------------------------------------------------------------

@mcp.tool(name="maps_search_places")
async def search_places(
    city: str,
    sector: str,
    page_token: str | None = None,
    max_results: int = 20,
) -> dict:
    """Google Places Text Search : `{sector} in {city}, Québec, Canada`.

    Retourne 1-20 résultats + `next_page_token` si plus de résultats disponibles.
    """
    out = await maps_tools.search_places(
        maps_tools.SearchPlacesIn(
            city=city, sector=sector, page_token=page_token, max_results=max_results
        )
    )
    return out.model_dump()


# ----------------------------------------------------------------------
# Tools enrich.* (Phase 1B — Apollo)
# ----------------------------------------------------------------------

@mcp.tool(name="enrich_apollo_org")
async def enrich_apollo_org(domain: str) -> dict:
    """Apollo organizations/enrich — fonctionne sur trial. Cache 90j."""
    out = await enrich_tools.enrich_org(enrich_tools.EnrichOrgIn(domain=domain))
    return out.model_dump()


@mcp.tool(name="enrich_apollo_decision_makers")
async def enrich_apollo_decision_makers(
    organization_id: str | None = None,
    organization_name: str | None = None,
    titles: list[str] | None = None,
    per_page: int = 5,
) -> dict:
    """Apollo mixed_people/search — paid plan only. Retourne les décideurs."""
    out = await enrich_tools.search_decision_makers(
        enrich_tools.SearchDecisionMakersIn(
            organization_id=organization_id,
            organization_name=organization_name,
            titles=titles or enrich_tools.DEFAULT_DECISION_MAKER_TITLES,
            per_page=per_page,
        )
    )
    return out.model_dump()


@mcp.tool(name="enrich_apollo_match_person")
async def enrich_apollo_match_person(
    first_name: str,
    last_name: str,
    organization_name: str | None = None,
    domain: str | None = None,
) -> dict:
    """Apollo people/match — paid plan only. Email + phone d'une personne."""
    out = await enrich_tools.match_person(
        enrich_tools.MatchPersonIn(
            first_name=first_name,
            last_name=last_name,
            organization_name=organization_name,
            domain=domain,
        )
    )
    return out.model_dump()


@mcp.tool(name="db_insert_contact")
async def insert_contact(payload: dict) -> dict:
    """Insert un contact (dédup sur company_id+email)."""
    out = await db_tools.insert_contact(db_tools.ContactIn(**payload))
    return out.model_dump()


@mcp.tool(name="db_list_companies_to_enrich")
async def list_companies_to_enrich(limit: int = 50) -> list[dict]:
    """Companies status='sourced' à traiter par WF-2."""
    return await db_tools.list_companies_to_enrich(limit=limit)


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="leadgen-mcp server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (Claude Code) ou http (SSE pour n8n)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()  # stdio par défaut
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
