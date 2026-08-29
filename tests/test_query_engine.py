"""Tests for astra.query.engine (token-budget context building) and astra.query.serializer."""
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import astra.indexer.embedder as embedder
from astra.graph.store import GraphStore
from astra.indexer.symbol_table import Symbol, Edge
from astra.query import engine as qengine
from astra.query.serializer import build_context, serialize_node, estimate_tokens


class _FakeSentenceTransformer:
    """encode() returns a deterministic zero vector so we never hit the network."""

    def __init__(self, model_name, *args, **kwargs):
        self._dim = 8

    def encode(self, texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True):
        return np.zeros((len(texts), self._dim), dtype=np.float32)


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    embedder._reset_model_cache()
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    qengine._nx_graph_cache.clear()
    yield
    embedder._reset_model_cache()
    qengine._nx_graph_cache.clear()


def _sym(name, file="mod.py", line=1, doc="doc", calls=None):
    return Symbol(
        type="function", name=name, file=file,
        signature=f"def {name}()", docstring=doc,
        line_start=line, line_end=line + 2,
        calls=calls or [],
    )


# ── serializer ────────────────────────────────────────────────────────────

def test_estimate_tokens_rough_chars_per_token():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 40) == 10


def test_serialize_node_includes_location_signature_docstring():
    node = {
        "file": "a.py", "line_start": 1, "line_end": 5,
        "signature": "def foo()", "type": "function", "name": "foo",
        "docstring": "does foo things",
    }
    out = serialize_node(node)
    assert "a.py:1-5" in out
    assert "def foo()" in out
    assert "does foo things" in out


def test_serialize_node_truncates_long_docstring():
    node = {
        "file": "a.py", "line_start": 1, "line_end": 5,
        "signature": "def foo()", "type": "function", "name": "foo",
        "docstring": "x" * 300,
    }
    out = serialize_node(node)
    assert "..." in out
    # only 200 chars of docstring content should appear plus ellipsis
    assert "x" * 201 not in out


def test_serialize_node_falls_back_to_type_name_without_signature():
    node = {
        "file": "a.py", "line_start": 1, "line_end": 5,
        "signature": "", "type": "function", "name": "foo",
    }
    out = serialize_node(node)
    assert "function foo" in out


def test_build_context_skips_file_type_nodes(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    file_sym = Symbol(type="file", name="mod.py", file="mod.py", signature="mod.py",
                       line_start=1, line_end=10)
    fn_sym = _sym("foo")
    store.upsert_node(file_sym)
    store.upsert_node(fn_sym)
    store.commit()

    ranked = [(file_sym.id, 1.0), (fn_sym.id, 0.9)]
    context, tokens = build_context(store, ranked, max_tokens=4000)
    assert "mod.py" not in context.split("\n")[0]  # header only mentions counts
    assert "def foo()" in context
    assert "1 symbols" in context
    store.close()


def test_build_context_respects_token_budget(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    syms = []
    for i in range(20):
        s = _sym(f"fn_{i}", line=i, doc="x" * 100)
        store.upsert_node(s)
        syms.append(s)
    store.commit()

    ranked = [(s.id, 1.0) for s in syms]
    # small budget should include far fewer than all 20 symbols
    context, tokens = build_context(store, ranked, max_tokens=50)
    assert tokens <= 50
    included = int(context.split("—")[1].split("symbols")[0].strip())
    assert included < 20
    store.close()


def test_build_context_skips_missing_nodes(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    context, tokens = build_context(store, [("nonexistent-id", 1.0)], max_tokens=100)
    assert tokens == 0
    assert "0 symbols" in context
    store.close()


# ── engine ────────────────────────────────────────────────────────────────

def test_get_context_empty_store_returns_placeholder(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    result = qengine.get_context(store, "do a thing")
    assert result["tokens"] == 0
    assert result["nodes"] == 0
    assert "astra init" in result["context"]
    store.close()


def test_get_context_with_indexed_symbols(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("process_payment", doc="handles payment processing")
    b = _sym("send_receipt", doc="sends a receipt email", calls=["process_payment"])
    store.upsert_node(a, np.zeros(8, dtype=np.float32))
    store.upsert_node(b, np.zeros(8, dtype=np.float32))
    store.upsert_edge(Edge(src=b.id, dst=a.id, relation="CALLS"))
    store.commit()

    result = qengine.get_context(store, "how does payment processing work", max_tokens=4000)
    assert result["tokens"] >= 0
    assert result["nodes"] >= 1
    assert "seeds" in result
    assert "node_ids" in result
    store.close()


def test_get_context_respects_max_tokens_budget(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    for i in range(10):
        s = _sym(f"fn_{i}", doc="x" * 200)
        store.upsert_node(s, np.zeros(8, dtype=np.float32))
    store.commit()

    result = qengine.get_context(store, "task", max_tokens=30)
    assert result["tokens"] <= 30


def test_search_symbols_returns_scored_nodes(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("find_me", doc="a searchable symbol")
    store.upsert_node(a, np.zeros(8, dtype=np.float32))
    store.commit()

    results = qengine.search_symbols(store, "find_me", top_k=5)
    assert len(results) == 1
    assert results[0]["name"] == "find_me"
    assert "score" in results[0]
    store.close()


def test_search_symbols_strips_embedding_blob(tmp_path):
    """Regression test: search_symbols() results must be JSON-serializable.

    store.get_node() returns the raw DB row, which includes the 384-dim
    float32 `embedding` BLOB. Real embeddings are essentially never valid
    UTF-8, so if this field leaks through into an API response (e.g. the
    dashboard's /api/search, which returns search_symbols() output
    directly), FastAPI's jsonable_encoder crashes trying to decode it as a
    string. Every other read path (daemon/core.py, dashboard/server.py's
    node/graph routes) already does `.pop("embedding", None)` before
    returning nodes — search_symbols() must do the same.
    """
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("find_me", doc="a searchable symbol")
    # non-UTF8-safe bytes, like a real embedding would produce (fixture's fake
    # embedder uses dim=8, see _FakeSentenceTransformer above)
    vec = np.array([0xD2000000, 1, 2, 3, 4, 5, 6, 7], dtype=np.uint32).view(np.float32)
    store.upsert_node(a, vec.astype(np.float32))
    store.commit()

    results = qengine.search_symbols(store, "find_me", top_k=5)
    assert len(results) == 1
    assert "embedding" not in results[0]

    import json
    json.dumps(results)  # must not raise
    store.close()


def test_search_symbols_empty_store_returns_empty(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    results = qengine.search_symbols(store, "anything")
    assert results == []
    store.close()


def test_graph_cache_reused_and_invalidated(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    a = _sym("a")
    store.upsert_node(a)
    store.commit()

    g1 = qengine._get_graph(store)
    g2 = qengine._get_graph(store)
    assert g1 is g2  # cache hit

    qengine.invalidate_graph_cache(store)
    g3 = qengine._get_graph(store)
    assert g3 is not g1
    store.close()
