"""The OUTLINE tier: grouped names, between an index line and a skeleton."""

from __future__ import annotations

from pathlib import Path

import pytest

from cslim.core import DiscoveryOptions, compress_paths
from cslim.core.models import DetailLevel
from cslim.core.renderer import build_outline, environment_variables
from cslim.core.service import CompressionService, CompressRequest

MODULE = '''
import os
from flask import Flask
from flask import request

MAX_RETRIES = 5
TIMEOUT = 30


def connect(dsn: str) -> None:
    url = os.environ["DATABASE_URL"]
    key = os.getenv("SECRET_KEY")
    ...


class Store:
    def get(self, key: str) -> str:
        ...

    def put(self, key: str, value: str) -> None:
        ...

    def __repr__(self) -> str:
        ...
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "store.py").write_text(MODULE, encoding="utf-8")
    return tmp_path


def outline_of(root: Path) -> str:
    bundle = compress_paths([root], discovery=DiscoveryOptions())
    return build_outline(bundle.files[0])


# --------------------------------------------------------------------------- #
# content
# --------------------------------------------------------------------------- #


def test_groups_what_a_file_contains(repo: Path) -> None:
    text = outline_of(repo)
    assert "store.py" in text
    assert "imports: " in text
    assert "constants: " in text
    assert "classes: Store" in text
    assert "functions: connect" in text
    assert "methods: " in text and "get" in text and "put" in text


def test_carries_no_signatures(repo: Path) -> None:
    """The whole point of the tier: names, not parameter lists."""
    text = outline_of(repo)
    assert "dsn: str" not in text
    assert "-> None" not in text
    assert "(" not in text.replace("store.py", "")


def test_imports_are_deduplicated(repo: Path) -> None:
    """`from flask import Flask` and `from flask import request` name one module."""
    text = outline_of(repo)
    imports = next(line for line in text.splitlines() if "imports:" in line)
    assert imports.count("flask") == 1


def test_dunder_methods_are_left_out(repo: Path) -> None:
    assert "__repr__" not in outline_of(repo)


def test_environment_variables_are_called_out(repo: Path) -> None:
    """Env vars answer "how is this deployed" and a skeleton hides them."""
    text = outline_of(repo)
    assert "env: " in text
    assert "DATABASE_URL" in text
    assert "SECRET_KEY" in text


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('os.environ["A_VAR"]', ["A_VAR"]),
        ('os.environ.get("B_VAR")', ["B_VAR"]),
        ('os.getenv("C_VAR", "x")', ["C_VAR"]),
        ("process.env.NODE_ENV", ["NODE_ENV"]),
        ('process.env["API_KEY"]', ["API_KEY"]),
        ('os.Getenv("GO_VAR")', ["GO_VAR"]),
        ("os.environ[key]", []),
        ('config["not_env"]', []),
    ],
)
def test_environment_variable_patterns(code: str, expected: list[str]) -> None:
    assert environment_variables(code) == expected


# --------------------------------------------------------------------------- #
# the tier
# --------------------------------------------------------------------------- #


def test_sits_between_index_and_skeleton(repo: Path) -> None:
    """The acceptance criterion. A tier outside that band has no reason to exist.

    Measured on pallets/flask, 80 files:
        index 2,817 · outline 6,120 · skeleton 32,176 tokens
    """
    sizes = {}
    for name, kwargs in (
        ("index", {"index_only": True}),
        ("outline", {"outline_only": True}),
        ("skeleton", {}),
    ):
        bundle = CompressionService().run(CompressRequest(paths=(repo,), **kwargs))
        sizes[name] = bundle.stats.compressed

    assert sizes["index"] < sizes["outline"] < sizes["skeleton"], sizes


def test_every_file_is_still_represented(repo: Path) -> None:
    """Cheaper tiers lose depth, never files."""
    bundle = CompressionService().run(
        CompressRequest(paths=(repo,), outline_only=True)
    )
    assert all(f.level is DetailLevel.OUTLINE for f in bundle.files)
    assert all(f.outline for f in bundle.files)


def test_the_header_does_not_promise_signatures(repo: Path) -> None:
    """A legend describing a skeleton over an outline sends Claude hunting."""
    bundle = CompressionService().run(
        CompressRequest(paths=(repo,), outline_only=True)
    )
    header = bundle.payload.splitlines()[0]
    assert "outline" in header.lower()
    assert "preserved verbatim" not in bundle.payload
