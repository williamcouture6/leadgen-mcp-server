"""Rafraîchit les données GOOGLE des fiches déjà en base.

Usage :
    python scripts/rehydrater_companies.py --dry-run
    python scripts/rehydrater_companies.py --limit 50
    python scripts/rehydrater_companies.py            # tout le track

🔴 POURQUOI UN SCRIPT, ET PAS LE PIOCHEUR. `db.insert_company` pré-vérifie le
`google_place_id` et rend « duplicate » SANS RIEN METTRE À JOUR. Repasser une
fiche connue par le piocheur coûterait donc 0,02 $ de Place Details pour jeter
la donnée fraîche à la poubelle. C'est une asymétrie facile à ne pas voir : le
chemin d'INSERTION et le chemin de RAFRAÎCHISSEMENT ne peuvent pas être le même.

CE QU'IL ÉCRIT, ET RIEN D'AUTRE : nom, adresse, ville, code postal, position,
site, domaine, téléphone, note, nombre d'avis, types, `raw_payload`.

🔴 CE QU'IL NE TOUCHE JAMAIS :
  · `research_json`, `metiers`, `fenetre_mois`, `metier_source` — ce sont les
    colonnes de la RECHERCHE, pas du sourcing. Les écraser ici referait le
    défaut que `backfill_metiers.py` documente : un backfill de colonnes
    dérivées doit être un no-op sur tout le reste de la fiche.
  · `last_enriched_at` — elle porte la ré-éligibilité à 90 jours du backlog de
    recherche. La toucher sortirait des fiches du backlog en silence.
  · `status`, SAUF deux cas explicites ci-dessous.
  · les `contacts` et `consent_registry` — la preuve légale de où chaque
    courriel a été trouvé ne se refait pas depuis Google.

LES DEUX SEULS CHANGEMENTS DE STATUT :
  · `CLOSED_PERMANENTLY` → `disqualified`. `prompts/research.md` le dit déjà :
    « personne ne lit ce champ : sans toi, cette entreprise traverse tout le
    pipeline et reçoit un courriel ». On paie 0,02 $ pour le savoir, autant
    s'en servir.
  · identifiant périmé (404) → `disqualified`, motif `place_id_perime`. Google
    recommande de rafraîchir un `place_id` de plus de 12 mois ; rien ne le
    faisait, et ce script est précisément l'occasion de les débusquer.

⚠️ CE SCRIPT COÛTE DE L'ARGENT — SKU Place Details, ~0,02 $ par fiche. Il n'a
PAS d'équivalent gratuit : le masque « identifiants seuls » du balayeur ne rend
aucune donnée. Toujours commencer par `--dry-run`, qui n'appelle rien.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import supabase_client as sb  # noqa: E402
from src.tools import maps as maps_tools  # noqa: E402

# Les seules colonnes que ce script a le droit d'écrire, plus les deux cas de
# statut. Écrite en dur pour qu'un ajout se fasse en connaissance de cause.
COLONNES_SOURCING = (
    "name", "address", "city", "postal_code", "latitude", "longitude",
    "website", "domain", "google_types", "google_rating",
    "google_reviews_count", "raw_payload",
)

DELAI_ENTRE_APPELS_S = 0.12


async def rehydrater(limit: int | None, track: str, dry_run: bool) -> None:
    fiches = await sb.select_all(
        "companies",
        order="id",
        params={
            "select": "id,name,google_place_id,status,domain",
            "track": f"eq.{track}",
            "google_place_id": "not.is.null",
            # Une fiche déjà disqualifiée n'a pas besoin d'être repayée.
            "status": "neq.disqualified",
        },
    )
    if limit:
        fiches = fiches[:limit]

    print(f"{len(fiches)} fiche(s) à rafraîchir{'  [DRY-RUN]' if dry_run else ''}")
    if dry_run:
        print("Aucun appel Google ne part. Coût estimé si lancé pour vrai : "
              f"~{len(fiches) * 0.02:.2f} $")
        return

    maj = fermees = perimees = inchangees = erreurs = 0
    for i, f in enumerate(fiches, 1):
        pid = f["google_place_id"]
        try:
            place = await maps_tools.get_place(pid)
        except maps_tools.PlaceIntrouvable:
            perimees += 1
            await sb.update(
                "companies",
                {
                    "status": "disqualified",
                    "disqualified_reason": f"place_id_perime (rehydratation {datetime.now(timezone.utc):%Y-%m-%d})",
                },
                filters={"id": f"eq.{f['id']}"},
            )
            print(f"  [{i}/{len(fiches)}] PÉRIMÉ   {f['name'][:44]}")
            continue
        except Exception as e:  # noqa: BLE001
            erreurs += 1
            print(f"  [{i}/{len(fiches)}] ERREUR   {f['name'][:36]} — {e!r}"[:110])
            await asyncio.sleep(DELAI_ENTRE_APPELS_S)
            continue

        if place.business_status == "CLOSED_PERMANENTLY":
            fermees += 1
            await sb.update(
                "companies",
                {
                    "status": "disqualified",
                    "disqualified_reason": "ferme_definitivement (Google CLOSED_PERMANENTLY)",
                },
                filters={"id": f"eq.{f['id']}"},
            )
            print(f"  [{i}/{len(fiches)}] FERMÉE   {f['name'][:44]}")
            await asyncio.sleep(DELAI_ENTRE_APPELS_S)
            continue

        patch = {
            "name": place.name or f["name"],
            "address": place.formatted_address,
            "city": place.city,
            "postal_code": place.postal_code,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "website": place.website,
            "domain": place.domain,
            "google_types": place.google_types,
            "google_rating": place.google_rating,
            "google_reviews_count": place.google_reviews_count,
            "raw_payload": place.raw_payload,
        }
        assert set(patch) <= set(COLONNES_SOURCING), "colonne hors périmètre"
        await sb.update("companies", patch, filters={"id": f"eq.{f['id']}"})
        if place.domain != f.get("domain") or place.name != f["name"]:
            maj += 1
            print(f"  [{i}/{len(fiches)}] CHANGÉ   {f['name'][:36]} → {place.name[:36]}")
        else:
            inchangees += 1
        await asyncio.sleep(DELAI_ENTRE_APPELS_S)

    print(
        f"\n{maj} modifiée(s) · {inchangees} inchangée(s) · {fermees} fermée(s) · "
        f"{perimees} périmée(s) · {erreurs} erreur(s)"
    )
    print(f"Coût approximatif : ~{(len(fiches) - erreurs) * 0.02:.2f} $")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rafraîchit les données Google des fiches")
    ap.add_argument("--track", default="agence-ia")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(rehydrater(a.limit, a.track, a.dry_run))


if __name__ == "__main__":
    main()
