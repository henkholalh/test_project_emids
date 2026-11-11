"""Model price table used for per-conversation cost budgets and the cost metric (NFR-7).

Prices are USD per million tokens at Anthropic first-party API rates. Cloud-marketplace prices (for example Amazon
Bedrock) can differ, so the table can be overridden with ``LLM_PRICES_JSON`` without a code change.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .base import Usage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute TTL write, 1.25x input


DEFAULT_PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    # Haiku cache prices derived from the standard multipliers (read 0.1x, 5-minute write 1.25x input).
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
    "fake": Price(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0),
}


def price_table() -> dict[str, Price]:
    table = dict(DEFAULT_PRICES)
    override = os.environ.get("LLM_PRICES_JSON")
    if override:
        for model, p in json.loads(override).items():
            table[model] = Price(**p)
    return table


def cost_usd(model: str, usage: Usage) -> float:
    """Cost of one call. Unknown models raise, so a budget is never silently computed as zero."""
    try:
        p = price_table()[model]
    except KeyError:
        raise KeyError(f"No price configured for model '{model}'. Set LLM_PRICES_JSON.") from None
    return (usage.input_tokens * p.input + usage.output_tokens * p.output
            + usage.cache_read_tokens * p.cache_read + usage.cache_write_tokens * p.cache_write) / 1_000_000
