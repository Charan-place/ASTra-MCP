"""Tests for Phase 1: ASTra Live Daemon"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# ensure repo root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── helpers ────────────────────────────────────────────────────────────────

def _make_temp_db(tmp_path: Path):
    from astra.graph.store import GraphStore
    from astra.indexer.symbol_table import Symbol
    db = GraphStore(tmp_path / "graph.db")
    sym = Symbol(
        type="function",
        name="hello",
        file=str(tmp_path / "hello.py"),
        signature="def hello()",
        docstring="greet",
        line_start=1, line_end=3,
        raw_text="def hello():\n    return 'hi'",
        calls=[],
    )
    import numpy as np
    db.upsert_node(sym, np.zeros(384, dtype="float32"))
    db.commit()
    return db


# ── Unit: incremental pagerank ─────────────────────────────────────────────

def test_incremental_pagerank_empty_graph():
    import networkx as nx
    from astra.daemon.core import _incremental_pagerank_update

    G = nx.DiGraph()
    with tempfile.TemporaryDirectory() as td:
        from astra.graph.store import GraphStore
        store = GraphStore(Path(td) / "g.db")
        scores = _incremental_pagerank_update(G, store, [], radius=2)
        assert scores == {}
        store.close()


def test_incremental_pagerank_small_graph():
    import networkx as nx
    from astra.daemon.core import _incremental_pagerank_update

    G = nx.DiGraph()
    G.add_nodes_from(["A", "B", "C"])
    G.add_edges_from([("A", "B"), ("B", "C"), ("C", "A")])

    with tempfile.TemporaryDirectory() as td:
        from astra.graph.store import GraphStore
        store = GraphStore(Path(td) / "g.db")
        scores = _incremental_pagerank_update(G, store, ["A"], radius=2)
        assert isinstance(scores, dict)
        assert len(scores) > 0
        store.close()


# ── Unit: GraphDelta ───────────────────────────────────────────────────────

def test_graph_delta_to_dict():
    from astra.daemon.core import GraphDelta
    d = GraphDelta("/some/file.py")
    d.added_nodes = ["n1"]
    d.removed_nodes = ["n2"]
    d.changed_nodes = ["n3"]

    out = d.to_dict()
    assert out["file"] == "/some/file.py"
    assert out["added"] == ["n1"]
    assert out["removed"] == ["n2"]
    assert out["changed"] == ["n3"]
    assert isinstance(out["ts"], float)


# ── Integration: daemon start/ping/status/stop ─────────────────────────────

@pytest.fixture
def running_daemon(tmp_path):
    """Starts a daemon in a background thread; yields DaemonClient; cleans up."""
    import hashlib
    import astra.daemon.core as core_mod
    from astra.daemon.core import AstraDaemon, DaemonClient

    # Unix socket path limit is 104 chars on macOS — use /tmp with short hash
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    test_sock = Path(f"/tmp/astra_test_{h}.sock")

    orig_socket = core_mod.SOCKET_PATH
    orig_pid = core_mod.PID_PATH
    orig_delta = core_mod.DELTA_PATH
    core_mod.SOCKET_PATH = test_sock
    core_mod.PID_PATH = tmp_path / "daemon.pid"
    core_mod.DELTA_PATH = tmp_path / "delta.json"

    db = _make_temp_db(tmp_path)
    db.close()

    daemon = AstraDaemon(tmp_path, tmp_path / "graph.db")
    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()

    # wait up to 3s
    client = DaemonClient(test_sock)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if client.ping():
            break
        time.sleep(0.05)
    else:
        daemon.stop()
        core_mod.SOCKET_PATH = orig_socket
        core_mod.PID_PATH = orig_pid
        core_mod.DELTA_PATH = orig_delta
        pytest.fail("Daemon did not start in time")

    yield client

    daemon.stop()
    t.join(timeout=3.0)
    core_mod.SOCKET_PATH = orig_socket
    core_mod.PID_PATH = orig_pid
    core_mod.DELTA_PATH = orig_delta


def test_daemon_ping(running_daemon):
    assert running_daemon.ping() is True


def test_daemon_status(running_daemon):
    resp = running_daemon.status()
    assert resp["ok"] is True
    data = resp["data"]
    assert "nodes" in data
    assert "edges" in data
    assert "uptime_s" in data
    assert data["uptime_s"] >= 0


def test_daemon_query(running_daemon):
    resp = running_daemon.query("hello function greet", max_tokens=500)
    assert resp["ok"] is True
    data = resp["data"]
    assert "context" in data
    assert "tokens" in data


def test_daemon_search(running_daemon):
    resp = running_daemon.search("hello greet", top_k=5)
    assert resp["ok"] is True
    assert isinstance(resp["data"], list)


def test_daemon_unknown_cmd(running_daemon):
    resp = running_daemon._send({"cmd": "nonexistent"})
    assert resp["ok"] is False
    assert "unknown cmd" in resp["error"]


def test_daemon_delta(running_daemon):
    resp = running_daemon.delta()
    assert resp["ok"] is True


# ── Unit: DaemonClient.is_running ─────────────────────────────────────────

def test_client_not_running_when_no_daemon(tmp_path):
    from astra.daemon.core import DaemonClient
    client = DaemonClient(tmp_path / "nonexistent.sock")
    assert client.is_running is False
