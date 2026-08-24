"""Which files deserve the token budget.

Every other codebase-to-prompt tool spends the same number of tokens per file,
so a 500-file repository produces a 250k-token dump that fits nowhere. But files
are not equally worth explaining: the module everything imports tells Claude far
more about a project than a leaf utility nobody calls.

So we build a reference graph and rank by it. The method is deliberately
language-agnostic — it works off the skeletons we already extracted, so it costs
one pass over text we've parsed anyway, and a new language backend inherits it
for free:

1. Map every symbol name to the file(s) that define it.
2. Scan each skeleton for identifiers defined *elsewhere* — those are edges.
3. Score a file by how many distinct other files reference it, damped by its own
   size so a huge file can't buy rank by sheer surface area.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

from .models import CompressedFile, SymbolKind

__all__ = ["FileRank", "build_reference_graph", "rank_files"]

_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")

#: Symbol kinds that make a file worth pointing at. Imports and local variables
#: describe what a file *uses*, not what it *offers*.
_EXPORTED_KINDS = frozenset(
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

#: Names so common that a match says nothing about a real dependency.
_STOPWORDS = frozenset(
    {
        "self", "None", "True", "False", "str", "int", "bool", "float", "list",
        "dict", "set", "tuple", "bytes", "object", "type", "Any", "Optional",
        "List", "Dict", "Sequence", "Iterable", "Path", "return", "class", "def",
        "import", "from", "async", "await", "const", "let", "var", "function",
        "export", "interface", "string", "number", "boolean", "void", "func",
        "package", "struct", "error", "nil", "get", "run", "main", "text",
        "name", "path", "value", "data", "options", "config", "result",
    }
)


@dataclass(slots=True)
class FileRank:
    rel_path: str
    score: float
    referenced_by: int = 0
    """How many other files mention something this file defines."""
    defines: int = 0
    entrypoint: bool = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.rel_path} score={self.score:.2f} refs={self.referenced_by}>"


#: Files that explain a project regardless of who imports them.
_ENTRYPOINT_NAMES = frozenset(
    {
        "main.py", "__main__.py", "app.py", "cli.py", "server.py", "index.ts",
        "index.js", "main.ts", "main.js", "main.go", "app.ts", "routes.ts",
        "schema.py", "models.py", "settings.py", "config.py", "types.ts",
    }
)


def _exported_names(file: CompressedFile) -> set[str]:
    return {
        symbol.name
        for symbol in file.symbols
        if symbol.kind in _EXPORTED_KINDS
        and len(symbol.name) > 2
        and symbol.name not in _STOPWORDS
        # Dunders are boilerplate present in every module: `__all__` defined in
        # one file would otherwise look like it's referenced by all the others.
        and not symbol.name.startswith("__")
    }


def build_reference_graph(
    files: list[CompressedFile],
) -> dict[str, set[str]]:
    """Return ``{defining file: {files that reference it}}``."""
    owners: dict[str, set[str]] = defaultdict(set)
    for file in files:
        for name in _exported_names(file):
            owners[name].add(file.rel_path)

    # A name defined in several files tells us nothing about who depends on
    # whom — most often it's a re-export barrel (`__init__.py`, `index.ts`)
    # claiming credit for symbols it merely forwards.
    unambiguous = {name: next(iter(paths)) for name, paths in owners.items() if len(paths) == 1}

    referenced: dict[str, set[str]] = defaultdict(set)
    for file in files:
        mentioned = set(_IDENTIFIER.findall(file.skeleton))
        for name in mentioned & unambiguous.keys():
            owner = unambiguous[name]
            if owner != file.rel_path:
                referenced[owner].add(file.rel_path)
    return referenced


def rank_files(files: list[CompressedFile]) -> list[FileRank]:
    """Rank files by architectural importance, most important first."""
    referenced = build_reference_graph(files)
    ranks: list[FileRank] = []

    for file in files:
        exported = _exported_names(file)
        in_degree = len(referenced.get(file.rel_path, ()))
        name = file.rel_path.rsplit("/", 1)[-1]
        entrypoint = name in _ENTRYPOINT_NAMES

        # Log damping: the 30th referrer matters less than the 3rd, and a file
        # defining 200 symbols isn't 100× more central than one defining 2.
        score = math.log1p(in_degree) * 3.0 + math.log1p(len(exported))
        # Nobody imports an entry point — that's its nature, not a demerit, and
        # it's still the file a newcomer reads first. Score it as if it were
        # well-referenced rather than adding a constant that leaves it last.
        if entrypoint:
            score = max(score, math.log1p(max(in_degree, 4)) * 3.0) + 1.0
        # A file nobody references and nothing exports is probably a leaf.
        elif in_degree == 0:
            score *= 0.5
        # Cheap-to-explain files are good value: bias slightly toward density.
        if file.compressed_tokens > 0:
            score += min(1.0, len(exported) / max(1, file.compressed_tokens / 100))

        ranks.append(
            FileRank(
                rel_path=file.rel_path,
                score=score,
                referenced_by=in_degree,
                defines=len(exported),
                entrypoint=entrypoint,
            )
        )

    ranks.sort(key=lambda r: (-r.score, r.rel_path))
    return ranks
