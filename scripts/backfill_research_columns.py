"""Backfill GRATUIT des colonnes dérivées de companies.research_json.

Recalcule, SANS appel LLM ni re-scrape, les colonnes que WF-3 remplit
aujourd'hui mais qui étaient vides sur les boîtes researchées avec un ancien
prompt :
  - decideur_confirme / decideur_potentiel  (depuis research_json.decideur_candidats
    + emails nominatifs reconstruits depuis contacts.raw_payload)
  - lead_potential_score / lead_potential_reason  (UNIQUEMENT si research_json
    contient déjà `lead_potential` — sinon laissé tel quel, ça exige un re-research)
  - status -> 'enriched'  (+ last_enriched_at)

On réutilise telle quelle la fonction de prod `db.update_company_research` : même
logique, donc le backfill ne peut pas diverger de WF-3. Les boîtes
disqualified/suppressed sont protégées par le filtre interne de cette fonction.

Usage :
    python scripts/backfill_research_columns.py --track REACTI --dry-run
    python scripts/backfill_research_columns.py --track REACTI
    python scripts/backfill_research_columns.py --track OPT
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

# UTF-8 stdout pour PowerShell
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import db as dbt  # noqa: E402
from src.lib.owner_match import summarize_company_decideur  # noqa: E402
from src import supabase_client as sb  # noqa: E402


async def _emails_found_for_company(company_id: str) -> list[dict]:
    """Reconstruit `emails_found` depuis les contacts déjà insérés.

    insert_contact stocke {kind, local, source_url} dans contacts.raw_payload :
    c'est exactement ce que summarize_company_decideur attend (il ne lit que
    `kind` + `local`). Fidèle à ce que WF-3 avait au moment du scrape, puisque
    les contacts SONT les emails scrapés.
    """
    rows = await sb.select(
        "contacts",
        params={"select": "raw_payload", "company_id": f"eq.{company_id}", "limit": "1000"},
    )
    out: list[dict] = []
    for r in rows:
        rp = r.get("raw_payload") or {}
        if rp.get("local"):
            out.append({"local": rp.get("local"), "kind": rp.get("kind")})
    return out


async def run(track: str, dry_run: bool) -> None:
    companies = await sb.select(
        "companies",
        params={
            "select": "id,name,status,research_json,lead_potential_score",
            "track": f"eq.{track}",
            "research_json": "not.is.null",
            "status": "not.in.(disqualified,suppressed)",
            "order": "created_at.asc",
            "limit": "2000",
        },
    )
    print(f"[{track}] {len(companies)} boîtes researchées (hors disqualified/suppressed).")

    n_confirme = n_potentiel = n_aucun = 0
    n_score_present = n_score_absent = 0
    n_written = 0

    for co in companies:
        rj = co.get("research_json") or {}
        emails = await _emails_found_for_company(co["id"])
        confirme, potentiel = summarize_company_decideur(
            rj.get("decideur_candidats"), emails
        )
        if confirme:
            n_confirme += 1
        elif potentiel:
            n_potentiel += 1
        else:
            n_aucun += 1

        has_score = isinstance(rj.get("lead_potential"), dict) and isinstance(
            rj["lead_potential"].get("score"), int
        )
        n_score_present += int(bool(has_score))
        n_score_absent += int(not has_score)

        if not dry_run:
            await dbt.update_company_research(co["id"], rj, emails_found=emails)
            n_written += 1

    print("─" * 60)
    print(f"décideur : confirmé={n_confirme}  potentiel={n_potentiel}  aucun={n_aucun}")
    print(f"score     : présent dans JSON={n_score_present}  absent (besoin re-research)={n_score_absent}")
    if dry_run:
        print("DRY-RUN : rien écrit. Relance sans --dry-run pour appliquer.")
    else:
        print(f"écrit : {n_written} boîtes mises à jour (status->enriched, décideur, score si présent).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", default="REACTI", choices=["OPT", "REACTI"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.track, args.dry_run))


if __name__ == "__main__":
    main()
