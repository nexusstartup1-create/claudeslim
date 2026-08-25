"""Shared, dependency-free data model for ClaudeSlim.

Everything in :mod:`cslim.core` speaks these types. The CLI (Typer/Rich) and the
TUI (Textual) are pure consumers: they never reach into parsing internals, they
only build a :class:`CompressionOptions` / :class:`DiscoveryOptions` pair and read
back a :class:`Bundle`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Literal

__all__ = [
    "EXTENSION_MAP",
    "Bundle",
    "CompressedFile",
    "CompressionOptions",
    "DocstringMode",
    "Language",
    "Symbol",
    "SymbolKind",
    "TokenStats",
    "detect_language",
]


class Language(str, Enum):
    """Languages with a dedicated skeleton extractor (plus generic fallbacks)."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    MARKDOWN = "markdown"
    TEXT = "text"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return self.value


EXTENSION_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".d.ts": Language.TYPESCRIPT,
    ".go": Language.GO,
    ".md": Language.MARKDOWN,
    ".mdx": Language.MARKDOWN,
    ".rst": Language.TEXT,
    ".txt": Language.TEXT,
    ".toml": Language.TEXT,
    ".cfg": Language.TEXT,
    ".ini": Language.TEXT,
    ".yaml": Language.TEXT,
    ".yml": Language.TEXT,
    ".json": Language.TEXT,
    ".sql": Language.TEXT,
    ".sh": Language.TEXT,
}

#: Extensions considered "source code" — the default discovery filter.
CODE_EXTENSIONS: frozenset[str] = frozenset(
    ext for ext, lang in EXTENSION_MAP.items()
    if lang in (Language.PYTHON, Language.JAVASCRIPT, Language.TYPESCRIPT, Language.GO)
)


def detect_language(path: Path | str) -> Language:
    """Map a file path to a :class:`Language` (extension based, cheap and total)."""
    name = Path(path).name.lower()
    if name.endswith(".d.ts"):
        return Language.TYPESCRIPT
    suffix = Path(name).suffix
    return EXTENSION_MAP.get(suffix, Language.UNKNOWN)


class SymbolKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    INTERFACE = "interface"
    STRUCT = "struct"
    TYPE = "type"
    ENUM = "enum"
    CONSTANT = "constant"
    VARIABLE = "variable"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A single public API element surviving compression."""

    name: str
    kind: SymbolKind
    line: int = 0
    signature: str = ""


DocstringMode = Literal["full", "summary", "none"]
RenderFormat = Literal["md", "plain", "xml"]


@dataclass(frozen=True, slots=True)
class CompressionOptions:
    """Knobs controlling how much of the skeleton survives.

    ``aggressive`` is a preset: call :meth:`resolved` before handing the options
    to a compressor so every backend sees the same effective configuration.
    """

    docstrings: DocstringMode = "summary"
    docstring_max_chars: int = 160
    keep_imports: bool = True
    keep_private: bool = False
    keep_constants: bool = True
    keep_decorators: bool = True
    keep_comments: bool = False
    """Keep leading doc-comments (``/** */``, ``///``) in brace languages."""
    keep_type_annotations: bool = True
    body_placeholder: str = "..."
    max_value_len: int = 80
    """Assignments whose right-hand side is longer than this collapse to ``...``."""
    aggressive: bool = False

    def resolved(self) -> CompressionOptions:
        if not self.aggressive:
            return self
        return replace(
            self,
            docstrings="none",
            keep_imports=False,
            keep_private=False,
            keep_constants=False,
            keep_decorators=False,
            keep_comments=False,
            max_value_len=24,
            aggressive=False,
        )


class DetailLevel(str, Enum):
    """How much of a file makes it into the payload.

    The whole point of tiering: a project's core modules earn a full skeleton,
    everything else earns one line naming what it offers. That is what lets a
    500-file repository fit in a budget at all.
    """

    SKELETON = "skeleton"
    """Signatures, types, class hierarchies — everything but the bodies."""
    OUTLINE = "outline"
    """Names only, grouped: imports, env vars, constants, classes, functions.

    The middle tier. An index line answers "which file?", a skeleton answers
    "what is the signature?", and between them sits "what does this module
    contain?" — which is the question you actually ask while orienting.
    """
    INDEX = "index"
    OMITTED = "omitted"


@dataclass(slots=True)
class CompressedFile:
    """Result of compressing one file."""

    path: Path
    rel_path: str
    language: Language
    skeleton: str
    symbols: list[Symbol] = field(default_factory=list)
    original_chars: int = 0
    original_lines: int = 0
    original_tokens: int = 0
    compressed_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    fallback: bool = False
    """True when no AST backend matched and a text-level cleanup was used."""
    included: bool = True
    """False when the file was dropped to honour a token budget."""
    level: DetailLevel = DetailLevel.SKELETON
    """How this file is rendered — set by the budget allocator."""
    index_line: str = ""
    """One-line summary: what this file offers, without any signatures."""
    index_tokens: int = 0
    env_vars: list[str] = field(default_factory=list)
    """Environment variables the file reads.

    Extracted from the *source*, not the skeleton: `os.environ[...]` lives
    inside a function body, and the skeleton is exactly what has no bodies. An
    outline that scanned the skeleton would show an empty env section forever.
    """
    outline: str = ""
    """Grouped names — imports, env, constants, classes, functions."""
    outline_tokens: int = 0
    rank: float = 0.0
    """Architectural importance from :mod:`cslim.core.ranking`."""

    @property
    def emitted_tokens(self) -> int:
        """Tokens this file actually costs at its assigned detail level."""
        if not self.included or self.level is DetailLevel.OMITTED:
            return 0
        if self.level is DetailLevel.INDEX:
            return self.index_tokens
        if self.level is DetailLevel.OUTLINE:
            return self.outline_tokens
        return self.compressed_tokens

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compressed_tokens)

    @property
    def ratio(self) -> float:
        """Fraction of tokens removed, in ``[0, 1]``."""
        if self.original_tokens <= 0:
            return 0.0
        return self.saved_tokens / self.original_tokens


@dataclass(frozen=True, slots=True)
class TokenStats:
    original: int = 0
    compressed: int = 0
    files: int = 0
    indexed_files: int = 0
    """Files rendered as a one-line index rather than a full skeleton."""
    outlined_files: int = 0
    """Files rendered as a grouped outline."""
    dropped_files: int = 0
    model_id: str = ""
    context_window: int = 0
    exact: bool = False

    @property
    def saved(self) -> int:
        return max(0, self.original - self.compressed)

    @property
    def ratio(self) -> float:
        if self.original <= 0:
            return 0.0
        return self.saved / self.original

    @property
    def context_pct(self) -> float:
        """Share of the model context window taken by the compressed payload."""
        if self.context_window <= 0:
            return 0.0
        return self.compressed / self.context_window

    @property
    def original_context_pct(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return self.original / self.context_window


@dataclass(slots=True)
class Bundle:
    """Everything the CLI/TUI needs to render or ship a compression run."""

    files: list[CompressedFile] = field(default_factory=list)
    stats: TokenStats = field(default_factory=TokenStats)
    root: Path = field(default_factory=Path.cwd)
    options: CompressionOptions = field(default_factory=CompressionOptions)
    warnings: list[str] = field(default_factory=list)
    payload: str = ""
    """Rendered text, ready to be piped to Claude Code."""

    @property
    def included(self) -> list[CompressedFile]:
        return [f for f in self.files if f.included]

    @property
    def dropped(self) -> list[CompressedFile]:
        return [f for f in self.files if not f.included]

    def symbols(self) -> list[Symbol]:
        out: list[Symbol] = []
        for f in self.included:
            out.extend(f.symbols)
        return out

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable view (used by ``cslim --json`` and by the TUI)."""
        return {
            "root": str(self.root),
            "stats": {
                "original_tokens": self.stats.original,
                "compressed_tokens": self.stats.compressed,
                "saved_tokens": self.stats.saved,
                "ratio": round(self.stats.ratio, 4),
                "files": self.stats.files,
                "dropped_files": self.stats.dropped_files,
                "model": self.stats.model_id,
                "context_window": self.stats.context_window,
                "context_pct": round(self.stats.context_pct, 4),
                "exact": self.stats.exact,
            },
            "files": [
                {
                    "path": f.rel_path,
                    "language": f.language.value,
                    "original_tokens": f.original_tokens,
                    "compressed_tokens": f.compressed_tokens,
                    "ratio": round(f.ratio, 4),
                    "symbols": len(f.symbols),
                    "included": f.included,
                    "fallback": f.fallback,
                    "errors": f.errors,
                }
                for f in self.files
            ],
            "warnings": self.warnings,
            "payload": self.payload,
        }


def sum_stats(
    files: Sequence[CompressedFile],
    *,
    model_id: str = "",
    context_window: int = 0,
    exact: bool = False,
) -> TokenStats:
    included = [f for f in files if f.included]
    return TokenStats(
        original=sum(f.original_tokens for f in included),
        compressed=sum(f.emitted_tokens for f in included),
        files=len(included),
        indexed_files=sum(1 for f in included if f.level is DetailLevel.INDEX),
        dropped_files=len(files) - len(included),
        model_id=model_id,
        context_window=context_window,
        exact=exact,
    )
