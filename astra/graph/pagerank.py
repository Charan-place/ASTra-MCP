"""Personalized PageRank over the code graph. Mimics hippocampal association recall."""
import networkx as nx

from astra.graph.store import GraphStore


def build_nx_graph(store: GraphStore) -> nx.DiGraph:
    """Load full graph from SQLite into NetworkX for PageRank traversal."""
    G = nx.DiGraph()

    conn = store.conn
    for row in conn.execute("SELECT id, name, type, file FROM nodes"):
        G.add_node(row["id"], name=row["name"], type=row["type"], file=row["file"])

    for row in conn.execute("SELECT src, dst, relation FROM edges"):
        if G.has_node(row["src"]) and G.has_node(row["dst"]):
            G.add_edge(row["src"], row["dst"], relation=row["relation"])
            # undirected for PageRank — callers are equally relevant as callees
            G.add_edge(row["dst"], row["src"], relation=row["relation"] + "_REV")

    return G


def personalized_pagerank(
    G: nx.DiGraph,
    seed_node_ids: list[str],
    alpha: float = 0.85,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """
    Run PPR from seed nodes (top semantic matches).
    Returns (node_id, score) sorted descending.

    Alpha=0.85: standard damping. Higher = stays closer to seeds.
    """
    if not seed_node_ids or G.number_of_nodes() == 0:
        return []

    # personalization vector: uniform over seeds
    personalization = {}
    weight = 1.0 / len(seed_node_ids)
    for nid in seed_node_ids:
        if nid in G:
            personalization[nid] = weight

    if not personalization:
        return []

    try:
        scores = nx.pagerank(G, alpha=alpha, personalization=personalization, max_iter=100)
    except nx.PowerIterationFailedConvergence:
        scores = nx.pagerank(G, alpha=alpha, personalization=personalization, max_iter=200, tol=1e-4)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_k]
