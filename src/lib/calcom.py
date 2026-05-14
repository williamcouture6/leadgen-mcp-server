"""Cal.com integration — source de vérité pour les créneaux dispos.

Port de `agents/lib/calcom.py` vers le service Railway. Lit l'API v2 Cal.com
(https://api.cal.com/v2/slots) pour récupérer les créneaux RÉELLEMENT disponibles
selon la config Cal.com de William (qui sync elle-même avec Google Calendar).

Le Personalization Agent pioche dans cette liste — interdiction d'inventer un créneau.
Cohérent avec [[feedback_cta_real_availability]] : si Cal.com dit pas dispo, on ne propose pas.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

CAL_API_URL = "https://api.cal.com/v2/slots"
CAL_API_VERSION = "2024-09-04"
DEFAULT_TIMEZONE = "America/Toronto"  # Lévis / Montréal

_DAY_FR = {
    "Monday": "lundi", "Tuesday": "mardi", "Wednesday": "mercredi",
    "Thursday": "jeudi", "Friday": "vendredi", "Saturday": "samedi", "Sunday": "dimanche",
}
_MONTH_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril", 5: "mai", 6: "juin",
    7: "juillet", 8: "août", 9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}


class CalcomError(Exception):
    """Raised when Cal.com API call fails or returns no slots."""


def _parse_iso_offset(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _format_time_fr(dt: datetime) -> str:
    return f"{dt.hour}h" if dt.minute == 0 else f"{dt.hour}h{dt.minute:02d}"


def _format_date_fr(dt: datetime) -> str:
    return f"{dt.day} {_MONTH_FR[dt.month]}"


def get_available_slots(
    days_ahead: int = 7,
    timezone_str: str = DEFAULT_TIMEZONE,
    event_type_id: str | None = None,
    api_key: str | None = None,
) -> list[dict]:
    """Récupère les créneaux dispos via Cal.com v2.

    Returns: liste structurée par jour
        [{day_fr, date_iso, date_fr, times: [...], starts_iso: [...]}, ...]

    Raises CalcomError si clé manquante, HTTP error, ou aucun slot.
    """
    api_key = api_key or os.environ.get("CALCOM_API_KEY", "").strip()
    event_type_id = event_type_id or os.environ.get("CALCOM_EVENT_TYPE_ID", "").strip()

    if not api_key:
        raise CalcomError("CALCOM_API_KEY absent")
    if not event_type_id:
        raise CalcomError("CALCOM_EVENT_TYPE_ID absent")

    # J+1 à J+(1+days_ahead) : exclut "aujourd'hui" (l'email peut être lu après le créneau).
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=days_ahead)

    try:
        resp = httpx.get(
            CAL_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "cal-api-version": CAL_API_VERSION,
            },
            params={
                "eventTypeId": event_type_id,
                "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "timeZone": timezone_str,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise CalcomError(f"HTTP error Cal.com: {type(e).__name__}: {e}") from e

    if resp.status_code != 200:
        raise CalcomError(f"Cal.com status {resp.status_code}: {resp.text[:200]}")

    payload = resp.json()
    if payload.get("status") != "success":
        raise CalcomError(f"Cal.com status field != success: {payload}")

    data = payload.get("data") or {}
    out: list[dict] = []
    for date_iso, slots in sorted(data.items()):
        if not slots:
            continue
        first_dt = _parse_iso_offset(slots[0]["start"])
        day_fr = _DAY_FR.get(first_dt.strftime("%A"), first_dt.strftime("%A").lower())
        out.append({
            "day_fr": day_fr,
            "date_iso": date_iso,
            "date_fr": _format_date_fr(first_dt),
            "times": [_format_time_fr(_parse_iso_offset(s["start"])) for s in slots],
            "starts_iso": [s["start"] for s in slots],
        })

    if not out:
        raise CalcomError(f"Cal.com OK mais aucun créneau dispo (window {days_ahead}j)")
    return out


def format_slots_for_prompt(slots: list[dict]) -> str:
    """Convertit la liste de créneaux en bloc texte injecté dans le prompt LLM."""
    if not slots:
        return (
            "## Créneaux disponibles (Cal.com)\n"
            "`[]` — Aucun créneau disponible récupéré. Utilise un CTA générique "
            "type \"15 minutes cette semaine ?\" sans proposer de jour/heure précis."
        )
    lines = [
        "## Créneaux disponibles (Cal.com — source de vérité)",
        "",
        "INSTRUCTION CRITIQUE: tu DOIS choisir EXACTEMENT 2 créneaux dans cette liste pour le CTA.",
        "Format CTA: \"{jour} {date} à {heure} ou {jour2} {date2} à {heure2}, 15 minutes ?\"",
        "Exemple: \"Mercredi 13 mai à 18h ou jeudi 14 mai à 18h30, 15 minutes ?\"",
        "INTERDICTION ABSOLUE d'inventer un jour ou une heure absent de cette liste.",
        "",
    ]
    for s in slots:
        times_str = ", ".join(s["times"])
        lines.append(f"- **{s['day_fr'].capitalize()} {s['date_fr']}** ({s['date_iso']}): {times_str}")
    return "\n".join(lines)


# ============================================================
# Validation pour le Compliance Agent (réutilisé tel quel depuis le proto)
# ============================================================

_SLOT_PATTERN = re.compile(
    r"\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b"
    r"(?:\s+(?:le\s+)?(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre))?"
    r"[^.!?\n]{0,15}?"
    r"\b(\d{1,2})h(\d{2})?\b",
    re.IGNORECASE,
)
_MONTH_NORMALIZE = {"fevrier": "février", "aout": "août", "decembre": "décembre"}


def extract_slots_from_text(text: str) -> list[tuple[str, str | None, str]]:
    hits: list[tuple[str, str | None, str]] = []
    for m in _SLOT_PATTERN.finditer(text):
        day = m.group(1).lower()
        day_num = m.group(2)
        month = m.group(3)
        hour = m.group(4)
        minute = m.group(5)
        time_str = f"{int(hour)}h" if not minute else f"{int(hour)}h{minute}"
        date_fr: str | None = None
        if day_num and month:
            month_norm = _MONTH_NORMALIZE.get(month.lower(), month.lower())
            date_fr = f"{int(day_num)} {month_norm}"
        hits.append((day, date_fr, time_str))
    return hits


def slot_in_available(
    day_fr: str,
    date_fr: str | None,
    time_fr: str,
    available_slots: list[dict],
) -> bool:
    day_lower = day_fr.lower()
    for s in available_slots:
        if s["day_fr"].lower() != day_lower:
            continue
        if time_fr not in s["times"]:
            continue
        if date_fr is None:
            return True
        if s.get("date_fr", "").lower() == date_fr.lower():
            return True
    return False
