"""La sélection rend-elle exactement la même chose qu'avant AC1c·A ?

🔴 CONTRE UN INSTANTANÉ FIGÉ, PAS CONTRE LA BASE. La file bouge toute seule :
WF-3 ajoute des fiches, WF-4 écrit des brouillons qui retirent autant de
contacts. Comparer deux exécutions en production rendrait un écart garanti, sans
rapport avec le code.

⚠️ CE QUE LA FAUSSE BASE HONORE, ET CE QU'ELLE IGNORE — a lire avant de
conclure quoi que ce soit d'un « IDENTIQUE ».

Elle honore : `direction` et `status` sur les messages, `offset`/`limit` sur les
contacts, `id=in.(...)` sur les entreprises.

🔴 Elle IGNORE : le `select` (elle rend la ligne entiere quelle que soit la
projection demandee), `status`/`track`/`email` sur les contacts, l'`order`, et le
`contact_id=in.(...)` des messages.

Consequences, mesurees par un conseil de relecture le 2026-09-12 :
  · ce rejeu est AVEUGLE a tout changement de projection — il dirait
    « IDENTIQUE » meme si on retirait `research_json` de la chaine `select` ;
  · il n'exerce JAMAIS le cas qui a motive la garde `entreprises_engagees` (un
    frere passe en `status='contacted'`), puisque la capture filtre
    `status in (new, ready)` a la source ;
  · il tourne a `limit=1000` alors que la production tourne a 10-20 : la
    pagination et la sortie anticipee ne sont jamais exercees.

Ce qu'il prouve reellement : que la liste rendue par la selection, sur CET
instantane, a CETTE date, est la meme avant et apres. C'est utile, et c'est
moins que ce que son nom suggere.

Usage :
    python scripts/preuve_selection_inchangee.py --capturer   # AVANT toute modif
    python scripts/preuve_selection_inchangee.py --rejouer    # après
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import db as dbt  # noqa: E402
from src import supabase_client as sb  # noqa: E402

CAPTURE = ROOT / "scripts" / "_capture_selection.json"
PAGE = 200


def _base_figee(data: dict):
    """La capture, servie AVEC les filtres de la vraie sélection."""

    class _BaseFigee:
        async def select(self, table, params=None, **_):
            p = params or {}
            if table == "contacts":
                deb = int(p.get("offset", 0))
                fin = deb + int(p.get("limit", 10**9))
                return data["contacts"][deb:fin]
            if table == "companies":
                brut = (p.get("id", "") or "").removeprefix("in.(").removesuffix(")")
                voulus = set(filter(None, brut.split(",")))
                return [c for c in data["companies"]
                        if not voulus or c["id"] in voulus]
            if table == "messages":
                # 🔴 Le vrai select (db.py, dans `_retenir`) porte
                # status=not.in.(failed) ET direction=eq.outbound.
                return [m for m in data["messages"]
                        if m.get("direction") == "outbound"
                        and m.get("status") != "failed"]
            return []

    return _BaseFigee()


async def _rejouer_sur(data: dict) -> list[str]:
    original = dbt.db
    dbt.db = _base_figee(data)
    try:
        rendus = await dbt.list_contacts_to_personalize(limit=1000, track="agence-ia")
        return [r["contact"]["id"] for r in rendus]
    finally:
        dbt.db = original


async def _lire_tout(table: str, params: dict) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        lot = await sb.select(table, params={
            **params, "limit": str(PAGE), "offset": str(offset)})
        if not lot:
            break
        out.extend(lot)
        if len(lot) < PAGE:
            break
        offset += PAGE
    return out


async def capturer() -> None:
    contacts = await _lire_tout("contacts", {
        "select": "*", "track": "eq.agence-ia", "email": "not.is.null",
        "status": "in.(new,ready)", "order": "created_at.asc"})
    ids = sorted({c["company_id"] for c in contacts if c.get("company_id")})
    companies: list[dict] = []
    for i in range(0, len(ids), PAGE):
        companies += await sb.select("companies", params={
            "select": "*", "id": f"in.({','.join(ids[i:i + PAGE])})",
            "limit": str(PAGE)})
    messages = await _lire_tout("messages", {
        "select": "contact_id,direction,status", "direction": "eq.outbound",
        "order": "created_at.asc"})

    data = {"contacts": contacts, "companies": companies, "messages": messages}
    # 🔴 L'attendu est lui aussi un REJEU sur la capture. Le calculer contre la
    # vraie base comparerait deux mondes, pas deux versions du code.
    data["attendu"] = await _rejouer_sur(data)
    CAPTURE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"capture : {len(contacts)} contacts, {len(companies)} entreprises, "
          f"{len(messages)} messages, {len(data['attendu'])} attendus")


async def rejouer() -> int:
    data = json.loads(CAPTURE.read_text(encoding="utf-8"))
    obtenu = await _rejouer_sur(data)
    attendu = data["attendu"]
    if obtenu == attendu:
        print(f"IDENTIQUE : {len(obtenu)} contacts, meme ordre.")
        return 0
    print(f"ECART : attendu={len(attendu)} obtenu={len(obtenu)}")
    print("  en trop   :", [c for c in obtenu if c not in attendu][:10])
    print("  manquants :", [c for c in attendu if c not in obtenu][:10])
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capturer", action="store_true")
    ap.add_argument("--rejouer", action="store_true")
    args = ap.parse_args()
    if args.capturer:
        asyncio.run(capturer())
    elif args.rejouer:
        sys.exit(asyncio.run(rejouer()))
    else:
        ap.error("choisir --capturer ou --rejouer")


if __name__ == "__main__":
    main()
