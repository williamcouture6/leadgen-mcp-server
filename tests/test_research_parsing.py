"""Tests du parsing de sortie du Research Agent (fix tool-use).

Régression : avant le fix, Claude renvoyait du JSON en texte libre et une
guillemet non-échappée (ex: citation d'avis Google) cassait `json.loads`
(JSONDecodeError) → company coincée dans le backlog WF-3. Le fix force un
`tool_use` dont l'`input` est un dict déjà parsé par le SDK.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.tools import research


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 11
    output_tokens = 22
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 5


class _Resp:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content
        self.usage = _Usage()


class _Messages:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _Resp:
        self.last_kwargs = kwargs
        return self._resp


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self.messages = _Messages(resp)


def _patch_client(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> _Client:
    client = _Client(resp)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(research, "Anthropic", lambda api_key: client)
    return client


def test_tool_use_input_is_used_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Un payload qui aurait cassé json.loads en texte libre (guillemets internes)
    expected = {
        "company_summary": 'Spa qui dit "détente garantie" sur sa page',
        "services_offered": ["massage", "soins"],
        "personalization_hooks": ["mentionne le 4.9 ★"],
    }
    resp = _Resp([_Block(type="tool_use", name="save_research", input=expected)])
    client = _patch_client(monkeypatch, resp)

    result = research._call_llm("place", "site")

    assert result.research_json == expected
    # tool_choice doit forcer l'outil save_research
    kwargs = client.messages.last_kwargs
    assert kwargs is not None
    assert kwargs["tool_choice"] == {"type": "tool", "name": "save_research"}
    assert kwargs["tools"][0]["name"] == "save_research"
    assert result.usage.input_tokens == 11
    assert result.usage.cache_read_input_tokens == 5


def test_falls_back_to_text_parse_when_no_tool_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Si l'API ne renvoyait pas de tool_use (ne devrait pas avec tool_choice forcé),
    # on retombe sur le parsing texte historique.
    resp = _Resp([_Block(type="text", text='{"company_summary": "fallback ok"}')])
    _patch_client(monkeypatch, resp)

    result = research._call_llm("place", "site")

    assert result.research_json == {"company_summary": "fallback ok"}


def test_tool_schema_keys_match_prompt_contract() -> None:
    # Le schéma de l'outil doit exposer les clés consommées en aval par le
    # Personalization Agent — garde-fou si quelqu'un renomme un champ.
    props = research._RESEARCH_TOOL["input_schema"]["properties"]
    for key in (
        "company_summary",
        "services_offered",
        "size_signals",
        "decideur_candidats",
        "pain_points_detected",
        "recent_review_snippet",
        "tech_savvy_score",
        "form_test_hint",
        "disqualifications",
        "personalization_hooks",
    ):
        assert key in props, f"clé manquante dans le schéma tool: {key}"
