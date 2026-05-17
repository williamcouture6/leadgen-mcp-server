"""Tool `compliance` — Compliance Agent (WF-5).

Pre-send firewall pour les drafts outbound. Deux layers :
  1. **Deterministic checks** (rapide, sans LLM) — voir `lib/compliance_checks.py`.
     Bloque sur mots bannis, actions 1ère personne, fake social proof, footer
     LCAP, longueur, CTA, vouvoiement, créneaux Cal.com fabriqués, warmup window.
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
        f"## research_json (faits vérifiables sur la cible)\n"
        f"```json\n{json.dumps(research_json or {}, ensure_ascii=False, indent=2)}\n```\n\n"
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
    verdict: str  # "approved" | "needs_revision" | "blocked" | "error"
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
    skip_llm: bool = False,
    model: str = _DEFAULT_MODEL,
) -> ComplianceCheckOut:
    """Lance les 2 layers de compliance sur un draft donné."""
    import asyncio

    started = time.monotonic()

    # Layer 1 — deterministic
    det_results: list[CheckResult] = run_all(
        email_body=body,
        social_proof_count=len(social_proof),
        available_slots=available_slots or None,
        template=template_used,
        email_subject=subject,
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
                _llm_judge, body, subject, research_json, social_proof, model,
            )
        except Exception as e:  # noqa: BLE001
            llm_verdict = {"error": f"LLM judge failed: {type(e).__name__}: {e}"}

    # Verdict final combinant warnings déterministes + LLM
    if llm_verdict and llm_verdict.get("send_decision") == "DO_NOT_SEND":
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
