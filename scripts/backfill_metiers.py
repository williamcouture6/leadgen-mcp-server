"""Backfill des QUATRE colonnes de métiers de `companies`, et rien d'autre.

Recalcule, sans appel LLM ni re-scrape, `metiers`, `fenetre_mois`,
`metier_source` et `metiers_calcules_le` depuis `companies.research_json`,
par `db.extract_metiers_patch` + un `UPDATE` direct.

🔴 POURQUOI IL N'APPELLE PAS `db.update_company_research`.
C'est la seule différence qui compte avec son voisin
`scripts/backfill_research_columns.py`, lequel est bâti entièrement dessus.
`update_company_research` repose `status` ET **`last_enriched_at`** — et
`last_enriched_at` porte la ré-éligibilité à 90 jours du backlog de recherche.
Mesuré au 2026-09-11 : **41 fiches** sortiraient du backlog pour trois mois, en
silence, si ce backfill passait par elle. Un backfill de colonnes dérivées doit
être un NO-OP sur tout le reste de la fiche : d'où l'`sb.update` direct, avec un
patch qui ne contient QUE les quatre colonnes.

🔴 IL NE SE LANCE QUE SUR DÉCISION EXPLICITE DE WILLIAM (2026-09-13).
« Pour le rattrapage je le ferai plus tard, quand je déciderai que c'est le bon
moment. » Une session qui lit le paragraphe suivant va vouloir le rejouer
d'elle-même : NE LE FAIS PAS. Signale que le rejeu est dû, explique ce qu'il
changerait, et attends. Deux raisons de fond, pas une politesse :
  · il ÉCRASE les colonnes des fiches déjà écrites par l'écrivain, donc il
    détruit toute mesure en cours sur `update_company_research` — il repeint
    par-dessus la panne et la rend invisible ;
  · il déplace `metiers_calcules_le` PARTOUT, ce qui change qui est joignable
    aujourd'hui. Ce n'est pas un no-op, même quand le dictionnaire n'a pas bougé.

🔴 IL DOIT ÊTRE REJOUÉ APRÈS TOUTE MODIFICATION DU DICTIONNAIRE.
`RACINES`, `EXIGE`, `EXCLUSIONS`, `ECRASE` dans `src/lib/metiers.py` : ces
colonnes sont un **cache que RIEN n'invalide**. La scène du courriel se
recalcule à chaque brouillon, la colonne non. Corriger un trou de dictionnaire
sans rejouer ce script ne reclasse RIEN — la fiche corrigée reste invisible à la
vue de sélection, et personne ne le voit passer.

C'est aussi pourquoi le script recalcule TOUTES les fiches researchées, et ne se
limite pas au critère de rattrapage : après une édition du dictionnaire,
`metiers_calcules_le` est postérieur à `last_enriched_at` PARTOUT, donc un script
filtré sur le critère ne trouverait plus rien à faire. Le critère sert à
VÉRIFIER la complétude, pas à choisir les lignes.

📏 LE CRITÈRE DE COMPLÉTUDE (commentaire de la colonne, migration 0059) :

    metiers_calcules_le is null or metiers_calcules_le < last_enriched_at

et **jamais** le seul `IS NULL`, qui n'est vrai qu'à la première passe. Le second
`UPDATE` de `update_company_research` ne lève jamais (il ne doit pas faire perdre
un `research_json` payé) : s'il échoue sur une fiche déjà calculée — une
re-recherche à 90 jours — l'horodatage garde son ancienne valeur NON NULLE
pendant que `research_json` a été remplacé. Colonnes périmées **et** invisibles.
En recalculant tout, ce script rattrape les deux cas.

🔴 LES STATUTS TERMINAUX SONT ÉCARTÉS, ICI **ET** DANS LE CRITÈRE. Une fiche
`disqualified`/`suppressed` ne reçoit pas de colonnes de métiers, et n'est pas
non plus comptée comme « à rattraper » — les deux vont ensemble, sinon le critère
resterait non nul pour toujours. Le second UPDATE de la production porte la même
garde, pour la même raison.

Ce n'est pas qu'une question de cohérence : `suppressed` est un RETRAIT DE
CONSENTEMENT, et `fenetre_mois` veut littéralement dire « les mois où on peut la
démarcher ». L'écrire sur une fiche retirée est un mauvais signal en soi, même si
personne ne le lit. Mesuré le 2026-09-12 : 0 fiche terminale porte un
`research_json` sur cette piste — la garde est prospective.

L'anti-clobber `metiers_verifies_a_la_main` est respecté lui aussi : c'est le seul
verrou qui protège une valeur posée à la main.

Usage :
    python scripts/backfill_metiers.py --track agence-ia --dry-run
    python scripts/backfill_metiers.py --track agence-ia
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
from src import supabase_client as sb  # noqa: E402

# Les QUATRE colonnes, et l'assurance qu'il n'en partira pas une cinquième.
COLONNES = ("metiers", "fenetre_mois", "metier_source", "metiers_calcules_le")

# 200, pas 2000. ⚠️ PostgREST plafonne TOUTE réponse à 1000 lignes sur ce projet
# et ne signale RIEN : un `limit=2000` sans pagination rend 1000 lignes et ment.
TAILLE_PAGE = 200


async def run(track: str, dry_run: bool) -> None:
    companies = await sb.select_all(
        "companies",
        order="id.asc",
        page_size=TAILLE_PAGE,
        params={
            "select": "id,name,research_json,metiers_verifies_a_la_main",
            "track": f"eq.{track}",
            "research_json": "not.is.null",
            # Voir le docstring : écarté ici ET dans le critère de complétude.
            "status": "not.in.(disqualified,suppressed)",
        },
    )
    print(f"[{track}] {len(companies)} fiches researchées lues (pages de {TAILLE_PAGE}).")

    n_lues = len(companies)
    n_ecrites = 0
    n_sautees = 0

    for co in companies:
        if co.get("metiers_verifies_a_la_main"):
            # Anti-clobber : valeur posée à la main, on n'y touche jamais.
            n_sautees += 1
            print(f"  SAUTÉE (vérifiée à la main) {co['id'][:8]} {co.get('name')}")
            continue

        patch = dbt.extract_metiers_patch(co.get("research_json"))
        # Ceinture : `extract_metiers_patch` est la seule source du patch, mais
        # une clé de plus ici serait un effet de bord silencieux sur la prod.
        inattendues = set(patch) - set(COLONNES)
        if inattendues:
            raise SystemExit(f"ABANDON : le patch porte des colonnes hors périmètre {inattendues}")

        if dry_run:
            print(
                f"  {co['id'][:8]} {co.get('name')} -> "
                f"metiers={patch['metiers']} fenetre_mois={patch['fenetre_mois']} "
                f"source={patch['metier_source']}"
            )
            continue

        await sb.update(
            "companies",
            patch,
            filters={
                "id": f"eq.{co['id']}",
                "metiers_verifies_a_la_main": "eq.false",
            },
        )
        n_ecrites += 1

    print("─" * 60)
    print(f"lues={n_lues}  écrites={0 if dry_run else n_ecrites}  sautées={n_sautees}")
    if dry_run:
        print("DRY-RUN : rien écrit. Relance sans --dry-run pour appliquer.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill des colonnes de métiers (4 colonnes, zéro effet de bord).")
    # ⚠️ 'REACTI' n'existe plus depuis le 2026-06-07 (migration 0020) : la
    # valeur live est 'agence-ia', 'OPT' est legacy inerte.
    ap.add_argument("--track", default="agence-ia", choices=["OPT", "agence-ia"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.track, args.dry_run))


if __name__ == "__main__":
    main()
