"""Entry point.

Adds one piece of ergonomics on top of Typer: an implicit default command, so
``cslim ./src`` works exactly like ``cslim pack ./src`` while ``cslim diff`` still
reaches the subcommand. That is the shape people expect from a compressor.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from .cli import app

DEFAULT_COMMAND = "pack"

#: Everything that must NOT be rewritten into `pack <arg>`.
_COMMANDS = frozenset(
    {
        "pack", "stats", "diff", "log", "clean",
        "models", "doctor", "tui", "hook", "install", "uninstall", "map", "test", "graph",
    }
)


def _normalize(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if not args:
        return args
    first = args[0]
    if first in _COMMANDS or first.startswith("-"):
        return args
    # `cslim ./src`, `cslim main.py` → `cslim pack ./src`
    return [DEFAULT_COMMAND, *args]


def main(argv: Sequence[str] | None = None) -> None:
    args = _normalize(sys.argv[1:] if argv is None else argv)
    try:
        app(args=args, prog_name="cslim")
    except BrokenPipeError:  # e.g. `cslim ./src | head`
        try:
            sys.stdout.close()
        finally:
            sys.exit(0)
    except KeyboardInterrupt:
        sys.stderr.write("\naborted\n")
        sys.exit(130)


if __name__ == "__main__":
    main()
