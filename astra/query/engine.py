"""Main query engine: task description → minimal relevant context."""
import networkx as nx

from astra.graph.store import GraphStore
from astra.graph.pagerank import build_nx_graph, personalized_pagerank
from astra.indexer.embedder import embed_text, top_k_similar
from astra.query.serializer import build_context

_nx_graph_cache: dict[str, nx.DiGraph] = {}


def _get_graph(store: GraphStore) -> nx.DiGraph:
    """Cache NX graph per db path. Rebuilt on explicit call."""
    key = str(store.db_path)
    if key not in _nx_graph_cache:
        _nx_graph_cache[key] = build_nx_graph(store)
    return _nx_graph_cache[key]


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
    corpus = store.all_embeddings()
    if not corpus:
        return {"context": "# ASTra: no indexed symbols found. Run: astra init", "tokens": 0, "nodes": 0}

    top_seeds = top_k_similar(query_vec, corpus, k=semantic_k)
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
    }


def search_symbols(store: GraphStore, query: str, top_k: int = 10) -> list[dict]:
    """Semantic symbol search. Returns list of node dicts with score."""
    query_vec = embed_text(query)
    corpus = store.all_embeddings()
    results = top_k_similar(query_vec, corpus, k=top_k)

    out = []
    for nid, score in results:
        node = store.get_node(nid)
        if node:
            node["score"] = round(score, 4)
            out.append(node)
    return out
