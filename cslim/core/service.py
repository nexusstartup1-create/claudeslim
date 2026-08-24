"""Orchestration facade — the single entry point for *both* frontends.

``cli.py`` (Typer/Rich) and the Textual TUI call exactly the same method:

    service = CompressionService(progress=my_callback)
    bundle = service.run(CompressRequest(paths=(Path("src"),), ...))

Nothing here prints, exits, or touches a terminal, which is what makes the TUI
integration trivial: hand it a callback, run it in a worker thread, read the
returned :class:`Bundle`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .compressor import compress_source
from .discovery import DiscoveryOptions, discover
from .models import (
    Bundle,
    CompressedFile,
    CompressionOptions,
    DetailLevel,
    sum_stats,
)
from .ranking import rank_files
from .renderer import RenderOptions, build_index_line, render_bundle
from .tokenizer import (
    ModelSpec,
    TokenBudget,
    TokenEstimator,
    fit_to_budget,
    resolve_estimator,
    resolve_model,
)

__all__ = ["CompressRequest", "CompressionService", "ProgressCallback", "compress_paths"]


class ProgressCallback(Protocol):
    def __call__(self, done: int, total: int, current: str) -> None: ...


@dataclass(slots=True)
class CompressRequest:
    paths: tuple[Path, ...] = ()
    discovery: DiscoveryOptions = field(default_factory=DiscoveryOptions)
    compression: CompressionOptions = field(default_factory=CompressionOptions)
    render: RenderOptions = field(default_factory=RenderOptions)
    model: str = "sonnet"
    max_tokens: int | None = None
    context_window: int | None = None
    exact_tokens: bool = False
    render_payload: bool = True
    """Set False for a stats-only pass (``cslim stats``)."""
    index_only: bool = False
    """Emit only the index tier — no skeletons at all.

    Under Anthropic's cache pricing a freshly injected token costs roughly what
    twelve cache-read tokens cost, so a map only pays for itself if it is far
    smaller than the exploration it prevents. This is the cheapest map cslim can
    produce: typically a few hundred tokens for a mid-sized project.
    """


class CompressionService:
    """Stateless orchestrator: discover → compress → count → budget → render."""

    def __init__(
        self,
        *,
        estimator: TokenEstimator | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._estimator = estimator
        self._progress = progress

    def estimator_for(self, request: CompressRequest) -> TokenEstimator:
        if self._estimator is not None:
            return self._estimator
        return resolve_estimator(
            exact=request.exact_tokens, model=resolve_model(request.model).id
        )

    def _emit(self, done: int, total: int, current: str) -> None:
        if self._progress is not None:
            self._progress(done, total, current)

    def run(self, request: CompressRequest) -> Bundle:
        model: ModelSpec = resolve_model(request.model)
        estimator = self.estimator_for(request)
        options = request.compression.resolved()

        discovered, root, warnings = discover(list(request.paths), request.discovery)
        bundle = Bundle(root=root, options=options, warnings=list(warnings))
        total = len(discovered)

        for index, item in enumerate(discovered, 1):
            self._emit(index, total, item.rel_path)
            try:
                source = item.path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                bundle.warnings.append(f"{item.rel_path}: {exc}")
                continue
            result = compress_source(source, item.path, item.language, options)
            compressed = CompressedFile(
                path=item.path,
                rel_path=item.rel_path,
                language=item.language,
                skeleton=result.text,
                symbols=result.symbols,
                original_chars=len(source),
                original_lines=source.count("\n") + 1,
                original_tokens=estimator.count(source),
                compressed_tokens=estimator.count(result.text),
                errors=result.errors,
                fallback=result.fallback,
            )
            bundle.files.append(compressed)

        # Rank and index every file: both are needed before the budget can
        # decide who earns a skeleton.
        for file, file_rank in zip(
            sorted(bundle.files, key=lambda f: f.rel_path),
            sorted(rank_files(bundle.files), key=lambda r: r.rel_path),
            strict=True,
        ):
            file.rank = file_rank.score
        for file in bundle.files:
            file.index_line = build_index_line(file)
            file.index_tokens = estimator.count(file.index_line)

        budget = TokenBudget.for_model(
            model, max_tokens=request.max_tokens, context_window=request.context_window
        )
        if request.index_only:
            for file in bundle.files:
                file.level = DetailLevel.INDEX
        elif request.max_tokens is not None:
            self._apply_budget(bundle, budget)

        bundle.stats = sum_stats(
            bundle.files,
            model_id=model.id,
            context_window=budget.window,
            exact=estimator.exact,
        )
        if request.render_payload:
            bundle.payload = render_bundle(bundle, request.render)
        return bundle

    @staticmethod
    def _apply_budget(bundle: Bundle, budget: TokenBudget) -> None:
        """Spend the budget where it explains the most.

        Three tiers instead of one cliff edge:

        * every file gets at least its index line — a project map with no holes;
        * the most referenced files get upgraded to a full skeleton, richest
          first, until the budget runs out;
        * only if even the index doesn't fit do files get dropped entirely.

        That's what makes a large repository representable at all: the old
        approach simply truncated the tail and left Claude blind to it.
        """
        limit = budget.usable
        files = bundle.files
        if not files:
            return

        index_cost = sum(f.index_tokens for f in files)
        overhead = 200

        # Worst case: not even the index fits. Drop the least important tail.
        if index_cost + overhead > limit:
            order = sorted(range(len(files)), key=lambda i: -files[i].rank)
            plan = fit_to_budget(
                [files[i].index_tokens for i in order], limit, overhead=overhead
            )
            for position in plan.dropped:
                files[order[position]].included = False
            for position in plan.kept:
                files[order[position]].level = DetailLevel.INDEX
            bundle.warnings.append(
                f"{len(plan.dropped)} file(s) dropped: even an index line each "
                f"exceeds {limit} tokens"
            )
            return

        # Everything is at least indexed; now upgrade what we can afford.
        for file in files:
            file.level = DetailLevel.INDEX
        spent = index_cost + overhead

        # Order by *value per token*, not by raw rank. Ranking alone would let
        # one expensive top-ranked file eat 85% of a tight budget while three
        # nearly-as-important files got nothing — this is the standard greedy
        # approximation for a knapsack, and it covers far more ground.
        def density(file: CompressedFile) -> float:
            extra = max(1, file.compressed_tokens - file.index_tokens)
            return file.rank / extra

        upgradable = sorted(
            (f for f in files if f.compressed_tokens > f.index_tokens),
            key=lambda f: -density(f),
        )
        upgraded = 0
        for file in upgradable:
            extra = file.compressed_tokens - file.index_tokens
            if spent + extra > limit:
                continue  # try the next one: a cheaper file may still fit
            file.level = DetailLevel.SKELETON
            spent += extra
            upgraded += 1

        if upgraded < len(upgradable):
            bundle.warnings.append(
                f"{len(upgradable) - upgraded} file(s) kept as index lines to "
                f"stay under {limit} tokens"
            )


def compress_paths(
    paths: Sequence[Path | str],
    *,
    compression: CompressionOptions | None = None,
    discovery: DiscoveryOptions | None = None,
    render: RenderOptions | None = None,
    model: str = "sonnet",
    max_tokens: int | None = None,
    index_only: bool = False,
    progress: ProgressCallback | None = None,
) -> Bundle:
    """One-call convenience API (used by the TUI and by library consumers)."""
    request = CompressRequest(
        paths=tuple(Path(p) for p in paths),
        discovery=discovery or DiscoveryOptions(),
        compression=compression or CompressionOptions(),
        render=render or RenderOptions(),
        model=model,
        max_tokens=max_tokens,
        index_only=index_only,
    )
    return CompressionService(progress=progress).run(request)
