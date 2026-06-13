"""Tests pour la validation des env vars au démarrage (audit #10)."""
from __future__ import annotations

import pytest

from src import config


def test_validate_env_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in config.REQUIRED_ENV + config.RECOMMENDED_ENV:
        monkeypatch.setenv(name, "x")
    res = config.validate_env()
    assert res["missing_required"] == []
    assert res["missing_recommended"] == []


def test_validate_env_flags_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in config.REQUIRED_ENV + config.RECOMMENDED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("AGENTS_HTTP_TOKEN", raising=False)
    res = config.validate_env()
    assert "AGENTS_HTTP_TOKEN" in res["missing_required"]
    assert res["missing_recommended"] == []


def test_validate_env_flags_missing_recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in config.REQUIRED_ENV + config.RECOMMENDED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("INSTANTLY_CAMPAIGN_ID", raising=False)
    res = config.validate_env()
    assert res["missing_required"] == []
    assert "INSTANTLY_CAMPAIGN_ID" in res["missing_recommended"]


def test_validate_env_blank_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Une var vide ou blanche = manquante (pas juste absente)."""
    for name in config.REQUIRED_ENV + config.RECOMMENDED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    res = config.validate_env()
    assert "ANTHROPIC_API_KEY" in res["missing_recommended"]


def test_pexels_key_is_recommended_not_required():
    from src import config
    assert "PEXELS_API_KEY" in config.RECOMMENDED_ENV
    assert "PEXELS_API_KEY" not in config.REQUIRED_ENV
