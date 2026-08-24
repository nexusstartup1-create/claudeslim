"""Textual front-end (optional extra: ``pip install 'claudeslim[tui]'``)."""

from __future__ import annotations

__all__ = ["ClaudeSlimApp"]


def __getattr__(name: str) -> object:
    # Lazy: importing `cslim.tui` must not require textual to be installed.
    if name == "ClaudeSlimApp":
        from .app import ClaudeSlimApp

        return ClaudeSlimApp
    raise AttributeError(name)
