"""Warm-amber terminal identity — shared by the Rich CLI and the Textual TUI.

The palette is a single source of truth (:data:`PALETTE`) so both frontends look
like the same tool: dark-orange structure, amber body text, bright gold accents,
sparingly used semantic colours.
"""

from __future__ import annotations

from typing import Final

from rich.console import Console
from rich.theme import Theme

__all__ = ["BANNER", "CSLIM_THEME", "PALETTE", "banner", "make_console"]

PALETTE: Final[dict[str, str]] = {
    "ember": "#7A2E00",      # deepest structure / rules
    "rust": "#A33B00",       # borders, inactive chrome
    "burnt": "#C25E00",      # secondary text, table borders
    "amber": "#FF9E1B",      # primary brand colour
    "gold": "#FFB000",       # body highlight
    "flare": "#FFD166",      # bright accent, numbers
    "cream": "#F5E6C8",      # default readable text
    "ash": "#8A6A3B",        # dimmed / muted
    "ok": "#7BD88F",
    "warn": "#FFC857",
    "danger": "#FF5F56",
}

CSLIM_THEME: Final[Theme] = Theme(
    {
        "cslim.brand": f"bold {PALETTE['amber']}",
        "cslim.title": f"bold {PALETTE['flare']}",
        "cslim.text": PALETTE["cream"],
        "cslim.dim": PALETTE["ash"],
        "cslim.accent": PALETTE["flare"],
        "cslim.value": f"bold {PALETTE['flare']}",
        "cslim.border": PALETTE["burnt"],
        "cslim.rule": PALETTE["rust"],
        "cslim.path": f"italic {PALETTE['gold']}",
        "cslim.lang": PALETTE["burnt"],
        "cslim.ok": f"bold {PALETTE['ok']}",
        "cslim.warn": PALETTE["warn"],
        "cslim.danger": f"bold {PALETTE['danger']}",
        "cslim.bar.complete": PALETTE["amber"],
        "cslim.bar.finished": PALETTE["ok"],
        "cslim.bar.pulse": PALETTE["ember"],
    }
)


def make_console(*, stderr: bool = True, quiet: bool = False, force_terminal: bool | None = None) -> Console:
    """UI console.

    Defaults to **stderr** so that ``cslim ./src | claude`` keeps stdout clean
    for the payload — the single most important detail of a pipeable CLI.
    """
    return Console(
        stderr=stderr,
        theme=CSLIM_THEME,
        quiet=quiet,
        force_terminal=force_terminal,
        highlight=False,
        soft_wrap=False,
    )


BANNER: Final[str] = r"""
 ██████ ███████ ██      ██ ███    ███
██      ██      ██      ██ ████  ████
██      ███████ ██      ██ ██ ████ ██
██           ██ ██      ██ ██  ██  ██
 ██████ ███████ ███████ ██ ██      ██
"""


def banner(version: str = "") -> str:
    """Rich-markup banner with an amber → gold vertical gradient."""
    shades = [PALETTE["ember"], PALETTE["rust"], PALETTE["burnt"], PALETTE["amber"], PALETTE["flare"]]
    lines = [line for line in BANNER.strip("\n").splitlines()]
    painted = [
        f"[{shades[min(i, len(shades) - 1)]}]{line}[/]" for i, line in enumerate(lines)
    ]
    tagline = (
        f"[cslim.dim]  claudeslim {version} — AST codebase compressor for "
        f"[/][cslim.brand]Claude Code[/]"
    )
    return "\n".join(painted) + "\n" + tagline
