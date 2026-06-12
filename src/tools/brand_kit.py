"""Tool build_brand_kit — produit companies.brand_kit pour le site démo P4.

Étape on-demand, séparée de research_company. Approche hybride : extraction
déterministe (brandkit_parse) + Google Places + un appel Sonnet (texte + choix
d'images par candidate_id) ; images ré-hébergées dans le bucket brand-assets,
Pexels en fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anthropic import Anthropic

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "brand_kit.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_BRANDKIT_TOOL_NAME = "save_brand_kit"

_BRANDKIT_TOOL: dict[str, Any] = {
    "name": _BRANDKIT_TOOL_NAME,
    "description": (
        "Enregistre le brand-kit extrait du site. null/tableau vide si inconnu. "
        "Pour les images, ne renvoie que des candidate_id de la liste fournie."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tagline": {"type": ["string", "null"]},
            "logo_candidate_id": {"type": ["integer", "null"]},
            "hero_candidate_id": {"type": ["integer", "null"]},
            "team_photo_candidate_id": {"type": ["integer", "null"]},
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": ["string", "null"]},
                        "details": {"type": ["string", "null"]},
                        "inclus": {"type": "array", "items": {"type": "string"}},
                        "image_candidate_id": {"type": ["integer", "null"]},
                        "overlay": {"enum": ["dark", "light", None]},
                    },
                },
            },
            "valeurs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "titre": {"type": "string"},
                        "texte": {"type": "string"},
                        "image_candidate_id": {"type": ["integer", "null"]},
                    },
                },
            },
            "faq": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "reponse": {"type": "string"},
                    },
                },
            },
            "legal": {
                "type": ["object", "null"],
                "properties": {"confidentialite": {"type": ["string", "null"]}},
            },
            "stats": {
                "type": ["object", "null"],
                "properties": {
                    "years_experience": {"type": ["integer", "null"]},
                    "projects": {"type": ["integer", "null"]},
                    "clients": {"type": ["integer", "null"]},
                },
            },
            "service_areas": {"type": "array", "items": {"type": "string"}},
            "team": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nom": {"type": "string"},
                        "role": {"type": ["string", "null"]},
                        "photo_candidate_id": {"type": ["integer", "null"]},
                    },
                },
            },
            "rbq": {"type": ["string", "null"]},
        },
        "required": [],
    },
}


def _call_brandkit_llm(
    candidates: list[dict[str, Any]],
    page_text: str,
    industry: str | None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2500,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    cand_block = json.dumps(
        [{"id": c["id"], "kind_hint": c["kind_hint"], "alt": c.get("alt", "")} for c in candidates],
        ensure_ascii=False,
    )
    user = (
        f"## Industrie\n{industry or 'inconnue'}\n\n"
        f"## Candidats images\n{cand_block}\n\n"
        f"## Texte des pages\n{page_text[:14000]}\n"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_BRANDKIT_TOOL],
        tool_choice={"type": "tool", "name": _BRANDKIT_TOOL_NAME},
        messages=[{"role": "user", "content": user}],
    )
    block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == _BRANDKIT_TOOL_NAME),
        None,
    )
    if block is not None and isinstance(block.input, dict):
        return block.input
    return {}
