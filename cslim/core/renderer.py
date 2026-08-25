"""Payload rendering — how the skeletons are laid out for the model.

Three formats:

* ``md``    — fenced code blocks with a language tag (best default for Claude Code).
* ``plain`` — minimal ``==== path ====`` separators, lowest overhead.
* ``xml``   — ``<file path=…>`` tags; easiest for Claude to reference precisely.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .models import (
    Bundle,
    CompressedFile,
    DetailLevel,
    Language,
    RenderFormat,
    SymbolKind,
    TokenStats,
)
from .tokenizer import humanize

__all__ = [
    "RenderOptions",
    "build_index_line",
    "build_outline",
    "environment_variables",
    "file_tree",
    "render_bundle",
    "render_header",
    "render_outline",
]

_FENCE_LANG: dict[Language, str] = {
    Language.PYTHON: "python",
    Language.JAVASCRIPT: "javascript",
    Language.TYPESCRIPT: "typescript",
    Language.GO: "go",
    Language.MARKDOWN: "markdown",
    Language.TEXT: "text",
    Language.UNKNOWN: "text",
}


#: Symbol kinds worth naming in a one-line index. Imports describe what a file
#: consumes; the index is about what it offers.
_INDEX_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.FUNCTION,
        SymbolKind.INTERFACE,
        SymbolKind.STRUCT,
        SymbolKind.TYPE,
        SymbolKind.ENUM,
        SymbolKind.CONSTANT,
    }
)


def build_index_line(file: CompressedFile, max_symbols: int = 14) -> str:
    """One line naming what a file offers — no signatures, no bodies.

    This is the cheap tier: roughly 10-20× smaller than a skeleton, and still
    enough for Claude to answer "which file should I open?".
    """
    seen: list[str] = []
    for symbol in file.symbols:
        # `__all__` / `__version__` are boilerplate: naming them in an index
        # spends tokens telling Claude nothing it couldn't assume.
        if symbol.name.startswith("__"):
            continue
        if symbol.kind in _INDEX_KINDS and symbol.name not in seen:
            seen.append(symbol.name)
    if not seen:
        return f"{file.rel_path}"
    shown = seen[:max_symbols]
    suffix = f", +{len(seen) - len(shown)} more" if len(seen) > len(shown) else ""
    return f"{file.rel_path}: {', '.join(shown)}{suffix}"


#: Where configuration comes from, across the languages cslim parses. Env vars
#: answer "how is this deployed" and are invisible in a signature-only skeleton,
#: so the outline names them separately.
_ENV_PATTERNS = (
    re.compile(r"""os\.environ(?:\.get)?\s*[\[(]\s*["']([A-Z_][A-Z0-9_]*)["']"""),
    re.compile(r"""os\.getenv\s*\(\s*["']([A-Z_][A-Z0-9_]*)["']"""),
    re.compile(r"""process\.env(?:\.([A-Z_][A-Z0-9_]*)|\[\s*["']([A-Z_][A-Z0-9_]*)["'])"""),
    re.compile(r"""os\.Getenv\s*\(\s*["']([A-Z_][A-Z0-9_]*)["']"""),
)

_OUTLINE_GROUPS: tuple[tuple[str, frozenset[SymbolKind]], ...] = (
    ("classes", frozenset({SymbolKind.CLASS, SymbolKind.STRUCT})),
    ("interfaces", frozenset({SymbolKind.INTERFACE, SymbolKind.TYPE, SymbolKind.ENUM})),
    ("functions", frozenset({SymbolKind.FUNCTION})),
    ("constants", frozenset({SymbolKind.CONSTANT})),
)


def environment_variables(text: str) -> list[str]:
    """Environment variables a file reads, in order of first appearance."""
    found: list[str] = []
    for pattern in _ENV_PATTERNS:
        for match in pattern.finditer(text):
            name = next((g for g in match.groups() if g), None)
            if name and name not in found:
                found.append(name)
    return found


def build_outline(file: CompressedFile, max_per_group: int = 24) -> str:
    """A file's contents as grouped names — no signatures, no bodies.

    Between an index line and a skeleton: it answers "what is in this module",
    which is the question you ask while orienting, without paying for the
    parameter lists you only need once you have chosen the file.
    """
    lines: list[str] = [file.rel_path]

    # `from flask import a` and `from flask import b` are two symbols naming one
    # module; listing it twice spends tokens saying nothing.
    imports = list(
        dict.fromkeys(s.name for s in file.symbols if s.kind is SymbolKind.IMPORT)
    )
    if imports:
        lines.append(f"  imports: {_join(imports, max_per_group)}")

    env = file.env_vars or environment_variables(file.skeleton)
    if env:
        lines.append(f"  env: {_join(env, max_per_group)}")

    for label, kinds in _OUTLINE_GROUPS:
        names: list[str] = []
        for symbol in file.symbols:
            if (
                symbol.kind in kinds
                and not symbol.name.startswith("__")
                and symbol.name not in names
            ):
                names.append(symbol.name)
        if names:
            lines.append(f"  {label}: {_join(names, max_per_group)}")

    # Methods are listed under their file rather than their class: the outline
    # is a lookup aid, and "which file has `refresh`" is the question it serves.
    methods = [
        s.name for s in file.symbols
        if s.kind is SymbolKind.METHOD and not s.name.startswith("__")
    ]
    unique_methods = list(dict.fromkeys(methods))
    if unique_methods:
        lines.append(f"  methods: {_join(unique_methods, max_per_group)}")

    return "\n".join(lines) if len(lines) > 1 else file.rel_path


def _join(names: list[str], limit: int) -> str:
    shown = names[:limit]
    suffix = f", +{len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return ", ".join(shown) + suffix


_OUTLINE_PREAMBLE = (
    "Files as grouped names — imports, environment variables, constants, "
    "classes, functions, methods. No signatures: open the file once one of "
    "these names is what you need."
)


def render_outline(
    files: Sequence[CompressedFile], fmt: RenderFormat, *, note: bool = True
) -> str:
    """The middle tier: grouped names, one block per file."""
    blocks = [f.outline or f.rel_path for f in files]
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    if fmt == "xml":
        attrs = f' note="{_OUTLINE_PREAMBLE}"' if note else ""
        return f"<outline{attrs}>\n{body}\n</outline>"
    heading = f"## Outline\n{_OUTLINE_PREAMBLE}\n\n" if note else ""
    return f"{heading}```\n{body}\n```"


@dataclass(frozen=True, slots=True)
class RenderOptions:
    format: RenderFormat = "md"
    header: bool = True
    """Emit the legend that tells Claude what `...` means."""
    tree: bool = False
    """Prepend a compact file tree."""
    stats_comment: bool = True
    instructions: str = ""
    """Free-form text injected right after the header (e.g. your question)."""


_LEGEND = (
    "Compressed with ClaudeSlim: function/method bodies are replaced by `...` "
    "(`{ ... }` in brace languages). Signatures, types, class hierarchies, "
    "imports, constants and summary docstrings are preserved verbatim. "
    "Ask for a specific file if you need a full body."
)


_INDEX_LEGEND = (
    "Ultralight map from ClaudeSlim: one line per file naming what it defines. "
    "No signatures, no types, no bodies. Use it to pick which file to open, "
    "then read that file in full."
)


_OUTLINE_LEGEND = (
    "Outline map from ClaudeSlim: each file's imports, environment variables, "
    "constants, classes, functions and methods, by name. No signatures and no "
    "bodies — open a file once one of these names is what you need."
)


def render_header(
    stats: TokenStats,
    options: RenderOptions,
    *,
    index_only: bool = False,
    outline_only: bool = False,
) -> str:
    # The legend must describe what's actually below it: promising preserved
    # signatures in a payload that has none would send Claude looking for
    # information that isn't there.
    if index_only:
        lines = ["# ClaudeSlim file index", f"# {_INDEX_LEGEND}"]
    elif outline_only:
        lines = ["# ClaudeSlim outline", f"# {_OUTLINE_LEGEND}"]
    else:
        lines = ["# ClaudeSlim skeleton bundle", f"# {_LEGEND}"]
    if options.stats_comment:
        saved = f"{stats.ratio * 100:.1f}%"
        detail = (
            f"# {stats.files} file{'s' if stats.files != 1 else ''} · "
            f"{humanize(stats.original)} → "
            f"{humanize(stats.compressed)} tokens (-{saved})"
        )
        if stats.context_window:
            detail += f" · {stats.context_pct * 100:.1f}% of {humanize(stats.context_window)} ctx"
        if not stats.exact:
            detail += " · estimated"
        lines.append(detail)
        if stats.dropped_files:
            lines.append(f"# {stats.dropped_files} file(s) dropped to fit the token budget")
    if options.instructions:
        lines.append("")
        lines.append(options.instructions.strip())
    return "\n".join(lines)


def file_tree(files: Sequence[CompressedFile]) -> str:
    """Compact, indentation-free tree: one path per line, grouped by directory."""
    lines = ["# Files"]
    current_dir = object()
    for f in files:
        parts = f.rel_path.rsplit("/", 1)
        directory = parts[0] if len(parts) == 2 else "."
        name = parts[-1]
        if directory != current_dir:
            lines.append(f"# {directory}/")
            current_dir = directory
        lines.append(f"#   {name}  ({humanize(f.compressed_tokens)}t)")
    return "\n".join(lines)


def _render_file(file: CompressedFile, fmt: RenderFormat) -> str:
    body = file.skeleton.strip()
    if not body:
        return ""
    if fmt == "xml":
        attrs = f'path="{file.rel_path}" lang="{file.language.value}"'
        return f"<file {attrs}>\n{body}\n</file>"
    if fmt == "plain":
        return f"==== {file.rel_path} ====\n{body}"
    fence = _FENCE_LANG.get(file.language, "text")
    return f"## {file.rel_path}\n```{fence}\n{body}\n```"


_INDEX_PREAMBLE = (
    "Remaining files, one line each: `path: what it defines`. "
    "No signatures — open the file if one of these names is what you need."
)


def render_index(
    files: Sequence[CompressedFile], fmt: RenderFormat, *, note: bool = True
) -> str:
    """The cheap tier: every peripheral file in one line apiece."""
    lines = [f.index_line or f.rel_path for f in files]
    if not lines:
        return ""
    body = "\n".join(lines)
    if fmt == "xml":
        attrs = f' note="{_INDEX_PREAMBLE}"' if note else ""
        return f"<index{attrs}>\n{body}\n</index>"
    heading = f"## Index\n{_INDEX_PREAMBLE}\n\n" if note else ""
    return f"{heading}```\n{body}\n```"


def render_bundle(bundle: Bundle, options: RenderOptions | None = None) -> str:
    """Turn a compressed bundle into the exact text handed to Claude."""
    opts = options or RenderOptions()
    included = bundle.included
    skeletons = [f for f in included if f.level is DetailLevel.SKELETON]
    outlined = [f for f in included if f.level is DetailLevel.OUTLINE]
    indexed = [f for f in included if f.level is DetailLevel.INDEX]

    index_only = bool(included) and not skeletons and not outlined
    outline_only = bool(outlined) and not skeletons and not indexed
    chunks: list[str] = []
    if opts.header:
        chunks.append(
            render_header(
                bundle.stats, opts, index_only=index_only, outline_only=outline_only
            )
        )
    if opts.tree and included:
        chunks.append(file_tree(included))

    if opts.format == "xml":
        inner = "\n".join(
            part for part in (_render_file(f, "xml") for f in skeletons) if part
        )
        chunks.append(f'<codebase root="{bundle.root.name}">\n{inner}\n</codebase>')
    else:
        for file in skeletons:
            rendered = _render_file(file, opts.format)
            if rendered:
                chunks.append(rendered)

    if outlined:
        chunks.append(render_outline(outlined, opts.format, note=not outline_only))
    if indexed:
        # In index-only mode the header already explains the format; repeating
        # the note would be a second preamble for a payload this small.
        chunks.append(render_index(indexed, opts.format, note=not index_only))
    return "\n\n".join(chunk for chunk in chunks if chunk).strip() + "\n"
