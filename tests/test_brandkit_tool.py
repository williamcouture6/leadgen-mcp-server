from typing import Any
import pytest
from src.tools import brand_kit as BK


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 5
    output_tokens = 9
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = _Usage()


class _Messages:
    def __init__(self, resp):
        self._resp = resp
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._resp


class _Client:
    def __init__(self, resp):
        self.messages = _Messages(resp)


def test_tool_schema_has_expected_keys():
    props = BK._BRANDKIT_TOOL["input_schema"]["properties"]
    for k in ("tagline", "logo_candidate_id", "hero_candidate_id",
              "team_photo_candidate_id", "services", "valeurs", "faq",
              "legal", "stats", "service_areas", "team", "rbq"):
        assert k in props, f"clé manquante: {k}"


def test_call_llm_forces_tool_and_returns_input(monkeypatch):
    expected = {"tagline": "Rénovation clé en main", "logo_candidate_id": 0,
                "services": [], "valeurs": [], "faq": []}
    client = _Client(_Resp([_Block(type="tool_use", name="save_brand_kit", input=expected)]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(BK, "Anthropic", lambda api_key: client)

    out = BK._call_brandkit_llm([{"id": 0, "url": "u", "kind_hint": "logo"}], "page text", "toiture")

    assert out == expected
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "save_brand_kit"}
