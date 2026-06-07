# mcp-server/tests/test_reacti_discover.py
from __future__ import annotations

from typing import Any

import pytest

from src.tools import reacti_discover as rd


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 10
    output_tokens = 20
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


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
    monkeypatch.setattr(rd, "Anthropic", lambda api_key: client)
    return client


def test_parses_save_discovery_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "found": True,
        "discovered_url": "https://facebook.com/deneigement-xyz",
        "page_kind": "facebook",
        "emails": [
            {
                "email": "info@deneigement-xyz.ca",
                "kind": "generic",
                "source_url": "https://facebook.com/deneigement-xyz/about",
                "published_on_own_page": True,
            }
        ],
        "confidence": "high",
        "match_reasoning": "Même ville et téléphone visibles sur la page.",
    }
    resp = _Resp([_Block(type="tool_use", name="save_discovery", input=payload)])
    client = _patch_client(monkeypatch, resp)

    result = rd._call_discovery_llm(
        name="Déneigement XYZ", city="Sherbrooke",
        address="1 rue X", phone="819-555-0000",
    )

    assert result.discovery["found"] is True
    assert result.discovery["emails"][0]["email"] == "info@deneigement-xyz.ca"
    kwargs = client.messages.last_kwargs
    assert kwargs is not None
    # web search server tool présent
    assert any(t.get("type") == "web_search_20250305" for t in kwargs["tools"])
    # tool structuré de sortie présent
    assert any(t.get("name") == "save_discovery" for t in kwargs["tools"])
    assert result.usage.input_tokens == 10


def test_no_tool_block_means_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # Si le modèle ne renvoie que du texte (pas de save_discovery), on traite
    # comme « rien trouvé » plutôt que de crasher.
    resp = _Resp([_Block(type="text", text="je n'ai rien trouvé")])
    _patch_client(monkeypatch, resp)

    result = rd._call_discovery_llm(
        name="Inconnu", city="Nulle Part", address=None, phone=None,
    )
    assert result.discovery["found"] is False
    assert result.discovery["emails"] == []


def test_truncated_tool_use_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    # stop_reason='max_tokens' = le save_discovery peut être tronqué (input partiel).
    # On ne doit JAMAIS le traiter comme une vraie trouvaille → fallback vide.
    payload = {"found": True, "confidence": "high",
               "emails": [{"email": "info@x.ca"}]}
    resp = _Resp([_Block(type="tool_use", name="save_discovery", input=payload)])
    resp.stop_reason = "max_tokens"
    _patch_client(monkeypatch, resp)

    result = rd._call_discovery_llm(
        name="Tronqué", city="Sherbrooke", address=None, phone=None,
    )
    assert result.discovery["found"] is False
    assert result.discovery["emails"] == []


def test_decide_low_confidence_marks_no_web_presence() -> None:
    discovery = {
        "found": True, "discovered_url": "https://x", "page_kind": "facebook",
        "emails": [{"email": "a@b.c", "kind": "generic",
                    "source_url": "https://x", "published_on_own_page": True}],
        "confidence": "low", "match_reasoning": "doute homonyme",
    }
    actions = rd.decide_discovery_actions(discovery)
    assert actions.new_status == "no_web_presence"
    assert actions.contacts == []
    assert actions.website_backfill is None


def test_decide_found_inserts_contact_and_backfills_website() -> None:
    discovery = {
        "found": True, "discovered_url": "https://facebook.com/xyz",
        "page_kind": "facebook",
        "emails": [{"email": "info@xyz.ca", "kind": "generic",
                    "source_url": "https://facebook.com/xyz/about",
                    "published_on_own_page": True}],
        "confidence": "high", "match_reasoning": "ok",
    }
    actions = rd.decide_discovery_actions(discovery)
    assert actions.new_status is None  # reste 'sourced'
    assert actions.website_backfill == "https://facebook.com/xyz"
    assert len(actions.contacts) == 1
    c = actions.contacts[0]
    assert c["email"] == "info@xyz.ca"
    assert c["email_verification_source"] == "reacti_discovery_own_page"
    assert c["source_url"] == "https://facebook.com/xyz/about"


def test_decide_directory_email_uses_directory_source() -> None:
    discovery = {
        "found": True, "discovered_url": "https://pages.ca/xyz",
        "page_kind": "directory",
        "emails": [{"email": "info@xyz.ca", "kind": "generic",
                    "source_url": "https://pages.ca/xyz",
                    "published_on_own_page": False}],
        "confidence": "high", "match_reasoning": "ok",
    }
    actions = rd.decide_discovery_actions(discovery)
    # annuaire tiers → pas de backfill website, source = directory
    assert actions.website_backfill is None
    assert actions.contacts[0]["email_verification_source"] == "reacti_discovery_directory"


def test_decide_not_found_marks_no_web_presence() -> None:
    actions = rd.decide_discovery_actions(dict(rd._EMPTY_DISCOVERY))
    assert actions.new_status == "no_web_presence"
    assert actions.contacts == []


def test_decide_found_but_no_emails_marks_no_web_presence() -> None:
    discovery = {**rd._EMPTY_DISCOVERY, "found": True, "confidence": "high"}
    actions = rd.decide_discovery_actions(discovery)
    assert actions.new_status == "no_web_presence"
