"""Rapport post-rendez-vous depuis Granola (Part B) — version stand-alone.

Workflow manuel : à la fin d'un appel de découverte, tu copies depuis Granola
les notes IA (« Copy text ») et/ou le transcript, tu les colles dans un fichier
.md/.txt, puis tu lances ce script. Il produit :

  1. Un rapport markdown structuré dans agents/out/ (résumé, plans du client,
     problèmes, ce que le client veut automatiser, opportunités repérées,
     angle de vente, prochaines étapes, citations).
  2. Une ligne en DB : booking_events.meeting_report_json (lié au RDV via
     --booking-uid, sinon au dernier booking_event du contact).

Loi 25 : seul le rapport structuré est persisté en DB — pas le transcript brut.

Usage :
    python scripts/meeting_report.py --file granola.md --email contact@x.com
    python scripts/meeting_report.py --file granola.md --email contact@x.com --booking-uid abc123
    python scripts/meeting_report.py --file granola.md            # transcript seul, sans contexte ni DB
    python scripts/meeting_report.py --file granola.md --email x@y.com --no-db

Prérequis env : ANTHROPIC_API_KEY (+ SUPABASE_URL / clé pour le lookup et l'écriture DB).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# UTF-8 stdout pour PowerShell
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import supabase_client as db  # noqa: E402
from src.tools import meeting  # noqa: E402

OUT_DIR_DEFAULT = ROOT.parent / "agents" / "out"


def _slugify(s: str) -> str:
    s = (s or "rdv").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:40] or "rdv"


# DB queries locales — pattern du repo (cf reply.py qui réplique aussi ses
# propres _find_contact_by_email plutôt que de cross-importer booking.py).
async def _find_contact_by_email(email: str) -> dict[str, Any] | None:
    if not email:
        return None
    rows = await db.select(
        "contacts",
        params={
            "select": "id,company_id,first_name,last_name,email",
            "email": f"eq.{email}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _get_company(company_id: str) -> dict[str, Any] | None:
    rows = await db.select(
        "companies",
        params={
            "select": "id,name,city,icp_segment,industry,research_json",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _find_booking_event_by_uid(uid: str) -> dict[str, Any] | None:
    if not uid:
        return None
    rows = await db.select(
        "booking_events",
        params={
            "select": "id,external_event_id,meeting_scheduled_for,booked_at",
            "external_event_id": f"eq.{uid}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _latest_booking_event_for_contact(contact_id: str) -> dict[str, Any] | None:
    rows = await db.select(
        "booking_events",
        params={
            "select": "id,external_event_id,meeting_scheduled_for,booked_at",
            "contact_id": f"eq.{contact_id}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def run(args: argparse.Namespace) -> int:
    src_path = Path(args.file)
    if not src_path.exists():
        print(f"✗ Fichier introuvable : {src_path}", file=sys.stderr)
        return 1
    transcript = src_path.read_text(encoding="utf-8", errors="replace")
    if not transcript.strip():
        print("✗ Fichier vide.", file=sys.stderr)
        return 1

    contact: dict[str, Any] | None = None
    company: dict[str, Any] | None = None
    booking_event: dict[str, Any] | None = None

    if args.email:
        email = args.email.strip().lower()
        contact = await _find_contact_by_email(email)
        if not contact:
            print(f"⚠ Aucun contact en DB pour {email} — analyse sans contexte entreprise.")
        else:
            if contact.get("company_id"):
                company = await _get_company(contact["company_id"])
            # Lien vers le booking_event (pour persister le rapport)
            if args.booking_uid:
                booking_event = await _find_booking_event_by_uid(args.booking_uid)
            if not booking_event:
                booking_event = await _latest_booking_event_for_contact(contact["id"])

    context = meeting.format_company_context(company, contact)
    print("→ Analyse de l'appel en cours (Claude Sonnet)…")
    out = await meeting.analyze_meeting(transcript, company_context=context, model=args.model)

    # ---- Livraison 1 : fichier markdown ----
    contact_name = None
    if contact:
        contact_name = f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}".strip()
    meta = {
        "company_name": (company or {}).get("name"),
        "contact_name": contact_name or args.email,
        "contact_email": args.email,
        "meeting_date": (booking_event or {}).get("meeting_scheduled_for"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    md = meeting.render_markdown(out.report, meta)

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR_DEFAULT
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify((company or {}).get("name") or args.email or src_path.stem)
    out_file = out_dir / f"meeting_report_{datetime.now().strftime('%Y%m%d')}_{slug}.md"
    out_file.write_text(md, encoding="utf-8")
    print(f"✓ Rapport écrit : {out_file}")
    print(f"  fit_score={out.report.get('fit_score', '?')}  "
          f"({out.usage.input_tokens} in / {out.usage.output_tokens} out tokens, {out.duration_ms} ms)")

    # ---- Livraison 2 : DB (booking_events.meeting_report_json) ----
    if args.no_db:
        print("· DB : sautée (--no-db).")
    elif booking_event:
        try:
            await db.update(
                "booking_events",
                {
                    "meeting_report_json": out.report,
                    "meeting_analyzed_at": datetime.now(timezone.utc).isoformat(),
                },
                filters={"id": f"eq.{booking_event['id']}"},
            )
            print(f"✓ DB : rapport lié au booking_event {booking_event['id']}.")
        except Exception as e:  # noqa: BLE001
            print(f"⚠ DB : échec de l'update ({e!r}). Le fichier markdown reste valide.",
                  file=sys.stderr)
    else:
        print("· DB : aucun booking_event trouvé pour ce contact — rapport non persisté "
              "(le fichier markdown reste la source). Passe --booking-uid si tu connais l'UID Cal.com.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Rapport post-RDV depuis notes/transcript Granola")
    parser.add_argument("--file", required=True, help="Fichier .md/.txt des notes/transcript Granola")
    parser.add_argument("--email", help="Email du contact (lie le rapport au lead + contexte recherche)")
    parser.add_argument("--booking-uid", dest="booking_uid",
                        help="UID Cal.com du RDV (external_event_id) — sinon dernier booking_event du contact")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Modèle Anthropic")
    parser.add_argument("--out-dir", dest="out_dir", help="Dossier de sortie (défaut: agents/out/)")
    parser.add_argument("--no-db", dest="no_db", action="store_true", help="N'écrit pas en DB")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
