"""Orchestrateur Python du WF-1 (sourcing) — version stand-alone.

🔴 CE N'EST PLUS LE CHEMIN DE LA PISTE `agence-ia` DEPUIS LE 2026-09-16.
Pour sourcer aujourd'hui, on ne lance PAS ce script : on lance
`scripts/balayage.py`, qui découpe une région en tuiles et énumère pour 0 $
(masque `places.id,nextPageToken`), puis WF-1 hydrate depuis l'inventaire.
Mesure du 2026-09-16 : le déneigement est passé de 201 à 922 entreprises
connues pour 3 063 appels gratuits — là où ce script-ci pose UNE question par
lancement, facturée, et écrit `city = p.city or city`, un repli sur la ville du
catalogue dont `companies.dedup_key` dépend.

Il reste utile pour la piste `OPT`, qui n'a aucun équivalent côté inventaire.
⚠️ Il duplique la boucle de `_run_wf1` : toute correction apportée là-bas doit
être reportée ici, et c'est exactement le trou qu'a déjà refermé
`test_sourcing_piste_vivante.py` (le script taguait `OPT` en silence).
⚠️ Il est hors du verrou serveur `_VERROUS_DE_LOT` : lancé pendant qu'une pioche
tourne, rien ne le refuse.


Reproduit la logique cible du n8n WF-1 :
  1. Trouve la prochaine cible (city, sector) via cooldown 30j
  2. Crée un sourcing_run
  3. Boucle sur les pages Google Places (max 3 pages = ~60 résultats)
  4. Pour chaque place : insert avec dédup → counts new vs duplicate
  5. Marque le run completed

Usage :
    python scripts/run_sourcing_pass.py                    # auto-pick next target
    python scripts/run_sourcing_pass.py --city Montréal --sector cafe --icp commerce_local
    python scripts/run_sourcing_pass.py --max-pages 1      # 1 page seulement (20 résultats)
    python scripts/run_sourcing_pass.py --dry-run          # n'insère rien
    python scripts/run_sourcing_pass.py --track OPT        # piste gelée (historique)

⚠️ `--track` gouverne DEUX choses : le catalogue où la cible est choisie, ET
la valeur écrite dans `companies.track` à l'insert. Jusqu'au 2026-09-15 le
script ne passait rien du tout : il sourçait le catalogue `OPT`, gelé depuis
le pivot du 2026-06-07, et surtout il TAGUAIT `OPT` toutes les fiches qu'il
insérait. Ces fiches sont invisibles pour WF-4, qui filtre `agence-ia` — le
sourcing paraissait réussi, les entreprises étaient bien en base, et rien
n'allait jamais les démarcher.
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

from src.tools import db, maps  # noqa: E402

# Délai entre 2 pages : Google Places (New) demande un court warm-up sur le
# nextPageToken (~2s). Sous-évaluer = INVALID_ARGUMENT.
PAGINATION_DELAY_SECONDS = 2.5


async def pick_target(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.city and args.sector and args.icp:
        return args.city, args.sector, args.icp
    target = await db.next_sourcing_target(track=args.track)
    if target is None:
        print("Aucune cible disponible (toutes en cooldown 30j). Stop.")
        sys.exit(0)
    print(f"Cible auto : {target.city} / {target.sector} ({target.icp_segment}) — {target.reason}")
    return target.city, target.sector, target.icp_segment


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default=None)
    parser.add_argument("--sector", default=None)
    parser.add_argument("--icp", default=None, help="commerce_local | services_pro | manufacturier")
    parser.add_argument("--max-pages", type=int, default=3, help="1..3 (Google retourne ~60 max)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--track", default="agence-ia",
        help="piste : agence-ia (vivante, défaut) | OPT (gelée). Gouverne le "
             "catalogue de cibles ET le tag écrit dans companies.track.",
    )
    args = parser.parse_args()

    city, sector, icp = await pick_target(args)
    print(f"\n>>> Sourcing pass : {city} | {sector} | {icp} | track={args.track} | max_pages={args.max_pages} | dry={args.dry_run}")

    run_id: str | None = None
    if not args.dry_run:
        run = await db.start_sourcing_run(
            db.StartRunIn(city=city, sector=sector, icp_segment=icp)
        )
        run_id = run.run_id
        print(f"sourcing_run id = {run_id}")

    page_token: str | None = None
    total_results = 0
    new_count = 0
    dup_count = 0
    error_text: str | None = None

    try:
        for page_num in range(args.max_pages):
            if page_num > 0 and page_token is None:
                break  # plus de pages disponibles

            if page_num > 0:
                await asyncio.sleep(PAGINATION_DELAY_SECONDS)

            print(f"\n--- Page {page_num + 1}/{args.max_pages} (token={'yes' if page_token else 'first'}) ---")
            search_out = await maps.search_places(
                maps.SearchPlacesIn(
                    city=city, sector=sector, page_token=page_token, max_results=20
                )
            )
            total_results += len(search_out.results)
            print(f"  {len(search_out.results)} résultats")

            for place in search_out.results:
                if args.dry_run:
                    print(f"  [dry] {place.name} | {place.google_place_id}")
                    continue
                res = await db.insert_company(
                    db.CompanyIn(
                        name=place.name,
                        google_place_id=place.google_place_id,
                        address=place.formatted_address,
                        city=place.city or city,
                        postal_code=place.postal_code,
                        latitude=place.latitude,
                        longitude=place.longitude,
                        website=place.website,
                        domain=place.domain,
                        icp_segment=icp,
                        industry=sector,
                        google_types=place.google_types,
                        google_rating=place.google_rating,
                        google_reviews_count=place.google_reviews_count,
                        # Sans ça, `CompanyIn.track` retombe sur son défaut
                        # `'OPT'` et la fiche devient invisible pour WF-4.
                        track=args.track,
                        raw_payload=place.raw_payload,
                    )
                )
                if res.status == "inserted":
                    new_count += 1
                    print(f"  [new] {place.name}")
                else:
                    dup_count += 1
                    print(f"  [dup:{res.dedup_reason}] {place.name}")

            page_token = search_out.next_page_token
            if not page_token:
                print("  (pas de page suivante)")
                break

    except Exception as e:  # noqa: BLE001
        error_text = repr(e)
        print(f"\n[ERREUR] {error_text}")

    if run_id and not args.dry_run:
        await db.complete_sourcing_run(
            db.CompleteRunIn(
                run_id=run_id,
                status="failed" if error_text else "completed",
                next_page_token=page_token,
                results_count=total_results,
                new_companies_count=new_count,
                duplicates_count=dup_count,
                error_text=error_text,
            )
        )

    print(
        f"\n=== Résumé ===\n"
        f"  Total résultats Google : {total_results}\n"
        f"  Nouvelles companies    : {new_count}\n"
        f"  Doublons               : {dup_count}\n"
        f"  Erreur                 : {error_text or 'aucune'}"
    )


if __name__ == "__main__":
    asyncio.run(main())
