"""Tests for pluggable embedding model support (ASTRA_EMBED_MODEL env var)."""
import sys
import types

import numpy as np
import pytest

import astra.indexer.embedder as embedder


class _FakeSentenceTransformer:
    """Records the model name it was constructed with; encode() returns zeros."""

    last_model_name = None

    def __init__(self, model_name, *args, **kwargs):
        _FakeSentenceTransformer.last_model_name = model_name
        self._dim = 8

    def encode(self, texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True):
        return np.zeros((len(texts), self._dim), dtype=np.float32)


@pytest.fixture(autouse=True)
def _isolate_embedder_state(monkeypatch):
    """Reset the cached model and fake out sentence_transformers for every test."""
    embedder._reset_model_cache()

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    yield

    embedder._reset_model_cache()


def test_default_model_name(monkeypatch):
    monkeypatch.delenv("ASTRA_EMBED_MODEL", raising=False)
    embedder.embed_texts(["hello world"])
    assert _FakeSentenceTransformer.last_model_name == embedder._MODEL_NAME_DEFAULT
    assert embedder._MODEL_NAME_DEFAULT == "all-MiniLM-L6-v2"


def test_env_var_overrides_model_name(monkeypatch):
    monkeypatch.setenv("ASTRA_EMBED_MODEL", "some-org/custom-model")
    embedder.embed_texts(["hello world"])
    assert _FakeSentenceTransformer.last_model_name == "some-org/custom-model"


def test_embed_texts_shape_matches_model_dim():
    embedder._reset_model_cache()
    vecs = embedder.embed_texts(["a", "b", "c"])
    assert vecs.shape == (3, 8)
    assert vecs.dtype == np.float32
