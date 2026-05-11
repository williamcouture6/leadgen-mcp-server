"""Orchestrateur Python du WF-2 (enrichissement Apollo).

⚠️  Nécessite Apollo Basic plan PAYANT (pas trial). À tester après purchase ~20 mai 2026.

Logique :
  1. Liste les companies status='sourced' avec un `domain` (skip celles sans site web)
  2. Pour chaque :
     a. organizations/enrich(domain) → organization_id + détails
     b. mixed_people/search(org_id, titles=décideurs) → 5 personnes max
     c. Pour chaque personne avec email vérifié → insert_contact
     d. mark_company_enriched
  3. Compte total contacts trouvés

Coût estimé Apollo : ~3 crédits par company enrichie (1 org + 1 search + 1 match si besoin).
Budget Basic = 75 crédits/mois → ~25 companies enrichies/mois.

Usage :
    python scripts/run_enrichment_pass.py --limit 5      # 5 companies max
    python scripts/run_enrichment_pass.py --dry-run      # n'écrit rien
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import db, enrich  # noqa: E402


async def enrich_one(company: dict, dry_run: bool) -> tuple[int, int]:
    """Retourne (decision_makers_trouvés, contacts_insérés)."""
    domain = company.get("domain")
    company_id = company["id"]
    name = company["name"]

    if not domain:
        print(f"  [skip] {name} — pas de domain")
        return (0, 0)

    print(f"  [enrich org] {name} ({domain})")
    try:
        org = await enrich.enrich_org(enrich.EnrichOrgIn(domain=domain))
    except Exception as e:  # noqa: BLE001
        print(f"    [ERREUR enrich_org] {repr(e)}")
        return (0, 0)

    if not org.organization_id:
        print(f"    [skip] Apollo n'a pas reconnu le domain")
        return (0, 0)

    print(f"    org_id={org.organization_id} | {org.estimated_num_employees} empl.")

    try:
        sm = await enrich.search_decision_makers(
            enrich.SearchDecisionMakersIn(
                organization_id=org.organization_id, per_page=5
            )
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [ERREUR search_decision_makers] {repr(e)}")
        return (0, 0)

    inserted = 0
    for person in sm.people:
        label = f"{person.first_name or '?'} {person.last_name or '?'} — {person.title or 'N/A'}"
        if not person.email or person.email_status not in ("verified", "likely_to_engage"):
            print(f"    [skip] {label} — pas d'email vérifié (status={person.email_status})")
            continue

        if dry_run:
            print(f"    [dry] {label} → {person.email}")
            inserted += 1
            continue

        res = await db.insert_contact(
            db.ContactIn(
                company_id=company_id,
                first_name=person.first_name,
                last_name=person.last_name,
                email=person.email,
                email_verified=person.email_status == "verified",
                email_verification_source="apollo",
                phone=person.phone,
                linkedin_url=person.linkedin_url,
                title=person.title,
                seniority=person.seniority,
                is_decision_maker=True,
                source="apollo",
                raw_payload={"apollo_id": person.apollo_id},
            )
        )
        if res.status == "inserted":
            print(f"    [new contact] {label} → {person.email}")
            inserted += 1
        else:
            print(f"    [dup contact] {label}")

    if not dry_run and inserted > 0:
        await db.mark_company_enriched(company_id)

    return (len(sm.people), inserted)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    companies = await db.list_companies_to_enrich(limit=args.limit)
    print(f">>> {len(companies)} companies à enrichir (limit={args.limit}, dry={args.dry_run})\n")

    total_dms = 0
    total_inserted = 0
    for c in companies:
        dms, inserted = await enrich_one(c, args.dry_run)
        total_dms += dms
        total_inserted += inserted

    print(f"\n=== Résumé ===")
    print(f"  Companies traitées       : {len(companies)}")
    print(f"  Décideurs trouvés        : {total_dms}")
    print(f"  Contacts ajoutés en DB   : {total_inserted}")


if __name__ == "__main__":
    asyncio.run(main())
