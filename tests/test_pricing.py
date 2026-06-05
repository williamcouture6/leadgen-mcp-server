"""Tests du calcul de coût LLM (lib/pricing)."""
from __future__ import annotations

from src.lib.pricing import estimated_cost_usd


def test_sonnet_basic_io():
    # 1M input @ $3 + 1M output @ $15 = $18.00
    cost = estimated_cost_usd(
        "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == 18.0


def test_haiku_with_date_suffix_matches():
    # suffixe de date ne casse pas le match ; 1M in @ $1 = $1.00
    cost = estimated_cost_usd(
        "claude-haiku-4-5-20251001", input_tokens=1_000_000
    )
    assert cost == 1.0


def test_cache_tokens_priced_separately():
    # sonnet : cache_write $3.75/M, cache_read $0.30/M
    cost = estimated_cost_usd(
        "claude-sonnet-4-6",
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert cost == round(3.75 + 0.30, 6)


def test_unknown_model_returns_none():
    assert estimated_cost_usd("gpt-4o", input_tokens=1000) is None


def test_zero_tokens_returns_none():
    assert estimated_cost_usd("claude-sonnet-4-6") is None
    assert estimated_cost_usd(
        "claude-sonnet-4-6", input_tokens=0, output_tokens=0
    ) is None


def test_typical_run_is_sub_cent():
    # run research typique ~3000 in / 1200 out tokens
    cost = estimated_cost_usd(
        "claude-sonnet-4-6", input_tokens=3000, output_tokens=1200
    )
    assert cost is not None
    assert 0 < cost < 0.05
