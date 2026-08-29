"""Tests for astra.graph.store (GraphStore CRUD) and astra.graph.pagerank."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from astra.graph.store import GraphStore
from astra.graph.pagerank import build_nx_graph, personalized_pagerank
from astra.indexer.symbol_table import Symbol, Edge


def _sym(name, file="mod.py", line=1, calls=None):
    return Symbol(
        type="function",
        name=name,
        file=file,
        signature=f"def {name}()",
        docstring=f"{name} docstring",
        line_start=line,
        line_end=line + 2,
        raw_text=f"def {name}(): pass",
        calls=calls or [],
    )


# ── GraphStore CRUD ──────────────────────────────────────────────────────

def test_upsert_node_and_get_node(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    sym = _sym("hub")
    store.upsert_node(sym, np.ones(8, dtype=np.float32))
    store.commit()

    node = store.get_node(sym.id)
    assert node is not None
    assert node["name"] == "hub"
    assert node["type"] == "function"
    assert node["signature"] == "def hub()"
    store.close()


def test_upsert_node_roundtrip_no_embedding(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    sym = _sym("leaf")
    store.upsert_node(sym)
    store.commit()
    node = store.get_node(sym.id)
    assert node is not None
    assert node["embedding"] is None
    store.close()


def test_upsert_node_replaces_existing(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    sym = _sym("dup")
    store.upsert_node(sym)
    store.commit()

    sym2 = Symbol(
        type="function", name="dup", file=sym.file, signature="def dup(x)",
        docstring="updated", line_start=sym.line_start, line_end=sym.line_end,
    )
    store.upsert_node(sym2)
    store.commit()

    node = store.get_node(sym.id)
    assert node["signature"] == "def dup(x)"
    assert node["docstring"] == "updated"
    store.close()


def test_upsert_edge_and_get_callers_callees(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("caller")
    b = _sym("callee", line=10)
    store.upsert_node(a)
    store.upsert_node(b)
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.commit()

    callers = store.get_callers(b.id)
    callees = store.get_callees(a.id)
    assert [c["name"] for c in callers] == ["caller"]
    assert [c["name"] for c in callees] == ["callee"]
    store.close()


def test_upsert_edge_ignores_duplicates(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a")
    b = _sym("b")
    store.upsert_node(a)
    store.upsert_node(b)
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.commit()

    stats = store.stats()
    assert stats["edges"] == 1
    store.close()


def test_delete_file_removes_nodes_and_edges(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a", file="a.py")
    b = _sym("b", file="b.py")
    store.upsert_node(a)
    store.upsert_node(b)
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.upsert_file_hash("a.py", "hash1")
    store.commit()

    store.delete_file("a.py")
    store.commit()

    assert store.get_node(a.id) is None
    assert store.get_node(b.id) is not None
    assert store.get_callees(a.id) == []
    assert store.get_file_hash("a.py") is None
    store.close()


def test_get_file_hash_roundtrip(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    assert store.get_file_hash("nope.py") is None
    store.upsert_file_hash("nope.py", "abc123")
    store.commit()
    assert store.get_file_hash("nope.py") == "abc123"
    store.close()


def test_get_nodes_by_file_and_name(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("shared_name", file="x.py")
    b = _sym("shared_name", file="y.py")
    store.upsert_node(a)
    store.upsert_node(b)
    store.commit()

    by_name = store.get_nodes_by_name("shared_name")
    assert len(by_name) == 2

    by_file = store.get_nodes_by_file("x.py")
    assert len(by_file) == 1
    assert by_file[0]["name"] == "shared_name"
    store.close()


def test_stats(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a")
    b = _sym("b")
    store.upsert_node(a)
    store.upsert_node(b)
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.upsert_file_hash("a.py", "h")
    store.commit()

    stats = store.stats()
    assert stats["nodes"] == 2
    assert stats["edges"] == 1
    assert stats["files"] == 1
    store.close()


def test_all_embeddings_and_node_ids(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a")
    b = _sym("b")
    store.upsert_node(a, np.array([1.0, 0.0], dtype=np.float32))
    store.upsert_node(b)  # no embedding
    store.commit()

    embeds = store.all_embeddings()
    assert len(embeds) == 1
    assert embeds[0][0] == a.id
    np.testing.assert_allclose(embeds[0][1], [1.0, 0.0])

    all_ids = set(store.all_node_ids())
    assert all_ids == {a.id, b.id}
    store.close()


def test_get_all_symbol_calls_and_name_to_ids(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a", calls=["b"])
    b = _sym("b")
    store.upsert_node(a)
    store.upsert_node(b)
    store.commit()

    calls_rows = store.get_all_symbol_calls()
    assert any(r["id"] == a.id for r in calls_rows)

    name_to_ids = store.get_name_to_ids()
    assert "a" in name_to_ids
    assert "b" in name_to_ids
    store.close()


# ── PageRank ─────────────────────────────────────────────────────────────

def test_pagerank_ranks_hub_above_leaf(tmp_path):
    store = GraphStore(tmp_path / "graph.db")

    hub = _sym("hub", line=1)
    leaf = _sym("leaf", line=10)
    callers = [_sym(f"caller_{i}", line=20 + i) for i in range(5)]

    store.upsert_node(hub)
    store.upsert_node(leaf)
    for c in callers:
        store.upsert_node(c)
        store.upsert_edge(Edge(src=c.id, dst=hub.id, relation="CALLS"))
    store.commit()

    G = build_nx_graph(store)
    assert G.number_of_nodes() == 7
    # heavily-called hub should outrank leaf when seeded from all callers
    seed_ids = [c.id for c in callers]
    ranked = personalized_pagerank(G, seed_ids, top_k=10)
    scores = dict(ranked)

    assert hub.id in scores
    assert leaf.id in scores
    assert scores[hub.id] > scores[leaf.id]
    store.close()


def test_pagerank_empty_seed_returns_empty():
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("a")
    assert personalized_pagerank(G, [], top_k=5) == []


def test_pagerank_empty_graph_returns_empty():
    import networkx as nx
    G = nx.DiGraph()
    assert personalized_pagerank(G, ["a"], top_k=5) == []


def test_build_nx_graph_adds_reverse_edges(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a")
    b = _sym("b")
    store.upsert_node(a)
    store.upsert_node(b)
    store.upsert_edge(Edge(src=a.id, dst=b.id, relation="CALLS"))
    store.commit()

    G = build_nx_graph(store)
    assert G.has_edge(a.id, b.id)
    assert G.has_edge(b.id, a.id)
    assert G.edges[b.id, a.id]["relation"] == "CALLS_REV"
    store.close()
