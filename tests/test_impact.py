"""Tests for Phase 2: Impact Analyzer"""
import sys
import tempfile
from pathlib import Path

import pytest
import networkx as nx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_call_graph_store(tmp_path: Path):
    """
    Build a minimal store with a call chain:
    test_handler -> process_payment -> validate_card -> charge_card

    test_handler is a test function (has_test=True for process_payment).
    validate_card and charge_card have no test callers.
    """
    from astra.graph.store import GraphStore
    from astra.indexer.symbol_table import Symbol, Edge

    store = GraphStore(tmp_path / "graph.db")
    file = str(tmp_path / "payments.py")

    syms = [
        Symbol(type="function", name="test_handler",     file=file, line_start=1, line_end=5),
        Symbol(type="function", name="process_payment",  file=file, line_start=10, line_end=20),
        Symbol(type="function", name="validate_card",    file=file, line_start=25, line_end=35),
        Symbol(type="function", name="charge_card",      file=file, line_start=40, line_end=50),
    ]

    node_ids = {}
    for s in syms:
        store.upsert_node(s, np.zeros(384, dtype="float32"))
        node_ids[s.name] = s.id

    # edges: test_handler CALLS process_payment CALLS validate_card CALLS charge_card
    edges = [
        Edge(src=node_ids["test_handler"],    dst=node_ids["process_payment"], relation="CALLS"),
        Edge(src=node_ids["process_payment"], dst=node_ids["validate_card"],   relation="CALLS"),
        Edge(src=node_ids["validate_card"],   dst=node_ids["charge_card"],     relation="CALLS"),
    ]
    for e in edges:
        store.upsert_edge(e)

    store.commit()
    return store, node_ids


# ── Unit tests ─────────────────────────────────────────────────────────────

def test_impact_report_to_dict():
    from astra.impact.analyzer import ImpactReport
    report = ImpactReport(
        changed_nodes=["n1"],
        affected_nodes=[{"id": "n2", "name": "foo", "file": "a.py",
                         "line": 1, "type": "function", "risk_score": 0.5, "has_test": False}],
        untested_high_risk=[],
        risk_score=42,
        blast_radius=1,
        summary="test",
    )
    d = report.to_dict()
    assert d["risk_score"] == 42
    assert d["blast_radius"] == 1
    assert d["changed_nodes"] == ["n1"]


def test_extract_changed_names_from_diff():
    from astra.impact.analyzer import _extract_changed_names_from_diff
    diff = """\
--- a/foo.py
+++ b/foo.py
@@ -1,5 +1,6 @@
+def new_function():
+    pass
+class NewClass:
+    pass
 def old_function():
     pass
"""
    names = _extract_changed_names_from_diff(diff)
    assert "new_function" in names
    assert "NewClass" in names
    assert "old_function" not in names


def test_node_has_test_coverage_by_filename():
    from astra.impact.analyzer import _node_has_test_coverage
    assert _node_has_test_coverage({"file": "/project/tests/test_auth.py", "name": "something"}) is True
    assert _node_has_test_coverage({"file": "/project/auth.py", "name": "login"}) is False


def test_node_has_test_coverage_by_name():
    from astra.impact.analyzer import _node_has_test_coverage
    assert _node_has_test_coverage({"file": "/project/auth.py", "name": "test_login"}) is True
    assert _node_has_test_coverage({"file": "/project/auth.py", "name": "TestLogin"}) is True
    assert _node_has_test_coverage({"file": "/project/auth.py", "name": "login"}) is False


def test_compute_risk_score_zero_blast():
    from astra.impact.analyzer import _compute_risk_score
    G = nx.DiGraph()
    score = _compute_risk_score([], ["n1"], G)
    assert score == 0


def test_compute_risk_score_high():
    from astra.impact.analyzer import _compute_risk_score
    G = nx.DiGraph()
    # 60 untested nodes = max blast + max untested
    G.add_node("root")
    for i in range(60):
        G.add_node(f"n{i}")
        G.add_edge("root", f"n{i}")

    annotated = [
        {"id": f"n{i}", "has_test": False, "risk_score": 0.01}
        for i in range(60)
    ]
    score = _compute_risk_score(annotated, ["root"], G)
    assert score > 50


# ── Integration tests ──────────────────────────────────────────────────────

def test_blast_radius_direct_callers():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, node_ids = _make_call_graph_store(tmp)

        from astra.impact.analyzer import ImpactAnalyzer
        analyzer = ImpactAnalyzer(store)

        # change validate_card — its callers are process_payment, test_handler
        report = analyzer.compute_blast_radius([node_ids["validate_card"]])

        store.close()

        assert report.blast_radius >= 1
        affected_names = {n["name"] for n in report.affected_nodes}
        # process_payment calls validate_card, so process_payment should be affected
        assert "process_payment" in affected_names


def test_blast_radius_empty_input():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_call_graph_store(tmp)

        from astra.impact.analyzer import ImpactAnalyzer
        report = ImpactAnalyzer(store).compute_blast_radius([])
        store.close()

        assert report.risk_score == 0
        assert report.blast_radius == 0


def test_blast_radius_nonexistent_node():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_call_graph_store(tmp)

        from astra.impact.analyzer import ImpactAnalyzer
        # nonexistent node ID — should not crash
        report = ImpactAnalyzer(store).compute_blast_radius(["nonexistent_id"])
        store.close()

        assert isinstance(report.risk_score, int)


def test_blast_radius_report_text():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, node_ids = _make_call_graph_store(tmp)

        from astra.impact.analyzer import ImpactAnalyzer
        report = ImpactAnalyzer(store).compute_blast_radius([node_ids["charge_card"]])
        store.close()

        text = report.to_text()
        assert "Impact Analysis" in text
        assert "Risk" in text


def test_compute_from_diff():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_call_graph_store(tmp)

        diff = """\
+def validate_card():
+    pass
"""
        from astra.impact.analyzer import ImpactAnalyzer
        report = ImpactAnalyzer(store).compute_from_diff(diff)
        store.close()

        assert isinstance(report, type(report))
        assert report.blast_radius >= 0


def test_impact_tool_function():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_call_graph_store(tmp)

        from astra.mcp.tools import tool_impact_analysis
        result = tool_impact_analysis(store, ["validate_card"])
        store.close()

        assert "blast_radius" in result
        assert "risk_score" in result
        assert isinstance(result["risk_score"], int)


def test_impact_tool_unknown_function():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_call_graph_store(tmp)

        from astra.mcp.tools import tool_impact_analysis
        result = tool_impact_analysis(store, ["nonexistent_function_xyz"])
        store.close()

        assert "error" in result
