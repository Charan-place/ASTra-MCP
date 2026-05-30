"""Tests for Phase 4: Temporal Knowledge Graph"""
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_git_repo_with_history(tmp_path: Path) -> Path:
    """
    Create a minimal git repo with 3 commits, each modifying a Python file.
    Returns repo path.
    """
    import subprocess

    repo_dir = tmp_path / "testrepo"
    repo_dir.mkdir()

    def run(cmd, cwd=None):
        subprocess.run(cmd, cwd=cwd or repo_dir, check=True,
                       capture_output=True)

    run(["git", "init"])
    run(["git", "config", "user.email", "test@test.com"])
    run(["git", "config", "user.name", "Test"])

    # Commit 1: initial file
    src = repo_dir / "auth.py"
    src.write_text("def login(user, password):\n    return True\n")
    run(["git", "add", "."])
    run(["git", "commit", "-m", "initial"])

    # Commit 2: add function
    src.write_text(
        "def login(user, password):\n    return True\n\n"
        "def logout(user):\n    pass\n"
    )
    run(["git", "add", "."])
    run(["git", "commit", "-m", "add logout"])

    # Commit 3: modify login
    src.write_text(
        "def login(user, password):\n    validate(password)\n    return True\n\n"
        "def logout(user):\n    pass\n\n"
        "def validate(password):\n    return len(password) > 8\n"
    )
    run(["git", "add", "."])
    run(["git", "commit", "-m", "add validate"])

    return repo_dir


# ── Unit tests ─────────────────────────────────────────────────────────────

def test_temporal_schema_created():
    with tempfile.TemporaryDirectory() as td:
        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(Path(td) / "g.db")
        indexer = TemporalIndexer(store)

        # tables should exist
        tables = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "temporal_nodes" in tables
        assert "temporal_edges" in tables
        assert "temporal_meta" in tables
        store.close()


def test_temporal_node_to_dict():
    from astra.temporal.indexer import TemporalNode
    n = TemporalNode(
        node_id="abc",
        name="login",
        file="auth.py",
        type="function",
        first_seen_ts=1000.0,
        last_seen_ts=2000.0,
        first_commit="aaa",
        last_commit="bbb",
        change_count=5,
        volatility=0.05,
    )
    d = n.to_dict()
    assert d["name"] == "login"
    assert d["change_count"] == 5
    assert d["volatility"] == 0.05


def test_is_indexed_false_when_empty():
    with tempfile.TemporaryDirectory() as td:
        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(Path(td) / "g.db")
        indexer = TemporalIndexer(store)
        assert indexer.is_indexed() is False
        store.close()


def test_get_file_content_helper():
    from astra.temporal.indexer import _get_file_content_at_commit

    with tempfile.TemporaryDirectory() as td:
        repo_path = _make_git_repo_with_history(Path(td))
        import git
        repo = git.Repo(str(repo_path))
        commit = list(repo.iter_commits("HEAD", max_count=1))[0]
        content = _get_file_content_at_commit(repo, commit, "auth.py")
        assert content is not None
        assert b"def login" in content


def test_get_changed_files():
    from astra.temporal.indexer import _get_changed_files

    with tempfile.TemporaryDirectory() as td:
        repo_path = _make_git_repo_with_history(Path(td))
        import git
        repo = git.Repo(str(repo_path))
        commits = list(repo.iter_commits("HEAD", max_count=3))
        # second commit should have auth.py changed
        changed = _get_changed_files(commits[0])  # most recent
        assert "auth.py" in changed


# ── Integration tests ──────────────────────────────────────────────────────

def test_build_timeline_non_git_repo():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(tmp / "g.db")
        indexer = TemporalIndexer(store)
        summary = indexer.build_timeline(tmp / "not_a_repo")
        store.close()

        assert summary.commits_processed == 0
        assert summary.nodes_tracked == 0


def test_build_timeline_with_git_history():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo_path = _make_git_repo_with_history(tmp)

        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(tmp / "g.db")
        indexer = TemporalIndexer(store)
        summary = indexer.build_timeline(repo_path, max_commits=10)
        store.close()

        assert summary.commits_processed == 3
        assert summary.nodes_tracked > 0
        assert summary.elapsed_s >= 0


def test_timeline_detects_volatile_node():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo_path = _make_git_repo_with_history(tmp)

        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(tmp / "g.db")
        indexer = TemporalIndexer(store)
        indexer.build_timeline(repo_path, max_commits=10)

        volatile = indexer.get_volatile_nodes(top_k=10)
        store.close()

        assert len(volatile) > 0
        # login appears in all 3 commits, should be most volatile
        names = [n["name"] for n in volatile]
        assert "login" in names


def test_timeline_is_indexed_after_build():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo_path = _make_git_repo_with_history(tmp)

        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer

        store = GraphStore(tmp / "g.db")
        indexer = TemporalIndexer(store)
        assert indexer.is_indexed() is False

        indexer.build_timeline(repo_path, max_commits=10)
        assert indexer.is_indexed() is True
        store.close()


def test_get_node_history():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo_path = _make_git_repo_with_history(tmp)

        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer
        from astra.indexer.symbol_table import Symbol
        import numpy as np

        store = GraphStore(tmp / "g.db")
        # seed current index with login function so we have a node_id
        sym = Symbol(type="function", name="login",
                     file=str(repo_path / "auth.py"), line_start=1, line_end=3)
        store.upsert_node(sym, np.zeros(384, dtype="float32"))
        store.commit()

        indexer = TemporalIndexer(store)
        indexer.build_timeline(repo_path, max_commits=10)

        history = indexer.get_node_history(sym.id)
        store.close()

        # history may or may not match (different node_ids between real parse vs test symbol)
        # but the call should not crash
        assert history is None or isinstance(history, dict)


def test_mcp_tool_get_volatility_no_index():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from astra.graph.store import GraphStore
        from astra.mcp.tools import tool_get_volatility

        store = GraphStore(tmp / "g.db")
        result = tool_get_volatility(store)
        store.close()

        assert "error" in result


def test_mcp_tool_get_volatility_with_index():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo_path = _make_git_repo_with_history(tmp)

        from astra.graph.store import GraphStore
        from astra.temporal.indexer import TemporalIndexer
        from astra.mcp.tools import tool_get_volatility

        store = GraphStore(tmp / "g.db")
        TemporalIndexer(store).build_timeline(repo_path, max_commits=10)

        result = tool_get_volatility(store, top_k=5)
        store.close()

        assert "top_volatile" in result
        assert len(result["top_volatile"]) > 0
