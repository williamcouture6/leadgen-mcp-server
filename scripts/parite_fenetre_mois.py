"""La preuve que `companies.fenetre_mois` dit la MÊME CHOSE que le calcul.

Compare, fiche par fiche et **mois par mois**, la colonne matérialisée
`companies.fenetre_mois` (lue en base) au calcul historique
`db.fenetre_saisonniere_ouverte(...)`. C'est le seul contrôle qui autorise, plus
tard, à remplacer l'un par l'autre dans la sélection des envois.

⚠️ La colonne est **LUE EN BASE**, jamais rejouée en mémoire. Rejouer
`extract_metiers_patch` des deux côtés ne prouverait que le déterminisme du
calcul ; ici on veut prouver que le backfill a **écrit** juste.

🔴 POURQUOI LES DOUZE MOIS, ET PAS LE MOIS COURANT.
La règle `EXIGE` de `lib/metiers.py` — `piscine` n'ouvre une fenêtre que si un
libellé porte un verbe d'entretien (`entretien`, `nettoyage`, `ouverture`,
`fermeture`…) — est **invisible en septembre** : la fenêtre piscine
(janvier→juillet) est fermée de toute façon, les deux côtés répondent « non »
pour de mauvaises raisons opposées et le contrôle passe au vert **avec le défaut
dedans**. Il ne se verrait qu'en janvier, en production, sur un envoi réel.
Trois relectures successives ont mis ce défaut à jour. D'où le balayage
1..12 (jour 15 de chaque mois).

🔴 POURQUOI UN SCRIPT, ET PAS UN TEST PYTEST.
Le dépôt n'a aucun `conftest.py` et aucun test ne parle à la base : 64 fichiers
de test sur 91 remplacent le client Supabase par un faux. Un balayage de ~441
fiches × 12 mois est un aller-retour réseau, pas un test unitaire — il n'a pas
sa place dans une suite qui doit tourner hors-ligne et en quelques secondes.
C'est un script commité, lancé à la main, dont le code de sortie fait foi.

🔴 CE QUI FAIT SORTIR EN ERREUR (code 1) — DEUX COMPTES, PAS UN :
  1. les **écarts** (colonne ≠ calcul) ;
  2. les fiches **non calculées ou périmées**
     (`metiers_calcules_le is null or metiers_calcules_le < last_enriched_at`).
Le second compte n'est pas décoratif : c'est le mode de panne de l'écrivain (le
second UPDATE de `update_company_research` ne lève jamais), et une colonne
absente est écartée **en silence** par le prédicat SQL de la sélection
(`fenetre_mois @> array[9]` rend NULL sur une colonne NULL, donc filtre la
ligne). Une fiche sans colonne ne produit aucun écart — elle disparaît. Compter
sans sortir en erreur reviendrait à ne rien contrôler.

🔴 LES FICHES `metiers_verifies_a_la_main = true` SONT EXCLUES du calcul
d'écart, et comptées à part. Une valeur posée à la main **diverge du calcul par
construction** — c'est la définition même de la correction humaine. Les compter
comme écarts ferait échouer la preuve à la première correction, pour une bonne
raison. (0 fiche dans ce cas au 2026-09-12 : la garde est prospective.)

La population lue est **exactement celle de `scripts/backfill_metiers.py`**
(`track`, `research_json not null`, statuts terminaux écartés) : sinon les deux
ne se parlent pas et la preuve porterait sur un autre ensemble que l'écrivain.

LECTURE SEULE : aucun `update`, `insert` ni `delete`.

Usage :
    python scripts/parite_fenetre_mois.py
    python scripts/parite_fenetre_mois.py --track agence-ia --annee 2026
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from datetime import date, datetime
from pathlib import Path

# UTF-8 stdout pour PowerShell
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import db as dbt  # noqa: E402
from src import supabase_client as sb  # noqa: E402

# 200, pas 2000. ⚠️ PostgREST plafonne TOUTE réponse à 1000 lignes sur ce projet
# et ne signale RIEN : un `limit=2000` sans pagination rend 1000 lignes et ment.
TAILLE_PAGE = 200

MOIS = tuple(range(1, 13))

MAX_ECARTS_AFFICHES = 40
MAX_NON_CALCULEES_AFFICHEES = 10


def _horodatage(valeur: object) -> datetime | None:
    """Un timestamptz PostgREST -> datetime, ou None si absent/illisible."""
    if not valeur:
        return None
    texte = str(valeur).strip()
    # PostgREST rend « ...+00:00 » ou « ...Z » selon les colonnes.
    if texte.endswith("Z"):
        texte = texte[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(texte)
    except ValueError:
        return None


def _perimee(co: dict) -> bool:
    """Le critère de complétude du commentaire de colonne (migration 0059).

    `metiers_calcules_le is null or metiers_calcules_le < last_enriched_at`.
    Et **jamais** le seul `IS NULL`, qui n'est vrai qu'à la première passe : une
    re-recherche à 90 jours remplace `research_json` en laissant l'horodatage à
    son ancienne valeur NON NULLE si le second UPDATE a échoué.
    """
    calcule = _horodatage(co.get("metiers_calcules_le"))
    if calcule is None:
        return True
    enrichi = _horodatage(co.get("last_enriched_at"))
    if enrichi is None:
        return False
    return calcule < enrichi


async def run(track: str, annee: int) -> int:
    companies = await sb.select_all(
        "companies",
        order="id.asc",
        page_size=TAILLE_PAGE,
        params={
            "select": (
                "id,name,research_json,fenetre_mois,metiers,"
                "metiers_verifies_a_la_main,metiers_calcules_le,last_enriched_at"
            ),
            "track": f"eq.{track}",
            "research_json": "not.is.null",
            # Exactement la population du backfill : sans ça, les deux ne se
            # parlent pas.
            "status": "not.in.(disqualified,suppressed)",
        },
    )
    print(f"[{track}] {len(companies)} fiches researchées lues (pages de {TAILLE_PAGE}).")

    n_lues = len(companies)
    n_comparees = 0
    n_verrouillees = 0
    n_non_calculees = 0
    n_verifications = 0
    ecarts: list[str] = []
    non_calculees: list[str] = []

    for co in companies:
        if _perimee(co):
            n_non_calculees += 1
            non_calculees.append(
                f"  NON CALCULÉE/PÉRIMÉE {co['id'][:8]} {co.get('name')} "
                f"calculee_le={co.get('metiers_calcules_le')} "
                f"enrichie_le={co.get('last_enriched_at')} "
                f"fenetre_mois={co.get('fenetre_mois')}"
            )

        if co.get("metiers_verifies_a_la_main"):
            # Diverge du calcul PAR CONSTRUCTION : c'est ce qu'est une
            # correction humaine. Comptée, jamais comparée.
            n_verrouillees += 1
            continue

        n_comparees += 1
        colonne = set(co.get("fenetre_mois") or [])

        for mois in MOIS:
            n_verifications += 1
            dit_colonne = mois in colonne
            dit_calcul = dbt.fenetre_saisonniere_ouverte(
                co, track=track, aujourdhui=date(annee, mois, 15)
            )
            if dit_colonne != dit_calcul:
                ecarts.append(
                    f"  ÉCART {co['id'][:8]} {co.get('name')} mois={mois:02d} "
                    f"colonne={dit_colonne} calcul={dit_calcul} "
                    f"fenetre_mois={co.get('fenetre_mois')} metiers={co.get('metiers')} "
                    f"services={((co.get('research_json') or {}).get('services_offered')) or []}"
                )

    print("─" * 70)
    print(
        f"fiches={n_lues}  comparees={n_comparees}  verrouillees={n_verrouillees}  "
        f"non_calculees={n_non_calculees}  verifications={n_verifications}  "
        f"ecarts={len(ecarts)}"
    )

    if ecarts:
        print(f"\nDétail des écarts ({len(ecarts)} au total, {MAX_ECARTS_AFFICHES} premiers) :")
        for ligne in ecarts[:MAX_ECARTS_AFFICHES]:
            print(ligne)

    if non_calculees:
        print(
            f"\nFiches non calculées ou périmées ({n_non_calculees} au total, "
            f"{MAX_NON_CALCULEES_AFFICHEES} premières) :"
        )
        for ligne in non_calculees[:MAX_NON_CALCULEES_AFFICHEES]:
            print(ligne)

    if ecarts or n_non_calculees:
        print(
            "\nÉCHEC : la colonne et le calcul ne disent pas la même chose, "
            "ou une fiche n'est pas calculée. Ne pas ajuster ce script — "
            "corriger l'écrivain ou le dictionnaire, puis rejouer "
            "scripts/backfill_metiers.py."
        )
        return 1

    print("\nOK : colonne et calcul concordent sur les douze mois.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parité colonne fenetre_mois / calcul fenetre_saisonniere_ouverte, sur douze mois (lecture seule)."
    )
    # ⚠️ 'REACTI' n'existe plus depuis le 2026-06-07 (migration 0020) : la
    # valeur live est 'agence-ia', 'OPT' est legacy inerte.
    ap.add_argument("--track", default="agence-ia", choices=["OPT", "agence-ia"])
    # L'année ne change rien à la règle (les fenêtres sont des mois, pas des
    # dates) ; le drapeau existe pour rejouer un cas précis si besoin.
    ap.add_argument("--annee", type=int, default=2026)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.track, args.annee)))


if __name__ == "__main__":
    main()
