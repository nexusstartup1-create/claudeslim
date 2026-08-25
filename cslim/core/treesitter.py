"""Real parsers for the brace languages.

The heuristic scanner in :mod:`cslim.core.compressor` counts braces with string
and comment awareness. That holds on ordinary code and mis-slices exactly where
code stops being ordinary: nested generics, decorator factories, dense JSX. This
replaces the counting with a grammar.

It is registered through :func:`cslim.core.compressor.register_compressor`, the
seam that already existed for this, so nothing outside ``compressor.py`` knows
which backend ran. When a grammar is missing the heuristic stays in place and
the output is marked ``fallback``, as it always was.

The transformation is the same either way: keep a declaration's header, replace
its body with ``{ ... }``, and recurse into the containers whose members *are*
signatures — a class body, an interface, a Go type block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import CompressionOptions, Language, Symbol, SymbolKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .compressor import SkeletonResult

__all__ = ["TreeSitterCompressor", "available_languages", "load_parser"]


@dataclass(frozen=True, slots=True)
class GrammarSpec:
    """Which node types matter, per language."""

    language: Language
    #: Emitted verbatim — imports, type aliases, interfaces, Go type blocks.
    keep_whole: frozenset[str]
    #: Header kept, body replaced by ``{ ... }``.
    elide_body: frozenset[str]
    #: Recursed into: their members are themselves declarations.
    containers: frozenset[str]
    #: Transparent wrappers — recurse without emitting anything of their own.
    transparent: frozenset[str] = field(default_factory=frozenset)
    doc_comments: bool = True


_JS_KEEP = frozenset({
    "import_statement", "type_alias_declaration", "interface_declaration",
    "enum_declaration", "import_alias", "ambient_declaration",
    # class members that are declarations rather than bodies
    "public_field_definition", "field_definition", "property_signature",
    "abstract_property_signature", "index_signature", "enum_assignment",
})
_JS_ELIDE = frozenset({
    "function_declaration", "generator_function_declaration", "method_definition",
    "method_signature", "function_expression", "arrow_function",
    "abstract_method_signature",
})
_JS_CONTAINERS = frozenset({
    "class_declaration", "abstract_class_declaration", "class", "class_body",
    "internal_module", "module",
})
_JS_TRANSPARENT = frozenset({"export_statement", "program"})

_GO_KEEP = frozenset({
    "package_clause", "import_declaration", "type_declaration",
    "const_declaration", "var_declaration",
})
_GO_ELIDE = frozenset({"function_declaration", "method_declaration"})
_GO_CONTAINERS: frozenset[str] = frozenset()
_GO_TRANSPARENT = frozenset({"source_file"})

SPECS: dict[Language, GrammarSpec] = {
    Language.JAVASCRIPT: GrammarSpec(
        Language.JAVASCRIPT, _JS_KEEP, _JS_ELIDE, _JS_CONTAINERS, _JS_TRANSPARENT
    ),
    Language.TYPESCRIPT: GrammarSpec(
        Language.TYPESCRIPT, _JS_KEEP, _JS_ELIDE, _JS_CONTAINERS, _JS_TRANSPARENT
    ),
    Language.GO: GrammarSpec(
        Language.GO, _GO_KEEP, _GO_ELIDE, _GO_CONTAINERS, _GO_TRANSPARENT
    ),
}

#: Declarations that carry a value we may want to shorten rather than keep.
_VALUE_DECLS = frozenset({"lexical_declaration", "variable_declaration"})

_SYMBOL_KINDS: dict[str, SymbolKind] = {
    "function_declaration": SymbolKind.FUNCTION,
    "generator_function_declaration": SymbolKind.FUNCTION,
    "method_declaration": SymbolKind.METHOD,
    "method_definition": SymbolKind.METHOD,
    "method_signature": SymbolKind.METHOD,
    "class_declaration": SymbolKind.CLASS,
    "abstract_class_declaration": SymbolKind.CLASS,
    "interface_declaration": SymbolKind.INTERFACE,
    "type_declaration": SymbolKind.TYPE,
    "type_alias_declaration": SymbolKind.TYPE,
    "enum_declaration": SymbolKind.ENUM,
    "const_declaration": SymbolKind.CONSTANT,
    "lexical_declaration": SymbolKind.CONSTANT,
    "var_declaration": SymbolKind.VARIABLE,
    "variable_declaration": SymbolKind.VARIABLE,
    "import_statement": SymbolKind.IMPORT,
    "import_declaration": SymbolKind.IMPORT,
}


def load_parser(language: Language, *, tsx: bool = False) -> Any | None:
    """A configured parser, or None when the grammar isn't installed."""
    try:
        from tree_sitter import Language as TSLanguage
        from tree_sitter import Parser
    except ImportError:
        return None

    # Distinct aliases: reusing one name makes the type checker collapse three
    # unrelated modules into whichever it saw first.
    raw: Any
    try:
        if language is Language.JAVASCRIPT:
            import tree_sitter_javascript as ts_js

            raw = ts_js.language()
        elif language is Language.TYPESCRIPT:
            import tree_sitter_typescript as ts_ts

            raw = ts_ts.language_tsx() if tsx else ts_ts.language_typescript()
        elif language is Language.GO:
            import tree_sitter_go as ts_go

            raw = ts_go.language()
        else:
            return None
    except (ImportError, AttributeError):
        return None

    try:
        return Parser(TSLanguage(raw))
    except Exception:
        return None


def available_languages() -> list[Language]:
    """Which brace languages have a grammar installed right now."""
    return [lang for lang in SPECS if load_parser(lang) is not None]


class TreeSitterCompressor:
    """Grammar-backed skeleton extraction for one language."""

    def __init__(self, language: Language) -> None:
        self.language = language
        self.spec = SPECS[language]

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _text(source: bytes, node: Any) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    @staticmethod
    def _end_with_punctuation(source: bytes, node: Any) -> int:
        """End byte extended through a trailing ``;`` or ``,``.

        The grammar leaves the terminator out of the declaration node, so
        slicing the node alone drops punctuation the source had. Keeping it
        makes the skeleton read like the file it came from.
        """
        end: int = node.end_byte
        sibling = node.next_sibling
        if sibling is not None and sibling.type in (";", ",") and sibling.start_byte == end:
            return int(sibling.end_byte)
        return end

    def _header(self, source: bytes, node: Any) -> str:
        """A declaration's text up to its body, with the body elided."""
        body = node.child_by_field_name("body")
        if body is None:
            return self._text(source, node).rstrip()
        head = source[node.start_byte : body.start_byte].decode("utf-8", "replace")
        return head.rstrip() + " { ... }"

    def _is_private(self, source: bytes, node: Any) -> bool:
        for child in node.children:
            if child.type == "accessibility_modifier":
                return self._text(source, child).strip() == "private"
        name = node.child_by_field_name("name")
        return name is not None and self._text(source, name).startswith("#")

    def _symbol(self, source: bytes, node: Any) -> Symbol | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # const/var declarations hold the name one level down
            for child in node.children:
                inner = child.child_by_field_name("name") if child.children else None
                if inner is not None:
                    name_node = inner
                    break
        if name_node is None:
            return None
        return Symbol(
            self._text(source, name_node),
            _SYMBOL_KINDS.get(node.type, SymbolKind.VARIABLE),
            node.start_point[0] + 1,
        )

    # -- traversal --------------------------------------------------------- #

    def _walk(
        self,
        source: bytes,
        node: Any,
        options: CompressionOptions,
        out: list[str],
        symbols: list[Symbol],
        depth: int = 0,
    ) -> None:
        indent = "  " * depth
        for child in node.named_children:
            kind = child.type

            if kind in ("comment", "line_comment", "block_comment"):
                text = self._text(source, child)
                if options.keep_comments and text.lstrip().startswith(("/**", "///", "//!")):
                    out.append(indent + text.splitlines()[0])
                continue

            if kind in self.spec.transparent:
                # `export function f() {}` — the export adds a prefix we want to
                # keep, so emit through the wrapper rather than skipping it.
                inner = [c for c in child.named_children if c.type not in ("comment",)]
                if child.type == "export_statement" and inner:
                    self._emit(source, child, inner[0], options, out, symbols, depth)
                else:
                    self._walk(source, child, options, out, symbols, depth)
                continue

            self._emit(source, child, child, options, out, symbols, depth)

    def _emit(
        self,
        source: bytes,
        outer: Any,
        node: Any,
        options: CompressionOptions,
        out: list[str],
        symbols: list[Symbol],
        depth: int,
    ) -> None:
        """Emit one declaration. ``outer`` may wrap ``node`` (an export)."""
        indent = "  " * depth
        kind = node.type
        prefix = ""
        if outer is not node:
            prefix = source[outer.start_byte : node.start_byte].decode("utf-8", "replace")

        if not options.keep_private and self._is_private(source, node):
            return

        symbol = self._symbol(source, node)
        if symbol is not None:
            symbols.append(symbol)

        if kind in self.spec.containers:
            body = node.child_by_field_name("body")
            head = (
                source[outer.start_byte : body.start_byte].decode("utf-8", "replace")
                if body is not None
                else self._text(source, outer)
            )
            out.append(indent + head.rstrip() + " {")
            if body is not None:
                self._walk(source, body, options, out, symbols, depth + 1)
            out.append(indent + "}")
            return

        if kind in self.spec.elide_body:
            out.append(indent + prefix + self._header(source, node).lstrip())
            return

        if kind in self.spec.keep_whole:
            end = self._end_with_punctuation(source, outer)
            text = source[outer.start_byte : end].decode("utf-8", "replace").rstrip()
            out.append("\n".join(indent + line for line in text.splitlines()))
            return

        if kind in _VALUE_DECLS:
            text = self._text(source, outer).rstrip()
            if len(text) <= max(options.max_value_len, 40) and "\n" not in text:
                out.append(indent + text)
            else:
                # a long initialiser (an arrow function, an object literal) is a
                # body in disguise
                name = node.child_by_field_name("name")
                declarator = next(
                    (c for c in node.named_children if c.type == "variable_declarator"), None
                )
                label = (
                    self._text(source, declarator.child_by_field_name("name"))
                    if declarator is not None and declarator.child_by_field_name("name")
                    else (self._text(source, name) if name is not None else "?")
                )
                keyword = text.split(None, 1)[0] if text.split() else "const"
                out.append(f"{indent}{prefix}{keyword} {label} = ...")
            return

    def compress(
        self, source: str, path: Path, options: CompressionOptions
    ) -> SkeletonResult:
        from .compressor import BraceCompressor, SkeletonResult  # local: avoid a cycle

        tsx = path.name.endswith((".tsx", ".jsx"))
        parser = load_parser(self.language, tsx=tsx)
        if parser is None:
            result = _heuristic(BraceCompressor, self.language, source, path, options)
            result.fallback = True
            result.errors.append("tree-sitter grammar unavailable; used the scanner")
            return result

        blob = source.encode("utf-8")
        tree = parser.parse(blob)
        if tree.root_node.has_error:
            # A file the grammar cannot parse is usually a file with a syntax
            # error, but it may also be a dialect the grammar predates. The
            # scanner has no opinion about either, so it is the safer answer.
            result = _heuristic(BraceCompressor, self.language, source, path, options)
            result.fallback = True
            result.errors.append("grammar reported a parse error; used the scanner")
            return result

        out: list[str] = []
        symbols: list[Symbol] = []
        self._walk(blob, tree.root_node, options, out, symbols)

        cleaned: list[str] = []
        blank = True
        for line in out:
            text = line.rstrip()
            if not text:
                if blank:
                    continue
                blank = True
            else:
                blank = False
            cleaned.append(text)
        return SkeletonResult(text="\n".join(cleaned).strip(), symbols=symbols)


def _heuristic(
    brace_cls: Any, language: Language, source: str, path: Path, options: CompressionOptions
) -> SkeletonResult:
    from .compressor import GO_SPEC, JS_SPEC, TS_SPEC

    spec = {Language.GO: GO_SPEC, Language.JAVASCRIPT: JS_SPEC}.get(language, TS_SPEC)
    result: SkeletonResult = brace_cls(spec).compress(source, path, options)
    return result
