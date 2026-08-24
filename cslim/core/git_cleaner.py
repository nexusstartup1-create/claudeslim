"""Git diff / log / terminal-output sanitizer.

A raw ``git diff`` wastes tokens on things Claude never reads: blob hashes, mode
bits, ``--- a/ +++ b/`` pairs, lockfile churn, minified bundles, whitespace-only
reflows and huge generated hunks. This module strips all of that while keeping
the semantics of the change intact.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DiffFile",
    "DiffResult",
    "GitCleanOptions",
    "GitError",
    "clean_diff",
    "clean_log",
    "clean_terminal",
    "git_diff",
    "git_log",
    "is_git_repo",
    "run_git",
]

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

_NOISE_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "copy from ",
    "copy to ",
    "no newline at end of file",
)

_DEFAULT_EXCLUDES = (
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|npm-shrinkwrap\.json)$",
    r"(^|/)(poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|composer\.lock|Gemfile\.lock)$",
    r"(^|/)go\.sum$",
    r"\.min\.(js|css)$",
    r"\.(map|snap|lock)$",
    r"(^|/)(dist|build|vendor|node_modules|__snapshots__)/",
    r"\.(png|jpe?g|gif|svg|ico|pdf|woff2?|ttf|eot|mp4|zip|tar|gz)$",
)


class GitError(RuntimeError):
    """Raised when a git subprocess fails or git is unavailable."""


@dataclass(frozen=True, slots=True)
class GitCleanOptions:
    keep_context: int = 0
    """Unchanged lines kept around each change block (0 = changes only)."""
    drop_binary: bool = True
    drop_excluded: bool = True
    ignore_whitespace_only: bool = True
    max_hunk_lines: int = 400
    """Hunks longer than this are truncated with a marker."""
    max_file_lines: int = 2_000
    compact_hunk_headers: bool = True
    """Rewrite ``@@ -1,7 +1,9 @@ def foo()`` as ``@@ def foo()``."""
    strip_ansi: bool = True
    extra_excludes: tuple[str, ...] = ()

    def excludes(self) -> tuple[re.Pattern[str], ...]:
        patterns = _DEFAULT_EXCLUDES + self.extra_excludes if self.drop_excluded else self.extra_excludes
        return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


@dataclass(slots=True)
class DiffFile:
    path: str
    status: str = "modified"
    added: int = 0
    removed: int = 0
    lines: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def kept(self) -> bool:
        return not self.skipped_reason and bool(self.lines)


@dataclass(slots=True)
class DiffResult:
    files: list[DiffFile] = field(default_factory=list)
    text: str = ""
    original_lines: int = 0
    cleaned_lines: int = 0

    @property
    def kept_files(self) -> list[DiffFile]:
        return [f for f in self.files if f.kept]

    @property
    def skipped_files(self) -> list[DiffFile]:
        return [f for f in self.files if f.skipped_reason]

    @property
    def added(self) -> int:
        return sum(f.added for f in self.kept_files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.kept_files)


_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?P<tail>.*)$")


def _normalize(line: str) -> str:
    return "".join(line.split())


def _hunk_is_whitespace_only(block: Sequence[str]) -> bool:
    removed = [line[1:] for line in block if line.startswith("-")]
    added = [line[1:] for line in block if line.startswith("+")]
    if not removed and not added:
        return True
    return [_normalize(line) for line in removed] == [_normalize(line) for line in added]


def clean_diff(text: str, options: GitCleanOptions | None = None) -> DiffResult:
    """Strip metadata noise from a unified diff, keeping the actual changes."""
    opts = options or GitCleanOptions()
    if opts.strip_ansi:
        text = ANSI_RE.sub("", text)
    excludes = opts.excludes()
    raw_lines = text.splitlines()
    result = DiffResult(original_lines=len(raw_lines))

    current: DiffFile | None = None
    hunk: list[str] = []
    hunk_header = ""

    def flush_hunk() -> None:
        nonlocal hunk, hunk_header
        if current is None or not hunk:
            hunk = []
            hunk_header = ""
            return
        if opts.ignore_whitespace_only and _hunk_is_whitespace_only(hunk):
            hunk = []
            hunk_header = ""
            return
        body = _trim_context(hunk, opts.keep_context)
        if len(body) > opts.max_hunk_lines:
            omitted = len(body) - opts.max_hunk_lines
            body = [*body[: opts.max_hunk_lines], f"… {omitted} more changed lines"]
        if hunk_header:
            current.lines.append(hunk_header)
        current.lines.extend(body)
        current.added += sum(1 for line in body if line.startswith("+"))
        current.removed += sum(1 for line in body if line.startswith("-"))
        hunk = []
        hunk_header = ""

    def flush_file() -> None:
        nonlocal current
        flush_hunk()
        if current is not None:
            if len(current.lines) > opts.max_file_lines:
                omitted = len(current.lines) - opts.max_file_lines
                current.lines = current.lines[: opts.max_file_lines]
                current.lines.append(f"… file diff truncated, {omitted} lines omitted")
            result.files.append(current)
        current = None

    for line in raw_lines:
        header = _DIFF_HEADER.match(line)
        if header:
            flush_file()
            path = header.group("b")
            current = DiffFile(path=path)
            if any(p.search(path) for p in excludes):
                current.skipped_reason = "generated/lock/binary path"
            continue

        if current is None:
            continue
        if current.skipped_reason:
            continue

        lower = line.lower()
        if any(lower.startswith(p) for p in _NOISE_PREFIXES):
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if opts.drop_binary:
                current.skipped_reason = "binary"
            continue
        if line.startswith("new file mode"):
            current.status = "added"
            continue
        if line.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if line.startswith("rename from "):
            current.status = f"renamed from {line[len('rename from '):]}"
            continue
        if line.startswith("rename to "):
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        hunk_match = _HUNK.match(line)
        if hunk_match:
            flush_hunk()
            tail = hunk_match.group("tail").strip()
            if opts.compact_hunk_headers:
                hunk_header = f"@@ {tail}" if tail else "@@"
            else:
                hunk_header = line
            continue

        if line and line[0] in "+- ":
            hunk.append(line)
        elif line.startswith("\\"):
            continue

    flush_file()

    chunks: list[str] = []
    for f in result.kept_files:
        chunks.append(f"### {f.path} ({f.status}) +{f.added}/-{f.removed}")
        chunks.extend(f.lines)
        chunks.append("")
    skipped = result.skipped_files
    if skipped:
        names = ", ".join(f.path for f in skipped[:12])
        more = f" (+{len(skipped) - 12} more)" if len(skipped) > 12 else ""
        chunks.append(f"# skipped {len(skipped)} noisy file(s): {names}{more}")

    result.text = "\n".join(chunks).strip()
    result.cleaned_lines = len(result.text.splitlines())
    return result


def _trim_context(block: Sequence[str], keep: int) -> list[str]:
    """Keep only ``keep`` unchanged lines around each run of +/- lines."""
    if keep < 0:
        return list(block)
    changed = [i for i, line in enumerate(block) if line[:1] in "+-"]
    if not changed:
        return []
    if keep == 0:
        return [block[i] for i in changed]
    wanted: set[int] = set()
    for index in changed:
        for offset in range(-keep, keep + 1):
            candidate = index + offset
            if 0 <= candidate < len(block):
                wanted.add(candidate)
    out: list[str] = []
    previous = -2
    for index in sorted(wanted):
        if previous >= 0 and index > previous + 1:
            out.append("…")
        out.append(block[index])
        previous = index
    return out


# --------------------------------------------------------------------------- #
# git log
# --------------------------------------------------------------------------- #

_COMMIT = re.compile(r"^commit ([0-9a-f]{7,40})")
_AUTHOR = re.compile(r"^Author:\s*(.+?)\s*<(.*)>\s*$")
_DATE = re.compile(r"^Date:\s*(.+)$")
_MERGE = re.compile(r"^Merge:\s")


def clean_log(
    text: str,
    *,
    keep_body: bool = False,
    drop_merges: bool = True,
    max_subject: int = 100,
) -> str:
    """Flatten ``git log`` output into ``<sha> <date> <author> — <subject>``."""
    text = ANSI_RE.sub("", text)
    out: list[str] = []
    sha = author = date = ""
    subject = ""
    body: list[str] = []
    is_merge = False

    def flush() -> None:
        nonlocal sha, author, date, subject, body, is_merge
        if sha and not (drop_merges and is_merge):
            head = f"{sha[:8]} {date} {author} — {subject[:max_subject]}"
            out.append(head.strip())
            if keep_body:
                out.extend(f"    {line}" for line in body[:5] if line.strip())
        sha = author = date = subject = ""
        body = []
        is_merge = False

    for raw in text.splitlines():
        line = raw.rstrip()
        commit = _COMMIT.match(line)
        if commit:
            flush()
            sha = commit.group(1)
            continue
        if not sha:
            # Tolerate --oneline / custom formats: pass them through verbatim.
            if line.strip():
                out.append(line.strip())
            continue
        if _MERGE.match(line):
            is_merge = True
            continue
        author_match = _AUTHOR.match(line)
        if author_match:
            author = author_match.group(1)
            continue
        date_match = _DATE.match(line)
        if date_match:
            date = _short_date(date_match.group(1))
            continue
        if line.startswith(("Commit:", "CommitDate:", "AuthorDate:", "Reflog:")):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if not subject:
            subject = stripped
        else:
            body.append(stripped)
    flush()
    return "\n".join(out).strip()


def _short_date(value: str) -> str:
    text = value.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-" and text[:4].isdigit():
        return text[:10]  # `--date=iso` / `--date=short`
    parts = text.split()
    # "Mon Jan 5 12:00:00 2026 +0100" -> "2026-01-05"
    if len(parts) >= 5:
        months = {
            m: i
            for i, m in enumerate(
                ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
            )
        }
        month = months.get(parts[1])
        if month and parts[2].isdigit() and parts[4].isdigit():
            return f"{parts[4]}-{month:02d}-{int(parts[2]):02d}"
    return text[:24]


# --------------------------------------------------------------------------- #
# terminal output
# --------------------------------------------------------------------------- #

_TERMINAL_NOISE = (
    re.compile(r"^\s*\[?\d+%\]?\s*[│|█▏▎▍▌▋▊▉ .=\-#>]*$"),
    re.compile(r"Requirement already satisfied:"),
    re.compile(r"^\s*(Downloading|Collecting|Using cached|Installing collected)\b"),
    re.compile(r"^\s*(added|removed|changed|audited)\s+\d+\s+packages?"),
    re.compile(r"^\s*npm (notice|WARN deprecated)\b"),
    re.compile(r"^\s*\d+ vulnerabilit(y|ies)"),
    re.compile(r"^\s*$"),
)

_TIMESTAMP = re.compile(
    r"^\s*(\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\]?\s*)"
)


def clean_terminal(
    text: str,
    *,
    strip_timestamps: bool = True,
    collapse_repeats: bool = True,
    max_line_len: int = 400,
    max_lines: int = 4_000,
) -> str:
    """De-noise pasted terminal / CI output before feeding it to Claude."""
    text = ANSI_RE.sub("", text)
    lines: list[str] = []
    # NB: split on "\n" only — str.splitlines() would also break on the bare
    # "\r" that progress bars use to rewrite themselves, which is exactly the
    # structure we need to collapse here.
    for raw in text.split("\n"):
        # A progress bar rewrites the same line: keep only its final state.
        line = raw.rstrip("\r").split("\r")[-1].rstrip()
        if strip_timestamps:
            line = _TIMESTAMP.sub("", line)
        if any(p.search(line) for p in _TERMINAL_NOISE):
            # A dropped blank line still separates blocks, but never doubles up.
            if not line.strip() and lines and lines[-1] != "":
                lines.append("")
            continue
        if len(line) > max_line_len:
            line = line[:max_line_len] + f"… (+{len(line) - max_line_len} chars)"
        lines.append(line)

    if collapse_repeats:
        collapsed: list[str] = []
        count = 1
        for line in lines:
            if collapsed and line == collapsed[-1] and line.strip():
                count += 1
                continue
            if count > 1:
                collapsed[-1] = f"{collapsed[-1]}  (×{count})"
                count = 1
            collapsed.append(line)
        if count > 1:
            collapsed[-1] = f"{collapsed[-1]}  (×{count})"
        lines = collapsed

    if len(lines) > max_lines:
        head = lines[: max_lines // 2]
        tail = lines[-max_lines // 2 :]
        lines = [*head, f"… {len(lines) - max_lines} lines omitted …", *tail]

    out: list[str] = []
    blank = True
    for line in lines:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def run_git(args: Sequence[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment specific
        raise GitError("git executable not found in PATH") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed"
        # git answers some mistakes with a full usage dump. The useful part is
        # always at the top; the other hundred lines are noise in a terminal.
        lines = message.splitlines()
        if len(lines) > 4:
            message = "\n".join(lines[:4]) + f"\n… (+{len(lines) - 4} more lines from git)"
        raise GitError(message)
    return proc.stdout


def is_git_repo(cwd: Path | None = None) -> bool:
    """True when ``cwd`` sits inside a git working tree.

    Worth checking before anything else: outside a repository ``git diff``
    silently switches to ``--no-index`` mode, where ``--cached`` doesn't exist,
    and the resulting error explains nothing.
    """
    try:
        run_git(["rev-parse", "--git-dir"], cwd=cwd)
    except GitError:
        return False
    return True


def git_diff(
    *,
    base: str | None = None,
    staged: bool = False,
    paths: Iterable[str] = (),
    context: int = 3,
    cwd: Path | None = None,
) -> str:
    args = ["diff", f"-U{max(0, context)}", "--no-color", "--no-ext-diff"]
    if staged:
        args.append("--cached")
    if base:
        args.append(base)
    # Always terminate the revision list with `--`, even when no paths follow:
    # without it a branch and a file sharing a name make git bail out with
    # "ambiguous argument ... known to git", which reads like a cslim failure.
    args.append("--")
    args.extend(paths)
    return run_git(args, cwd=cwd)


def git_log(
    *,
    count: int = 30,
    since: str | None = None,
    author: str | None = None,
    paths: Iterable[str] = (),
    cwd: Path | None = None,
) -> str:
    args = ["log", f"-n{max(1, count)}", "--no-color", "--date=iso"]
    if since:
        args.append(f"--since={since}")
    if author:
        args.append(f"--author={author}")
    paths = list(paths)
    if paths:
        args.append("--")
        args.extend(paths)
    return run_git(args, cwd=cwd)
