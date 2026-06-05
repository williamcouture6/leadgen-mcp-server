"""Estimation du coût LLM par run d'agent.

Tarifs Anthropic publics (USD par million de tokens). On matche le modèle par
sous-chaîne pour être robuste aux suffixes de date (ex.
`claude-haiku-4-5-20251001`). Si le modèle est inconnu → on retourne None
plutôt que d'inventer un prix (le champ reste NULL = signal honnête).

Convention de l'API Anthropic : `input_tokens` rapporté par `usage` EXCLUT déjà
les tokens de cache ; `cache_read_input_tokens` et
`cache_creation_input_tokens` sont comptés à part avec leurs propres tarifs.
"""
from __future__ import annotations

# (input, output, cache_write_5min, cache_read) en USD / million de tokens.
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.00, 75.00, 18.75, 1.50),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "haiku": (1.00, 5.00, 1.25, 0.10),
}


def _rates(model: str) -> tuple[float, float, float, float] | None:
    m = (model or "").lower()
    for key, rates in _PRICES.items():
        if key in m:
            return rates
    return None


def estimated_cost_usd(
    model: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
) -> float | None:
    """Coût estimé d'un appel, ou None si modèle inconnu ou aucun token.

    Arrondi à 6 décimales (micro-dollars) — un run coûte typiquement < 0.01 $.
    """
    rates = _rates(model)
    if rates is None:
        return None
    in_rate, out_rate, cache_w_rate, cache_r_rate = rates
    ti = input_tokens or 0
    to = output_tokens or 0
    tcr = cache_read_tokens or 0
    tcw = cache_creation_tokens or 0
    if ti == to == tcr == tcw == 0:
        return None
    cost = (
        ti * in_rate
        + to * out_rate
        + tcw * cache_w_rate
        + tcr * cache_r_rate
    ) / 1_000_000
    return round(cost, 6)
