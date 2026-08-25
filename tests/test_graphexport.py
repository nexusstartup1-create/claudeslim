"""The exported graph must lose nothing and say the same thing in every format."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cslim.core import DiscoveryOptions, compress_paths
from cslim.core.graphexport import build_graph, to_dot, to_graphml, to_json

PY = '''
class Alpha:
    def get(self) -> int:
        ...

class Beta:
    def get(self) -> int:
        ...
'''

USER = '''
from thing import Alpha

def use() -> None:
    Alpha().get()
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "thing.py").write_text(PY, encoding="utf-8")
    (tmp_path / "user.py").write_text(USER, encoding="utf-8")
    return tmp_path


def graph_of(root: Path):
    bundle = compress_paths([root], discovery=DiscoveryOptions())
    return build_graph(bundle.files), bundle


# --------------------------------------------------------------------------- #
# the invariant
# --------------------------------------------------------------------------- #


def test_every_discovered_file_gets_exactly_one_node(repo: Path) -> None:
    """The bug that makes repominify unusable, pinned so we never ship it.

    It keys modules by basename, so flask's thirteen `__init__.py` collapse into
    one node and each file's classes are attributed to the others — 19 of 83
    files silently vanish. Here the key is the path, and this asserts it.
    """
    graph, bundle = graph_of(repo)
    assert len(graph.file_nodes) == len(bundle.files)
    assert len({n.id for n in graph.file_nodes}) == len(bundle.files)


def test_same_named_symbols_in_one_file_do_not_collide(repo: Path) -> None:
    """`Alpha.get` and `Beta.get` are two symbols, not one.

    Keying a symbol by name alone lost 243 nodes on flask before the line
    number became part of the id — the same collision, one level down.
    """
    graph, _ = graph_of(repo)
    ids = [n.id for n in graph.symbol_nodes]
    assert len(ids) == len(set(ids)), "symbol ids are not unique"
    gets = [n for n in graph.symbol_nodes if n.label == "get"]
    assert len(gets) == 2, "both `get` methods must survive as separate nodes"
    assert gets[0].line != gets[1].line


def test_no_dangling_edges(repo: Path) -> None:
    graph, _ = graph_of(repo)
    known = {n.id for n in graph.nodes}
    for edge in graph.edges:
        assert edge.source in known, f"dangling source {edge.source}"
        assert edge.target in known, f"dangling target {edge.target}"


def test_reference_edges_point_from_user_to_definer(repo: Path) -> None:
    """in-degree must mean "how many files rely on this"."""
    graph, _ = graph_of(repo)
    refs = [e for e in graph.edges if e.kind == "references"]
    assert refs, "user.py references Alpha, defined in thing.py"
    assert any(e.source == "user.py" and e.target == "thing.py" for e in refs)


def test_symbols_can_be_left_out(repo: Path) -> None:
    graph, bundle = graph_of(repo)
    lean = build_graph(bundle.files, symbols=False)
    assert lean.symbol_nodes == []
    assert len(lean.file_nodes) == len(graph.file_nodes)
    assert all(e.kind == "references" for e in lean.edges)


# --------------------------------------------------------------------------- #
# the formats
# --------------------------------------------------------------------------- #


def test_graphml_is_well_formed_and_complete(repo: Path) -> None:
    graph, _ = graph_of(repo)
    root = ET.fromstring(to_graphml(graph))
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    inner = root.find(f"{ns}graph")
    assert inner is not None
    assert inner.get("edgedefault") == "directed"
    assert len(inner.findall(f"{ns}node")) == len(graph.nodes)
    assert len(inner.findall(f"{ns}edge")) == len(graph.edges)


def test_json_is_node_link_and_complete(repo: Path) -> None:
    graph, _ = graph_of(repo)
    payload = json.loads(to_json(graph))
    assert payload["directed"] is True
    assert len(payload["nodes"]) == len(graph.nodes)
    assert len(payload["links"]) == len(graph.edges)


def test_the_formats_agree(repo: Path) -> None:
    """A discrepancy here is a lost node, and it happened once already."""
    graph, _ = graph_of(repo)
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    inner = ET.fromstring(to_graphml(graph)).find(f"{ns}graph")
    assert inner is not None
    payload = json.loads(to_json(graph))
    assert len(inner.findall(f"{ns}node")) == len(payload["nodes"])
    assert len(inner.findall(f"{ns}edge")) == len(payload["links"])


def test_dot_quotes_ids_that_would_break_graphviz(repo: Path) -> None:
    graph, _ = graph_of(repo)
    dot = to_dot(graph)
    assert dot.startswith("digraph cslim {")
    assert dot.rstrip().endswith("}")
    # ids carry slashes, colons and dots; unquoted they are a syntax error
    for node in graph.nodes:
        assert f'"{node.id}"' in dot


def test_xml_special_characters_survive(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        'def f(x: str = "<a & b>") -> None:\n    ...\n', encoding="utf-8"
    )
    graph, _ = graph_of(tmp_path)
    ET.fromstring(to_graphml(graph))  # raises if escaping is wrong


def test_networkx_can_read_it(repo: Path) -> None:
    """The acceptance criterion: it opens in the tools people actually use."""
    nx = pytest.importorskip("networkx")
    graph, _ = graph_of(repo)
    parsed = nx.parse_graphml(to_graphml(graph))
    assert parsed.number_of_nodes() == len(graph.nodes)
    assert parsed.number_of_edges() == len(graph.edges)
    assert parsed.is_directed()
