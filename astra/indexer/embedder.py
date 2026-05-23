"""Embed symbol text into 384-dim vectors using sentence-transformers."""
from typing import Optional
import numpy as np

_model = None
_MODEL_NAME = "all-MiniLM-L6-v2"


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    """Batch embed. Returns (N, 384) float32 array."""
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
