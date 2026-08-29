"""Tests for the optional HNSW-backed ANN index (astra.graph.ann_index)."""
import numpy as np
import pytest

hnswlib = pytest.importorskip("hnswlib")

from astra.graph.ann_index import HNSWIndex
from astra.indexer.embedder import top_k_similar


def _make_corpus(n=50, dim=32, seed=0):
    rng = np.random.default_rng(seed)
    vecs = rng.normal(size=(n, dim)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    ids = [f"node_{i}" for i in range(n)]
    return ids, vecs


def test_hnsw_build_and_query_matches_brute_force():
    ids, vecs = _make_corpus(n=50, dim=32)
    idx = HNSWIndex()
    idx.build(ids, vecs)

    rng = np.random.default_rng(123)
    query = rng.normal(size=32).astype(np.float32)
    query /= np.linalg.norm(query)

    hnsw_results = idx.query(query, k=5)
    corpus = list(zip(ids, vecs))
    brute_results = top_k_similar(query, corpus, k=5)

    hnsw_top1 = hnsw_results[0][0]
    brute_top1 = brute_results[0][0]
    assert hnsw_top1 == brute_top1

    hnsw_ids = {nid for nid, _ in hnsw_results}
    brute_ids = {nid for nid, _ in brute_results}
    overlap = len(hnsw_ids & brute_ids) / len(brute_ids)
    assert overlap >= 0.8


def test_hnsw_incremental_add():
    ids, vecs = _make_corpus(n=20, dim=16, seed=1)
    idx = HNSWIndex()
    idx.build(ids[:10], vecs[:10])
    for nid, vec in zip(ids[10:], vecs[10:]):
        idx.add(nid, vec)

    results = idx.query(vecs[15], k=1)
    assert results[0][0] == "node_15"


def test_hnsw_save_load_roundtrip(tmp_path):
    ids, vecs = _make_corpus(n=30, dim=24, seed=2)
    idx = HNSWIndex()
    idx.build(ids, vecs)

    path = tmp_path / "hnsw.index"
    idx.save(path)

    idx2 = HNSWIndex()
    idx2.load(path)

    query = vecs[5]
    r1 = idx.query(query, k=3)
    r2 = idx2.query(query, k=3)
    assert [nid for nid, _ in r1] == [nid for nid, _ in r2]


def test_hnsw_empty_query_returns_empty():
    idx = HNSWIndex()
    idx.build([], np.zeros((0, 8), dtype=np.float32))
    assert idx.query(np.zeros(8, dtype=np.float32), k=5) == []
