"""Tool `meeting` — Analyste post-rendez-vous (Part B).

À partir des notes/transcript d'un appel de découverte (capturés via Granola et
collés manuellement), produit un rapport de débreffage structuré : résumé,
plans du client, problèmes identifiés, ce que le client veut automatiser,
opportunités d'automatisation repérées, angle de vente, prochaines étapes.

Réutilise le pattern LLM de `research.py` (Anthropic sync + tenacity retry sur
529/429/réseau + prompt caching du system prompt). Le system prompt vit dans
`src/prompts/meeting_report.md`.

Livraison (gérée par le CLI `scripts/meeting_report.py`) : fichier markdown
local + colonne `booking_events.meeting_report_json`. Pas de Slack ici.

Loi 25 : on ne persiste que le rapport structuré (dérivé), jamais le transcript
verbatim brut — celui-ci reste sur la machine de l'opérateur.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "meeting_report.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


# ----------------------------------------------------------------------
# LLM helpers — mirror du pattern Research Agent.
# Dupliqués volontairement plutôt que cross-import de symboles privés
# (cf reply.py qui réplique aussi ses propres _find_contact_by_email).
# ----------------------------------------------------------------------

class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text[:300]}")
    return json.loads(match.group(0))


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    """True si l'erreur Anthropic est transitoire et mérite un retry.

    Catch surtout 529 OverloadedError + 429 RateLimitError + erreurs réseau.
    """
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in (502, 503, 504, 529)
    return False


# ----------------------------------------------------------------------
# Formatage du contexte entreprise (recherche pré-appel) pour le LLM
# ----------------------------------------------------------------------

def format_company_context(
    company: dict[str, Any] | None,
    contact: dict[str, Any] | None = None,
) -> str:
    """Bloc texte décrivant l'entreprise/contact connus, pour ancrer l'analyse.

    Tolère `research_json` direct ou wrappé `{"research": {...}}`. Retourne
    "(aucun contexte fourni)" si rien d'exploitable.
    """
    if not company and not contact:
        return "(aucun contexte fourni)"
    parts: list[str] = []
    if contact:
        name = f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}".strip()
        if name:
            parts.append(f"Contact : {name}")
        if contact.get("email"):
            parts.append(f"Email : {contact['email']}")
    if company:
        if company.get("name"):
            parts.append(f"Entreprise : {company['name']}")
        if company.get("industry"):
            parts.append(f"Secteur : {company['industry']}")
        if company.get("city"):
            parts.append(f"Ville : {company['city']}")
        rj = company.get("research_json")
        if isinstance(rj, dict):
            inner = rj.get("research") if isinstance(rj.get("research"), dict) else rj
            summary = (inner.get("company_summary") or "").strip()
            if summary:
                parts.append(f"Résumé (recherche) : {summary}")
            pains = inner.get("pain_points_detected")
            if isinstance(pains, list) and pains:
                known = [
                    (p.get("pain") if isinstance(p, dict) else str(p)) or ""
                    for p in pains[:3]
                ]
                known = [k.strip() for k in known if k and k.strip()]
                if known:
                    parts.append("Pain points déjà repérés : " + " ; ".join(known))
    return "\n".join(parts) if parts else "(aucun contexte fourni)"


# ----------------------------------------------------------------------
# Appel LLM
# ----------------------------------------------------------------------

class AnalyzeMeetingOut(BaseModel):
    report: dict[str, Any]
    model: str
    duration_ms: int
    usage: LLMUsage


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_llm(
    transcript: str,
    company_context: str,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 3000,
) -> tuple[dict[str, Any], LLMUsage]:
    """Appel Anthropic synchrone (wrappé via asyncio.to_thread).

    Retry backoff exponentiel sur les erreurs transitoires (529/429/502/503/504
    + réseau), comme le Research Agent.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        "## Contexte entreprise (recherche pré-appel)\n"
        f"{company_context}\n\n"
        "## Notes / transcript de l'appel (Granola)\n"
        f"{transcript}\n"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return _parse_json(text), LLMUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


async def analyze_meeting(
    transcript: str,
    company_context: str = "(aucun contexte fourni)",
    model: str = _DEFAULT_MODEL,
) -> AnalyzeMeetingOut:
    """Analyse un transcript/notes de RDV → rapport structuré.

    `transcript` : texte brut collé depuis Granola (notes IA et/ou transcript).
    `company_context` : bloc texte (voir `format_company_context`) — optionnel.
    """
    if not transcript or not transcript.strip():
        raise ValueError("transcript vide — rien à analyser")
    started = time.monotonic()
    report, usage = await asyncio.to_thread(_call_llm, transcript, company_context, model)
    return AnalyzeMeetingOut(
        report=report,
        model=model,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage=usage,
    )


# ----------------------------------------------------------------------
# Rendu markdown du rapport (livraison fichier)
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Matching Granola note ↔ booking (Part C / WF-9)
# ----------------------------------------------------------------------

def _safe_emails_from_note(note: dict[str, Any]) -> set[str]:
    """Extrait tous les emails (attendees + creator) d'une note Granola, normalisés."""
    people = note.get("people") if isinstance(note.get("people"), dict) else {}
    emails: set[str] = set()
    attendees = people.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:
            if isinstance(a, dict) and a.get("email"):
                emails.add(str(a["email"]).strip().lower())
    creator = people.get("creator")
    if isinstance(creator, dict) and creator.get("email"):
        emails.add(str(creator["email"]).strip().lower())
    return emails


def _parse_iso_safe(s: str | None) -> Any:
    """Parse ISO 8601 → datetime aware, ou None si invalide."""
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime as _dt, timezone as _tz
    try:
        d = _dt.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz.utc)
        return d
    except (ValueError, TypeError):
        return None


def match_granola_note(
    notes: list[dict[str, Any]],
    *,
    attendee_email: str | None,
    meeting_start_iso: str | None,
    contact_name: str | None = None,
    company_name: str | None = None,
    gcal_event_id: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Trouve la note Granola qui correspond à un booking Cal.com.

    Stratégie de scoring (additive) :
      +100 : google_calendar_event.id == gcal_event_id (déterministe)
      +50  : attendee_email présent dans note.people.attendees|creator
      +25  : note `valid_meeting=True`
      +20  : note créée dans ±2h du meeting_start
      +10  : note créée dans ±4h
      +5   : titre contient le nom contact ou entreprise
      −100 : note `valid_meeting=False` explicite (probable doc/test, pas un vrai meeting)

    Threshold d'acceptation : meilleur score ≥ 50. Sinon retourne (None, best_score)
    pour que le caller logue et re-tente plus tard.
    """
    from datetime import timedelta as _td

    if not notes:
        return None, 0

    target_email = (attendee_email or "").strip().lower() or None
    target_name = (contact_name or "").strip().lower() or None
    target_company = (company_name or "").strip().lower() or None
    meeting_start = _parse_iso_safe(meeting_start_iso)

    best: tuple[dict[str, Any] | None, int] = (None, 0)
    for note in notes:
        if not isinstance(note, dict):
            continue
        score = 0

        valid = note.get("valid_meeting")
        if valid is True:
            score += 25
        elif valid is False:
            score -= 100

        if gcal_event_id:
            gcal = note.get("google_calendar_event")
            note_gcal_id = (gcal.get("id") if isinstance(gcal, dict) else None)
            if note_gcal_id and str(note_gcal_id) == str(gcal_event_id):
                score += 100

        if target_email:
            emails = _safe_emails_from_note(note)
            if target_email in emails:
                score += 50

        if meeting_start:
            note_created = _parse_iso_safe(note.get("created_at"))
            if note_created:
                delta = abs(note_created - meeting_start)
                if delta <= _td(hours=2):
                    score += 20
                elif delta <= _td(hours=4):
                    score += 10

        title = (note.get("title") or "").strip().lower()
        if title:
            if target_name and target_name in title:
                score += 5
            if target_company and target_company in title:
                score += 5

        if score > best[1]:
            best = (note, score)

    # Threshold : on n'accepte que les matches avec un signal d'identification fort
    # (au moins email match ou GCal match). Pure proximité temporelle = pas suffisant.
    if best[0] is None or best[1] < 50:
        return None, best[1]
    return best


def granola_note_to_text(note: dict[str, Any]) -> str:
    """Aplatit une note Granola en un blob texte exploitable par `analyze_meeting`.

    Combine : titre, AI summary, notes utilisateur (si présentes), transcript.
    Transcript = entrées `[{speaker.source, text}]` formatées "Source: text".
    Tolère les shapes manquants (note partielle).
    """
    parts: list[str] = []
    title = (note.get("title") or "").strip()
    if title:
        parts.append(f"# {title}")

    summary = note.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append("## Résumé IA Granola\n" + summary.strip())

    user_notes = note.get("notes")
    if isinstance(user_notes, str) and user_notes.strip():
        parts.append("## Notes prises pendant l'appel\n" + user_notes.strip())
    elif isinstance(user_notes, dict):
        # Granola peut wrapper les notes dans {content: "..."} ou un format ProseMirror
        content = user_notes.get("content") or user_notes.get("text")
        if isinstance(content, str) and content.strip():
            parts.append("## Notes prises pendant l'appel\n" + content.strip())

    transcript = note.get("transcript")
    if isinstance(transcript, list) and transcript:
        lines = ["## Transcript"]
        for entry in transcript:
            if not isinstance(entry, dict):
                continue
            speaker = entry.get("speaker") if isinstance(entry.get("speaker"), dict) else {}
            source = speaker.get("source") or speaker.get("diarization_label") or "?"
            text = (entry.get("text") or "").strip()
            if text:
                lines.append(f"**{source}:** {text}")
        if len(lines) > 1:
            parts.append("\n".join(lines))

    return "\n\n".join(parts).strip()


# ----------------------------------------------------------------------
# Rendu markdown du rapport (livraison fichier)
# ----------------------------------------------------------------------

def _bullets(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "_(aucun)_"
    return "\n".join(f"- {str(i).strip()}" for i in items if str(i).strip())


def render_markdown(report: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    """Transforme le rapport JSON en markdown lisible pour fichier local."""
    meta = meta or {}
    r = report or {}
    lines: list[str] = []

    title = meta.get("company_name") or meta.get("contact_name") or "Rendez-vous"
    lines.append(f"# Rapport post-RDV — {title}")
    meta_bits = []
    if meta.get("contact_name"):
        meta_bits.append(f"**Contact :** {meta['contact_name']}")
    if meta.get("contact_email"):
        meta_bits.append(f"**Email :** {meta['contact_email']}")
    if meta.get("meeting_date"):
        meta_bits.append(f"**Date RDV :** {meta['meeting_date']}")
    if meta.get("generated_at"):
        meta_bits.append(f"**Généré le :** {meta['generated_at']}")
    fit = r.get("fit_score")
    if fit:
        meta_bits.append(f"**Fit :** {fit}")
    if meta_bits:
        lines.append("  \n".join(meta_bits))
    lines.append("")

    if r.get("resume_executif"):
        lines.append("## Résumé exécutif")
        lines.append(str(r["resume_executif"]).strip())
        lines.append("")

    if r.get("contexte_entreprise"):
        lines.append("## Contexte entreprise")
        lines.append(str(r["contexte_entreprise"]).strip())
        lines.append("")

    lines.append("## Plans & objectifs du client")
    lines.append(_bullets(r.get("plans_objectifs_client")))
    lines.append("")

    lines.append("## Problèmes identifiés")
    problems = r.get("problemes_identifies")
    if isinstance(problems, list) and problems:
        for p in problems:
            if isinstance(p, dict):
                line = f"- {str(p.get('probleme', '')).strip()}"
                vb = p.get("verbatim")
                if vb:
                    line += f"\n  > {str(vb).strip()}"
                lines.append(line)
            else:
                lines.append(f"- {str(p).strip()}")
    else:
        lines.append("_(aucun)_")
    lines.append("")

    lines.append("## Ce que le client veut automatiser")
    lines.append(_bullets(r.get("automatisation_souhaitee_client")))
    lines.append("")

    lines.append("## Opportunités d'automatisation (repérées)")
    opps = r.get("opportunites_automatisation")
    if isinstance(opps, list) and opps:
        for o in opps:
            if isinstance(o, dict):
                lines.append(
                    f"- **{str(o.get('processus', '')).strip()}** "
                    f"({str(o.get('complexite', '?')).strip()})  \n"
                    f"  Solution : {str(o.get('solution', '')).strip()}  \n"
                    f"  Impact : {str(o.get('impact', '')).strip()}"
                )
            else:
                lines.append(f"- {str(o).strip()}")
    else:
        lines.append("_(aucune)_")
    lines.append("")

    if r.get("angle_vente"):
        lines.append("## Angle de vente recommandé")
        lines.append(str(r["angle_vente"]).strip())
        lines.append("")

    lines.append("## Objections & signaux")
    lines.append(_bullets(r.get("objections_signaux")))
    lines.append("")

    lines.append("## Prochaines étapes")
    steps = r.get("prochaines_etapes")
    if isinstance(steps, list) and steps:
        for s in steps:
            if isinstance(s, dict):
                resp = s.get("responsable") or "?"
                ech = s.get("echeance")
                suffix = f" _(→ {ech})_" if ech else ""
                lines.append(f"- [{resp}] {str(s.get('action', '')).strip()}{suffix}")
            else:
                lines.append(f"- {str(s).strip()}")
    else:
        lines.append("_(aucune)_")
    lines.append("")

    citations = r.get("citations_cles")
    if isinstance(citations, list) and citations:
        lines.append("## Citations clés")
        for c in citations:
            if str(c).strip():
                lines.append(f"> {str(c).strip()}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"
