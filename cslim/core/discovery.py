"""File discovery: what actually enters the compressor.

Cheap wins first — never even *read* ``node_modules``, lockfiles, build output or
binaries. ``.gitignore`` is honoured through ``pathspec`` when installed, with a
built-in glob matcher as fallback.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import CODE_EXTENSIONS, EXTENSION_MAP, Language, detect_language

__all__ = ["DiscoveredFile", "DiscoveryOptions", "common_root", "discover"]

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".vs",
        "node_modules", "bower_components", "vendor", "third_party",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        ".venv", "venv", "env", ".env.d", "site-packages", ".eggs",
        "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
        ".turbo", ".parcel-cache", "coverage", ".coverage", "htmlcov",
        ".terraform", ".serverless", ".gradle", ".dart_tool",
        "__snapshots__", ".cache", ".output",
    }
)

DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "*.min.js", "*.min.css", "*.map", "*.lock", "*.snap",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll", "*.exe", "*.bin",
    "*_pb2.py", "*.generated.*", "*.g.dart",
)


@dataclass(frozen=True, slots=True)
class DiscoveryOptions:
    include: tuple[str, ...] = ()
    """Glob whitelist applied to the path relative to the root."""
    exclude: tuple[str, ...] = ()
    languages: tuple[Language, ...] = ()
    """Restrict to these languages (empty = all supported)."""
    max_file_bytes: int = 400_000
    max_files: int = 5_000
    follow_symlinks: bool = False
    use_gitignore: bool = True
    hidden: bool = False
    all_files: bool = False
    """Include non-code text files (markdown, yaml, …) too."""

    def allowed_extensions(self) -> frozenset[str]:
        if self.languages:
            return frozenset(
                ext for ext, lang in EXTENSION_MAP.items() if lang in self.languages
            )
        if self.all_files:
            return frozenset(EXTENSION_MAP)
        return CODE_EXTENSIONS


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    rel_path: str
    language: Language
    size: int


def common_root(paths: Sequence[Path]) -> Path:
    """Nearest shared directory — used to build short, readable relative paths."""
    resolved = [p.resolve() for p in paths if p.exists()]
    if not resolved:
        return Path.cwd()
    dirs = [p if p.is_dir() else p.parent for p in resolved]
    if len(dirs) == 1:
        return dirs[0]
    parts = [d.parts for d in dirs]
    shared: list[str] = []
    for chunk in zip(*parts, strict=False):  # stop at the shortest: that's the prefix
        if len(set(chunk)) == 1:
            shared.append(chunk[0])
        else:
            break
    return Path(*shared) if shared else Path(os.sep)


class _GitignoreMatcher:
    """``pathspec`` when available, otherwise a small fnmatch-based subset."""

    def __init__(self, root: Path) -> None:
        self._spec = None
        self._patterns: list[str] = []
        gitignore = root / ".gitignore"
        if not gitignore.is_file():
            return
        try:
            lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return
        patterns = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        if not patterns:
            return
        try:
            import pathspec

            self._spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        except Exception:
            self._patterns = [p.rstrip("/") for p in patterns if not p.startswith("!")]

    def matches(self, rel_path: str) -> bool:
        if self._spec is not None:
            return bool(self._spec.match_file(rel_path))
        for pattern in self._patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(rel_path, f"{pattern}/*") or f"/{pattern}/" in f"/{rel_path}/":
                return True
        return False


def _is_binary(path: Path, probe: int = 2048) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(probe)
    except OSError:
        return True
    return b"\0" in chunk


def _matches_any(rel_path: str, name: str, globs: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in globs
    )


def discover(
    paths: Sequence[Path], options: DiscoveryOptions | None = None
) -> tuple[list[DiscoveredFile], Path, list[str]]:
    """Walk ``paths`` and return ``(files, root, warnings)``.

    Explicitly-named files bypass the extension filter — if you asked for it,
    you get it.
    """
    opts = options or DiscoveryOptions()
    targets = [Path(p) for p in paths] or [Path.cwd()]
    root = common_root(targets)
    warnings: list[str] = []
    extensions = opts.allowed_extensions()
    gitignore = _GitignoreMatcher(root) if opts.use_gitignore else None
    seen: set[Path] = set()
    found: list[DiscoveredFile] = []

    def relative(path: Path) -> str:
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.name

    def consider(path: Path, *, explicit: bool) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        rel = relative(resolved)
        name = resolved.name
        if not opts.hidden and not explicit and any(
            part.startswith(".") for part in Path(rel).parts
        ):
            return
        if _matches_any(rel, name, DEFAULT_IGNORE_GLOBS):
            return
        if opts.exclude and _matches_any(rel, name, opts.exclude):
            return
        if opts.include and not _matches_any(rel, name, opts.include):
            return
        if gitignore is not None and not explicit and gitignore.matches(rel):
            return
        language = detect_language(resolved)
        if not explicit:
            suffix = ".d.ts" if name.endswith(".d.ts") else resolved.suffix
            if suffix not in extensions:
                return
        if opts.languages and language not in opts.languages and not explicit:
            return
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            warnings.append(f"{rel}: {exc}")
            return
        if size > opts.max_file_bytes:
            warnings.append(f"{rel}: skipped, {size // 1024} KB > limit")
            return
        if size == 0:
            return
        if _is_binary(resolved):
            warnings.append(f"{rel}: skipped, binary")
            return
        seen.add(resolved)
        found.append(DiscoveredFile(resolved, rel, language, size))

    for target in targets:
        if not target.exists():
            warnings.append(f"{target}: not found")
            continue
        if target.is_file():
            consider(target, explicit=True)
            continue
        for dirpath, dirnames, filenames in os.walk(
            target, followlinks=opts.follow_symlinks
        ):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in DEFAULT_IGNORE_DIRS and (opts.hidden or not d.startswith("."))
            ]
            for filename in sorted(filenames):
                consider(Path(dirpath) / filename, explicit=False)
                if len(found) >= opts.max_files:
                    warnings.append(f"file limit reached ({opts.max_files})")
                    break
            if len(found) >= opts.max_files:
                break

    found.sort(key=lambda f: (f.language.value, f.rel_path))
    return found, root, warnings
