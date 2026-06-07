# mcp-server/src/tools/reacti_discover.py
"""Tool `reacti_discover` — découverte de contact pour PME REACTI sans site web.

Étape pré-research (WF-reacti-2). Fait UN appel Anthropic avec le web search
natif (`web_search_20250305`) pour trouver la présence web officielle d'une
PME sans `websiteUri`, puis extraire un courriel public. La logique de décision
(quoi insérer / quel statut poser) est isolée dans `decide_discovery_actions`
(fonction pure, testable sans I/O).

Voir spec docs/superpowers/specs/2026-06-06-reacti-no-website-contact-discovery-design.md
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "reacti" / "discover.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_WEB_SEARCH_MAX_USES = 5

_DISCOVERY_TOOL_NAME = "save_discovery"
_DISCOVERY_TOOL: dict[str, Any] = {
    "name": _DISCOVERY_TOOL_NAME,
    "description": (
        "Enregistre le résultat de la découverte de contact. N'invente rien : "
        "found=false si aucune présence web/courriel public fiable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "discovered_url": {"type": ["string", "null"]},
            "page_kind": {"enum": ["own_site", "facebook", "directory", None]},
            "emails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "kind": {"enum": ["generic", "nominative"]},
                        "source_url": {"type": ["string", "null"]},
                        "published_on_own_page": {"type": "boolean"},
                    },
                    "required": ["email"],
                },
            },
            "confidence": {"enum": ["high", "medium", "low"]},
            "match_reasoning": {"type": ["string", "null"]},
        },
        "required": ["found"],
    },
}


class DiscoveryUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class DiscoveryLLMResult(BaseModel):
    discovery: dict[str, Any]
    model: str
    usage: DiscoveryUsage


_EMPTY_DISCOVERY: dict[str, Any] = {
    "found": False,
    "discovered_url": None,
    "page_kind": None,
    "emails": [],
    "confidence": "low",
    "match_reasoning": None,
}


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) in (502, 503, 504, 529)
    return False


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_discovery_llm(
    *,
    name: str,
    city: str | None,
    address: str | None,
    phone: str | None,
    model: str = _DEFAULT_MODEL,
) -> DiscoveryLLMResult:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user = (
        "## Entreprise à trouver\n"
        f"nom: {name}\n"
        f"ville: {city or ''}\n"
        f"adresse: {address or ''}\n"
        f"téléphone: {phone or ''}\n"
    )

    resp = client.messages.create(
        # max_tokens large : le web search natif consomme plusieurs rounds avant le
        # tool_use final save_discovery. Trop bas → stop_reason='max_tokens' tronque
        # le tool_use (input partiel) → mauvaise décision silencieuse.
        model=model,
        max_tokens=2000,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[
            {"type": "web_search_20250305", "name": "web_search", "max_uses": _WEB_SEARCH_MAX_USES},
            _DISCOVERY_TOOL,
        ],
        messages=[{"role": "user", "content": user}],
    )

    tool_block = next(
        (b for b in resp.content
         if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == _DISCOVERY_TOOL_NAME),
        None,
    )
    # tool_choice=auto (forcé impossible avec web_search) → le modèle peut finir en
    # texte seul (rien trouvé) OU être tronqué (stop_reason='max_tokens'). Dans les
    # deux cas on retombe sur un résultat vide = no_web_presence (faux négatif sûr).
    # Un save_discovery tronqué ne doit JAMAIS être traité comme une vraie trouvaille.
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"
    if tool_block is not None and isinstance(tool_block.input, dict) and not truncated:
        discovery = {**_EMPTY_DISCOVERY, **tool_block.input}
    else:
        discovery = dict(_EMPTY_DISCOVERY)

    usage = resp.usage
    return DiscoveryLLMResult(
        discovery=discovery,
        model=model,
        usage=DiscoveryUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


class DiscoveryActions(BaseModel):
    """Résultat de décision appliqué par l'endpoint (aucune I/O ici).

    - new_status: nouvelle valeur companies.status, ou None pour ne pas toucher
      (la company reste 'sourced' et sera researchée par WF-reacti-3).
    - website_backfill: URL à écrire dans companies.website si actuellement NULL.
    - contacts: dicts prêts pour insert_contact (email, kind, source, source_url).
    """
    new_status: Literal["no_web_presence"] | None = None
    website_backfill: str | None = None
    contacts: list[dict[str, Any]] = []


def decide_discovery_actions(discovery: dict[str, Any]) -> DiscoveryActions:
    """Traduit la sortie LLM en actions. Fonction pure (pas d'I/O).

    Règles (voir spec) :
    - confidence='low' OU not found OU 0 email → 'no_web_presence' (on préfère
      rater que polluer le pipeline d'un faux positif).
    - sinon → insérer chaque courriel comme contact ; backfill website seulement
      si la page est la page propre de la boîte (own_site|facebook).
    - email_verification_source = 'reacti_discovery_own_page' si publié sur la
      page propre, sinon 'reacti_discovery_directory' (base légale honnête).
    """
    emails = discovery.get("emails") or []
    if (
        not discovery.get("found")
        or discovery.get("confidence") == "low"
        or not emails
    ):
        return DiscoveryActions(new_status="no_web_presence")

    page_kind = discovery.get("page_kind")
    discovered_url = discovery.get("discovered_url")
    website_backfill = (
        discovered_url if page_kind in ("own_site", "facebook") else None
    )

    contacts: list[dict[str, Any]] = []
    for em in emails:
        email = (em.get("email") or "").strip()
        if not email:
            continue
        own_page = bool(em.get("published_on_own_page"))
        contacts.append({
            "email": email,
            "kind": em.get("kind") or "generic",
            "source_url": em.get("source_url"),
            "email_verification_source": (
                "reacti_discovery_own_page" if own_page else "reacti_discovery_directory"
            ),
        })

    if not contacts:
        return DiscoveryActions(new_status="no_web_presence")

    return DiscoveryActions(
        new_status=None,
        website_backfill=website_backfill,
        contacts=contacts,
    )
