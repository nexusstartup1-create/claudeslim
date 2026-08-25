"""What a context choice costs, in dollars.

Every other tool in this category reports a token-count reduction, and the ones
that quote money get it by multiplying tokens by the base input price. That
multiplication assumes every token costs the same. It does not:

* a **cache read** bills at ~0.1× base input — re-reading a file Claude already
  pulled is nearly free;
* a **cache write** bills at ~1.25× — and injecting *anything* into a session
  invalidates a boundary, forcing a block rewrite whose size barely tracks the
  payload (``bench/cache_probe.py``: 5k and 30k injected were statistically
  indistinguishable);
* the same payload costs **+176% through a hook and +36% through CLAUDE.md**
  (``bench/inject_probe.py``), because one lands in the stable cached prefix
  and the other does not.

So a token count cannot tell you what a map costs. This module is the model
that can: every constant below comes from a measurement in ``bench/``, is named
after the probe that produced it, and is wrong in ways the docstrings state.

It predicts, it does not measure. `bench/ab.py` measures. Where the two
disagree, the harness wins and this file is the thing to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .tokenizer import ModelSpec, resolve_model

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "INJECTION_PLATEAU_TOKENS",
    "PRICES",
    "CostBreakdown",
    "Delivery",
    "break_even_ratio",
    "estimate",
    "exploration_tokens_needed",
]

# --------------------------------------------------------------------------- #
# Measured constants
# --------------------------------------------------------------------------- #

#: Anthropic's published cache multipliers, against base input price.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

#: Injecting through the hook costs a roughly fixed cache write whatever the
#: payload. bench/inject_probe.py: +14,258 cache-write tokens for a 5,026-token
#: payload; bench/cache_probe.py: 5k and 30k injected differed by less than the
#: run-to-run spread. Treat this as a floor with wide error bars, not a
#: precise figure — two runs put the real-map delta at +9.5k and +6.3k.
INJECTION_PLATEAU_TOKENS = 14_000

#: CLAUDE.md adds no measurable cache write: it rides a prefix Claude Code was
#: going to cache anyway. Replicated in both 12-turn A/B runs, where the
#: CLAUDE.md index arm matched the control (9,335 vs 9,356; 9,518 vs 14,044).
CLAUDE_MD_WRITE_TOKENS = 0

#: Base input price per token, USD. From the model overview table, 2026-08-25.
PRICES: dict[str, float] = {
    "claude-fable-5": 10.0 / 1_000_000,
    "claude-opus-5": 5.0 / 1_000_000,
    "claude-sonnet-5": 2.0 / 1_000_000,
    "claude-haiku-4-5-20251001": 1.0 / 1_000_000,
}
_FALLBACK_PRICE = 2.0 / 1_000_000


class Delivery(str, Enum):
    NONE = "none"
    HOOK = "hook"
    CLAUDE_MD = "claude-md"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Predicted dollars for one delivery choice over one session."""

    delivery: Delivery
    map_tokens: int
    turns: int
    write_tokens: float
    read_tokens: float
    write_usd: float
    read_usd: float

    @property
    def total_usd(self) -> float:
        return self.write_usd + self.read_usd

    @property
    def per_turn_usd(self) -> float:
        return self.total_usd / max(1, self.turns)


def _price(model: str | ModelSpec) -> float:
    spec = model if isinstance(model, ModelSpec) else resolve_model(model)
    return PRICES.get(spec.id, _FALLBACK_PRICE)


def estimate(
    map_tokens: int,
    *,
    delivery: Delivery = Delivery.CLAUDE_MD,
    turns: int = 12,
    model: str = "sonnet",
) -> CostBreakdown:
    """What carrying a map of this size costs, per session.

    The map is written once and read every turn thereafter. Which of those two
    dominates is the whole question, and it flips depending on delivery:

    * ``hook`` pays the injection plateau — a large, near-fixed cache write —
      and then reads the map each turn;
    * ``claude-md`` pays no measurable write, only the per-turn reads;
    * ``none`` pays nothing, and is the control every claim is measured against.
    """
    price = _price(model)

    if delivery is Delivery.NONE:
        return CostBreakdown(delivery, 0, turns, 0.0, 0.0, 0.0, 0.0)

    if delivery is Delivery.HOOK:
        # Flat, not max(plateau, payload). cache_probe injected 5,000 and 30,000
        # tokens and moved cache write by less than the run-to-run spread
        # (+17,384 vs +16,734): six times the content, no measurable change. The
        # write is the price of the *act*, so a smaller map does not buy a
        # smaller one, and tiering saves nothing on this delivery.
        write_tokens = float(INJECTION_PLATEAU_TOKENS)
    else:
        write_tokens = float(CLAUDE_MD_WRITE_TOKENS)

    # Both deliveries leave the map in context to be re-read each later turn,
    # at 0.1x. The term is the same for both, so it cancels when you compare
    # them — the difference between the mechanisms is the write, not the reads.
    #
    # An earlier draft modelled CLAUDE.md as re-read on *every* turn and the
    # hook as not, inferred from one arm carrying +108,653 tokens against the
    # control while the hook arm carried 9,596 fewer. That arm is the bimodal
    # one, and building an asymmetry on the arm we cannot explain would encode
    # the mystery as if it were understood. Until it is instrumented, both get
    # the same read term and the model claims only what replicated.
    read_tokens = float(map_tokens * max(0, turns - 1))

    write_usd = write_tokens * price * CACHE_WRITE_MULTIPLIER
    read_usd = read_tokens * price * CACHE_READ_MULTIPLIER
    return CostBreakdown(
        delivery, map_tokens, turns, write_tokens, read_tokens, write_usd, read_usd
    )


def break_even_ratio() -> float:
    """How much exploration a map must prevent, per token, to pay for itself.

    A freshly written token bills at 1.25×; a token Claude re-reads from cache
    bills at 0.1×. So one injected token has to displace ~12.5 cache-read
    tokens before it breaks even — the single ratio behind every result in
    ``bench/README.md``.
    """
    return CACHE_WRITE_MULTIPLIER / CACHE_READ_MULTIPLIER


def exploration_tokens_needed(map_tokens: int, delivery: Delivery) -> float:
    """Cache-read tokens a map must prevent before it is worth carrying."""
    if delivery is Delivery.NONE:
        return 0.0
    written = (
        max(INJECTION_PLATEAU_TOKENS, map_tokens)
        if delivery is Delivery.HOOK
        else CLAUDE_MD_WRITE_TOKENS
    )
    return written * break_even_ratio()
