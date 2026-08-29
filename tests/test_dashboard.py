"""Tests for astra.dashboard.server route handlers (FastAPI app, via TestClient)."""
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import astra.indexer.embedder as embedder
from astra.graph.store import GraphStore
from astra.indexer.symbol_table import Symbol, Edge
import astra.dashboard.server as dashboard_server


class _FakeSentenceTransformer:
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
    yield
    embedder._reset_model_cache()


def _sym(name, file="mod.py", line=1, doc="doc", calls=None):
    return Symbol(
        type="function", name=name, file=file,
        signature=f"def {name}()", docstring=doc,
        line_start=line, line_end=line + 2,
        calls=calls or [],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh dashboard state pointed at an isolated .astra dir with a small indexed graph."""
    astra_dir = tmp_path / ".astra"
    astra_dir.mkdir()
    monkeypatch.setenv("ASTRA_DATA_DIR", str(astra_dir))
    monkeypatch.setenv("ASTRA_PROJECT", str(tmp_path))

    store = GraphStore(astra_dir / "graph.db")
    a = _sym("process_payment", doc="handles a payment")
    b = _sym("send_receipt", doc="sends a receipt", calls=["process_payment"])
    store.upsert_node(a, np.zeros(8, dtype=np.float32))
    store.upsert_node(b, np.zeros(8, dtype=np.float32))
    store.upsert_edge(Edge(src=b.id, dst=a.id, relation="CALLS"))
    store.commit()
    store.close()

    # reset module-level dashboard state so each test starts clean
    dashboard_server._state["store"] = None
    dashboard_server._state["queries"] = []
    dashboard_server._state["total_saved"] = 0
    dashboard_server._state["total_naive"] = 0
    dashboard_server._state["total_astra"] = 0
    dashboard_server._naive_cache["root"] = None
    dashboard_server._naive_cache["ts"] = 0.0

    with TestClient(dashboard_server.app) as c:
        yield c

    if dashboard_server._state["store"] is not None:
        dashboard_server._state["store"].close()
        dashboard_server._state["store"] = None


def test_index_route_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_api_stats_returns_graph_counts(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["graph"]["nodes"] == 2
    assert data["graph"]["edges"] == 1
    assert data["total_saved"] == 0


def test_api_query_requires_task(client):
    resp = client.post("/api/query", json={})
    assert resp.status_code == 200
    assert resp.json() == {"error": "task required"}


def test_api_query_returns_context_and_updates_state(client):
    resp = client.post("/api/query", json={"task": "how does payment work", "max_tokens": 2000})
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data
    assert "astra_tokens" in data
    assert data["symbols"] >= 0

    stats = client.get("/api/stats").json()
    assert len(stats["queries"]) == 1


def test_api_graph_returns_nodes_and_edges(client):
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


def test_api_graph_filters_by_file(client):
    resp = client.get("/api/graph", params={"file": "nonexistent"})
    data = resp.json()
    assert data["nodes"] == []


def test_api_graph_hierarchy_shape(client):
    resp = client.get("/api/graph/hierarchy")
    assert resp.status_code == 200
    data = resp.json()
    assert "folders" in data
    assert "files" in data
    assert "functions" in data
    assert len(data["functions"]) == 2


def test_api_graph_node_not_found(client):
    resp = client.get("/api/graph/node/does-not-exist")
    assert resp.status_code == 200
    assert resp.json() == {"error": "not found"}


def test_api_search_empty_query_returns_empty_list(client):
    resp = client.get("/api/search", params={"q": ""})
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_search_returns_results(client):
    resp = client.get("/api/search", params={"q": "payment"})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_api_graphs_empty_when_no_snapshots(client):
    resp = client.get("/api/graphs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_graphs_current_missing_returns_404(client):
    resp = client.get("/graphs/current")
    assert resp.status_code == 404


def test_api_latest_snapshot_defaults_empty(client):
    resp = client.get("/api/latest_snapshot")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_api_query_trail_defaults_empty(client):
    resp = client.get("/api/query_trail")
    assert resp.status_code == 200
    assert resp.json() == []
