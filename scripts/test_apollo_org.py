"""Test live Apollo organizations/enrich (accessible même sur trial Basic)."""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import enrich  # noqa: E402


async def main() -> None:
    # Test 1 : domaine random connu (valide le mécanisme + Apollo répond)
    test_domains = ["stripe.com", "shopify.com"]
    for d in test_domains:
        print(f"\n--- enrich_org({d}) ---")
        try:
            out = await enrich.enrich_org(enrich.EnrichOrgIn(domain=d))
            print(f"name={out.name}")
            print(f"organization_id={out.organization_id}")
            print(f"industry={out.industry}")
            print(f"estimated_num_employees={out.estimated_num_employees}")
            print(f"linkedin={out.linkedin_url}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERREUR] {repr(e)}")

    print("\n[OK] Test terminé. Cache 90j en place dans enrichment_cache.")


if __name__ == "__main__":
    asyncio.run(main())
