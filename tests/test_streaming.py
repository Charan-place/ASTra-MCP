"""Tests for streaming context updates (graph_updated notifications + graph_version)."""
import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from astra.graph.store import GraphStore
from astra.indexer.symbol_table import Symbol
from astra.mcp.notifications import (
    GRAPH_UPDATED_METHOD,
    build_graph_updated_notification,
    send_graph_updated_notification,
)
from astra.mcp.tools import tool_get_context, tool_index_status


# ── helpers ──────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


def _add_symbol(store: GraphStore, name: str, file: str):
    sym = Symbol(
        type="function",
        name=name,
        file=file,
        signature=f"def {name}()",
        docstring="doc",
        line_start=1, line_end=3,
        raw_text=f"def {name}():\n    pass",
        calls=[],
    )
    store.upsert_node(sym, np.zeros(8, dtype="float32"))
    store.upsert_file_hash(file, "hash-" + name)
    store.commit()


class _FakeSession:
    """Stand-in for mcp.server.session.ServerSession."""
    def __init__(self):
        self.sent = []

    async def send_notification(self, notification):
        self.sent.append(notification)


# ── (a) notification shape ─────────────────────────────────────────────────

def test_build_graph_updated_notification_shape():
    delta = {
        "file": "/repo/foo.py",
        "added": ["foo.py::bar"],
        "removed": [],
        "changed": ["foo.py::baz"],
        "graph_version": 12345.6,
        "ts": 1710000000.0,
    }
    notification = build_graph_updated_notification(delta)

    assert notification.method == GRAPH_UPDATED_METHOD
    assert notification.params.file == "/repo/foo.py"
    assert notification.params.symbols_changed == 2  # 1 added + 1 changed
    assert notification.params.added == ["foo.py::bar"]
    assert notification.params.changed == ["foo.py::baz"]
    assert notification.params.graph_version == 12345.6
    assert notification.params.timestamp == 1710000000.0


def test_send_graph_updated_notification_fires_on_fake_session():
    """Simulate a file change while a fake/mocked MCP session is 'open'
    and assert a notification call fires with the expected shape."""
    session = _FakeSession()
    delta = {
        "file": "/repo/mod.py",
        "added": ["mod.py::new_fn"],
        "removed": [],
        "changed": [],
        "graph_version": 42.0,
        "ts": time.time(),
    }

    asyncio.run(send_graph_updated_notification(session, delta))

    assert len(session.sent) == 1
    sent = session.sent[0]
    assert sent.method == GRAPH_UPDATED_METHOD
    assert sent.params.file == "/repo/mod.py"
    assert sent.params.added == ["mod.py::new_fn"]
    assert sent.params.symbols_changed == 1
    assert sent.params.graph_version == 42.0


def test_send_graph_updated_notification_noop_without_session():
    """No connected session -> no crash, no notification sent."""
    delta = {"file": "x.py", "added": [], "removed": [], "changed": [], "ts": time.time()}
    # Should not raise even though session is None.
    asyncio.run(send_graph_updated_notification(None, delta))


def test_send_graph_updated_notification_swallows_send_errors():
    """A broken session (send raises) must not propagate the exception —
    a failed push notification shouldn't crash the daemon-bridge loop."""
    class _BrokenSession:
        async def send_notification(self, notification):
            raise RuntimeError("pipe closed")

    delta = {"file": "x.py", "added": [], "removed": [], "changed": [], "ts": time.time()}
    asyncio.run(send_graph_updated_notification(_BrokenSession(), delta))


# ── (b) graph_version / staleness signal ────────────────────────────────────

def test_graph_version_increases_after_reindex(tmp_path):
    store = _make_store(tmp_path)
    v0 = store.get_graph_version()
    assert v0 == 0.0  # empty store

    _add_symbol(store, "alpha", str(tmp_path / "a.py"))
    v1 = store.get_graph_version()
    assert v1 > v0

    time.sleep(0.01)
    _add_symbol(store, "beta", str(tmp_path / "b.py"))
    v2 = store.get_graph_version()
    assert v2 > v1


def test_tool_get_context_reports_graph_version_change(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    _add_symbol(store, "alpha", str(tmp_path / "a.py"))

    # Stub get_context so we don't need the full semantic search pipeline.
    def _fake_get_context(store, task, max_tokens=4000):
        return {"context": "ctx", "tokens": 10, "nodes": []}

    monkeypatch.setattr("astra.mcp.tools.get_context", _fake_get_context)
    monkeypatch.setattr("astra.mcp.tools.save_snapshot", lambda *a, **k: "")

    result1 = tool_get_context(store, "do something")
    assert "graph_version" in result1
    v1 = result1["graph_version"]

    # Simulate the daemon re-indexing a file while the MCP session is open.
    time.sleep(0.01)
    _add_symbol(store, "gamma", str(tmp_path / "c.py"))

    result2 = tool_get_context(store, "do something")
    v2 = result2["graph_version"]

    assert v2 > v1


def test_tool_index_status_reports_graph_version(tmp_path):
    store = _make_store(tmp_path)
    _add_symbol(store, "alpha", str(tmp_path / "a.py"))

    status1 = tool_index_status(store)
    assert "graph_version" in status1
    assert "last_updated" in status1
    v1 = status1["graph_version"]

    time.sleep(0.01)
    _add_symbol(store, "delta_fn", str(tmp_path / "d.py"))

    status2 = tool_index_status(store)
    assert status2["graph_version"] > v1
