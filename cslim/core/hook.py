"""Automatic mode: cslim as a Claude Code hook.

Instead of piping by hand, cslim registers itself on the ``UserPromptSubmit``
event and injects the compressed codebase map straight into Claude's context.
No daemon, no proxy, no account: Claude Code invokes this as a subprocess and
reads the JSON we print on stdout.

Two rules drive the whole design:

**Inject once per session.** ``UserPromptSubmit`` fires on *every* prompt. Re-
injecting an 8k map twenty times would multiply token usage, not reduce it — the
exact opposite of the point. Sessions already served are tracked in a state file.

**Never break the session.** A hook that crashes, hangs or prints garbage
degrades the user's Claude Code. Every failure path here exits 0 with no output,
so the worst case is "no map injected", never "session broken".
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import DiscoveryOptions, discover
from .models import CompressionOptions, Language
from .renderer import RenderOptions
from .service import CompressionService, CompressRequest
from .tokenizer import humanize

__all__ = ["HookConfig", "HookOutcome", "build_map", "run_hook", "state_dir"]

#: Sessions older than this are pruned from the state file.
_SESSION_TTL = 7 * 24 * 3600

#: Cached maps older than this are deleted.
_CACHE_TTL = 14 * 24 * 3600


@dataclass(slots=True)
class HookConfig:
    """Everything the hook needs, supplied as flags in ``settings.json``."""

    paths: tuple[Path, ...] = ()
    max_tokens: int | None = 25_000
    aggressive: bool = False
    model: str = "sonnet"
    every_prompt: bool = False
    """Re-inject on every prompt. Off by default — it multiplies token usage."""
    min_files: int = 8
    """Below this, Claude can just read the repo; a map is pure overhead."""
    languages: tuple[str, ...] = ()
    quiet: bool = False
    index_only: bool | None = None
    """``True``/``False`` to force a tier; ``None`` to decide per repository.

    See :func:`choose_tier` — the automatic choice exists because a map is only
    worth injecting when it is much cheaper than the exploration it replaces.
    """
    index_threshold: int = 30
    """At or below this many files, prefer the ultralight index."""
    outline_only: bool = False
    """Force the middle tier: grouped names, no signatures."""


@dataclass(slots=True)
class HookOutcome:
    """What the hook decided, for tests and for `cslim hook --dry-run`."""

    injected: bool
    reason: str
    stdout: str = ""
    tokens: int = 0
    files: int = 0
    cached: bool = False


def state_dir() -> Path:
    """Per-user cache directory (never inside the user's repository)."""
    override = os.environ.get("CSLIM_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "cslim"


# --------------------------------------------------------------------------- #
# session bookkeeping
# --------------------------------------------------------------------------- #


def _sessions_file() -> Path:
    return state_dir() / "sessions.json"


def _load_sessions() -> dict[str, float]:
    try:
        raw = json.loads(_sessions_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    return {
        key: float(value)
        for key, value in raw.items()
        if isinstance(value, (int, float)) and now - float(value) < _SESSION_TTL
    }


def _mark_session(key: str) -> None:
    sessions = _load_sessions()
    sessions[key] = time.time()
    path = _sessions_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sessions), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a hook must never fail on bookkeeping


# --------------------------------------------------------------------------- #
# map building + caching
# --------------------------------------------------------------------------- #


def _fingerprint(
    config: HookConfig, files: list[tuple[str, int, int]], *, index_only: bool
) -> str:
    payload = {
        "opts": [
            config.max_tokens,
            config.aggressive,
            config.model,
            sorted(config.languages),
            index_only,
            config.outline_only,
        ],
        "files": files,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _cache_path(fingerprint: str) -> Path:
    return state_dir() / "maps" / f"{fingerprint}.md"


def _prune_cache() -> None:
    directory = state_dir() / "maps"
    now = time.time()
    try:
        for entry in directory.glob("*.md"):
            if now - entry.stat().st_mtime > _CACHE_TTL:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def choose_tier(config: HookConfig, file_count: int) -> bool:
    """Decide whether this repository gets the index tier. Returns ``index_only``.

    Anthropic bills a freshly written cache token at roughly 1.25× base input
    and a cache read at roughly 0.1×, so one injected token costs about what a
    dozen cache-read tokens cost. A map therefore only pays for itself when it
    is *far* smaller than the exploration it prevents.

    On a small repository there is barely any exploration to prevent, so only
    the ultralight index can come out ahead. On a large one, exploration is
    expensive enough that full skeletons can earn their place.

    Measured, not assumed: see ``bench/README.md`` for the numbers behind the
    default threshold.
    """
    if config.index_only is not None:
        return config.index_only
    return file_count <= config.index_threshold


def build_map(config: HookConfig, cwd: Path) -> tuple[str, int, int, bool, bool]:
    """Return ``(payload, tokens, file_count, from_cache, index_only)``.

    The tree is fingerprinted by (path, mtime, size) so an unchanged repository
    reuses the previous render instead of re-parsing every file.
    """
    targets = list(config.paths) or [cwd]
    selected: list[Language] = []
    for name in config.languages:
        try:
            selected.append(Language(name))
        except ValueError:
            continue  # unknown language in settings.json: ignore, never crash
    discovery = DiscoveryOptions(languages=tuple(selected))

    discovered, _root, _warnings = discover(targets, discovery)
    if len(discovered) < config.min_files:
        return "", 0, len(discovered), False, False

    stamps: list[tuple[str, int, int]] = []
    for item in discovered:
        try:
            stat = item.path.stat()
        except OSError:
            continue
        stamps.append((item.rel_path, int(stat.st_mtime), stat.st_size))

    # An explicit --outline is a decision; the automatic tier choice is a
    # heuristic. The decision wins.
    index_only = False if config.outline_only else choose_tier(config, len(discovered))
    fingerprint = _fingerprint(config, stamps, index_only=index_only)
    cache_file = _cache_path(fingerprint)
    if cache_file.is_file():
        try:
            cached = cache_file.read_text(encoding="utf-8")
            header, _, body = cached.partition("\n")
            meta = json.loads(header)
            return body, int(meta["tokens"]), int(meta["files"]), True, index_only
        except (OSError, ValueError, KeyError):
            pass  # corrupt cache entry: fall through and rebuild

    request = CompressRequest(
        paths=tuple(targets),
        discovery=discovery,
        compression=CompressionOptions(aggressive=config.aggressive),
        render=RenderOptions(format="md", tree=False),
        outline_only=config.outline_only,
        model=config.model,
        max_tokens=config.max_tokens,
        index_only=index_only,
    )
    bundle = CompressionService().run(request)
    payload = bundle.payload
    tokens = bundle.stats.compressed
    count = bundle.stats.files

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        meta = json.dumps({"tokens": tokens, "files": count})
        cache_file.write_text(f"{meta}\n{payload}", encoding="utf-8")
        _prune_cache()
    except OSError:
        pass

    return payload, tokens, count, False, index_only


# --------------------------------------------------------------------------- #
# the hook entry point
# --------------------------------------------------------------------------- #

_SKELETON_PREAMBLE = (
    "Compressed architecture map of this project, generated by ClaudeSlim.\n"
    "Function bodies are elided (`...` / `{ ... }`); signatures, types, class\n"
    "hierarchies, imports and summary docstrings are verbatim.\n"
    "Use it to locate code instead of exploring the tree, then read in full only\n"
    "the files you actually need. It reflects the repository at session start.\n"
)

# The index tier contains no signatures at all, so it must not promise any:
# a preamble that oversells its payload sends Claude hunting for detail that
# was never injected, which costs exactly the exploration the map should save.
_INDEX_PREAMBLE = (
    "File index for this project, generated by ClaudeSlim: one line per file\n"
    "naming what it defines. No signatures, no types, no bodies.\n"
    "Use it to pick which file to open, then read that file in full.\n"
    "It reflects the repository at session start.\n"
)

#: Fingerprints of a map already present in the prompt (piped in by hand).
_MAP_MARKERS = (
    "# ClaudeSlim skeleton bundle",
    "# ClaudeSlim file index",
    "generated by ClaudeSlim",
)


def run_hook(stdin_text: str, config: HookConfig, *, dry_run: bool = False) -> HookOutcome:
    """Pure core of ``cslim hook``: JSON in, decision out. Never raises."""
    try:
        event: dict[str, Any] = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        return HookOutcome(False, "invalid hook payload on stdin")
    if not isinstance(event, dict):
        return HookOutcome(False, "hook payload was not an object")

    session_id = str(event.get("session_id") or "")
    cwd = Path(str(event.get("cwd") or Path.cwd()))

    # `cslim . | claude -p "..."` with automatic mode on would pay for the map
    # twice: once through the pipe, once through us. If a map is already in the
    # prompt, stand down.
    prompt = str(event.get("prompt") or "")
    if any(marker in prompt for marker in _MAP_MARKERS):
        return HookOutcome(False, "a cslim map is already in the prompt")

    # Scope the session key to the directory: the same session moving between
    # projects should get the map of each.
    key = f"{session_id}:{cwd}"
    if not config.every_prompt and session_id and key in _load_sessions():
        return HookOutcome(False, "already injected for this session")

    try:
        payload, tokens, files, cached, index_only = build_map(config, cwd)
    except Exception as exc:
        return HookOutcome(False, f"map build failed: {type(exc).__name__}: {exc}")

    if not payload:
        return HookOutcome(False, f"only {files} source file(s): map not worth it")

    preamble = _INDEX_PREAMBLE if index_only else _SKELETON_PREAMBLE
    context = f"{preamble}\n{payload}"
    tier = "index" if index_only else "skeleton"
    message = (
        f"cslim: injected a {humanize(tokens)}-token {tier} map of {files} files"
        f"{' (cached)' if cached else ''}"
    )
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    if not config.quiet:
        output["systemMessage"] = message

    if not dry_run and session_id:
        _mark_session(key)

    return HookOutcome(
        injected=True,
        reason=message,
        stdout=json.dumps(output),
        tokens=tokens,
        files=files,
        cached=cached,
    )
