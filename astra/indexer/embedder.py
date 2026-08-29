"""Embed symbol text into vectors using sentence-transformers.

The model is swappable via the ASTRA_EMBED_MODEL env var (any
sentence-transformers-compatible model name or local path). Defaults to
all-MiniLM-L6-v2 (384-dim). Downstream storage (astra/graph/schema.sql's
`embedding BLOB` column and astra/graph/store.py's np.frombuffer reads) does
not assume a fixed dimension, so swapping models is safe as long as a given
`.astra/graph.db` is not queried with vectors from two different-dimension
models (re-run `astra init --force` after switching models to re-embed
everything consistently).
"""
import os
from typing import Optional
import numpy as np

_model = None
_MODEL_NAME_DEFAULT = "all-MiniLM-L6-v2"


def _model_name() -> str:
    return os.environ.get("ASTRA_EMBED_MODEL", _MODEL_NAME_DEFAULT)


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_model_name())
    return _model


def _reset_model_cache():
    """Test helper: force the next embed call to reconstruct the model."""
    global _model
    _model = None


def embed_texts(texts: list[str]) -> np.ndarray:
    """Batch embed. Returns (N, D) float32 array, D depends on the active model."""
    model = _get_model()
    vecs = model.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
    return vecs.astype(np.float32)


def embed_text(text: str) -> np.ndarray:
    return embed_texts([text])[0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Both vectors assumed L2-normalized (output of embed_texts with normalize=True)."""
    return float(np.dot(a, b))


def top_k_similar(
    query_vec: np.ndarray,
    corpus: list[tuple[str, np.ndarray]],
    k: int = 10,
) -> list[tuple[str, float]]:
    """Return top-k (node_id, score) sorted descending."""
    if not corpus:
        return []
    ids, vecs = zip(*corpus)
    matrix = np.stack(vecs)                             # (N, 384)
    scores = matrix @ query_vec                         # (N,)
    top_idx = np.argsort(scores)[::-1][:k]
    return [(ids[i], float(scores[i])) for i in top_idx]
