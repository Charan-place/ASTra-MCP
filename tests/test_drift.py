"""Tests for Phase 3: Semantic Drift Detector"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_drift_store(tmp_path: Path):
    """
    Build a store with two functions:
    - 'validate_user': docstring says 'validate user credentials'
      but calls: send_email, log_analytics, update_session (semantic mismatch)
    - 'authenticate': docstring says 'check password hash'
      and calls: hash_password, compare_hash (semantic match)
    """
    from astra.graph.store import GraphStore
    from astra.indexer.symbol_table import Symbol, Edge
    from astra.indexer.embedder import embed_text

    store = GraphStore(tmp_path / "graph.db")
    file = str(tmp_path / "auth.py")

    # Main functions
    validate_user = Symbol(
        type="function", name="validate_user",
        file=file, line_start=1, line_end=20,
        docstring="validate user credentials",
    )
    authenticate = Symbol(
        type="function", name="authenticate",
        file=file, line_start=30, line_end=50,
        docstring="check password hash and return token",
    )

    # Callees for validate_user (semantically different from its name/docstring)
    send_email = Symbol(type="function", name="send_email",
                        file=file, line_start=60, line_end=70, docstring="send email notification")
    log_analytics = Symbol(type="function", name="log_analytics",
                           file=file, line_start=80, line_end=90, docstring="log analytics event")
    update_session = Symbol(type="function", name="update_session",
                            file=file, line_start=100, line_end=110, docstring="update user session")

    # Callees for authenticate (semantically aligned)
    hash_password = Symbol(type="function", name="hash_password",
                           file=file, line_start=120, line_end=130, docstring="hash the password")
    compare_hash = Symbol(type="function", name="compare_hash",
                          file=file, line_start=140, line_end=150, docstring="compare password hash")

    all_syms = [validate_user, authenticate, send_email, log_analytics,
                update_session, hash_password, compare_hash]

    for s in all_syms:
        vec = embed_text(s.embed_text)
        store.upsert_node(s, vec)

    node_ids = {s.name: s.id for s in all_syms}

    # Edges: validate_user calls email/analytics/session (mismatch)
    for callee in ["send_email", "log_analytics", "update_session"]:
        store.upsert_edge(Edge(src=node_ids["validate_user"], dst=node_ids[callee], relation="CALLS"))

    # Edges: authenticate calls hash/compare (aligned)
    for callee in ["hash_password", "compare_hash"]:
        store.upsert_edge(Edge(src=node_ids["authenticate"], dst=node_ids[callee], relation="CALLS"))

    store.commit()
    return store, node_ids


# ── Unit tests ─────────────────────────────────────────────────────────────

def test_drift_warning_to_dict():
    from astra.semantics.drift import DriftWarning
    w = DriftWarning(
        node_id="abc",
        name="validate_user",
        file="/a/b.py",
        line=10,
        declared_intent="validate user credentials",
        actual_callees=["send_email", "log_analytics"],
        drift_score=0.72,
        explanation="Drift detected.",
    )
    d = w.to_dict()
    assert d["name"] == "validate_user"
    assert d["drift_score"] == 0.72
    assert "send_email" in d["actual_callees"]


def test_build_explanation():
    from astra.semantics.drift import _build_explanation
    exp = _build_explanation("validate_user", "validate credentials", ["send_email"], 0.75)
    assert "validate_user" in exp
    assert "send_email" in exp
    assert "drift" in exp.lower()


def test_build_explanation_no_docstring():
    from astra.semantics.drift import _build_explanation
    exp = _build_explanation("process_data", "", ["write_file", "send_slack"], 0.4)
    assert "no docstring" in exp


# ── Integration tests ──────────────────────────────────────────────────────

def test_detector_finds_drifted_function():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, node_ids = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        detector = SemanticDriftDetector(store, threshold=0.1)  # low threshold to ensure detection
        warnings = detector.scan()
        store.close()

        # validate_user should be detected (calling email/analytics doesn't match 'validate credentials')
        warning_names = {w.name for w in warnings}
        assert "validate_user" in warning_names


def test_detector_check_node_by_id():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, node_ids = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        detector = SemanticDriftDetector(store, threshold=0.0)  # catch everything
        warning = detector.check_node(node_ids["validate_user"])
        store.close()

        assert warning is not None
        assert warning.name == "validate_user"
        assert 0.0 <= warning.drift_score <= 1.0


def test_detector_skips_functions_with_few_callees():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, node_ids = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        # authenticate only has 2 callees = exactly MIN_CALLEES, should be checked
        # but validate_user has 3 callees = above threshold
        detector = SemanticDriftDetector(store, threshold=0.0)
        warning = detector.check_node(node_ids["hash_password"])  # leaf node, 0 callees
        store.close()

        # leaf node with 0 callees should return None (no signal)
        assert warning is None


def test_detector_nonexistent_node():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        result = SemanticDriftDetector(store).check_node("nonexistent_id_xyz")
        store.close()

        assert result is None


def test_scan_returns_sorted_by_drift():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        warnings = SemanticDriftDetector(store, threshold=0.0).scan()
        store.close()

        if len(warnings) > 1:
            scores = [w.drift_score for w in warnings]
            assert scores == sorted(scores, reverse=True)


def test_scan_file_filter():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_drift_store(tmp)

        from astra.semantics.drift import SemanticDriftDetector
        file_path = str(tmp / "auth.py")
        warnings = SemanticDriftDetector(store, threshold=0.0).scan(file_filter=file_path)
        store.close()

        # all warnings should be from auth.py
        for w in warnings:
            assert w.file == file_path


def test_mcp_tool_semantic_audit():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store, _ = _make_drift_store(tmp)

        from astra.mcp.tools import tool_semantic_audit
        results = tool_semantic_audit(store, threshold=0.0)
        store.close()

        assert isinstance(results, list)
        for r in results:
            assert "name" in r
            assert "drift_score" in r
            assert "explanation" in r
