"""Tool `compliance` — Compliance Agent (WF-5).

Pre-send firewall pour les drafts outbound. Deux layers :
  1. **Deterministic checks** (rapide, sans LLM) — voir `lib/compliance_checks.py`.
     Bloque sur mots bannis, actions 1ère personne, fake social proof, footer
     LCAP, longueur, CTA, registre (cohérence tu/vous), créneaux Cal.com
     fabriqués, warmup window.
  2. **LLM judge** (Claude Sonnet) — voir `prompts/compliance.md`. Détecte les
     violations sémantiques que les regex ne peuvent pas voir (faits non
     vérifiables, preuve sociale subtile, promesses non tenables, etc.).

Un verdict `blocked` du layer 1 court-circuite — layer 2 skipped.

Le verdict final est écrit dans `messages` :
  - `compliance_check_passed` : true (approved) | false (blocked/needs_revision)
  - `compliance_notes` : résumé des violations + suggestions
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..lib.compliance_checks import CheckResult, run_all
from .research import sans_diagnostic

# ----------------------------------------------------------------------
# Prompt + modèle
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "compliance.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


# ----------------------------------------------------------------------
# LLM call (retry sur 529)
# ----------------------------------------------------------------------

def _is_transient_anthropic_error(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in (502, 503, 504, 529)
    return False


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _llm_judge(
    body: str,
    subject: str,
    research_json: dict[str, Any] | None,
    social_proof: list[dict[str, Any]] | None,
    contact: dict[str, Any] | None = None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2500,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        f"## Email à juger\n\n"
        f"**Sujet**: {subject}\n\n"
        f"**Corps**:\n{body}\n\n"
        f"## Destinataire (contact vérifié — source de vérité de l'identité)\n"
        f"```json\n{json.dumps(contact or {}, ensure_ascii=False, indent=2)}\n```\n"
        f"Le prénom/nom/titre ci-dessus viennent de la fiche contact vérifiée "
        f"(`email_source`: website_scrape = site officiel ; apollo = contact vérifié hérité). "
        f"Ils sont ANCRÉS par cette fiche — ne les traite JAMAIS comme un fait inventé "
        f"ni un contact_mismatch même s'ils n'apparaissent pas dans le research_json.\n\n"
        f"## research_json (faits vérifiables sur l'ENTREPRISE — pas l'identité du contact)\n"
        # `sans_diagnostic` retire la télémétrie du scraper d'emails : le juge
        # n'a pas à voir des compteurs de rejets ni des adresses tierces jetées
        # (bruit + risque de faux contact_mismatch).
        f"```json\n{json.dumps(sans_diagnostic(research_json), ensure_ascii=False, indent=2)}\n```\n\n"
        f"## social_proof disponible\n"
        f"```json\n{json.dumps(social_proof or [], ensure_ascii=False, indent=2)}\n```\n"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.1,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON in compliance LLM response: {text[:300]}")
        return json.loads(match.group(0))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class ComplianceCheckIn(BaseModel):
    message_id: str
    skip_llm: bool = False
    model: str = _DEFAULT_MODEL


class ComplianceCheckOut(BaseModel):
    message_id: str
    # "non_juge" = le juge LLM n'a pas répondu, le draft n'a PAS été inspecté.
    # Distinct de "error" (la passe elle-même a échoué) et de NULL en base
    # (jamais tenté). Voir migration 0045.
    verdict: str  # "approved" | "needs_revision" | "blocked" | "non_juge" | "error"
    send_decision: str  # "SEND" | "REVIEW_THEN_SEND" | "DO_NOT_SEND"
    deterministic_blockers: list[dict[str, Any]] = []
    deterministic_warnings: list[dict[str, Any]] = []
    llm_judge: dict[str, Any] | None = None
    reasoning: str = ""
    duration_ms: int | None = None
    error_text: str | None = None


async def compliance_check(
    *,
    message_id: str,
    body: str,
    subject: str,
    template_used: str | None,
    research_json: dict[str, Any] | None,
    social_proof: list[dict[str, Any]],
    available_slots: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    skip_llm: bool = False,
    model: str = _DEFAULT_MODEL,
    track: str | None = None,
    tentatives: int = 0,
) -> ComplianceCheckOut:
    """Lance les 2 layers de compliance sur un draft donné.

    `track` sélectionne les critères du layer 1 qui en dépendent (registre
    tu/vous, bornes de longueur). Sans lui, `check_registre` retombe sur
    `vous` et bloque TOUS les corps `agence-ia`, qui tutoient.

    `tentatives` = valeur de `messages.compliance_tentatives` AVANT cette
    passe. Sert de garde anti-boucle quand le juge LLM tombe : voir la
    cascade de verdict plus bas.
    """
    import asyncio

    started = time.monotonic()

    # `compliance_tentatives` absent d'un SELECT rend None côté appelant, et
    # `None >= 2` lève un TypeError qui ferait avorter toute la passe.
    tentatives = tentatives or 0

    # Footer LCAP injecté par l'ESP (Instantly) au moment de l'envoi — pas dans
    # le body généré par WF-4. On le passe aux checks déterministes pour que
    # le scan legal_footer / loi25_privacy le voie. Vide = pas d'ESP footer
    # (mode dev ou tout est dans le body).
    appended_footer = os.environ.get("INSTANTLY_CAMPAIGN_FOOTER", "")

    # Layer 1 — deterministic
    det_results: list[CheckResult] = run_all(
        email_body=body,
        social_proof_count=len(social_proof),
        available_slots=available_slots or None,
        template=template_used,
        email_subject=subject,
        appended_footer=appended_footer,
        track=track,
    )
    det_blockers = [r for r in det_results if not r.passed and r.severity == "block"]
    det_warnings = [r for r in det_results if not r.passed and r.severity == "warn"]

    if det_blockers:
        return ComplianceCheckOut(
            message_id=message_id,
            verdict="blocked",
            send_decision="DO_NOT_SEND",
            deterministic_blockers=[asdict(r) for r in det_blockers],
            deterministic_warnings=[asdict(r) for r in det_warnings],
            llm_judge=None,
            reasoning=f"Layer 1 a bloqué {len(det_blockers)} violation(s) déterministe(s).",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Layer 2 — LLM judge (semantic)
    llm_verdict: dict[str, Any] | None = None
    if not skip_llm:
        try:
            llm_verdict = await asyncio.to_thread(
                _llm_judge, body, subject, research_json, social_proof, contact, model,
            )
        except Exception as e:  # noqa: BLE001
            llm_verdict = {"error": f"LLM judge failed: {type(e).__name__}: {e}"}

    # Verdict final combinant warnings déterministes + LLM.
    #
    # L'ORDRE COMPTE : la panne du juge se teste AVANT tout le reste, parce
    # qu'un courriel non inspecté n'est pas un courriel approuvé. Le cron est
    # quotidien par lots de 20 : quelques minutes d'indisponibilité chez
    # Anthropic suffisaient à approuver le lot du jour, et l'alerte (qui
    # compte `needs_revision + blocked`) restait muette.
    juge_en_panne = bool(llm_verdict and llm_verdict.get("error"))

    if juge_en_panne and tentatives >= 2:
        # 3e tentative : un échec permanent doit réveiller quelqu'un, pas
        # tourner en rond. Devient un vrai refus, donc sort du lot du lendemain.
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    elif juge_en_panne:
        final_verdict = "non_juge"
        final_decision = "DO_NOT_SEND"
    elif llm_verdict and llm_verdict.get("send_decision") == "DO_NOT_SEND":
        final_verdict = "blocked"
        final_decision = "DO_NOT_SEND"
    elif llm_verdict and llm_verdict.get("send_decision") == "REVIEW_THEN_SEND":
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    elif det_warnings:
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    else:
        final_verdict = "approved"
        final_decision = "SEND"

    if juge_en_panne:
        # Surtout PAS « Aucune violation détectée » : rien n'a été inspecté.
        # `compliance_notes` se lit à l'œil nu, et cette phrase-là rassurerait
        # à tort sur le seul cas où il ne faut pas être rassuré.
        reasoning = (
            f"Juge LLM injoignable — corps NON inspecté (tentative {tentatives + 1}). "
            + ("Plafond atteint : passe en refus." if tentatives >= 2
               else "Sera rejugé à la prochaine passe.")
        )
    else:
        reasoning = (
            (llm_verdict or {}).get("reasoning_one_line")
            or (f"{len(det_warnings)} warning(s) déterministe(s)" if det_warnings else "Aucune violation détectée.")
        )

    return ComplianceCheckOut(
        message_id=message_id,
        verdict=final_verdict,
        send_decision=final_decision,
        deterministic_blockers=[],
        deterministic_warnings=[asdict(r) for r in det_warnings],
        llm_judge=llm_verdict,
        reasoning=reasoning,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def format_compliance_notes(out: ComplianceCheckOut) -> str:
    """Texte concis pour `messages.compliance_notes` (lecture humaine)."""
    parts = [f"[{out.verdict.upper()}] {out.send_decision} — {out.reasoning}"]
    for b in out.deterministic_blockers:
        parts.append(f"BLOCK [{b['name']}]: {b['message']}")
        for m in b.get("matches", [])[:3]:
            parts.append(f"  - {m}")
    for w in out.deterministic_warnings:
        parts.append(f"warn [{w['name']}]: {w['message']}")
    if out.llm_judge and not out.llm_judge.get("error"):
        for v in (out.llm_judge.get("semantic_violations") or [])[:5]:
            parts.append(f"semantic [{v.get('category')}]: {v.get('issue')} → {v.get('suggested_fix')}")
    elif out.llm_judge and out.llm_judge.get("error"):
        parts.append(f"llm_error: {out.llm_judge['error']}")
    return "\n".join(parts)
