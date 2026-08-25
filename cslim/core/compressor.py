"""AST / structural compression engine.

Strategy per language family:

* **Python** — real ``ast`` parse. Signatures, decorators, base classes, type
  annotations, module/class/function docstrings and module-level constants are
  kept; every function body collapses to ``...``.
* **JS / TS / Go** — a brace-aware scanner (string- and comment-safe) that keeps
  top-level declarations plus the members of *container* blocks
  (``class`` / ``interface`` / ``enum`` / ``struct`` / ``type {}`` / Go groups),
  and elides every executable body into ``{ ... }``.
* **Anything else** — whitespace/comment normalisation, never destructive.

Adding a language is a one-liner: implement :class:`Compressor` and call
:func:`register_compressor`. Swapping the heuristic scanner for a tree-sitter
backend is exactly the same operation — nothing outside this module changes.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import CompressionOptions, Language, Symbol, SymbolKind

__all__ = [
    "BraceCompressor",
    "Compressor",
    "GenericCompressor",
    "MarkdownCompressor",
    "PythonCompressor",
    "SkeletonResult",
    "compress_source",
    "get_compressor",
    "register_compressor",
]


@dataclass(slots=True)
class SkeletonResult:
    text: str
    symbols: list[Symbol] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fallback: bool = False


@runtime_checkable
class Compressor(Protocol):
    """Backend contract: pure function from source text to skeleton."""

    language: Language

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult: ...


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #

#: Dunders that are part of a public API and survive `--no-private`.
_KEPT_DUNDERS = frozenset(
    {
        "__init__", "__new__", "__call__", "__repr__", "__str__", "__eq__",
        "__hash__", "__len__", "__iter__", "__next__", "__getitem__",
        "__setitem__", "__contains__", "__enter__", "__exit__", "__aenter__",
        "__aexit__", "__aiter__", "__anext__", "__post_init__", "__all__",
    }
)

_DEF_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Import,
    ast.ImportFrom,
    ast.Assign,
    ast.AnnAssign,
)


def _format_docstring(doc: str | None, options: CompressionOptions) -> str | None:
    """Collapse a docstring to a single, budget-friendly triple-quoted line."""
    if not doc or options.docstrings == "none":
        return None
    text = doc.strip()
    if not text:
        return None
    if options.docstrings == "summary":
        text = text.split("\n\n", 1)[0]
    text = " ".join(text.split())
    limit = max(16, options.docstring_max_chars)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = text.replace('"""', "'''").rstrip("\\")
    return f'"""{text}"""'


def _is_docstring_node(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _strip_trailing_pass(source: str) -> str:
    lines = source.split("\n")
    while lines and lines[-1].strip() in ("pass", ""):
        lines.pop()
    return "\n".join(lines)


def _unparse_header(node: ast.AST) -> str:
    """Unparse a def/class node with an empty body, yielding just the header."""
    stub = copy.copy(node)
    stub.body = [ast.Pass()]  # type: ignore[attr-defined]
    stub.decorator_list = []  # type: ignore[attr-defined]
    return _strip_trailing_pass(ast.unparse(stub))


def _contains_defs(nodes: list[ast.stmt]) -> bool:
    return any(isinstance(n, _DEF_NODES) for n in nodes)


class _PythonEmitter:
    """Walks a module AST and emits indented skeleton lines."""

    def __init__(self, options: CompressionOptions) -> None:
        self.o = options
        self.out: list[str] = []
        self.symbols: list[Symbol] = []

    # -- output helpers ----------------------------------------------------- #

    def _write(self, indent: int, text: str) -> None:
        pad = "    " * indent
        for line in text.split("\n"):
            self.out.append(pad + line if line.strip() else "")

    def _blank(self) -> None:
        if self.out and self.out[-1] != "":
            self.out.append("")

    def render(self) -> str:
        lines: list[str] = []
        blank = True
        for line in self.out:
            stripped = line.rstrip()
            if not stripped:
                if blank:
                    continue
                blank = True
            else:
                blank = False
            lines.append(stripped)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    # -- traversal ---------------------------------------------------------- #

    def module(self, tree: ast.Module) -> None:
        doc = ast.get_docstring(tree, clean=True)
        rendered = _format_docstring(doc, self.o)
        if rendered:
            self._write(0, rendered)
            self._blank()
        self.block(tree.body, 0, in_class=False, skip_docstring=doc is not None)

    def block(
        self,
        nodes: list[ast.stmt],
        indent: int,
        *,
        in_class: bool,
        skip_docstring: bool = False,
    ) -> int:
        emitted = 0
        for index, node in enumerate(nodes):
            if index == 0 and skip_docstring and _is_docstring_node(node):
                continue
            emitted += self.node(node, indent, in_class=in_class)
        return emitted

    def node(self, node: ast.stmt, indent: int, *, in_class: bool) -> int:
        o = self.o
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if not o.keep_imports:
                return 0
            self._write(indent, ast.unparse(node))
            self.symbols.append(
                Symbol(_import_name(node), SymbolKind.IMPORT, getattr(node, "lineno", 0))
            )
            return 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self.function(node, indent, in_class=in_class)
        if isinstance(node, ast.ClassDef):
            return self.klass(node, indent)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            return self.assignment(node, indent, in_class=in_class)
        if hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):  # py3.12+
            self._write(indent, ast.unparse(node))
            return 1
        if isinstance(node, ast.If):
            return self.conditional(node, indent, in_class=in_class)
        if isinstance(node, ast.Try):
            return self.try_block(node, indent, in_class=in_class)
        # while/for/with/expressions at module level are runtime work: dropped.
        return 0

    def _skip_name(self, name: str) -> bool:
        if self.o.keep_private:
            return False
        if name in _KEPT_DUNDERS:
            return False
        return name.startswith("_")

    def function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        indent: int,
        *,
        in_class: bool,
    ) -> int:
        if self._skip_name(node.name):
            return 0
        if self.o.keep_decorators:
            for dec in node.decorator_list:
                self._write(indent, "@" + ast.unparse(dec))
        header = _unparse_header(node)
        self._write(indent, header)
        doc = _format_docstring(ast.get_docstring(node, clean=True), self.o)
        if doc:
            self._write(indent + 1, doc)
        self._write(indent + 1, self.o.body_placeholder)
        self.symbols.append(
            Symbol(
                node.name,
                SymbolKind.METHOD if in_class else SymbolKind.FUNCTION,
                node.lineno,
                header.split("\n")[0],
            )
        )
        return 1

    def klass(self, node: ast.ClassDef, indent: int) -> int:
        if self._skip_name(node.name):
            return 0
        if self.o.keep_decorators:
            for dec in node.decorator_list:
                self._write(indent, "@" + ast.unparse(dec))
        header = _unparse_header(node)
        self._write(indent, header)
        self.symbols.append(Symbol(node.name, SymbolKind.CLASS, node.lineno, header))

        doc_src = ast.get_docstring(node, clean=True)
        doc = _format_docstring(doc_src, self.o)
        if doc:
            self._write(indent + 1, doc)
        emitted = self.block(
            node.body, indent + 1, in_class=True, skip_docstring=doc_src is not None
        )
        if emitted == 0 and not doc:
            self._write(indent + 1, self.o.body_placeholder)
        self._blank()
        return 1

    def assignment(
        self, node: ast.Assign | ast.AnnAssign, indent: int, *, in_class: bool
    ) -> int:
        if not in_class and not self.o.keep_constants:
            return 0
        names = _assign_names(node)
        if names and all(self._skip_name(n) for n in names):
            return 0
        stub = copy.copy(node)
        value = getattr(node, "value", None)
        if value is not None and len(ast.unparse(value)) > self.o.max_value_len:
            stub.value = ast.Constant(value=Ellipsis, kind=None)  # type: ignore[attr-defined]
        try:
            text = ast.unparse(stub)
        except Exception:  # pragma: no cover - defensive
            return 0
        self._write(indent, text)
        kind = SymbolKind.VARIABLE if in_class else SymbolKind.CONSTANT
        for name in names:
            self.symbols.append(Symbol(name, kind, getattr(node, "lineno", 0), text))
        return 1

    def conditional(self, node: ast.If, indent: int, *, in_class: bool) -> int:
        if not _contains_defs(node.body) and not _contains_defs(node.orelse):
            return 0
        self._write(indent, f"if {ast.unparse(node.test)}:")
        if self.block(node.body, indent + 1, in_class=in_class) == 0:
            self._write(indent + 1, self.o.body_placeholder)
        if node.orelse and _contains_defs(node.orelse):
            self._write(indent, "else:")
            if self.block(node.orelse, indent + 1, in_class=in_class) == 0:
                self._write(indent + 1, self.o.body_placeholder)
        return 1

    def try_block(self, node: ast.Try, indent: int, *, in_class: bool) -> int:
        if not _contains_defs(node.body):
            return 0
        self._write(indent, "try:")
        if self.block(node.body, indent + 1, in_class=in_class) == 0:
            self._write(indent + 1, self.o.body_placeholder)
        for handler in node.handlers:
            head = "except"
            if handler.type is not None:
                head += f" {ast.unparse(handler.type)}"
            if handler.name:
                head += f" as {handler.name}"
            self._write(indent, head + ":")
            if self.block(handler.body, indent + 1, in_class=in_class) == 0:
                self._write(indent + 1, self.o.body_placeholder)
        return 1


def _import_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or "."
    return node.names[0].name if node.names else "import"


def _assign_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(e.id for e in target.elts if isinstance(e, ast.Name))
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


class PythonCompressor:
    """Native ``ast``-based skeletonizer."""

    language = Language.PYTHON

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult:
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            result = GenericCompressor().compress(source, path, options)
            result.errors.append(f"python parse error line {exc.lineno}: {exc.msg}")
            result.fallback = True
            return result
        emitter = _PythonEmitter(options)
        emitter.module(tree)
        return SkeletonResult(text=emitter.render(), symbols=emitter.symbols)


# --------------------------------------------------------------------------- #
# Brace languages (JS / TS / Go)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BraceSpec:
    """Per-language lexical + structural configuration for :class:`BraceCompressor`."""

    language: Language
    keep_top: tuple[re.Pattern[str], ...]
    containers: tuple[re.Pattern[str], ...]
    groups: tuple[re.Pattern[str], ...] = ()
    line_comment: tuple[str, ...] = ("//",)
    block_comment: tuple[str, str] | None = ("/*", "*/")
    strings: str = "\"'`"
    multiline_strings: str = "`"
    doc_prefixes: tuple[str, ...] = ("/**", "///", "//!", "*")
    private_prefixes: tuple[str, ...] = ("private ", "#")


_JS_KEEP_TOP: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(import|export|require)\b"),
    re.compile(
        r"^(export\s+)?(default\s+)?(declare\s+)?(abstract\s+)?(async\s+)?"
        r"(function\s*\*?|class|interface|type|enum|namespace|module|const|let|var)\b"
    ),
    re.compile(r"^@[A-Za-z_$][\w$]*"),
    re.compile(r"^(module\.exports|exports\.)"),
)

_JS_CONTAINERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(class|interface|enum|namespace)\b[^=]*\{\s*$"),
    re.compile(r"^(export\s+)?(declare\s+)?type\s+[\w$]+[^=]*=\s*\{\s*$"),
    re.compile(r"^(declare\s+)?module\s+.*\{\s*$"),
)

_GO_KEEP_TOP: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(package|import|func|type|var|const)\b"),
)

_GO_CONTAINERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(struct|interface)\s*\{\s*$"),
)

_GO_GROUPS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(import|var|const|type)\s*\(\s*$"),
)

JS_SPEC = BraceSpec(Language.JAVASCRIPT, _JS_KEEP_TOP, _JS_CONTAINERS)
TS_SPEC = BraceSpec(Language.TYPESCRIPT, _JS_KEEP_TOP, _JS_CONTAINERS)
GO_SPEC = BraceSpec(
    Language.GO,
    _GO_KEEP_TOP,
    _GO_CONTAINERS,
    groups=_GO_GROUPS,
    strings="\"'`",
    multiline_strings="`",
    doc_prefixes=("//",),
    private_prefixes=(),
)

_SYMBOL_RE = re.compile(
    r"\b(?P<kw>class|interface|enum|type|struct|func|function|const|let|var)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)

_SYMBOL_KINDS: dict[str, SymbolKind] = {
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "enum": SymbolKind.ENUM,
    "type": SymbolKind.TYPE,
    "struct": SymbolKind.STRUCT,
    "func": SymbolKind.FUNCTION,
    "function": SymbolKind.FUNCTION,
    "const": SymbolKind.CONSTANT,
    "let": SymbolKind.VARIABLE,
    "var": SymbolKind.VARIABLE,
}


@dataclass(slots=True)
class _ScanState:
    in_block_comment: bool = False
    in_raw_string: str = ""


def _strip_line(line: str, spec: BraceSpec, state: _ScanState) -> str:
    """Return the line's *code* content, with strings/comments removed.

    ``state`` is mutated so block comments and template literals survive across
    lines. Brace/paren accounting always runs on this sanitised text, which is
    what makes the scanner robust against ``"{"`` inside a string literal.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    close = spec.block_comment[1] if spec.block_comment else None
    open_ = spec.block_comment[0] if spec.block_comment else None

    while i < n:
        ch = line[i]
        if state.in_block_comment:
            if close and line.startswith(close, i):
                state.in_block_comment = False
                i += len(close)
            else:
                i += 1
            continue
        if state.in_raw_string:
            if ch == "\\":
                i += 2
                continue
            if ch == state.in_raw_string:
                state.in_raw_string = ""
            i += 1
            continue
        if any(line.startswith(marker, i) for marker in spec.line_comment):
            break
        if open_ and line.startswith(open_, i):
            state.in_block_comment = True
            i += len(open_)
            continue
        if ch in spec.strings:
            if ch in spec.multiline_strings:
                state.in_raw_string = ch
                i += 1
                continue
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == ch:
                    break
                j += 1
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class BraceCompressor:
    """Structural skeletonizer for curly-brace languages.

    Keeps: top-level declarations, container members (class/interface/struct/
    enum/Go groups), multi-line signatures. Elides: every ``{ ... }`` body that
    is not a container.
    """

    def __init__(self, spec: BraceSpec) -> None:
        self.spec = spec
        self.language = spec.language

    # -- predicates --------------------------------------------------------- #

    def _is_container(self, code: str) -> bool:
        stripped = code.strip()
        return any(p.search(stripped) for p in self.spec.containers)

    def _is_group(self, code: str) -> bool:
        stripped = code.strip()
        return any(p.search(stripped) for p in self.spec.groups)

    def _keep_top(self, code: str) -> bool:
        stripped = code.strip()
        return any(p.search(stripped) for p in self.spec.keep_top)

    def _keep_member(self, raw: str, options: CompressionOptions) -> bool:
        stripped = raw.strip()
        return not (
            not options.keep_private
            and self.spec.private_prefixes
            and any(stripped.startswith(p) for p in self.spec.private_prefixes)
        )

    def _is_doc_comment(self, raw: str) -> bool:
        stripped = raw.strip()
        return any(stripped.startswith(p) for p in self.spec.doc_prefixes)

    def _is_comment_line(self, raw: str) -> bool:
        """True when the line *starts* as a comment (not merely code-free)."""
        stripped = raw.strip()
        markers = [*self.spec.line_comment, "*"]
        if self.spec.block_comment:
            markers.append(self.spec.block_comment[0])
        return any(stripped.startswith(m) for m in markers)

    # -- emission ----------------------------------------------------------- #

    def _emit_declaration(
        self,
        raw: str,
        opens: int,
        closes: int,
        options: CompressionOptions,
        out: list[str],
    ) -> str:
        """Emit one declaration line; return the kind of its first ``{``."""
        text = raw.rstrip()
        if opens - closes <= 0:
            out.append(text)
            return "skip"
        if self._is_container(text):
            out.append(text)
            return "container"
        index = text.rfind("{")
        head = text[:index].rstrip() if index != -1 else text
        placeholder = f"{{ {options.body_placeholder} }}"
        if head.endswith("("):  # e.g. a decorator factory: @Component({
            out.append(f"{head}{placeholder})")
        else:
            out.append(f"{head} {placeholder}" if head else placeholder)
        return "elided"

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult:
        out: list[str] = []
        symbols: list[Symbol] = []
        pending_doc: list[str] = []
        stack: list[str] = []
        state = _ScanState()
        elide = 0
        paren_depth = 0
        continuing = False

        for lineno, raw in enumerate(source.splitlines(), 1):
            code = _strip_line(raw, self.spec, state)
            stripped_raw = raw.strip()
            opens = code.count("{")
            closes = code.count("}")
            kind_first_open = "skip"
            at_top = elide == 0
            member_level = bool(stack) and stack[-1] in ("container", "group")
            emitting = at_top and (not stack or member_level)

            if not emitting:
                pass
            elif paren_depth > 0:
                # Continuation of a multi-line signature / parameter list.
                if continuing:
                    kind_first_open = self._emit_declaration(
                        raw, opens, closes, options, out
                    )
                    if kind_first_open != "skip" or opens > closes:
                        continuing = False
            elif not stripped_raw:
                if out and out[-1] != "":
                    out.append("")
            elif stack and stack[-1] == "group":
                # Group members are often bare string literals (Go imports), so
                # this must be checked before the "no code left" branch below.
                if stripped_raw.startswith(")"):
                    out.append(raw.rstrip())
                    stack.pop()
                    continue
                if not self._is_comment_line(stripped_raw):
                    out.append(raw.rstrip())
            elif not code.strip() and self._is_comment_line(stripped_raw):
                if options.keep_comments and self._is_doc_comment(stripped_raw):
                    pending_doc.append(raw.rstrip())
                    if len(pending_doc) > 8:
                        pending_doc.pop(0)
            else:
                keep = (
                    self._keep_member(raw, options)
                    if member_level
                    else self._keep_top(code)
                )
                if keep:
                    if pending_doc:
                        out.extend(pending_doc)
                        pending_doc.clear()
                    if self._is_group(code):
                        out.append(raw.rstrip())
                        stack.append("group")
                        continue
                    kind_first_open = self._emit_declaration(
                        raw, opens, closes, options, out
                    )
                    match = _SYMBOL_RE.search(code)
                    if match:
                        symbols.append(
                            Symbol(
                                match.group("name"),
                                _SYMBOL_KINDS.get(match.group("kw"), SymbolKind.VARIABLE),
                                lineno,
                                stripped_raw,
                            )
                        )
                    if opens == closes and code.count("(") > code.count(")"):
                        continuing = True
                else:
                    pending_doc.clear()

            # -- structural bookkeeping (runs for every line) ---------------- #
            first = True
            for ch in code:
                if ch == "{":
                    kind = kind_first_open if first else "skip"
                    first = False
                    stack.append(kind)
                    if kind in ("elided", "skip"):
                        elide += 1
                elif ch == "}":
                    if stack:
                        kind = stack.pop()
                        if kind in ("elided", "skip"):
                            elide = max(0, elide - 1)

            if at_top:
                paren_depth = max(0, paren_depth + code.count("(") - code.count(")"))
                if paren_depth == 0:
                    continuing = False
            else:
                paren_depth = 0
                continuing = False

        return SkeletonResult(text=_squeeze(out), symbols=symbols)


def _squeeze(lines: list[str]) -> str:
    cleaned: list[str] = []
    blank = True
    for line in lines:
        text = line.rstrip()
        if not text:
            if blank:
                continue
            blank = True
        else:
            blank = False
        cleaned.append(text)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


# --------------------------------------------------------------------------- #
# Fallbacks
# --------------------------------------------------------------------------- #

_GENERIC_COMMENT_MARKERS = ("#", "//", ";;", "--")


class GenericCompressor:
    """Non-destructive cleanup for unknown/plain-text files."""

    language = Language.UNKNOWN

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult:
        lines: list[str] = []
        for raw in source.splitlines():
            text = raw.rstrip()
            if not text.strip():
                if lines and lines[-1]:
                    lines.append("")
                continue
            if options.aggressive or not options.keep_comments:
                stripped = text.lstrip()
                if stripped.startswith(_GENERIC_COMMENT_MARKERS) and options.aggressive:
                    continue
            lines.append(text)
        return SkeletonResult(text=_squeeze(lines), fallback=True)


_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")


class MarkdownCompressor:
    """Keeps the document outline plus the first sentence under each heading."""

    language = Language.MARKDOWN

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult:
        out: list[str] = []
        symbols: list[Symbol] = []
        in_fence = False
        budget = 0
        for lineno, raw in enumerate(source.splitlines(), 1):
            if _MD_FENCE.match(raw):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = _MD_HEADING.match(raw)
            if heading:
                out.append(raw.rstrip())
                symbols.append(Symbol(heading.group(2).strip(), SymbolKind.MODULE, lineno))
                budget = 0 if options.docstrings == "none" else 1
                continue
            if budget > 0 and raw.strip():
                text = " ".join(raw.split())
                limit = max(40, options.docstring_max_chars)
                out.append(text[:limit] + ("…" if len(text) > limit else ""))
                budget -= 1
        return SkeletonResult(text=_squeeze(out), symbols=symbols, fallback=True)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[Language, Compressor] = {
    Language.PYTHON: PythonCompressor(),
    Language.JAVASCRIPT: BraceCompressor(JS_SPEC),
    Language.TYPESCRIPT: BraceCompressor(TS_SPEC),
    Language.GO: BraceCompressor(GO_SPEC),
    Language.MARKDOWN: MarkdownCompressor(),
}

_FALLBACK: Compressor = GenericCompressor()


def _install_treesitter() -> list[Language]:
    """Swap in grammar-backed backends for whatever grammars are installed.

    This is the whole integration: the registry was built for it, so no caller
    outside this module learns which backend ran. A language with no grammar
    keeps the heuristic scanner, and its output keeps saying ``fallback``.
    """
    try:
        from .treesitter import SPECS, TreeSitterCompressor, load_parser
    except ImportError:  # pragma: no cover - tree_sitter not installed at all
        return []

    installed: list[Language] = []
    for language in SPECS:
        if load_parser(language) is None:
            continue
        _REGISTRY[language] = TreeSitterCompressor(language)
        installed.append(language)
    return installed


TREESITTER_LANGUAGES: list[Language] = _install_treesitter()


def register_compressor(language: Language, compressor: Compressor) -> None:
    """Plug in (or override) a backend — e.g. a tree-sitter powered one."""
    _REGISTRY[language] = compressor


def get_compressor(language: Language) -> Compressor:
    return _REGISTRY.get(language, _FALLBACK)


def compress_source(
    source: str,
    path: Path,
    language: Language,
    options: CompressionOptions,
) -> SkeletonResult:
    """Compress ``source``; never raises — failures degrade to a text fallback."""
    resolved = options.resolved()
    compressor = get_compressor(language)
    try:
        return compressor.compress(source, path, resolved)
    except RecursionError:
        result = _FALLBACK.compress(source, path, resolved)
        result.errors.append("recursion limit reached, used text fallback")
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result = _FALLBACK.compress(source, path, resolved)
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return result
