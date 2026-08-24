"""Claude-oriented token counting and context-window budgeting.

Three estimators, resolved in order of what's available:

1. :class:`AnthropicEstimator` — exact, uses ``messages.count_tokens`` (needs the
   ``anthropic`` extra and an API key). Opt-in via ``--exact``: it's a network call.
2. :class:`TiktokenEstimator` — offline BPE (``cl100k_base``) rescaled by a
   Claude calibration factor. Good to a few percent on source code.
3. :class:`HeuristicEstimator` — zero dependencies, sub-millisecond, models the
   subword behaviour of BPE on identifiers, numbers, punctuation and indentation.

All three are interchangeable through the :class:`TokenEstimator` protocol, so the
CLI/TUI never care which one is active — they just read ``estimator.name``.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, runtime_checkable

__all__ = [
    "MODELS",
    "AnthropicEstimator",
    "BudgetPlan",
    "HeuristicEstimator",
    "ModelSpec",
    "TiktokenEstimator",
    "TokenBudget",
    "TokenEstimator",
    "fit_to_budget",
    "humanize",
    "resolve_estimator",
    "resolve_model",
]


# --------------------------------------------------------------------------- #
# Model registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    alias: str
    context_window: int
    max_output: int
    family: str = "claude"

    @property
    def label(self) -> str:
        return f"{self.alias} ({self.id})"


#: Context windows are conservative defaults; override with ``--context-window``
#: when you run with an extended-context beta header.
MODELS: dict[str, ModelSpec] = {
    "opus": ModelSpec("claude-opus-5", "opus", 200_000, 64_000),
    "sonnet": ModelSpec("claude-sonnet-5", "sonnet", 200_000, 64_000),
    "haiku": ModelSpec("claude-haiku-4-5-20251001", "haiku", 200_000, 64_000),
    "fable": ModelSpec("claude-fable-5", "fable", 200_000, 64_000),
    "sonnet-1m": ModelSpec("claude-sonnet-5", "sonnet-1m", 1_000_000, 64_000),
}

DEFAULT_MODEL = "sonnet"


def resolve_model(name: str | None) -> ModelSpec:
    """Accept an alias (``sonnet``), a full model id, or ``None``."""
    if not name:
        return MODELS[DEFAULT_MODEL]
    key = name.strip().lower()
    if key in MODELS:
        return MODELS[key]
    for spec in MODELS.values():
        if spec.id == key:
            return spec
    # Unknown id: assume a standard 200k Claude context rather than failing.
    return ModelSpec(name, name, 200_000, 64_000)


# --------------------------------------------------------------------------- #
# Estimators
# --------------------------------------------------------------------------- #


@runtime_checkable
class TokenEstimator(Protocol):
    name: str
    exact: bool

    def count(self, text: str) -> int: ...


_PIECE_RE = re.compile(r"[A-Za-z]+|\d+|\s+|[^\sA-Za-z\d]")

#: Empirically-tuned scaling between the heuristic's raw piece count and the
#: Claude tokenizer. Fitted against ``cl100k_base`` over this codebase and then
#: shifted by :data:`TIKTOKEN_CALIBRATION`; residual per-file error is ~±10%.
#: Tune with ``CSLIM_CALIBRATION``.
HEURISTIC_CALIBRATION = float(os.environ.get("CSLIM_CALIBRATION", "0.83"))

#: Weight of a punctuation character. Below 1.0 because runs such as ``);``,
#: ``=>`` or ``::`` collapse into a single BPE token.
_PUNCTUATION_WEIGHT = 0.8

#: cl100k_base is close to, but not identical with, the Claude tokenizer.
TIKTOKEN_CALIBRATION = float(os.environ.get("CSLIM_TIKTOKEN_CALIBRATION", "1.08"))


class HeuristicEstimator:
    """Dependency-free BPE approximation tuned for source code.

    Models the four things that dominate token counts in code: identifier
    subword splitting, numeric literals, punctuation runs, and indentation.
    """

    name = "heuristic"
    exact = False

    def __init__(self, calibration: float = HEURISTIC_CALIBRATION) -> None:
        self.calibration = calibration

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0.0
        punctuation = 0
        for piece in _PIECE_RE.findall(text):
            head = piece[0]
            length = len(piece)
            if head.isspace():
                newlines = piece.count("\n")
                if newlines:
                    # A newline plus its indentation is normally one token.
                    total += newlines
                else:
                    # A lone separating space merges into the next token.
                    total += 0 if length == 1 else (
                        1 if length <= 3 else math.ceil(length / 4)
                    )
            elif head.isalpha():
                total += 1 if length <= 6 else 1 + math.ceil((length - 6) / 4)
            elif head.isdigit():
                total += max(1, math.ceil(length / 3))
            else:
                punctuation += 1
        total += punctuation * _PUNCTUATION_WEIGHT
        return max(1, round(total * self.calibration))


class TiktokenEstimator:
    """Offline BPE via ``tiktoken``, rescaled towards Claude's tokenizer."""

    name = "tiktoken"
    exact = False

    def __init__(self, calibration: float = TIKTOKEN_CALIBRATION) -> None:
        import tiktoken

        self._encoding = tiktoken.get_encoding("cl100k_base")
        self.calibration = calibration

    def count(self, text: str) -> int:
        if not text:
            return 0
        raw = len(self._encoding.encode(text, disallowed_special=()))
        return max(1, round(raw * self.calibration))


class AnthropicEstimator:
    """Exact counts from the Anthropic API (``messages.count_tokens``).

    One network round-trip per call, so the service layer batches whole payloads
    rather than calling it per file when ``--exact`` is on.
    """

    name = "anthropic"
    exact = True

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._model = model
        self._fallback = HeuristicEstimator()

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            response = self._client.messages.count_tokens(
                model=self._model,
                messages=[{"role": "user", "content": text}],
            )
            return int(response.input_tokens)
        except Exception:
            return self._fallback.count(text)


@lru_cache(maxsize=8)
def resolve_estimator(
    *, exact: bool = False, model: str = "claude-sonnet-5", allow_tiktoken: bool = True
) -> TokenEstimator:
    """Pick the best estimator available, degrading silently."""
    if exact:
        try:
            return AnthropicEstimator(model=model)
        except Exception:
            pass
    if allow_tiktoken:
        try:
            return TiktokenEstimator()
        except Exception:
            pass
    return HeuristicEstimator()


# --------------------------------------------------------------------------- #
# Budgeting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """How many tokens the compressed payload is allowed to occupy."""

    model: ModelSpec
    context_window: int = 0
    reserve_output: int = 8_000
    """Room left for Claude's reply."""
    reserve_prompt: int = 4_000
    """Room left for the system prompt, tools and your actual question."""
    hard_limit: int | None = None
    """Explicit ``--max-tokens`` ceiling; wins over the derived value."""

    @classmethod
    def for_model(
        cls,
        model: ModelSpec,
        *,
        max_tokens: int | None = None,
        context_window: int | None = None,
    ) -> TokenBudget:
        return cls(
            model=model,
            context_window=context_window or model.context_window,
            hard_limit=max_tokens,
        )

    @property
    def window(self) -> int:
        return self.context_window or self.model.context_window

    @property
    def usable(self) -> int:
        derived = max(1_000, self.window - self.reserve_output - self.reserve_prompt)
        if self.hard_limit is not None:
            return min(derived, max(1, self.hard_limit))
        return derived

    def pct(self, tokens: int) -> float:
        return tokens / self.window if self.window else 0.0


@dataclass(slots=True)
class BudgetPlan:
    limit: int
    kept: list[int]
    """Indices of the items that fit."""
    dropped: list[int]
    total: int

    @property
    def fits(self) -> bool:
        return not self.dropped


def fit_to_budget(
    costs: Sequence[int], limit: int, *, overhead: int = 0
) -> BudgetPlan:
    """Greedy first-fit in the caller's order (relevance order beats raw size).

    Items are consumed in sequence; the first one that would blow the budget and
    every item after it are reported as dropped, so the payload stays a coherent
    prefix rather than a shuffled subset.
    """
    kept: list[int] = []
    dropped: list[int] = []
    running = overhead
    for index, cost in enumerate(costs):
        if dropped or running + cost > limit:
            dropped.append(index)
            continue
        running += cost
        kept.append(index)
    return BudgetPlan(limit=limit, kept=kept, dropped=dropped, total=running)


def humanize(count: int) -> str:
    """Compact token counts: ``1_234`` -> ``1.2k``."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.2f}M"
