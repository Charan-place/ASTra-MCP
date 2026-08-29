"""Optional HNSW-backed approximate nearest neighbor index for large corpora.

Requires the optional `hnswlib` dependency (`pip install astra-mcp[scale]`).
Falls back gracefully: importing this module without hnswlib installed will
raise ImportError only when HNSWIndex is actually instantiated, so the rest
of the codebase can import ann_index unconditionally.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np


class HNSWIndex:
    """Thin wrapper around hnswlib for cosine-similarity ANN search.

    Vectors are expected to be L2-normalized float32 (as produced by
    astra.indexer.embedder.embed_texts), so we use the 'ip' (inner product)
    space, which is equivalent to cosine similarity for unit vectors.
    """

    def __init__(self, dim: Optional[int] = None, max_elements: int = 200_000,
                 ef_construction: int = 200, M: int = 16):
        try:
            import hnswlib
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "hnswlib is required for HNSWIndex. Install with: pip install astra-mcp[scale]"
            ) from e
        self._hnswlib = hnswlib
        self.dim = dim
        self.max_elements = max_elements
        self.ef_construction = ef_construction
        self.M = M
        self._index = None
        self._ids: list[str] = []          # int label -> node id
        self._id_to_label: dict[str, int] = {}
        self._next_label = 0

    # ── construction ──────────────────────────────────────────────────────

    def _ensure_index(self, dim: int):
        if self._index is None:
            self.dim = dim
            self._index = self._hnswlib.Index(space="ip", dim=dim)
            self._index.init_index(
                max_elements=self.max_elements,
                ef_construction=self.ef_construction,
                M=self.M,
            )
            self._index.set_ef(max(self.ef_construction, 50))

    def build(self, ids: list[str], vectors: np.ndarray):
        """(Re)build the index from scratch given a full set of ids/vectors."""
        if len(ids) == 0:
            self._index = None
            self._ids = []
            self._id_to_label = {}
            self._next_label = 0
            return
        vectors = np.asarray(vectors, dtype=np.float32)
        dim = vectors.shape[1]
        self.max_elements = max(self.max_elements, len(ids))
        self._index = self._hnswlib.Index(space="ip", dim=dim)
        self._index.init_index(
            max_elements=self.max_elements,
            ef_construction=self.ef_construction,
            M=self.M,
        )
        self._index.set_ef(max(self.ef_construction, 50))
        self.dim = dim
        labels = np.arange(len(ids))
        self._index.add_items(vectors, labels)
        self._ids = list(ids)
        self._id_to_label = {nid: i for i, nid in enumerate(ids)}
        self._next_label = len(ids)

    def add(self, id: str, vector: np.ndarray):
        """Incrementally add (or update) a single vector."""
        vector = np.asarray(vector, dtype=np.float32)
        self._ensure_index(vector.shape[0])

        if id in self._id_to_label:
            label = self._id_to_label[id]
        else:
            label = self._next_label
            self._next_label += 1
            self._id_to_label[id] = label
            if label < len(self._ids):
                self._ids[label] = id
            else:
                self._ids.append(id)

        # grow capacity if needed
        current_count = self._index.get_current_count()
        if self._next_label >= self.max_elements:
            self.max_elements = max(self.max_elements * 2, self._next_label + 1)
            self._index.resize_index(self.max_elements)

        self._index.add_items(vector.reshape(1, -1), np.array([label]))

    def add_many(self, ids: list[str], vectors: np.ndarray):
        for nid, vec in zip(ids, vectors):
            self.add(nid, vec)

    # ── query ────────────────────────────────────────────────────────────

    def query(self, vector: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        if self._index is None or self._index.get_current_count() == 0:
            return []
        vector = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        k = min(k, self._index.get_current_count())
        labels, distances = self._index.knn_query(vector, k=k)
        out = []
        for label, dist in zip(labels[0], distances[0]):
            if label < 0 or label >= len(self._ids):
                continue
            # hnswlib 'ip' space returns distance = 1 - inner_product for
            # normalized vectors; convert back to a cosine-similarity score.
            score = 1.0 - float(dist)
            out.append((self._ids[int(label)], score))
        return out

    # ── persistence ──────────────────────────────────────────────────────

    def save(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            self._index.save_index(str(path))
        meta_path = path.with_suffix(path.suffix + ".meta.npy")
        np.save(
            meta_path,
            {
                "ids": self._ids,
                "dim": self.dim,
                "max_elements": self.max_elements,
                "ef_construction": self.ef_construction,
                "M": self.M,
                "next_label": self._next_label,
            },
            allow_pickle=True,
        )

    def load(self, path: Path):
        path = Path(path)
        meta_path = path.with_suffix(path.suffix + ".meta.npy")
        meta = np.load(meta_path, allow_pickle=True).item()
        self._ids = meta["ids"]
        self.dim = meta["dim"]
        self.max_elements = meta["max_elements"]
        self.ef_construction = meta["ef_construction"]
        self.M = meta["M"]
        self._next_label = meta["next_label"]
        self._id_to_label = {nid: i for i, nid in enumerate(self._ids)}
        if path.exists() and self.dim is not None:
            self._index = self._hnswlib.Index(space="ip", dim=self.dim)
            self._index.load_index(str(path), max_elements=self.max_elements)
            self._index.set_ef(max(self.ef_construction, 50))
        else:
            self._index = None
