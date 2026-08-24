"""Ranking: which files earn the token budget.

These tests exist because both behaviours below were wrong on first
implementation, and a silently wrong ranking is worse than no ranking — it
spends the budget confidently on the wrong files.
"""

from __future__ import annotations

from pathlib import Path

from cslim.core import compress_paths
from cslim.core.models import DetailLevel
from cslim.core.ranking import build_reference_graph, rank_files

CORE = '''
"""The shared data model."""

class Widget:
    """A widget."""
    name: str

def build_widget(name: str) -> Widget:
    return Widget()
'''

USER = '''
from core import Widget, build_widget

def handle(w: Widget) -> None:
    build_widget("x")
'''

LEAF = '''
def unused_helper(x: int) -> int:
    return x + 1
'''

BARREL = '''
"""Re-export barrel."""
from core import Widget, build_widget
__all__ = ["Widget", "build_widget"]
'''


def make_project(root: Path, users: int = 4) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "core.py").write_text(CORE, encoding="utf-8")
    (root / "leaf.py").write_text(LEAF, encoding="utf-8")
    (root / "__init__.py").write_text(BARREL, encoding="utf-8")
    (root / "main.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    for i in range(users):
        (root / f"user{i}.py").write_text(USER, encoding="utf-8")
    return root


def ranked(root: Path) -> list[str]:
    bundle = compress_paths([root])
    return [r.rel_path for r in rank_files(bundle.files)]


def test_widely_referenced_file_ranks_above_a_leaf(tmp_path: Path) -> None:
    order = ranked(make_project(tmp_path / "p"))
    assert order.index("core.py") < order.index("leaf.py")


def test_reexport_barrel_does_not_steal_rank(tmp_path: Path) -> None:
    """Regression: `__init__.py` outranked real modules.

    Barrels forward names they don't define, and `__all__` appears in every
    module — together that made empty barrels look like the busiest files in
    the project.
    """
    order = ranked(make_project(tmp_path / "p"))
    assert order.index("core.py") < order.index("__init__.py")
    assert order[-1] == "__init__.py"


def test_dunders_create_no_edges(tmp_path: Path) -> None:
    bundle = compress_paths([make_project(tmp_path / "p")])
    graph = build_reference_graph(bundle.files)
    assert "__init__.py" not in graph, "a barrel owns nothing of its own"


def test_entrypoint_is_not_ranked_last(tmp_path: Path) -> None:
    """Regression: nobody imports `main.py`, so it sank to the bottom.

    That's the nature of an entry point, not a sign it's unimportant — it is
    precisely the file a newcomer reads first.
    """
    order = ranked(make_project(tmp_path / "p"))
    assert order.index("main.py") < order.index("leaf.py")


def test_tight_budget_promotes_by_value_per_token(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p", users=10)
    bundle = compress_paths([root], max_tokens=380)

    assert bundle.stats.compressed <= 380
    assert not bundle.dropped, "everything stays at least discoverable"
    skeletons = [f for f in bundle.files if f.level is DetailLevel.SKELETON]
    indexed = [f for f in bundle.files if f.level is DetailLevel.INDEX]
    assert skeletons and indexed, "a tight budget mixes both tiers"
    assert min(f.rank for f in skeletons) >= min(f.rank for f in bundle.files)


def test_index_line_names_what_a_file_offers(tmp_path: Path) -> None:
    bundle = compress_paths([make_project(tmp_path / "p")], max_tokens=280)
    lines = {f.rel_path: f.index_line for f in bundle.files}
    assert "Widget" in lines["core.py"]
    assert "build_widget" in lines["core.py"]
    assert "__all__" not in lines["__init__.py"], "dunders are boilerplate"


def test_stats_count_the_tier_actually_emitted(tmp_path: Path) -> None:
    bundle = compress_paths([make_project(tmp_path / "p", users=10)], max_tokens=380)
    emitted = sum(f.emitted_tokens for f in bundle.files if f.included)
    assert bundle.stats.compressed == emitted
    assert bundle.stats.indexed_files == sum(
        1 for f in bundle.files if f.level is DetailLevel.INDEX
    )
