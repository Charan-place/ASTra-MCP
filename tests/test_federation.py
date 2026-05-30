"""Tests for Phase 5: Cross-repo Federation"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_store_with_function(tmp_path: Path, db_name: str, func_name: str, file_name: str, in_init: bool = False):
    """Create a GraphStore with one function node."""
    from astra.graph.store import GraphStore
    from astra.indexer.symbol_table import Symbol

    store = GraphStore(tmp_path / db_name)
    if in_init:
        file_path = str(tmp_path / "__init__.py")
    else:
        file_path = str(tmp_path / file_name)

    sym = Symbol(
        type="function",
        name=func_name,
        file=file_path,
        signature=f"def {func_name}()",
        docstring=f"{func_name} function",
        line_start=1, line_end=5,
    )
    store.upsert_node(sym, np.zeros(384, dtype="float32"))
    store.commit()
    return store, sym.id


# ── Unit tests ─────────────────────────────────────────────────────────────

def test_federation_db_creation():
    with tempfile.TemporaryDirectory() as td:
        from astra.federation.resolver import FederationDB
        db = FederationDB(Path(td) / "fed.db")

        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "fed_repos" in tables
        assert "fed_boundary_nodes" in tables
        assert "fed_cross_edges" in tables
        db.close()


def test_register_repo():
    with tempfile.TemporaryDirectory() as td:
        from astra.federation.resolver import FederationDB
        db = FederationDB(Path(td) / "fed.db")
        db.register_repo("repo-1", "/path/to/repo", "/path/to/db")
        repos = db.get_repos()
        assert len(repos) == 1
        assert repos[0]["repo_id"] == "repo-1"
        db.close()


def test_detect_link_types_endpoint():
    from astra.federation.resolver import _detect_link_types
    node = {
        "id": "abc",
        "name": "get_user",
        "file": "/project/api/routes.py",
        "type": "function",
        "signature": "def get_user(request)",
        "docstring": "",
    }
    links = _detect_link_types(node)
    link_types = [lt for lt, _ in links]
    assert "ENDPOINT" in link_types


def test_detect_link_types_init_export():
    from astra.federation.resolver import _detect_link_types
    node = {
        "id": "abc",
        "name": "authenticate",
        "file": "/project/auth/__init__.py",
        "type": "function",
        "signature": "def authenticate()",
        "docstring": "",
    }
    links = _detect_link_types(node)
    link_types = [lt for lt, _ in links]
    assert "EXPORT" in link_types


def test_detect_link_types_name_match():
    from astra.federation.resolver import _detect_link_types
    node = {
        "id": "abc",
        "name": "process_payment",
        "file": "/project/payments/handler.py",
        "type": "function",
        "signature": "def process_payment(amount)",
        "docstring": "",
    }
    links = _detect_link_types(node)
    link_types = [lt for lt, _ in links]
    assert "NAME_MATCH" in link_types


def test_detect_link_types_private_skipped():
    from astra.federation.resolver import _detect_link_types
    node = {
        "id": "abc",
        "name": "_internal_helper",
        "file": "/project/utils.py",
        "type": "function",
        "signature": "def _internal_helper()",
        "docstring": "",
    }
    links = _detect_link_types(node)
    # private function should not get NAME_MATCH
    link_types = [lt for lt, _ in links]
    assert "NAME_MATCH" not in link_types


def test_compute_confidence_endpoint():
    from astra.federation.resolver import _compute_confidence
    src = {"link_type": "ENDPOINT", "file": "/a/routes.py"}
    dst = {"link_type": "ENDPOINT", "file": "/b/routes.py"}
    conf = _compute_confidence(src, dst)
    assert conf >= 0.9


def test_compute_confidence_name_match():
    from astra.federation.resolver import _compute_confidence
    src = {"link_type": "NAME_MATCH", "file": "/a/auth.py"}
    dst = {"link_type": "NAME_MATCH", "file": "/b/utils.py"}
    conf = _compute_confidence(src, dst)
    assert 0.5 <= conf <= 0.8


def test_fed_edge_to_dict():
    from astra.federation.resolver import FedEdge
    e = FedEdge("r1", "n1", "r2", "n2", "EXPORT", 0.9)
    d = e.to_dict()
    assert d["src_repo"] == "r1"
    assert d["confidence"] == 0.9


# ── Integration tests ──────────────────────────────────────────────────────

def test_add_two_repos_and_link():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.federation.resolver import FederatedResolver

        # service-A has function "validate_token" in __init__.py (EXPORT)
        store_a, _ = _make_store_with_function(tmp, "a.db", "validate_token", "init.py", in_init=True)
        # service-B also has "validate_token" in __init__.py
        tmp_b = tmp / "b"
        tmp_b.mkdir()
        store_b, _ = _make_store_with_function(tmp_b, "b.db", "validate_token", "init.py", in_init=True)

        resolver = FederatedResolver(tmp / "fed.db")
        resolver.add_repo("service-a", tmp, store_a)
        resolver.add_repo("service-b", tmp_b, store_b)

        fed_graph = resolver.link_all()

        store_a.close()
        store_b.close()
        resolver.close()

        assert "service-a" in fed_graph.repos
        assert "service-b" in fed_graph.repos
        assert len(fed_graph.cross_edges) > 0


def test_federated_graph_has_cross_edges():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.federation.resolver import FederatedResolver

        store_a, _ = _make_store_with_function(tmp, "a.db", "get_user", "api/routes.py")
        tmp_b = tmp / "b"
        tmp_b.mkdir()
        store_b, _ = _make_store_with_function(tmp_b, "b.db", "get_user", "api/routes.py")

        resolver = FederatedResolver(tmp / "fed.db")
        resolver.add_repo("frontend", tmp, store_a)
        resolver.add_repo("backend", tmp_b, store_b)
        fed_graph = resolver.link_all()

        store_a.close()
        store_b.close()
        resolver.close()

        assert fed_graph.G.number_of_nodes() > 0
        # cross edges should exist between frontend and backend
        assert len(fed_graph.cross_edges) > 0


def test_trace_cross_repo_single_repo():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.federation.resolver import FederatedResolver

        store_a, node_id = _make_store_with_function(tmp, "a.db", "process", "handler.py")

        resolver = FederatedResolver(tmp / "fed.db")
        resolver.add_repo("service-a", tmp, store_a)
        resolver.link_all()

        # trace from process — no cross-repo edges, just intra-repo
        trace = resolver.trace_cross_repo(node_id, "service-a", max_hops=2)
        store_a.close()
        resolver.close()

        assert isinstance(trace, list)
        # at minimum the starting node should be in trace
        assert any(t["node_id"] == node_id for t in trace)


def test_federated_graph_to_dict():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.federation.resolver import FederatedResolver

        store_a, _ = _make_store_with_function(tmp, "a.db", "shared_fn", "init.py", in_init=True)
        tmp_b = tmp / "b"
        tmp_b.mkdir()
        store_b, _ = _make_store_with_function(tmp_b, "b.db", "shared_fn", "init.py", in_init=True)

        resolver = FederatedResolver(tmp / "fed.db")
        resolver.add_repo("repo-a", tmp, store_a)
        resolver.add_repo("repo-b", tmp_b, store_b)
        fed_graph = resolver.link_all()
        d = fed_graph.to_dict()

        store_a.close()
        store_b.close()
        resolver.close()

        assert "repos" in d
        assert "cross_edges" in d
        assert "total_nodes" in d
        assert d["cross_edge_count"] == len(d["cross_edges"])
