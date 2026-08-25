"""Workstream 2 subtractors, checked against captured real output.

Every fixture in ``tests/fixtures/`` is real: a genuine pytest run and 40
commits of pallets/flask history. Synthetic samples would let these tests pass
against output no tool ever sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cslim.core import clean_log, clean_terminal
from cslim.core.testrun import extract_failures
from cslim.core.tokenizer import HeuristicEstimator
from cslim.core.traces import TraceOptions, collapse_traces, is_vendor_path

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def saving(before: str, after: str) -> float:
    est = HeuristicEstimator()
    b = est.count(before)
    return 0.0 if not b else 1 - est.count(after) / b


# --------------------------------------------------------------------------- #
# stack traces
# --------------------------------------------------------------------------- #


def test_folds_the_recursion_in_a_real_pytest_run() -> None:
    raw = fixture("pytest_failures.txt")
    out = collapse_traces(raw)

    assert "⋮ same frame × 6" in out, "six identical frames should fold to one"
    assert out.count("return deep(n - 1)") == 1
    # the failure itself must survive intact
    assert 'raise ValueError("bottom reached")' in out
    assert "E       assert 66.0 == 70.0" in out
    assert saving(raw, out) > 0.10


def test_keeps_everything_when_folding_is_off() -> None:
    raw = fixture("pytest_failures.txt")
    out = collapse_traces(raw, TraceOptions(collapse_repeats=False, drop_vendor=False))
    assert out.count("return deep(n - 1)") == 6


def test_vendor_frames_collapse_to_the_boundary() -> None:
    trace = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "app.py", line 10, in handler',
            "    client.send(payload)",
            '  File "/x/.venv/lib/python3.12/site-packages/lib/a.py", line 1, in send',
            "    return _do(payload)",
            '  File "/x/.venv/lib/python3.12/site-packages/lib/b.py", line 2, in _do',
            "    return _encode(payload)",
            '  File "/x/.venv/lib/python3.12/site-packages/lib/c.py", line 3, in _encode',
            "    raise TypeError('nope')",
            "TypeError: nope",
        ]
    )
    out = collapse_traces(trace)
    assert "client.send(payload)" in out, "your own frame always survives"
    assert "3 frame(s) in third-party code" in out
    assert "_encode" in out, "the boundary frame is where the library raised"
    assert "TypeError: nope" in out
    assert "return _do(payload)" not in out


def test_normalises_cpythons_own_fold() -> None:
    trace = "\n".join(
        [
            '  File "a.py", line 1, in f',
            "    f()",
            "  [Previous line repeated 47 more times]",
            "RecursionError: maximum recursion depth exceeded",
        ]
    )
    out = collapse_traces(trace)
    assert "⋮ same frame × 48" in out
    assert "RecursionError" in out


@pytest.mark.parametrize(
    ("path", "vendor"),
    [
        ("/app/src/handler.py", False),
        ("/x/.venv/lib/python3.12/site-packages/requests/api.py", True),
        (r"C:\proj\.venv\Lib\site-packages\requests\api.py", True),
        ("node_modules/express/lib/router.js", True),
        ("tests/test_cart.py", False),
    ],
)
def test_vendor_detection(path: str, vendor: bool) -> None:
    assert is_vendor_path(path) is vendor


def test_clean_terminal_folds_frames_by_default() -> None:
    raw = fixture("pytest_failures.txt")
    assert "⋮ same frame × 6" in clean_terminal(raw)
    assert "⋮ same frame" not in clean_terminal(raw, collapse_frames=False)


# --------------------------------------------------------------------------- #
# failure extraction
# --------------------------------------------------------------------------- #


def test_extracts_only_failures_from_a_real_suite_run() -> None:
    """The realistic case: a big suite where one test fails."""
    raw = fixture("pytest_suite_one_failure.txt")
    out, green = extract_failures(raw)

    assert not green
    assert "AssertionError: assert 6 == 999" in out
    assert "short test summary info" in out
    assert "warnings summary" not in out
    assert "test session starts" not in out
    assert saving(raw, out) > 0.40, "a mostly-passing run is mostly removable"


def test_a_green_run_collapses_to_its_summary() -> None:
    raw = "collecting ...\n" + ".\n" * 200 + "90 passed in 0.77s\n"
    out, green = extract_failures(raw)
    assert green
    assert out == "90 passed in 0.77s"


def test_non_pytest_failures_fall_back_to_signatures() -> None:
    raw = "\n".join(
        ["building", "ok 1 - adds", "not ok 2 - subtracts", "  expected 4 got 5", "done"]
    )
    out, green = extract_failures(raw, context=1)
    assert not green
    assert "not ok 2 - subtracts" in out
    assert "expected 4 got 5" in out
    assert "building" not in out


def test_failures_win_over_a_pass_summary() -> None:
    """A run that both fails and prints 'passed' must not read as green."""
    raw = "FAILED tests/test_x.py::test_y - AssertionError\n1 failed, 89 passed in 1.0s\n"
    _out, green = extract_failures(raw)
    assert not green


# --------------------------------------------------------------------------- #
# git log
# --------------------------------------------------------------------------- #


def test_flattens_real_flask_history() -> None:
    raw = fixture("git_log_flask.txt")
    out = clean_log(raw)

    assert "commit d318b683471101618febed18996405ad26462110" not in out
    assert "d318b683" in out and "explain seek" in out
    assert "Author:" not in out and "Date:" not in out
    assert saving(raw, out) > 0.75


def test_grouping_beats_the_flat_form_on_real_history() -> None:
    raw = fixture("git_log_flask.txt")
    grouped = clean_log(raw)
    flat = clean_log(raw, group=False)
    assert saving(raw, grouped) > saving(raw, flat)
    # a run by one author on one day states the author once
    assert grouped.count("David Lord") < flat.count("David Lord")


def test_merge_commits_are_dropped_by_default() -> None:
    raw = fixture("git_log_flask.txt")
    assert "add `app.query` route decorator" not in clean_log(raw)
    assert "add `app.query` route decorator" in clean_log(raw, drop_merges=False)


def test_trailers_are_stripped_from_bodies() -> None:
    raw = "\n".join(
        [
            "commit abc1234567890",
            "Author: Ada <ada@example.com>",
            "Date:   2026-01-05 10:00:00 +0100",
            "",
            "    fix the parser",
            "",
            "    It mis-sliced generics.",
            "    Co-authored-by: Bob <bob@example.com>",
            "    Signed-off-by: Ada <ada@example.com>",
            "    Closes: #123",
            "",
        ]
    )
    out = clean_log(raw, keep_body=True)
    assert "It mis-sliced generics." in out
    assert "Co-authored-by" not in out
    assert "Signed-off-by" not in out
    assert "Closes" not in out
