"""Test live (network + DB réelle) : valide bout en bout maps + db.

À lancer manuellement, jamais en CI. Coût estimé : 1 Google Places Text Search
(~$0.035 USD) + ~3 requêtes Supabase REST (gratuit).
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

# Force UTF-8 sur stdout (PowerShell par défaut = cp1252)
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ajout du parent dans sys.path pour pouvoir lancer `python scripts/live_test.py`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import db, maps  # noqa: E402


async def main() -> None:
    # 1) Cherche 3 restaurants à Montréal
    print("\n=== maps.search_places (restaurant in Montréal) ===")
    out = await maps.search_places(
        maps.SearchPlacesIn(city="Montréal", sector="restaurant", max_results=3)
    )
    print(f"Got {len(out.results)} results, next_page_token={out.next_page_token!r}")
    for r in out.results:
        print(f"  - {r.name} | {r.city} {r.postal_code} | {r.google_place_id}")

    if not out.results:
        print("Aucun résultat — abandonne le test DB.")
        return

    first = out.results[0]

    # 2) Crée un sourcing_run de test
    print("\n=== db.start_sourcing_run ===")
    run = await db.start_sourcing_run(
        db.StartRunIn(
            city="Montréal",
            sector="restaurant",
            icp_segment="commerce_local",
            search_query="restaurant in Montréal, Québec, Canada (LIVE TEST)",
        )
    )
    print(f"run_id={run.run_id}")

    # 3) Insert la 1re company
    print("\n=== db.insert_company (1er essai) ===")
    res1 = await db.insert_company(
        db.CompanyIn(
            name=first.name,
            google_place_id=first.google_place_id,
            address=first.formatted_address,
            city=first.city or "Montréal",
            postal_code=first.postal_code,
            latitude=first.latitude,
            longitude=first.longitude,
            website=first.website,
            domain=first.domain,
            icp_segment="commerce_local",
            industry="restaurant",
            google_types=first.google_types,
            google_rating=first.google_rating,
            google_reviews_count=first.google_reviews_count,
            raw_payload=first.raw_payload,
        )
    )
    print(f"  -> {res1.model_dump()}")

    # 4) Réessaie → doit retourner duplicate
    print("\n=== db.insert_company (2e essai même place_id, doit être duplicate) ===")
    res2 = await db.insert_company(
        db.CompanyIn(
            name=first.name,
            google_place_id=first.google_place_id,
            city=first.city or "Montréal",
            postal_code=first.postal_code,
            icp_segment="commerce_local",
        )
    )
    print(f"  -> {res2.model_dump()}")
    assert res2.status == "duplicate", "Dédup cassée !"

    # 5) Complete le run
    print("\n=== db.complete_sourcing_run ===")
    await db.complete_sourcing_run(
        db.CompleteRunIn(
            run_id=run.run_id,
            status="completed",
            results_count=len(out.results),
            new_companies_count=1 if res1.status == "inserted" else 0,
            duplicates_count=1 if res2.status == "duplicate" else 0,
        )
    )

    # 6) Liste les 5 dernières companies
    print("\n=== db.list_recent_companies(limit=5) ===")
    recent = await db.list_recent_companies(limit=5)
    print(json.dumps(recent, indent=2, ensure_ascii=False, default=str))

    print("\n[OK] Tous les checks passent.")


if __name__ == "__main__":
    asyncio.run(main())
