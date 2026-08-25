"""Run a test command and keep only what failed.

A green run is a handful of tokens' worth of information — "everything passed" —
delivered in hundreds of lines of dots, collection headers, warnings summaries
and plugin banners. A red run buries the three lines that matter in the same
noise. This runs the command and emits the failures.

Subtractive: nothing is injected, so like the rest of ``cslim clean`` it cannot
cost more than not using it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .git_cleaner import ANSI_RE, clean_terminal

__all__ = ["TestRunResult", "extract_failures", "run_tests"]

#: Section banners pytest prints; the value says whether the section is worth
#: keeping when the run failed.
_PYTEST_SECTIONS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"^=+ (FAILURES|ERRORS) =+$"), True),
    (re.compile(r"^=+ short test summary info =+$"), True),
    (re.compile(r"^=+ warnings summary( \(final\))? =+$"), False),
    (re.compile(r"^=+ (test session starts|slowest \d+ durations) =+$"), False),
    (re.compile(r"^=+ .* =+$"), False),
)

#: Failure signatures for commands that are not pytest.
_GENERIC_FAILURE = re.compile(
    r"\b(FAIL(ED|URE)?|ERROR|Traceback \(most recent call last\)|AssertionError"
    r"|panic:|✗|✖|not ok \d)",
    re.IGNORECASE,
)

_PASS_ONLY = re.compile(r"^\s*(\d+ passed|OK|ok \d+|PASS)\b", re.IGNORECASE)


@dataclass(slots=True)
class TestRunResult:
    command: list[str]
    exit_code: int
    raw: str
    output: str
    """What a caller should show: failures only, or a one-line pass summary."""
    passed: bool = False
    raw_lines: int = 0
    kept_lines: int = 0
    warnings: list[str] = field(default_factory=list)


def _pytest_failures(lines: list[str]) -> list[str] | None:
    """Slice out pytest's failure sections; None when this isn't pytest output."""
    starts = [
        i
        for i, line in enumerate(lines)
        if _PYTEST_SECTIONS[0][0].match(line.strip())
        or _PYTEST_SECTIONS[1][0].match(line.strip())
    ]
    if not starts:
        return None

    kept: list[str] = []
    keeping = False
    for line in lines:
        stripped = line.strip()
        matched = False
        for pattern, wanted in _PYTEST_SECTIONS:
            if pattern.match(stripped):
                keeping = wanted
                matched = True
                break
        if matched:
            if keeping:
                kept.append(line)
            continue
        if keeping:
            kept.append(line)
    return kept or None


def _generic_failures(lines: list[str], context: int) -> list[str]:
    """Keep failure lines plus a little of what surrounds them."""
    hits = [i for i, line in enumerate(lines) if _GENERIC_FAILURE.search(line)]
    if not hits:
        return []
    wanted: set[int] = set()
    for index in hits:
        for offset in range(-context, context + 1):
            if 0 <= index + offset < len(lines):
                wanted.add(index + offset)
    out: list[str] = []
    previous = -2
    for index in sorted(wanted):
        if previous >= 0 and index > previous + 1:
            out.append("    ⋮")
        out.append(lines[index])
        previous = index
    return out


def extract_failures(text: str, *, context: int = 3) -> tuple[str, bool]:
    """Return ``(failures_only, looked_green)``.

    Tries pytest's own section structure first, because it is exact, and falls
    back to signature matching for any other runner.
    """
    cleaned = ANSI_RE.sub("", text)
    lines = [line.rstrip() for line in cleaned.split("\n")]

    sliced = _pytest_failures(lines)
    if sliced is not None:
        return "\n".join(sliced).strip(), False

    generic = _generic_failures(lines, context)
    if generic:
        return "\n".join(generic).strip(), False

    # Nothing looked like a failure: report the summary line if there is one.
    summary = [line for line in lines if _PASS_ONLY.match(line.strip())]
    return (summary[-1].strip() if summary else ""), True


def run_tests(
    command: list[str],
    *,
    cwd: Path | None = None,
    context: int = 3,
    timeout: float | None = None,
) -> TestRunResult:
    """Run ``command`` and keep only the failing parts of its output."""
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return TestRunResult(
            command,
            127,
            "",
            f"command not found: {command[0]}",
            warnings=[f"{command[0]} is not on PATH"],
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(
            command, 124, "", f"timed out after {timeout}s", warnings=["timeout"]
        )

    raw = (proc.stdout or "") + (proc.stderr or "")
    failures, green = extract_failures(raw, context=context)

    if proc.returncode == 0 and green:
        body = failures or "all tests passed"
    else:
        # A non-zero exit with nothing recognisable is the dangerous case: say
        # so and hand back the cleaned tail rather than an empty, reassuring
        # result.
        body = failures or clean_terminal(raw)[-4000:]

    return TestRunResult(
        command=command,
        exit_code=proc.returncode,
        raw=raw,
        output=body.strip(),
        passed=proc.returncode == 0,
        raw_lines=len(raw.split("\n")),
        kept_lines=len(body.split("\n")),
        warnings=(
            []
            if (proc.returncode == 0 or failures)
            else ["no recognisable failures in the output; showing the cleaned tail"]
        ),
    )
