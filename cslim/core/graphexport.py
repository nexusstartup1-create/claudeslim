"""The reference graph, as an artefact you can open.

``core/ranking.py`` builds a graph of which file references which, uses it to
decide who earns a full skeleton, and then throws it away. It is the most
informative thing cslim computes and until now it was invisible.

This exports it. Three formats, no new dependencies — GraphML is XML, DOT is a
few lines of text, JSON is JSON, and adding a graph library to a package whose
runtime deps are `typer` and `rich` would cost more than it buys.

**Nodes are keyed by path, never by name.** repominify keys modules by basename
and consequently folds all thirteen of flask's ``__init__.py`` into one node,
attributing each file's classes and imports to the others. A map that puts code
in the wrong file is worse than no map, so the invariant here is that every
discovered file gets exactly one node, and a test enforces it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from .models import CompressedFile
from .ranking import build_reference_graph, rank_files

__all__ = [
    "Graph",
    "GraphEdge",
    "GraphNode",
    "build_graph",
    "to_dot",
    "to_graphml",
    "to_json",
]

NodeKind = Literal["file", "symbol"]
EdgeKind = Literal["references", "defines"]


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    """Path-based and unique: ``src/app.py`` or ``src/app.py::Flask:31``.

    The line number is part of a symbol's id because a name alone is not
    unique inside a file: two classes each defining ``get`` would otherwise
    share a node, and the graph would lose one of them. That is the same class
    of collision that makes repominify fold thirteen ``__init__.py`` into one —
    caught here by the invariant test rather than shipped.
    """
    kind: NodeKind
    label: str
    path: str
    language: str = ""
    line: int = 0
    rank: float = 0.0
    tokens: int = 0
    symbol_kind: str = ""


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    kind: EdgeKind


@dataclass(slots=True)
class Graph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    @property
    def file_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if n.kind == "file"]

    @property
    def symbol_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if n.kind == "symbol"]


def build_graph(files: list[CompressedFile], *, symbols: bool = True) -> Graph:
    """Turn the reference analysis into an explicit graph.

    Edge direction is *user → used*: ``A --references--> B`` means A mentions
    something B defines. That is the direction dependency tools expect, and the
    one that makes in-degree mean "how many files rely on this".
    """
    graph = Graph()
    ranks = {r.rel_path: r for r in rank_files(files)}
    by_path = {f.rel_path: f for f in files}

    for file in files:
        rank = ranks.get(file.rel_path)
        graph.nodes.append(
            GraphNode(
                id=file.rel_path,
                kind="file",
                label=file.rel_path,
                path=file.rel_path,
                language=file.language.value,
                rank=rank.score if rank else 0.0,
                tokens=file.compressed_tokens,
            )
        )

    if symbols:
        for file in files:
            for symbol in file.symbols:
                node_id = f"{file.rel_path}::{symbol.name}:{symbol.line}"
                graph.nodes.append(
                    GraphNode(
                        id=node_id,
                        kind="symbol",
                        label=symbol.name,
                        path=file.rel_path,
                        language=file.language.value,
                        line=symbol.line,
                        symbol_kind=symbol.kind.value,
                    )
                )
                graph.edges.append(GraphEdge(file.rel_path, node_id, "defines"))

    # build_reference_graph returns {defining file: {files that reference it}},
    # so the edge runs from each referencing file to the definer.
    for defined_in, referencing in build_reference_graph(files).items():
        if defined_in not in by_path:
            continue
        for user in sorted(referencing):
            if user in by_path:
                graph.edges.append(GraphEdge(user, defined_in, "references"))

    return graph


# --------------------------------------------------------------------------- #
# formats
# --------------------------------------------------------------------------- #

_GRAPHML_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("kind", "node", "kind", "string"),
    ("label", "node", "label", "string"),
    ("path", "node", "path", "string"),
    ("language", "node", "language", "string"),
    ("line", "node", "line", "int"),
    ("rank", "node", "rank", "double"),
    ("tokens", "node", "tokens", "int"),
    ("symbolKind", "node", "symbolKind", "string"),
    ("edgeKind", "edge", "edgeKind", "string"),
)


def to_graphml(graph: Graph) -> str:
    """GraphML — what Gephi, yEd, Cytoscape and networkx all read."""
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
    ]
    for key, target, name, kind in _GRAPHML_KEYS:
        out.append(
            f'  <key id={quoteattr(key)} for={quoteattr(target)} '
            f'attr.name={quoteattr(name)} attr.type={quoteattr(kind)}/>'
        )
    out.append('  <graph id="cslim" edgedefault="directed">')

    for node in graph.nodes:
        out.append(f"    <node id={quoteattr(node.id)}>")
        out.append(f'      <data key="kind">{escape(node.kind)}</data>')
        out.append(f'      <data key="label">{escape(node.label)}</data>')
        out.append(f'      <data key="path">{escape(node.path)}</data>')
        if node.language:
            out.append(f'      <data key="language">{escape(node.language)}</data>')
        if node.line:
            out.append(f'      <data key="line">{node.line}</data>')
        if node.kind == "file":
            out.append(f'      <data key="rank">{node.rank:.6f}</data>')
            out.append(f'      <data key="tokens">{node.tokens}</data>')
        if node.symbol_kind:
            out.append(f'      <data key="symbolKind">{escape(node.symbol_kind)}</data>')
        out.append("    </node>")

    for index, edge in enumerate(graph.edges):
        out.append(
            f'    <edge id="e{index}" source={quoteattr(edge.source)} '
            f"target={quoteattr(edge.target)}>"
        )
        out.append(f'      <data key="edgeKind">{escape(edge.kind)}</data>')
        out.append("    </edge>")

    out.append("  </graph>")
    out.append("</graphml>")
    return "\n".join(out) + "\n"


def to_json(graph: Graph) -> str:
    """Node-link JSON, the shape D3 and networkx's ``node_link_graph`` expect."""
    payload = {
        "directed": True,
        "multigraph": False,
        "graph": {"generator": "cslim"},
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "label": n.label,
                "path": n.path,
                **({"language": n.language} if n.language else {}),
                **({"line": n.line} if n.line else {}),
                **({"rank": round(n.rank, 6), "tokens": n.tokens} if n.kind == "file" else {}),
                **({"symbolKind": n.symbol_kind} if n.symbol_kind else {}),
            }
            for n in graph.nodes
        ],
        "links": [
            {"source": e.source, "target": e.target, "kind": e.kind} for e in graph.edges
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def _dot_id(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(graph: Graph, *, symbols: bool = True) -> str:
    """Graphviz DOT — `dot -Tsvg` renders it without any of it being installed here.

    Files are sized by rank so the modules that explain the project are the ones
    you see first.
    """
    out = ["digraph cslim {", "  rankdir=LR;", '  node [shape=box, fontname="Helvetica"];']
    ranks = [n.rank for n in graph.file_nodes] or [0.0]
    top = max(ranks) or 1.0

    for node in graph.nodes:
        if node.kind == "symbol":
            if not symbols:
                continue
            out.append(
                f"  {_dot_id(node.id)} [label={_dot_id(node.label)}, "
                'shape=ellipse, fontsize=9, color="#8A6A3B"];'
            )
            continue
        weight = node.rank / top
        out.append(
            f"  {_dot_id(node.id)} [label={_dot_id(node.label)}, "
            f"fontsize={9 + weight * 9:.0f}, penwidth={1 + weight * 3:.1f}];"
        )

    for edge in graph.edges:
        if edge.kind == "defines":
            if not symbols:
                continue
            out.append(
                f"  {_dot_id(edge.source)} -> {_dot_id(edge.target)} "
                '[style=dotted, color="#C25E00"];'
            )
        else:
            out.append(f"  {_dot_id(edge.source)} -> {_dot_id(edge.target)};")

    out.append("}")
    return "\n".join(out) + "\n"
