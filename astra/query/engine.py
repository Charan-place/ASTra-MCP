"""Main query engine: task description → minimal relevant context."""
import os
import time
import networkx as nx

from astra.graph.store import GraphStore
from astra.graph.pagerank import build_nx_graph, personalized_pagerank
from astra.indexer.embedder import embed_text, top_k_similar
from astra.query.serializer import build_context

# Cache: db_path -> (graph, built_at_timestamp)
_nx_graph_cache: dict[str, tuple[nx.DiGraph, float]] = {}
_CACHE_TTL_S = 300  # rebuild graph after 5 minutes of no invalidation

# Cache: db_path -> HNSWIndex (only populated when ASTRA_INDEX_BACKEND=hnsw)
_ann_index_cache: dict[str, "object"] = {}


def _index_backend() -> str:
    return os.environ.get("ASTRA_INDEX_BACKEND", "brute").lower()


def _get_ann_index(store: GraphStore):
    """Load (or build+cache) the HNSW index for this store, from disk if present."""
    from astra.graph.ann_index import HNSWIndex

    key = str(store.db_path)
    idx = _ann_index_cache.get(key)
    if idx is not None:
        return idx

    idx = HNSWIndex()
    index_path = store.db_path.parent / "hnsw.index"
    meta_path = index_path.with_suffix(index_path.suffix + ".meta.npy")
    if index_path.exists() and meta_path.exists():
        idx.load(index_path)
    else:
        corpus = store.all_embeddings()
        if corpus:
            ids, vecs = zip(*corpus)
            import numpy as np
            idx.build(list(ids), np.stack(vecs))
            idx.save(index_path)
    _ann_index_cache[key] = idx
    return idx


def _semantic_top_k(store: GraphStore, query_vec, k: int) -> list[tuple[str, float]]:
    """Dispatch to the configured similarity backend."""
    if _index_backend() == "hnsw":
        idx = _get_ann_index(store)
        results = idx.query(query_vec, k=k)
        if results:
            return results
        # fall through to brute-force if the ANN index is empty/unbuilt
    corpus = store.all_embeddings()
    return top_k_similar(query_vec, corpus, k=k)


def _get_graph(store: GraphStore) -> nx.DiGraph:
    key = str(store.db_path)
    now = time.monotonic()
    entry = _nx_graph_cache.get(key)
    if entry is not None:
        graph, built_at = entry
        if now - built_at < _CACHE_TTL_S:
            return graph
    graph = build_nx_graph(store)
    _nx_graph_cache[key] = (graph, now)
    return graph


def invalidate_graph_cache(store: GraphStore):
    key = str(store.db_path)
    _nx_graph_cache.pop(key, None)


def get_context(
    store: GraphStore,
    task: str,
    max_tokens: int = 4000,
    semantic_k: int = 5,
    pagerank_k: int = 25,
) -> dict:
    """
    Core pipeline:
    1. Embed task → query vector
    2. Cosine similarity → top-k seed nodes
    3. Personalized PageRank → expand to related nodes
    4. Serialize → token-minimal context string

    Returns dict with context, token count, and nodes used.
    """
    # step 1: embed task
    query_vec = embed_text(task)

    # step 2: semantic seed finding
    if not store.has_embeddings():
        return {"context": "# ASTra: no indexed symbols found. Run: astra init", "tokens": 0, "nodes": 0}

    top_seeds = _semantic_top_k(store, query_vec, k=semantic_k)
    seed_ids = [nid for nid, _ in top_seeds]

    # step 3: PageRank expansion
    G = _get_graph(store)
    ranked = personalized_pagerank(G, seed_ids, top_k=pagerank_k)

    # merge: seeds first (highest semantic relevance), then PageRank expansion
    seen = set()
    merged: list[tuple[str, float]] = []
    for nid, score in top_seeds:
        if nid not in seen:
            merged.append((nid, score))
            seen.add(nid)
    for nid, score in ranked:
        if nid not in seen:
            merged.append((nid, score * 0.8))  # slight discount for structural nodes
            seen.add(nid)

    # step 4: serialize to token budget
    context, token_count = build_context(store, merged, max_tokens=max_tokens)

    return {
        "context": context,
        "tokens": token_count,
        "nodes": len(merged),
        "seeds": seed_ids,
        "node_ids": [nid for nid, _ in merged],
    }


def search_symbols(store: GraphStore, query: str, top_k: int = 10) -> list[dict]:
    """Semantic symbol search. Returns list of node dicts with score."""
    query_vec = embed_text(query)
    results = _semantic_top_k(store, query_vec, k=top_k)

    out = []
    for nid, score in results:
        node = store.get_node(nid)
        if node:
            node.pop("embedding", None)  # raw float32 bytes aren't JSON-serializable
            node["score"] = round(score, 4)
            out.append(node)
    return out
