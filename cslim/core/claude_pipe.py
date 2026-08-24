"""Delivery layer: pipe, clipboard, file, or a direct Claude Code invocation.

The golden rule of a pipeable CLI: **payload on stdout, UI on stderr.** That is
what makes ``cslim ./src | claude -p "review this"`` work while still showing the
compression stats to the human. :func:`stdout_is_pipe` is how the CLI decides.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "ClipboardError",
    "Delivery",
    "OutputMode",
    "claude_binary",
    "clipboard_backend",
    "copy_to_clipboard",
    "deliver",
    "read_stdin",
    "send_to_claude",
    "stdin_has_data",
    "stdout_is_pipe",
]


class OutputMode(str, Enum):
    AUTO = "auto"
    """stdout when piped/redirected, clipboard otherwise."""
    STDOUT = "stdout"
    CLIPBOARD = "clipboard"
    CLAUDE = "claude"
    FILE = "file"


class ClipboardError(RuntimeError):
    """No usable clipboard backend on this platform."""


@dataclass(frozen=True, slots=True)
class Delivery:
    mode: OutputMode
    detail: str = ""
    exit_code: int = 0


def stdout_is_pipe() -> bool:
    try:
        return not sys.stdout.isatty()
    except Exception:  # pragma: no cover - exotic streams
        return False


def stdin_has_data() -> bool:
    """True only when something was really piped/redirected into stdin.

    ``not sys.stdin.isatty()`` is not enough: under CI, cron or a shell that
    wires stdin to ``/dev/null`` it is also false, which would make ``cslim log``
    silently read an empty stream instead of calling git. So we inspect the
    file type: a pipe/FIFO, a socket, or a non-empty regular file counts;
    a character device (``/dev/null``, a tty) does not.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return False
        mode = os.fstat(sys.stdin.fileno()).st_mode
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            return True
        if stat.S_ISREG(mode):
            return os.fstat(sys.stdin.fileno()).st_size > 0
        return False
    except Exception:  # pragma: no cover - exotic streams
        return False


def read_stdin() -> str:
    """Read stdin **without** universal-newline translation.

    ``sys.stdin.read()`` rewrites every bare ``\\r`` into ``\\n``, which destroys
    exactly the structure progress bars use to redraw themselves — the thing
    :func:`cslim.core.git_cleaner.clean_terminal` needs in order to collapse
    them. Reading the raw buffer keeps carriage returns intact.
    """
    if not stdin_has_data():
        return ""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # pragma: no cover - wrapped streams under test harnesses
        return sys.stdin.read()
    raw: bytes = buffer.read()
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Clipboard (macOS / Linux / Windows / WSL)
# --------------------------------------------------------------------------- #

_Backend = tuple[str, list[str]]


def _is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    if "microsoft" in platform.release().lower():
        return True
    return bool(os.environ.get("WSL_DISTRO_NAME"))


def _candidate_backends() -> list[_Backend]:
    system = platform.system()
    if system == "Darwin":
        return [("pbcopy", ["pbcopy"])]
    if system == "Windows":
        return [
            ("clip", ["clip"]),
            ("powershell", ["powershell", "-NoProfile", "-Command", "Set-Clipboard"]),
        ]
    backends: list[_Backend] = []
    if _is_wsl():
        backends.append(("clip.exe", ["clip.exe"]))
    if os.environ.get("WAYLAND_DISPLAY"):
        backends.append(("wl-copy", ["wl-copy"]))
    backends.extend(
        [
            ("xclip", ["xclip", "-selection", "clipboard"]),
            ("xsel", ["xsel", "--clipboard", "--input"]),
            ("wl-copy", ["wl-copy"]),
        ]
    )
    return backends


def clipboard_backend() -> str | None:
    """Name of the clipboard tool that would be used, or ``None``."""
    for name, command in _candidate_backends():
        if shutil.which(command[0]):
            return name
    try:
        import pyperclip  # noqa: F401

        return "pyperclip"
    except Exception:
        return None


def copy_to_clipboard(text: str) -> str:
    """Copy ``text`` to the system clipboard; returns the backend used."""
    for name, command in _candidate_backends():
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True)
            return name
        except (subprocess.CalledProcessError, OSError):
            continue
    try:
        import pyperclip

        pyperclip.copy(text)
        return "pyperclip"
    except Exception as exc:
        raise ClipboardError(
            "no clipboard backend found "
            "(install xclip or wl-clipboard on Linux, or `pip install pyperclip`)"
        ) from exc


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #


def claude_binary() -> str | None:
    return shutil.which(os.environ.get("CSLIM_CLAUDE_BIN", "claude"))


def send_to_claude(
    text: str,
    *,
    prompt: str | None = None,
    model: str | None = None,
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Spawn Claude Code with the payload on stdin.

    Mirrors what ``cslim ./src | claude -p "..."`` does, but keeps the
    orchestration inside cslim so the TUI can trigger it too.
    """
    binary = claude_binary()
    if binary is None:
        raise FileNotFoundError(
            "`claude` not found in PATH — install Claude Code or pipe manually: "
            "cslim ./src | claude"
        )
    command = [binary]
    if prompt:
        command += ["-p", prompt]
    if model:
        command += ["--model", model]
    command += list(extra_args)
    proc = subprocess.run(
        command,
        input=text,
        text=True,
        cwd=str(cwd) if cwd else None,
        check=False,
    )
    return proc.returncode


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


def deliver(
    text: str,
    mode: OutputMode = OutputMode.AUTO,
    *,
    out_file: Path | None = None,
    prompt: str | None = None,
    model: str | None = None,
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> Delivery:
    """Route the payload according to ``mode`` (``AUTO`` inspects the tty)."""
    if mode is OutputMode.AUTO:
        mode = OutputMode.STDOUT if stdout_is_pipe() else OutputMode.CLIPBOARD

    if mode is OutputMode.FILE:
        if out_file is None:
            raise ValueError("OutputMode.FILE requires out_file")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(text, encoding="utf-8")
        return Delivery(mode, str(out_file))

    if mode is OutputMode.CLIPBOARD:
        try:
            backend = copy_to_clipboard(text)
            return Delivery(mode, backend)
        except ClipboardError:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
            return Delivery(OutputMode.STDOUT, "clipboard unavailable, wrote stdout")

    if mode is OutputMode.CLAUDE:
        code = send_to_claude(
            text, prompt=prompt, model=model, extra_args=extra_args, cwd=cwd
        )
        return Delivery(mode, "claude", exit_code=code)

    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()
    return Delivery(OutputMode.STDOUT, "stdout")
