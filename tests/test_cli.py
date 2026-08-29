"""Tests for the ASTra typer-based CLI (astra/cli/main.py)."""
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

import astra.indexer.embedder as embedder
from astra.cli.main import app

runner = CliRunner()


class _FakeSentenceTransformer:
    def __init__(self, model_name, *args, **kwargs):
        self._dim = 8

    def encode(self, texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True):
        return np.zeros((len(texts), self._dim), dtype=np.float32)


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    """Avoid downloading the real sentence-transformers model in CI/sandboxed envs."""
    embedder._reset_model_cache()
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    yield
    embedder._reset_model_cache()


@pytest.fixture
def project(tmp_path):
    """A tiny project dir with one Python source file."""
    (tmp_path / "app.py").write_text(
        "def greet(name):\n"
        "    '''Say hello.'''\n"
        "    return f'hi {name}'\n"
    )
    return tmp_path


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "ASTra v" in result.stdout


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    # no_args_is_help=True: click/typer exits 2 but still prints help text
    assert result.exit_code == 2
    assert "ASTra" in result.stdout


def test_status_without_index_fails(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 1
    assert "Not indexed" in result.stdout


def test_init_indexes_project(project):
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0
    assert "Done" in result.stdout
    assert (project / ".astra" / "graph.db").exists()


def test_status_after_init_reports_nodes(project):
    init_result = runner.invoke(app, ["init", str(project)])
    assert init_result.exit_code == 0

    status_result = runner.invoke(app, ["status", str(project)])
    assert status_result.exit_code == 0
    assert "Nodes" in status_result.stdout


def test_query_without_index_fails(tmp_path):
    result = runner.invoke(app, ["query", "do something", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "Not indexed" in result.stdout


def test_query_after_init_returns_context(project):
    init_result = runner.invoke(app, ["init", str(project)])
    assert init_result.exit_code == 0

    query_result = runner.invoke(
        app, ["query", "say hello to someone", "--project", str(project)]
    )
    assert query_result.exit_code == 0
    assert "tokens" in query_result.stdout


def test_memory_ls_empty(project):
    result = runner.invoke(app, ["memory", "ls", "--project", str(project)])
    assert result.exit_code == 0
    assert "No sessions" in result.stdout


def test_memory_save_and_ls(project):
    save_result = runner.invoke(
        app, ["memory", "save", "did something useful", "--project", str(project)]
    )
    assert save_result.exit_code == 0
    assert "Saved session" in save_result.stdout

    ls_result = runner.invoke(app, ["memory", "ls", "--project", str(project)])
    assert ls_result.exit_code == 0
    assert "did something useful" in ls_result.stdout


def test_memory_show_unknown_session(project):
    result = runner.invoke(app, ["memory", "show", "doesnotexist", "--project", str(project)])
    assert result.exit_code == 1
    assert "Session not found" in result.stdout


def test_impact_without_index_fails(tmp_path):
    result = runner.invoke(app, ["impact", "foo", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "Not indexed" in result.stdout


def test_federate_requires_repos():
    result = runner.invoke(app, ["federate"])
    assert result.exit_code == 1
    assert "Provide at least one repo path" in result.stdout
