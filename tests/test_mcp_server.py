"""Tests for astra.mcp.server tool list and astra.mcp.tools handler functions."""
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import astra.indexer.embedder as embedder
from astra.graph.store import GraphStore
from astra.indexer.symbol_table import Symbol, Edge
from astra.memory.session import SessionMemory
from astra.mcp import server as mcp_server
from astra.mcp import tools as mcp_tools


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
def store(tmp_path):
    s = GraphStore(tmp_path / "graph.db")
    a = _sym("process_payment", doc="handles a payment")
    b = _sym("send_receipt", doc="sends a receipt", calls=["process_payment"])
    s.upsert_node(a, np.zeros(8, dtype=np.float32))
    s.upsert_node(b, np.zeros(8, dtype=np.float32))
    s.upsert_edge(Edge(src=b.id, dst=a.id, relation="CALLS"))
    s.commit()
    yield s
    s.close()


# ── Tool list shape ─────────────────────────────────────────────────────

def test_tool_list_has_11_tools():
    assert len(mcp_server._TOOLS) == 11


def test_tool_list_names_are_unique_and_prefixed():
    names = [t.name for t in mcp_server._TOOLS]
    assert len(names) == len(set(names))
    assert all(n.startswith("astra_") for n in names)


def test_tool_list_contains_get_context():
    names = {t.name for t in mcp_server._TOOLS}
    assert "astra_get_context" in names
    assert "astra_search" in names
    assert "astra_get_callers" in names
    assert "astra_get_callees" in names
    assert "astra_get_file_map" in names


def test_each_tool_has_valid_input_schema():
    for tool in mcp_server._TOOLS:
        assert tool.inputSchema["type"] == "object"
        assert "properties" in tool.inputSchema


# ── tools.py handler functions, called directly ─────────────────────────

def test_tool_get_context_returns_expected_shape(store):
    result = mcp_tools.tool_get_context(store, "how does payment work", max_tokens=2000)
    assert "context" in result
    assert "token_estimate" in result
    assert "symbols_included" in result
    assert isinstance(result["token_estimate"], int)


def test_tool_search_returns_list_of_matches(store):
    results = mcp_tools.tool_search(store, "payment", top_k=5)
    assert isinstance(results, list)
    assert all("name" in r and "score" in r for r in results)


def test_tool_get_callers(store):
    callers = mcp_tools.tool_get_callers(store, "process_payment")
    names = [c["name"] for c in callers]
    assert "send_receipt" in names


def test_tool_get_callers_unknown_function_returns_empty(store):
    assert mcp_tools.tool_get_callers(store, "does_not_exist") == []


def test_tool_get_callees(store):
    callees = mcp_tools.tool_get_callees(store, "send_receipt")
    names = [c["name"] for c in callees]
    assert "process_payment" in names


def test_tool_get_file_map_returns_symbol_listing(store):
    text = mcp_tools.tool_get_file_map(store, "mod.py")
    assert "process_payment" in text
    assert "send_receipt" in text


def test_tool_get_file_map_no_symbols(store):
    text = mcp_tools.tool_get_file_map(store, "unknown.py")
    assert "No indexed symbols" in text


def test_tool_index_status(store):
    result = mcp_tools.tool_index_status(store)
    assert result["nodes"] == 2
    assert result["edges"] == 1
    assert result["files_indexed"] == 0


def test_tool_session_memory(tmp_path, store):
    mem = SessionMemory(tmp_path / "sessions.db")
    mem.save_session("s1", str(tmp_path), "worked on payments", ["payments"])
    text = mcp_tools.tool_session_memory(mem, "payments", str(tmp_path))
    assert isinstance(text, str)
    mem.close()


def test_tool_impact_analysis_unknown_names_returns_error(store):
    result = mcp_tools.tool_impact_analysis(store, ["nonexistent_fn"])
    assert "error" in result


def test_tool_impact_analysis_known_function(store):
    result = mcp_tools.tool_impact_analysis(store, ["process_payment"])
    assert "error" not in result


def test_tool_get_volatility_without_temporal_index(store):
    result = mcp_tools.tool_get_volatility(store)
    assert "error" in result
    assert "astra timeline" in result["error"]


def test_tool_trace_cross_repo_without_federation(store):
    result = mcp_tools.tool_trace_cross_repo(store, "process_payment", fed_db_path=str(Path("/nonexistent/federation.db")))
    assert "error" in result
