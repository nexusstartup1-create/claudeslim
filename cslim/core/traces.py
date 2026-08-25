"""Stack-trace subtraction.

A failing test run is mostly frames Claude cannot act on: the same recursive
frame forty times, and the walk down through ``site-packages`` that ends in
someone else's ``raise``. What matters is the frame in *your* code and the
assertion text. This removes the rest.

Subtractive like the rest of ``cslim clean``: it deletes lines and injects
nothing, so it cannot cost more than not using it — see `bench/README.md` for
why that distinction decides everything in this project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

__all__ = ["TraceOptions", "collapse_traces", "is_vendor_path"]

#: Paths whose frames are almost never the bug you are chasing.
VENDOR_MARKERS: tuple[str, ...] = (
    "site-packages",
    "dist-packages",
    "node_modules",
    "/.venv/",
    "\\.venv\\",
    "/vendor/",
    "lib/python3",
    "/usr/lib/python",
    "<frozen importlib",
)


@dataclass(frozen=True, slots=True)
class TraceOptions:
    collapse_repeats: bool = True
    """Fold consecutive identical frames — the signature of recursion."""
    drop_vendor: bool = True
    """Replace runs of third-party frames with a one-line summary."""
    keep_vendor_boundary: bool = True
    """Keep the last vendor frame: it is where your code handed control over."""
    min_repeat: int = 2
    max_frames: int = 40
    """Frames kept per traceback before the middle is elided."""


# A CPython frame header: `  File "x.py", line 7, in deep`
_CPY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>.*)$')

# A pytest short-traceback frame: `test_cart.py:7: in deep`
_PYTEST_FRAME = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+): in (?P<func>.+)$")

# Python's own recursion fold, which we normalise rather than double-count.
_ALREADY_FOLDED = re.compile(r"^\s*\[Previous line repeated (\d+) more times?\]\s*$")


def is_vendor_path(path: str) -> bool:
    normalised = path.replace("\\", "/")
    return any(marker.replace("\\", "/") in normalised for marker in VENDOR_MARKERS)


@dataclass(slots=True)
class _Frame:
    """One stack frame: its header line plus the source lines under it."""

    path: str
    line: str
    func: str
    lines: list[str]

    @property
    def key(self) -> tuple[str, str, str]:
        """What makes two frames "the same" — location and function, not text."""
        return (self.path, self.line, self.func)

    @property
    def vendor(self) -> bool:
        return is_vendor_path(self.path)

    def short(self) -> str:
        return f"{PurePosixPath(self.path.replace(chr(92), '/')).name}:{self.line} in {self.func}"


def _frame_at(lines: list[str], i: int) -> tuple[_Frame, int] | None:
    """Parse a frame starting at ``i``; return it and the index after it."""
    match = _CPY_FRAME.match(lines[i]) or _PYTEST_FRAME.match(lines[i])
    if match is None:
        return None
    body = [lines[i]]
    j = i + 1
    # A frame owns the indented source lines that follow it, up to the next
    # frame header or a line that starts a new (unindented) section.
    while j < len(lines):
        nxt = lines[j]
        if not nxt.strip():
            break
        if _CPY_FRAME.match(nxt) or _PYTEST_FRAME.match(nxt):
            break
        if _ALREADY_FOLDED.match(nxt):
            # CPython's own fold marker is indented, so it would otherwise be
            # swallowed as a source line and never reach the normaliser.
            break
        if not nxt.startswith((" ", "\t")):
            break
        body.append(nxt)
        j += 1
    return _Frame(match.group("path"), match.group("line"), match.group("func"), body), j


def collapse_traces(text: str, options: TraceOptions | None = None) -> str:
    """Fold repeated frames and third-party frames out of a traceback."""
    opts = options or TraceOptions()
    lines = text.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        parsed = _frame_at(lines, i)
        if parsed is None:
            folded = _ALREADY_FOLDED.match(lines[i])
            if folded and opts.collapse_repeats:
                # CPython already folded this run; keep its count in our idiom.
                out.append(f"    ⋮ same frame × {int(folded.group(1)) + 1}")
            else:
                out.append(lines[i])
            i += 1
            continue

        # Gather the whole contiguous run of frames — one traceback.
        frames: list[_Frame] = []
        while True:
            frames.append(parsed[0])
            i = parsed[1]
            if i >= len(lines):
                break
            nxt = _frame_at(lines, i)
            if nxt is None:
                break
            parsed = nxt

        out.extend(_render(frames, opts))

    return "\n".join(out)


def _render(frames: list[_Frame], opts: TraceOptions) -> list[str]:
    out: list[str] = []
    index = 0
    kept = 0

    while index < len(frames):
        frame = frames[index]

        # 1. a run of identical frames — recursion
        if opts.collapse_repeats:
            run = 1
            while index + run < len(frames) and frames[index + run].key == frame.key:
                run += 1
            if run >= opts.min_repeat:
                out.extend(frame.lines)
                out.append(f"    ⋮ same frame × {run}")
                index += run
                kept += 1
                continue

        # 2. a run of third-party frames — someone else's code
        if opts.drop_vendor and frame.vendor:
            run = 1
            while index + run < len(frames) and frames[index + run].vendor:
                run += 1
            boundary = frames[index + run - 1]
            if run == 1 and opts.keep_vendor_boundary:
                out.extend(frame.lines)
            else:
                out.append(f"    ⋮ {run} frame(s) in third-party code")
                if opts.keep_vendor_boundary:
                    # the last one is where the library actually raised
                    out.extend(boundary.lines)
            index += run
            kept += 1
            continue

        out.extend(frame.lines)
        index += 1
        kept += 1

        # 3. a very deep trace: keep the ends, drop the middle
        if kept == opts.max_frames and len(frames) - index > 2:
            out.append(f"    ⋮ {len(frames) - index - 1} frame(s) elided")
            index = len(frames) - 1

    return out
