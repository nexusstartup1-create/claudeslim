"""Register / unregister the cslim hook in Claude Code's ``settings.json``.

Editing someone's settings file is destructive if done carelessly, so this
module is deliberately conservative: it merges into the existing structure,
never rewrites unrelated keys, always writes a ``.bak`` copy first, and reports
exactly what it changed.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "InstallResult",
    "InstallScope",
    "hook_command",
    "hook_status",
    "install_hook",
    "settings_path",
    "uninstall_hook",
]

EVENT = "UserPromptSubmit"

#: How we recognise our own entry when updating or removing it.
#:
#: Matching the literal string "cslim hook" looked fine until CI ran on Windows,
#: where the executable is `cslim.exe` and the command reads "cslim.exe hook" —
#: the marker never matched, so install was not idempotent and uninstall found
#: nothing to remove. The command is also `python -m cslim.main hook` when cslim
#: is not on PATH. So we match the two invariant parts instead of a literal.
MARKER = "cslim hook"
_OURS = re.compile(r"cslim(?:\.exe|\.main)?[\"']?\s+hook(?:\s|$)", re.IGNORECASE)


class InstallScope:
    PROJECT = "project"
    """`.claude/settings.json` — shared with the team via git."""
    LOCAL = "local"
    """`.claude/settings.local.json` — personal, gitignored."""
    USER = "user"
    """`~/.claude/settings.json` — every project."""


@dataclass(slots=True)
class InstallResult:
    path: Path
    action: str
    """``installed`` | ``updated`` | ``removed`` | ``unchanged`` | ``absent``"""
    command: str = ""
    backup: Path | None = None


def settings_path(scope: str, project_dir: Path | None = None) -> Path:
    root = project_dir or Path.cwd()
    if scope == InstallScope.USER:
        return Path.home() / ".claude" / "settings.json"
    if scope == InstallScope.LOCAL:
        return root / ".claude" / "settings.local.json"
    return root / ".claude" / "settings.json"


def hook_command(
    *,
    paths: tuple[str, ...] = (),
    max_tokens: int | None = 25_000,
    aggressive: bool = False,
    min_files: int = 8,
    languages: tuple[str, ...] = (),
    quiet: bool = False,
    index_only: bool | None = None,
    index_threshold: int = 30,
) -> str:
    """Build the shell command Claude Code will run on every prompt."""
    executable = shutil.which("cslim")
    parts = [executable] if executable else [sys.executable, "-m", "cslim.main"]
    parts.append("hook")
    for path in paths:
        parts += ["--path", path]
    for language in languages:
        parts += ["--lang", language]
    if max_tokens is not None:
        parts += ["--max-tokens", str(max_tokens)]
    if aggressive:
        parts.append("--aggressive")
    if min_files != 8:
        parts += ["--min-files", str(min_files)]
    if index_only is True:
        parts.append("--index-only")
    elif index_only is False:
        parts.append("--full-map")
    if index_threshold != 30:
        parts += ["--index-threshold", str(index_threshold)]
    if quiet:
        parts.append("--quiet")
    return " ".join(_quote(part) for part in parts)


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> Path | None:
    backup: Path | None = None
    if path.is_file():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return backup


def _groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("`hooks` in settings.json is not an object")
    groups = hooks.setdefault(EVENT, [])
    if not isinstance(groups, list):
        raise ValueError(f"`hooks.{EVENT}` in settings.json is not an array")
    return groups


def _is_ours(entry: Any) -> bool:
    """Is this hook entry one of ours, whatever the executable is called?"""
    if not isinstance(entry, dict):
        return False
    return bool(_OURS.search(str(entry.get("command", ""))))


def install_hook(
    scope: str = InstallScope.PROJECT,
    *,
    command: str | None = None,
    project_dir: Path | None = None,
    timeout: int = 30,
) -> InstallResult:
    """Add (or refresh) the cslim UserPromptSubmit hook. Idempotent."""
    path = settings_path(scope, project_dir)
    data = _load(path)
    groups = _groups(data)
    cmd = command or hook_command()

    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if _is_ours(handler):
                if handler.get("command") == cmd and handler.get("timeout") == timeout:
                    return InstallResult(path, "unchanged", cmd)
                handler["command"] = cmd
                handler["timeout"] = timeout
                backup = _save(path, data)
                return InstallResult(path, "updated", cmd, backup)

    groups.append(
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": cmd, "timeout": timeout}],
        }
    )
    backup = _save(path, data)
    return InstallResult(path, "installed", cmd, backup)


def uninstall_hook(
    scope: str = InstallScope.PROJECT, *, project_dir: Path | None = None
) -> InstallResult:
    """Remove our hook, leaving every other hook in place."""
    path = settings_path(scope, project_dir)
    if not path.is_file():
        return InstallResult(path, "absent")
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(EVENT), list):
        return InstallResult(path, "absent")

    removed = False
    surviving: list[Any] = []
    for group in hooks[EVENT]:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            kept = [h for h in group["hooks"] if not _is_ours(h)]
            if len(kept) != len(group["hooks"]):
                removed = True
            if not kept:
                continue  # the group existed only for us
            group["hooks"] = kept
        surviving.append(group)

    if not removed:
        return InstallResult(path, "absent")

    if surviving:
        hooks[EVENT] = surviving
    else:
        hooks.pop(EVENT, None)
        if not hooks:
            data.pop("hooks", None)
    backup = _save(path, data)
    return InstallResult(path, "removed", backup=backup)


def hook_status(project_dir: Path | None = None) -> list[tuple[str, Path, str | None]]:
    """``(scope, path, command)`` for every scope; ``command`` is None if absent."""
    out: list[tuple[str, Path, str | None]] = []
    for scope in (InstallScope.PROJECT, InstallScope.LOCAL, InstallScope.USER):
        path = settings_path(scope, project_dir)
        command: str | None = None
        try:
            data = _load(path)
        except ValueError:
            out.append((scope, path, "<invalid JSON>"))
            continue
        hooks = data.get("hooks")
        if isinstance(hooks, dict) and isinstance(hooks.get(EVENT), list):
            for group in hooks[EVENT]:
                if not isinstance(group, dict):
                    continue
                for handler in group.get("hooks", []) or []:
                    if _is_ours(handler):
                        command = str(handler.get("command", ""))
        out.append((scope, path, command))
    return out
