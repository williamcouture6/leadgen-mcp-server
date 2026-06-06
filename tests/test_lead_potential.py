"""Tests de l'extraction du score de potentiel du lead (research_json -> colonnes flat).

Pas de network ni de DB : on teste seulement la fonction pure
`extract_lead_potential_patch`, qui décide quelles colonnes `companies` patcher
à partir du `research_json` renvoyé par le Research Agent.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-google-key")
    from src.config import settings
    settings.cache_clear()


def _extract(rj):
    from src.tools.db import extract_lead_potential_patch
    return extract_lead_potential_patch(rj)


def test_score_valide_ecrit_les_deux_colonnes() -> None:
    patch = _extract({"lead_potential": {"score": 72, "reasoning": "grosse base réactivable"}})
    assert patch == {
        "lead_potential_score": 72,
        "lead_potential_reason": "grosse base réactivable",
    }


def test_bornes_inclusives() -> None:
    assert _extract({"lead_potential": {"score": 0}}) == {"lead_potential_score": 0}
    assert _extract({"lead_potential": {"score": 100}}) == {"lead_potential_score": 100}


def test_score_hors_borne_ignore() -> None:
    assert _extract({"lead_potential": {"score": 101}}) == {}
    assert _extract({"lead_potential": {"score": -1}}) == {}


def test_score_absent_ou_non_entier_ignore() -> None:
    assert _extract({"lead_potential": {"reasoning": "x"}}) == {}
    assert _extract({"lead_potential": {"score": "72"}}) == {}
    assert _extract({"lead_potential": {"score": 72.5}}) == {}
    assert _extract({"lead_potential": {"score": True}}) == {}  # bool n'est pas un score


def test_lead_potential_absent_ou_malforme() -> None:
    assert _extract({}) == {}
    assert _extract({"lead_potential": None}) == {}
    assert _extract({"lead_potential": "nope"}) == {}
    assert _extract(None) == {}


def test_reasoning_tronque_a_500() -> None:
    long = "x" * 600
    patch = _extract({"lead_potential": {"score": 50, "reasoning": long}})
    assert patch["lead_potential_score"] == 50
    assert len(patch["lead_potential_reason"]) == 500


def test_reasoning_non_string_ignore_mais_score_garde() -> None:
    patch = _extract({"lead_potential": {"score": 50, "reasoning": 123}})
    assert patch == {"lead_potential_score": 50}
