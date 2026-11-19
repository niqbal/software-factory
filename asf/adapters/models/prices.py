"""List prices for Claude models, and the cost of one call from its token counts.

Shared by the API provider and the Claude Code providers. Claude Code writes its prompt cache
with the 1-hour TTL, billed at 2x input; the API provider uses the 5-minute TTL (1.25x input).
"""

from __future__ import annotations

# $ per million tokens: input, output, cache read, cache write (5 minute TTL = 1.25x input)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}


def cost_of(model: str, usage: dict[str, int], hour_cache: bool = False) -> float:
    """Dollars for one call. ``hour_cache``: cache writes at the 1-hour rate (Claude Code)."""
    pin, pout, pread, pwrite = PRICES.get(model.split(":")[-1], PRICES["claude-opus-5-5"])
    if hour_cache:
        pwrite = 2 * pin
    return (usage.get("input", 0) * pin + usage.get("output", 0) * pout
            + usage.get("cache_read", 0) * pread + usage.get("cache_write", 0) * pwrite) / 1_000_000
