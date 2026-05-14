"""Tool `personalize` — Personalization Agent (WF-4).

Génère un cold email personnalisé (Template A ou B) à partir de :
  1. `research_json` de la company (produit par WF-3)
  2. Données contact (Apollo : prénom, nom, titre, email)
  3. Liste de créneaux Cal.com (source de vérité du CTA — voir [[feedback_cta_real_availability]])
  4. Liste de social_proof (clients référence — voir [[project_zero_client_references]])

Appel Claude Sonnet avec le prompt système `src/prompts/personalize.md`
(cache_control=ephemeral pour réduire le coût input sur appels successifs).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel

# ----------------------------------------------------------------------
# Prompt + modèle
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "personalize.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ----------------------------------------------------------------------
# Construction du user message
# ----------------------------------------------------------------------

def _format_input_for_llm(
    *,
    research: dict[str, Any],
    company: dict[str, Any],
    contact: dict[str, Any] | None,
    social_proof: list[dict[str, Any]],
    template_choice: str,
    slots_block: str,
) -> str:
    """Reprend exactement le format du proto CLI (`agents/personalize_agent.py`)."""
    place_name = company.get("name", "")
    website = company.get("website", "") or ""

    parts = [
        f"## Template à utiliser\n{template_choice}",
        f"\n## Entreprise ciblée\nname: {place_name}\nwebsite: {website}",
        f"\n## research_json (Research Agent output)\n```json\n{json.dumps(research, ensure_ascii=False, indent=2)}\n```",
    ]

    if contact:
        parts.append(
            "\n## apollo_contact (Apollo enrichment)\n"
            f"```json\n{json.dumps(contact, ensure_ascii=False, indent=2)}\n```"
        )
    else:
        parts.append(
            "\n## apollo_contact\n`null` — Apollo n'a pas matché. "
            "Utilise les `decideur_candidats` du research_json pour le prénom si disponible, "
            "sinon écris 'Bonjour,' sans nom. Mets un warning 'Email pas trouvé via Apollo — fallback manuel requis'."
        )

    if social_proof:
        parts.append(
            "\n## social_proof (références client réelles, citables uniquement si match secteur/ville)\n"
            f"```json\n{json.dumps(social_proof, ensure_ascii=False, indent=2)}\n```"
        )
    else:
        parts.append(
            "\n## social_proof\n`[]` — Couture IA n'a aucun client référence actuellement. "
            "**INTERDICTION ABSOLUE d'inventer ou de suggérer l'existence de clients passés.** "
            "L'email doit être convaincant sans aucune référence à d'autres clients."
        )

    parts.append("\n" + slots_block)
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class PersonalizeIn(BaseModel):
    research_json: dict[str, Any]
    company: dict[str, Any]  # nom, website, city, etc. (extrait de la row companies)
    contact: dict[str, Any] | None = None  # apollo_contact normalisé (first_name, last_name, email, title)
    social_proof: list[dict[str, Any]] = []
    template_choice: str = "A"  # "A" ou "B"
    available_slots: list[dict[str, Any]] = []  # output de get_available_slots
    model: str = _DEFAULT_MODEL


class PersonalizeOut(BaseModel):
    email: dict[str, Any]  # {subject, body_text, justification, warnings, word_count, template_used}
    template_used: str
    contact_used: bool
    social_proof_count: int
    available_slots_at_generation: list[dict[str, Any]]
    duration_ms: int
    model: str
    usage: LLMUsage


def _call_llm(
    user_message: str,
    model: str,
    max_tokens: int = 2500,
) -> tuple[dict[str, Any], LLMUsage]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.4,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return (
        _parse_json(text),
        LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


async def personalize(payload: PersonalizeIn) -> PersonalizeOut:
    """Génère un draft email personnalisé. Synchrone à l'intérieur (Anthropic SDK),
    wrappé async avec asyncio.to_thread pour ne pas bloquer FastAPI."""
    import asyncio
    from ..lib.calcom import format_slots_for_prompt

    started = time.monotonic()
    slots_block = format_slots_for_prompt(payload.available_slots)

    user_message = _format_input_for_llm(
        research=payload.research_json,
        company=payload.company,
        contact=payload.contact,
        social_proof=payload.social_proof,
        template_choice=payload.template_choice,
        slots_block=slots_block,
    )

    email_json, usage = await asyncio.to_thread(_call_llm, user_message, payload.model)

    return PersonalizeOut(
        email=email_json,
        template_used=payload.template_choice,
        contact_used=payload.contact is not None,
        social_proof_count=len(payload.social_proof),
        available_slots_at_generation=payload.available_slots,
        duration_ms=int((time.monotonic() - started) * 1000),
        model=payload.model,
        usage=usage,
    )
