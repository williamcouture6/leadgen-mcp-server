"""Le BALAYEUR — énumère un secteur dans une région, et remplit l'inventaire.

Usage :
    python scripts/balayage.py --region Montréal --secteur "entrepreneur en déneigement"
    python scripts/balayage.py --secteur "entrepreneur en déneigement"   # les 10 régions
    python scripts/balayage.py --region Lévis                            # tous les secteurs
    python scripts/balayage.py --dry-run                                 # n'écrit rien

🔴 POURQUOI UN SCRIPT ET PAS UN CRON. Un balayage complet prend des heures et ne
se refait que deux fois par an. Le mettre derrière un endpoint Railway
l'exposerait au timeout de 300 s — celui qui a déjà forcé WF-4 à se couper en
deux lots de 10. Et n8n RELANCE ce qu'il croit mort, pendant que la coroutine
d'origine continue de tourner : deux balayages concurrents sur la même région.
On le lance à la main, on le regarde, et le journal dit ce qui s'est passé.

LE PRINCIPE, mesuré le 2026-09-15 sur l'île de Montréal :

  « entrepreneur en déneigement in Montréal, Québec, Canada »  ->  28
  la MÊME zone passée en rectangle, sans le nom de ville      ->  60
  la même question sur 256 tuiles de 2,2 km                   -> 308  (186 neuves)

Google ne tronque pas à 60 sur nos secteurs (aucun de nos 43 runs n'y touche,
max 55) : il s'arrête AVANT, sur un seuil de PERTINENCE. Un entrepreneur classé
180ᵉ sur « Montréal » est 3ᵉ sur une tuile de 2 km. Rétrécir la zone ne change
pas l'index de Google — ça change contre QUI l'entreprise est comparée.

🔴 LE MASQUE NE DOIT JAMAIS S'ÉLARGIR. `places.id,nextPageToken` correspond au
SKU « Text Search Essentials (IDs Only) », listé « Unlimited » à 0 $. Ajouter
`places.displayName` « pour filtrer le junk plus tôt » bascule TOUTE la passe en
Pro (32 $/1000) ou Enterprise (35 $/1000) : ~25 000 appels pour un balayage
complet, donc ~870 $ au lieu de rien. Le filtre anti-junk tourne APRÈS
l'hydratation, et c'est le prix assumé.
⚠️ Le « 0 $ » n'est pas confirmé par une facture : la page d'offres de Google
annonce par ailleurs 10 000 appels gratuits/mois pour le palier Essentials. Une
alerte de budget à 5 $ a été posée le 2026-09-15 ; c'est elle la vraie garde.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import supabase_client as sb  # noqa: E402
from src.config import settings  # noqa: E402
from src.tools import db as db_tools  # noqa: E402

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# 🔴 NE RIEN AJOUTER ICI. Voir l'avertissement du docstring.
MASQUE_IDS_SEULS = "places.id,nextPageToken"

# En deçà, une tuile n'a rien à cacher : la découper ne rapporterait que des
# appels. Mesuré le 2026-09-15 — les tuiles à moins de ~8 résultats ne rendaient
# plus rien de neuf une fois découpées.
SEUIL_SUBDIVISION = 8

# Un niveau est « faible » quand il rapporte moins que ça, en proportion du cumul.
# 🔴 LA RÈGLE EST « ON DÉCOUPE TANT QUE DÉCOUPER RAPPORTE », JAMAIS UNE
# PROFONDEUR FIXE. Mesuré : la tonte sature au niveau 4 (+7 entreprises) alors
# que le déneigement montait encore (+41). Un niveau posé en dur servirait trop
# l'un et affamerait l'autre.
GAIN_MIN_RELATIF = 0.03

# 🔴 COMBIEN DE PALIERS FAIBLES D'AFFILÉE AVANT DE CONCLURE. **DEUX, PAS UN.**
#
# Le gain par niveau N'EST PAS MONOTONE, et c'est mesuré : sur Montréal, le
# 2026-09-15, les niveaux ont rendu +147, puis **+17**, puis **+43**. Un seul
# palier faible ne prouve donc rien — il peut précéder le meilleur niveau de la
# passe.
#
# Ce que la version à un seul palier a coûté, mesuré le 2026-09-16 sur le
# premier balayage réel : Sherbrooke, Saguenay et Trois-Rivières se sont
# arrêtées au NIVEAU 1, après 5 tuiles et 7 appels — un balayage de surface sur
# des villes de 140 000 à 170 000 habitants.
#
# ⚠️ Ne pas « optimiser » en revenant à un seul palier parce que deux coûtent
# des appels : ces appels sont à 0 $ au masque identifiants-seuls, et une
# entreprise jamais trouvée l'est pour toujours.
PALIERS_FAIBLES_POUR_CONCLURE = 2

# Plancher de taille de tuile. En deçà, on interroge des pâtés de maisons.
TUILE_MIN_DEGRES = 0.008  # ~0,9 km en latitude

# Garde-fou de volume. Un balayage qui part en vrille doit s'arrêter tout seul
# plutôt que de consommer un quota dont on ne connaît pas encore la vraie borne.
PLAFOND_APPELS_PAR_PASSE = 4000

DELAI_ENTRE_APPELS_S = 0.15
DELAI_PAGINATION_S = 1.2


class Compteur:
    """Les appels d'UNE passe, plus le plafond qui l'arrête."""

    def __init__(self) -> None:
        self.appels = 0
        self.erreurs: list[str] = []

    def plafond_atteint(self) -> bool:
        return self.appels >= PLAFOND_APPELS_PAR_PASSE


async def interroger(
    client: httpx.AsyncClient, secteur: str, rect: tuple[float, float, float, float],
    cpt: Compteur,
) -> set[str]:
    """Une question sur un rectangle, jusqu'à 3 pages. Rend des `place_id`.

    🔴 UNE TUILE QUI TOMBE N'EMPORTE PAS LA PASSE. `_run_wf1` porte aujourd'hui
    le défaut inverse — un `try/except` unique autour de toute la boucle — et
    avec une grille ça ferait annuler le travail de 255 tuiles pour une seule
    erreur. Ici on attrape par tuile et on consigne.

    ⚠️ ON NE REJOUE PAS LES 4xx. `maps.search_places` porte un
    `@retry(stop_after_attempt(3))` SANS `retry_if_exception_type` : il rejoue
    n'importe quelle exception, y compris un 400. Un rectangle malformé y
    coûterait trois tentatives et jusqu'à 9 s de backoff, multipliées par le
    nombre de tuiles. Ici, un 4xx est une erreur de notre côté : on la note et
    on passe.
    """
    bas_lat, bas_lng, haut_lat, haut_lng = rect
    trouves: set[str] = set()
    jeton: str | None = None
    for _page in range(3):
        if cpt.plafond_atteint():
            cpt.erreurs.append(f"plafond de {PLAFOND_APPELS_PAR_PASSE} appels atteint")
            break
        corps: dict[str, object] = {
            # ⚠️ PAS de nom de ville dans la requête. Avec un rectangle, le token
            # géographique ramène le classement vers le centre-ville et annule
            # une partie du bénéfice : mesuré, 28 au lieu de 60 sur la MÊME zone.
            "textQuery": secteur,
            "regionCode": "CA",
            "languageCode": "fr-CA",
            "pageSize": 20,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": bas_lat, "longitude": bas_lng},
                    "high": {"latitude": haut_lat, "longitude": haut_lng},
                }
            },
        }
        if jeton:
            corps["pageToken"] = jeton
            await asyncio.sleep(DELAI_PAGINATION_S)
        try:
            r = await client.post(
                PLACES_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": settings().google_places_api_key,
                    "X-Goog-FieldMask": MASQUE_IDS_SEULS,
                },
                json=corps,
                timeout=30.0,
            )
            cpt.appels += 1
            if r.status_code != 200:
                cpt.erreurs.append(f"HTTP {r.status_code} sur {rect}: {r.text[:160]}")
                break
            data = r.json()
        except Exception as e:  # noqa: BLE001
            cpt.appels += 1
            cpt.erreurs.append(f"{type(e).__name__} sur {rect}: {e}")
            break
        for p in data.get("places", []):
            if p.get("id"):
                trouves.add(p["id"])
        jeton = data.get("nextPageToken")
        if not jeton:
            break
        await asyncio.sleep(DELAI_ENTRE_APPELS_S)
    return trouves


def decouper(rect: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    bas_lat, bas_lng, haut_lat, haut_lng = rect
    mlat = (bas_lat + haut_lat) / 2
    mlng = (bas_lng + haut_lng) / 2
    return [
        (bas_lat, bas_lng, mlat, mlng),
        (bas_lat, mlng, mlat, haut_lng),
        (mlat, bas_lng, haut_lat, mlng),
        (mlat, mlng, haut_lat, haut_lng),
    ]


def trop_petite(rect: tuple[float, float, float, float]) -> bool:
    bas_lat, bas_lng, haut_lat, haut_lng = rect
    return (haut_lat - bas_lat) < TUILE_MIN_DEGRES or (haut_lng - bas_lng) < TUILE_MIN_DEGRES


async def balayer(
    client: httpx.AsyncClient, secteur: str, rect: tuple[float, float, float, float],
    niveau_max: int, cpt: Compteur, bavard: bool = True,
) -> tuple[set[str], int, int]:
    """Balaie un rectangle par niveaux. Rend (place_ids, niveau atteint, tuiles).

    On descend niveau par niveau, et on ne redécoupe QUE les tuiles qui ont
    rendu beaucoup : une tuile à 2 résultats ne cache pas 50 entreprises. C'est
    ce qui évite de payer 4^n appels pour un territoire à moitié vide.
    """
    cumul: set[str] = set()
    actifs = [rect]
    niveau = 0
    tuiles = 0
    faibles = 0   # paliers faibles consécutifs
    while actifs and niveau <= niveau_max:
        avant = len(cumul)
        prochains: list[tuple[float, float, float, float]] = []
        for tuile in actifs:
            if cpt.plafond_atteint():
                break
            ids = await interroger(client, secteur, tuile, cpt)
            tuiles += 1
            cumul |= ids
            if len(ids) >= SEUIL_SUBDIVISION and not trop_petite(tuile):
                prochains += decouper(tuile)
            await asyncio.sleep(DELAI_ENTRE_APPELS_S)
        gain = len(cumul) - avant
        if bavard:
            h = (actifs[0][2] - actifs[0][0]) * 111.0
            l = (actifs[0][3] - actifs[0][1]) * 78.0
            print(
                f"    niveau {niveau}: {len(actifs):4d} tuiles de {h:5.1f}x{l:4.1f} km"
                f"  -> cumul {len(cumul):5d}  (+{gain})"
            )
        # On s'arrête quand découper cesse de rapporter — pas à une profondeur
        # fixe, et pas au PREMIER palier faible : voir
        # PALIERS_FAIBLES_POUR_CONCLURE. Le niveau 0 est exempté, il n'a rien à
        # quoi se comparer.
        if niveau > 0 and gain < max(1, int(GAIN_MIN_RELATIF * len(cumul))):
            faibles += 1
            if faibles >= PALIERS_FAIBLES_POUR_CONCLURE:
                break
        else:
            faibles = 0
        if cpt.plafond_atteint():
            break
        actifs = prochains
        niveau += 1
    return cumul, niveau, tuiles


async def enregistrer(
    ids: set[str], secteur: str, region: str, track: str
) -> int:
    """Upsert dans l'inventaire. Rend combien étaient NOUVEAUX.

    ⚠️ On envoie `trouve_par` et `regions` en sachant que PostgREST fait
    `do update set <colonne> = excluded.<colonne>` : c'est le trigger
    `sourcing_inventaire_fusion_trg` (migration 0070) qui transforme cet
    écrasement en fusion. Sans lui, une entreprise à cheval sur deux tuiles
    perdrait la moitié de ses étiquettes DANS LA MÊME PASSE.
    ⚠️ On n'envoie SURTOUT PAS `etat` : le trigger le protège, mais l'envoyer
    reviendrait à s'en remettre à lui pour une faute qu'on peut ne pas commettre.
    """
    if not ids:
        return 0
    connus = {
        r["google_place_id"]
        for r in await sb.select_all(
            "sourcing_inventaire",
            order="google_place_id",
            params={"select": "google_place_id"},
        )
    }
    nouveaux = len(ids - connus)
    maintenant = datetime.now(timezone.utc).isoformat()
    lignes = [
        {
            "google_place_id": pid,
            "track": track,
            "trouve_par": [secteur],
            "regions": [region],
            "derniere_vue": maintenant,
        }
        for pid in sorted(ids)
    ]
    # Par paquets : un POST de 3 000 lignes dépasse les limites raisonnables.
    for i in range(0, len(lignes), 500):
        await sb.insert(
            "sourcing_inventaire", lignes[i : i + 500],
            on_conflict="google_place_id",
        )
    return nouveaux


async def passe(
    client: httpx.AsyncClient, region: str, secteur: str, niveau_max: int,
    track: str, dry_run: bool,
) -> None:
    rect = db_tools.REGIONS_BALAYAGE[region]
    print(f"\n── {region} · {secteur}")
    cpt = Compteur()
    run_id: str | None = None

    if not dry_run:
        lignes = await sb.insert(
            "sourcing_balayages",
            {
                "track": track, "region": region, "secteur": secteur,
                "bas_lat": rect[0], "bas_lng": rect[1],
                "haut_lat": rect[2], "haut_lng": rect[3],
                "statut": "running",
            },
        )
        run_id = lignes[0]["id"]

    debut = time.monotonic()
    try:
        ids, niveau, tuiles = await balayer(client, secteur, rect, niveau_max, cpt)
        nouveaux = 0 if dry_run else await enregistrer(ids, secteur, region, track)
        statut = "failed" if (cpt.erreurs and not ids) else "completed"
        print(
            f"    => {len(ids)} distinctes, {nouveaux} NEUVES, "
            f"{cpt.appels} appels, {time.monotonic() - debut:.0f}s"
        )
        if cpt.erreurs:
            print(f"    !! {len(cpt.erreurs)} erreur(s) de tuile, 1re : {cpt.erreurs[0]}")
    except Exception as e:  # noqa: BLE001
        ids, niveau, tuiles, nouveaux, statut = set(), 0, 0, 0, "failed"
        cpt.erreurs.append(repr(e))
        print(f"    !! PASSE EN ÉCHEC : {e!r}")

    if run_id:
        # La ligne de fermeture est ce qui distingue une passe finie d'une passe
        # MORTE EN VOL. Son absence est le seul signal, et l'alerte de famine la
        # surveille (`statut='running'` depuis plus de 2 h).
        await sb.update(
            "sourcing_balayages",
            {
                "statut": statut,
                "niveau_atteint": niveau,
                "tuiles_interrogees": tuiles,
                "appels_google": cpt.appels,
                "trouves": len(ids),
                "nouveaux": nouveaux,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error_text": (" | ".join(cpt.erreurs)[:2000] or None),
            },
            filters={"id": f"eq.{run_id}"},
        )


async def main() -> None:
    ap = argparse.ArgumentParser(description="Balayage géographique de l'inventaire")
    ap.add_argument("--region", action="append", help="par défaut : toutes")
    ap.add_argument("--secteur", action="append", help="par défaut : tout le catalogue")
    ap.add_argument(
        "--niveau-max", type=int, default=7,
        help="plafond de profondeur. 5 avait COUPÉ Montréal le 2026-09-16 "
             "alors qu'elle montait encore ; c'est la règle des deux "
             "paliers faibles qui doit arrêter la passe, pas ce plafond.",
    )
    ap.add_argument("--track", default="agence-ia")
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien en base")
    args = ap.parse_args()

    regions = args.region or list(db_tools.REGIONS_BALAYAGE)
    inconnues = [r for r in regions if r not in db_tools.REGIONS_BALAYAGE]
    if inconnues:
        sys.exit(
            f"région(s) inconnue(s) : {inconnues}\n"
            f"connues : {list(db_tools.REGIONS_BALAYAGE)}"
        )
    catalogue = db_tools._CATALOGS.get(args.track, {})
    tous = [s for secteurs in catalogue.values() for s in secteurs]
    secteurs = args.secteur or tous
    inconnus = [s for s in secteurs if s not in tous]
    if inconnus:
        sys.exit(f"secteur(s) hors catalogue {args.track} : {inconnus}\nconnus : {tous}")

    print(f"Balayage — {len(regions)} région(s) x {len(secteurs)} secteur(s)"
          f"{'  [DRY-RUN]' if args.dry_run else ''}")
    async with httpx.AsyncClient() as client:
        for secteur in secteurs:
            for region in regions:
                await passe(client, region, secteur, args.niveau_max,
                            args.track, args.dry_run)
    print("\nTerminé.")


if __name__ == "__main__":
    asyncio.run(main())
