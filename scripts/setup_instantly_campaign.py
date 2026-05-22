"""Crée la campagne Instantly d'outreach PME QC via l'API v2.

Usage :
    python mcp-server/scripts/setup_instantly_campaign.py
    python mcp-server/scripts/setup_instantly_campaign.py --dry-run
    python mcp-server/scripts/setup_instantly_campaign.py --name "Autre nom"

Pré-requis dans `.env` :
    INSTANTLY_API_KEY=<clé API Instantly v2>
    M365_FROM_EMAIL=<inbox warmupée à attacher>
    M365_FROM_NAME=<nom de l'expéditeur>

Ce que le script fait :
    1. POST /api/v2/campaigns avec config standardisée (schedule Lun-Ven 9-17 ET,
       sequence step `{{email_subject}}` / `{{email_body}}`, tracking sain).
    2. Laisse la campagne EN PAUSE (jamais activée automatiquement).
    3. Écrit `INSTANTLY_CAMPAIGN_ID=<uuid>` dans `.env` (remplace ou append).

Ce que tu dois faire manuellement après :
    - UI Instantly → Campaign settings → Footer / Signature : coller un bloc
      texte avec [nom légal], [adresse postale], "{{unsubscribe_link}}".
    - Vérifier visuellement le template rendu.
    - Activer la campagne le 27 mai 2026 (fin warmup).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Windows PowerShell default stdout = cp1252 → crash sur unicode. Force utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
load_dotenv(ENV_PATH)

API_BASE = "https://api.instantly.ai/api/v2"

# --- Config standardisée -------------------------------------------------
# Aligné avec la sortie de WF-4 (Personalize Agent) qui produit subject+body
# en texte brut, déjà compliant. La campagne ne fait QUE relayer ces 2 vars.
CAMPAIGN_DEFAULTS = {
    "name": "Cold outreach PME QC",
    "subject_template": "{{email_subject}}",
    "body_template": "{{email_body}}",
    "daily_limit": 30,
    "email_gap_minutes": 10,
    # Instantly v2 enum custom — `America/Toronto` et `America/New_York` rejetés,
    # `America/Detroit` accepté (même offset ET, DST identique). Probé 2026-05-21.
    "timezone": "America/Detroit",
    "from_hour": "09:00",
    "to_hour": "17:00",
    "schedule_days": {  # 0=dim, 1=lun, ..., 6=sam (convention Instantly v2)
        "0": False, "1": True, "2": True, "3": True,
        "4": True, "5": True, "6": False,
    },
    "open_tracking": True,
    "link_tracking": False,
    "stop_on_reply": True,
    "stop_on_auto_reply": True,
    "text_only": True,
}


def _require_env(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        sys.exit(f"❌ {name} absent dans .env")
    return val


def build_payload(*, name: str, from_email: str) -> dict:
    d = CAMPAIGN_DEFAULTS
    return {
        "name": name,
        "campaign_schedule": {
            "schedules": [{
                "name": "Heures bureau QC",
                "timing": {"from": d["from_hour"], "to": d["to_hour"]},
                "days": d["schedule_days"],
                "timezone": d["timezone"],
            }],
            "start_date": None,
            "end_date": None,
        },
        "email_list": [from_email],
        "sequences": [{
            "steps": [{
                "type": "email",
                "delay": 0,
                "variants": [{
                    "subject": d["subject_template"],
                    "body": d["body_template"],
                }],
            }],
        }],
        "daily_limit": d["daily_limit"],
        "email_gap": d["email_gap_minutes"],
        "stop_on_reply": d["stop_on_reply"],
        "stop_on_auto_reply": d["stop_on_auto_reply"],
        "open_tracking": d["open_tracking"],
        "link_tracking": d["link_tracking"],
        "text_only": d["text_only"],
    }


def create_campaign(api_key: str, payload: dict) -> dict:
    url = f"{API_BASE}/campaigns"
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    if r.status_code >= 400:
        sys.exit(
            f"❌ Instantly POST /campaigns {r.status_code}:\n{r.text[:600]}\n\n"
            f"Payload envoyé :\n{json.dumps(payload, indent=2, ensure_ascii=False)}"
        )
    return r.json()


def update_env_var(name: str, value: str) -> str:
    """Remplace ou append `NAME=value` dans le `.env`. Retourne le statut."""
    if not ENV_PATH.exists():
        sys.exit(f"❌ {ENV_PATH} introuvable")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "replaced" if found else "appended"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default=CAMPAIGN_DEFAULTS["name"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le payload sans appeler Instantly")
    args = parser.parse_args()

    api_key = _require_env("INSTANTLY_API_KEY")
    from_email = _require_env("M365_FROM_EMAIL")

    existing_id = (os.environ.get("INSTANTLY_CAMPAIGN_ID") or "").strip()
    if existing_id:
        print(f"⚠️  INSTANTLY_CAMPAIGN_ID déjà set : {existing_id}")
        ans = input("Créer une NOUVELLE campagne quand même ? (y/N) ").strip().lower()
        if ans != "y":
            print("Annulé.")
            return 0

    payload = build_payload(name=args.name, from_email=from_email)

    if args.dry_run:
        print("=== DRY RUN — payload Instantly POST /campaigns ===")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"→ Création campagne '{args.name}' avec inbox {from_email} …")
    resp = create_campaign(api_key, payload)
    campaign_id = resp.get("id") or resp.get("campaign_id")
    if not campaign_id:
        print("❌ Réponse Instantly sans id :")
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 1

    status = update_env_var("INSTANTLY_CAMPAIGN_ID", campaign_id)
    print(f"✅ Campagne créée : {campaign_id}")
    print(f"✅ .env mis à jour ({status} ligne INSTANTLY_CAMPAIGN_ID)")
    print()
    print("Étapes manuelles restantes (UI Instantly) :")
    print("  1. Ouvrir la campagne → Settings → Footer / Signature")
    print("     coller : '[Nom légal] · [Adresse postale] · Désabo : {{unsubscribe_link}}'")
    print("  2. Vérifier le rendu de la sequence step 1 (subject/body)")
    print("  3. NE PAS activer la campagne avant le 27 mai 2026")
    print()
    print("Ensuite pour valider côté code :")
    print("  curl -H \"Authorization: Bearer $AGENTS_HTTP_TOKEN\" \\")
    print("    $LEADGEN_API_URL/send/healthcheck")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
