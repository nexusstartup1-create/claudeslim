"""Smoke + edge-case tests for the ClaudeSlim engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from cslim.core import (
    CompressionOptions,
    GitCleanOptions,
    Language,
    RenderOptions,
    clean_diff,
    clean_log,
    clean_terminal,
    compress_paths,
    compress_source,
    detect_language,
)
from cslim.core.tokenizer import HeuristicEstimator, TokenBudget, fit_to_budget, resolve_model

PY_SOURCE = '''
"""Module docstring.

Second paragraph that must be dropped in summary mode.
"""
from __future__ import annotations
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX = 10
BIG = {"aaaa": 1, "bbbb": 2, "cccc": 3, "dddd": 4, "eeee": 5, "ffff": 6, "gggg": 7, "hhhh": 8}


@dataclass(frozen=True)
class Widget(Base, metaclass=Meta):
    """A widget."""

    name: str
    size: int = 3

    def render(self, *, loud: bool = False) -> str:
        """Render it."""
        total = 0
        for i in range(10):
            total += i
        return str(total)

    def _hidden(self) -> None:
        pass

    def __init__(self, name: str) -> None:
        self.name = name


async def fetch(url: str, *args: int, timeout: float = 1.0, **kw: str) -> bytes:
    async with session() as s:
        return await s.get(url)


def _private_helper() -> None:
    pass
'''


def compress_py(source: str, **kwargs: object) -> str:
    options = CompressionOptions(**kwargs)  # type: ignore[arg-type]
    return compress_source(source, Path("x.py"), Language.PYTHON, options).text


# --------------------------------------------------------------------------- #
# python
# --------------------------------------------------------------------------- #


def test_python_keeps_signatures_and_drops_bodies() -> None:
    out = compress_py(PY_SOURCE)
    assert "class Widget(Base, metaclass=Meta):" in out
    assert "def render(self, *, loud: bool=False) -> str:" in out
    assert "async def fetch(url: str, *args: int, timeout: float=1.0, **kw: str) -> bytes:" in out
    assert "total += i" not in out
    assert "await s.get" not in out


def test_python_privacy_rules() -> None:
    out = compress_py(PY_SOURCE)
    assert "_hidden" not in out
    assert "_private_helper" not in out
    assert "__init__" in out, "public dunders survive --no-private"
    assert "_hidden" in compress_py(PY_SOURCE, keep_private=True)


def test_python_docstring_modes() -> None:
    summary = compress_py(PY_SOURCE, docstrings="summary")
    assert "Module docstring." in summary
    assert "Second paragraph" not in summary
    assert "Second paragraph" in compress_py(PY_SOURCE, docstrings="full")
    assert '"""' not in compress_py(PY_SOURCE, docstrings="none")


def test_python_constants_and_long_values() -> None:
    out = compress_py(PY_SOURCE)
    assert "MAX = 10" in out
    assert "BIG = ..." in out, "long literals collapse"
    assert "if TYPE_CHECKING:" in out
    assert "from collections.abc import Iterator" in out


def test_python_aggressive_drops_everything_but_signatures() -> None:
    out = compress_py(PY_SOURCE, aggressive=True)
    assert "def render" in out
    assert "import os" not in out
    assert '"""' not in out
    assert "MAX = 10" not in out


def test_python_syntax_error_falls_back() -> None:
    result = compress_source(
        "def broken(:\n  pass", Path("bad.py"), Language.PYTHON, CompressionOptions()
    )
    assert result.fallback
    assert result.errors and "parse error" in result.errors[0]
    assert "def broken" in result.text


def test_python_output_is_valid_python() -> None:
    import ast

    ast.parse(compress_py(PY_SOURCE))
    ast.parse(compress_py(PY_SOURCE, aggressive=True))


# --------------------------------------------------------------------------- #
# brace languages
# --------------------------------------------------------------------------- #

TS_SOURCE = """
import { a } from "b";

export interface User {
  id: string;
  greet(loud: boolean): string;
}

const D = { brace: "{ not a block }", n: 1 };

export async function fetchUser(
  id: string,
  opts = D,
): Promise<User> {
  if (!id) { throw new Error("no"); }
  return { id } as User;
}

export class Service {
  private cache = new Map();
  readonly base: string;
  async get(id: string): Promise<User | null> {
    return null;
  }
}
"""

GO_SOURCE = """
package store

import (
\t"context"
\t"fmt"
)

type User struct {
\tID string `json:"id"`
}

type Repo interface {
\tGet(ctx context.Context, id string) (*User, error)
}

func New(dsn string) (*Repo, error) {
\tif dsn == "" {
\t\treturn nil, fmt.Errorf("empty")
\t}
\treturn nil, nil
}
"""


def test_typescript_structure() -> None:
    out = compress_source(TS_SOURCE, Path("a.ts"), Language.TYPESCRIPT, CompressionOptions()).text
    assert "export interface User {" in out
    assert "greet(loud: boolean): string;" in out
    assert "): Promise<User> { ... }" in out, "multi-line signature is kept whole"
    assert "throw new Error" not in out
    assert 'const D = { brace: "{ not a block }", n: 1 };' in out, "braces in strings ignored"
    assert "private cache" not in out
    assert "readonly base: string;" in out
    assert "async get(id: string): Promise<User | null> { ... }" in out


def test_go_structure() -> None:
    out = compress_source(GO_SOURCE, Path("a.go"), Language.GO, CompressionOptions()).text
    assert '"context"' in out and '"fmt"' in out, "import group members survive"
    assert 'ID string `json:"id"`' in out
    assert "Get(ctx context.Context, id string) (*User, error)" in out
    assert "func New(dsn string) (*Repo, error) { ... }" in out
    assert "return nil, nil" not in out


def test_brace_scanner_is_balanced() -> None:
    out = compress_source(TS_SOURCE, Path("a.ts"), Language.TYPESCRIPT, CompressionOptions()).text
    assert out.count("{") == out.count("}")


# --------------------------------------------------------------------------- #
# tokenizer / budget
# --------------------------------------------------------------------------- #


def test_heuristic_estimator_is_monotonic_and_sane() -> None:
    est = HeuristicEstimator()
    assert est.count("") == 0
    assert est.count("def f(x): return x") > 4
    assert est.count(PY_SOURCE) > est.count(PY_SOURCE[:100])
    # A BPE tokenizer lands around 3-5 characters per token on source code.
    ratio = len(PY_SOURCE) / est.count(PY_SOURCE)
    assert 2.0 < ratio < 6.0, ratio


def test_budget_reserves_room_for_the_answer() -> None:
    budget = TokenBudget.for_model(resolve_model("sonnet"))
    assert budget.usable < budget.window
    assert TokenBudget.for_model(resolve_model("sonnet"), max_tokens=5_000).usable == 5_000


def test_fit_to_budget_keeps_a_prefix() -> None:
    plan = fit_to_budget([100, 100, 5_000, 10], limit=250)
    assert plan.kept == [0, 1]
    assert plan.dropped == [2, 3], "no reordering: the payload stays coherent"
    assert not plan.fits


# --------------------------------------------------------------------------- #
# git cleaner
# --------------------------------------------------------------------------- #

DIFF = """diff --git a/src/app.py b/src/app.py
index 8e44d29..9b4a4fe 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,5 +1,5 @@ def main():
 a = 1
-b = 2
+b = 22
 c = 3
diff --git a/package-lock.json b/package-lock.json
index c160618..dfd3313 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{"a": 1}
+{"a": 2}
diff --git a/ws.py b/ws.py
index 111..222 100644
--- a/ws.py
+++ b/ws.py
@@ -1 +1 @@
-x = 1
+x  =  1
"""


def test_clean_diff_strips_noise() -> None:
    result = clean_diff(DIFF)
    assert "index 8e44d29" not in result.text
    assert "--- a/" not in result.text and "+++ b/" not in result.text
    assert "package-lock.json" not in result.text.split("# skipped")[0]
    assert "-b = 2" in result.text and "+b = 22" in result.text
    assert " a = 1" not in result.text, "context dropped by default"
    assert "@@ def main():" in result.text, "hunk header keeps the enclosing symbol"
    assert result.added == 1 and result.removed == 1
    assert len(result.kept_files) == 1


def test_clean_diff_drops_whitespace_only_hunks() -> None:
    assert "ws.py" not in clean_diff(DIFF).text
    assert "ws.py" in clean_diff(DIFF, GitCleanOptions(ignore_whitespace_only=False)).text


def test_clean_diff_keeps_context_when_asked() -> None:
    result = clean_diff(DIFF, GitCleanOptions(keep_context=1))
    assert " a = 1" in result.text


def test_clean_log() -> None:
    raw = (
        "commit abcdef1234567890\n"
        "Merge: 111 222\n"
        "Author: Ada <ada@example.com>\n"
        "Date:   2026-01-05 10:00:00 +0100\n\n"
        "    merge branch\n\n"
        "commit ffffff1234567890\n"
        "Author: Bob <bob@example.com>\n"
        "Date:   2026-01-04 09:00:00 +0100\n\n"
        "    fix the parser\n"
    )
    out = clean_log(raw)
    assert "merge branch" not in out, "merge commits dropped by default"
    assert out == "ffffff12 2026-01-04 Bob — fix the parser"
    assert "merge branch" in clean_log(raw, drop_merges=False)


def test_clean_terminal() -> None:
    raw = (
        "\x1b[32mINFO\x1b[0m start\n"
        "2026-01-05 10:00:00 dup\ndup\ndup\n"
        "Requirement already satisfied: six\n"
        "downloading...\rdone\n"
    )
    out = clean_terminal(raw)
    assert "\x1b[" not in out
    assert "INFO start" in out
    assert "dup  (×3)" in out
    assert "Requirement already satisfied" not in out
    assert "done" in out and "downloading" not in out


# --------------------------------------------------------------------------- #
# end-to-end
# --------------------------------------------------------------------------- #


def test_end_to_end_bundle(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(PY_SOURCE, encoding="utf-8")
    (tmp_path / "pkg" / "app.ts").write_text(TS_SOURCE, encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.ts").write_text(TS_SOURCE, encoding="utf-8")
    (tmp_path / "pkg" / "yarn.lock").write_text("noise\n", encoding="utf-8")

    bundle = compress_paths([tmp_path], render=RenderOptions(format="md"))
    names = {f.rel_path for f in bundle.files}
    assert names == {"pkg/mod.py", "pkg/app.ts"}, "ignore lists applied"
    # These fixtures are declaration-heavy (little body to remove); real code
    # lands far higher — cslim on its own core/ measures ~0.77.
    assert bundle.stats.ratio > 0.2
    assert "total += i" not in bundle.payload
    assert "throw new Error" not in bundle.payload
    assert "```python" in bundle.payload and "```typescript" in bundle.payload
    assert "ClaudeSlim" in bundle.payload
    assert bundle.symbols()


def test_render_formats(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(PY_SOURCE, encoding="utf-8")
    xml = compress_paths([tmp_path], render=RenderOptions(format="xml")).payload
    assert '<file path="m.py" lang="python">' in xml and "</codebase>" in xml
    plain = compress_paths([tmp_path], render=RenderOptions(format="plain")).payload
    assert "==== m.py ====" in plain


def test_budget_downgrades_instead_of_dropping(tmp_path: Path) -> None:
    """A tight budget must still leave Claude a complete map of the project.

    The old behaviour truncated the tail, so Claude never learned those files
    existed. Now they survive as one-line index entries.
    """
    from cslim.core.models import DetailLevel

    for i in range(6):
        (tmp_path / f"m{i}.py").write_text(PY_SOURCE, encoding="utf-8")
    bundle = compress_paths([tmp_path], max_tokens=400)

    assert bundle.stats.compressed <= 400, "budget respected"
    assert not bundle.dropped, "nothing vanishes entirely"
    assert any(f.level is DetailLevel.INDEX for f in bundle.files)
    for f in bundle.files:
        assert f.rel_path in bundle.payload, "every file is still discoverable"


def test_generous_budget_keeps_full_skeletons(tmp_path: Path) -> None:
    from cslim.core.models import DetailLevel

    for i in range(3):
        (tmp_path / f"m{i}.py").write_text(PY_SOURCE, encoding="utf-8")
    bundle = compress_paths([tmp_path], max_tokens=100_000)
    assert all(f.level is DetailLevel.SKELETON for f in bundle.files)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.py", Language.PYTHON),
        ("a.tsx", Language.TYPESCRIPT),
        ("a.d.ts", Language.TYPESCRIPT),
        ("a.mjs", Language.JAVASCRIPT),
        ("a.go", Language.GO),
        ("a.rs", Language.UNKNOWN),
    ],
)
def test_detect_language(name: str, expected: Language) -> None:
    assert detect_language(Path(name)) is expected
