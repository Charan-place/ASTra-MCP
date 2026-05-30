"""
Cross-repo Federation — trace calls across repository boundaries.

Problem: Modern systems are microservices. Function A in service-1 calls
function B in service-2 via HTTP/gRPC/shared-lib. No tool shows this graph.

Solution: ASTra federated graph links boundary nodes across repos by:
1. Detecting "boundary nodes" — API endpoints, SDK exports, shared lib symbols
2. Matching them across repos by name + type + signature similarity
3. Adding cross-repo edges to a federated SQLite DB
4. PageRank runs across the full federation

Link detection strategies:
- Python shared package imports: `from service_common.auth import validate_token`
- REST endpoint matching: if service-A calls `POST /users` and service-B defines it
- gRPC proto method matching: same method name across repos
- Direct name match (same function name exported from multiple repos)

Schema: stored in a dedicated federation.db (separate from per-repo graph.db)
so any number of repos can be federated together.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx

from astra.graph.store import GraphStore

logger = logging.getLogger("astra.federation")

FEDERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS fed_repos (
    repo_id     TEXT PRIMARY KEY,
    repo_path   TEXT NOT NULL,
    db_path     TEXT NOT NULL,
    indexed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fed_boundary_nodes (
    node_id     TEXT NOT NULL,
    repo_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    file        TEXT NOT NULL,
    type        TEXT NOT NULL,
    link_type   TEXT NOT NULL,   -- EXPORT|ENDPOINT|GRPC|IMPORT_TARGET
    link_key    TEXT NOT NULL,   -- normalized key for matching
    PRIMARY KEY (node_id, repo_id)
);

CREATE TABLE IF NOT EXISTS fed_cross_edges (
    src_repo    TEXT NOT NULL,
    src_node    TEXT NOT NULL,
    dst_repo    TEXT NOT NULL,
    dst_node    TEXT NOT NULL,
    link_type   TEXT NOT NULL,
    confidence  REAL NOT NULL,   -- 0-1
    PRIMARY KEY (src_repo, src_node, dst_repo, dst_node)
);

CREATE INDEX IF NOT EXISTS idx_fbn_link_key ON fed_boundary_nodes(link_key);
CREATE INDEX IF NOT EXISTS idx_fbn_name     ON fed_boundary_nodes(name);
"""


@dataclass
class FedEdge:
    src_repo: str
    src_node: str
    dst_repo: str
    dst_node: str
    link_type: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "src_repo": self.src_repo,
            "src_node": self.src_node,
            "dst_repo": self.dst_repo,
            "dst_node": self.dst_node,
            "link_type": self.link_type,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class FederatedGraph:
    repos: list[str]
    nodes: int
    cross_edges: list[FedEdge]
    G: nx.DiGraph = field(default_factory=nx.DiGraph, repr=False)

    def to_dict(self) -> dict:
        return {
            "repos": self.repos,
            "total_nodes": self.nodes,
            "cross_edges": [e.to_dict() for e in self.cross_edges],
            "cross_edge_count": len(self.cross_edges),
        }


class FederationDB:
    """Manages the cross-repo federation SQLite database."""

    def __init__(self, fed_db_path: Path):
        import sqlite3
        self.db_path = fed_db_path
        self.conn = sqlite3.connect(str(fed_db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(FEDERATION_SCHEMA)
        self.conn.commit()

    def register_repo(self, repo_id: str, repo_path: str, db_path: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO fed_repos (repo_id, repo_path, db_path, indexed_at) VALUES (?,?,?,?)",
            (repo_id, repo_path, db_path, time.time()),
        )
        self.conn.commit()

    def upsert_boundary_node(self, node_id: str, repo_id: str, name: str,
                              file: str, node_type: str, link_type: str, link_key: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO fed_boundary_nodes
               (node_id, repo_id, name, file, type, link_type, link_key)
               VALUES (?,?,?,?,?,?,?)""",
            (node_id, repo_id, name, file, node_type, link_type, link_key),
        )

    def upsert_cross_edge(self, edge: FedEdge):
        self.conn.execute(
            """INSERT OR REPLACE INTO fed_cross_edges
               (src_repo, src_node, dst_repo, dst_node, link_type, confidence)
               VALUES (?,?,?,?,?,?)""",
            (edge.src_repo, edge.src_node, edge.dst_repo, edge.dst_node,
             edge.link_type, edge.confidence),
        )

    def commit(self):
        self.conn.commit()

    def get_cross_edges(self) -> list[FedEdge]:
        rows = self.conn.execute("SELECT * FROM fed_cross_edges").fetchall()
        return [FedEdge(**dict(r)) for r in rows]

    def get_repos(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM fed_repos").fetchall()
        return [dict(r) for r in rows]

    def find_matching_nodes(self, link_key: str, exclude_repo: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fed_boundary_nodes WHERE link_key=? AND repo_id!=?",
            (link_key, exclude_repo),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


class FederatedResolver:
    """
    Links boundary nodes across multiple repos.

    Usage:
        resolver = FederatedResolver(fed_db_path)
        resolver.add_repo("service-auth",  Path("/repos/auth"),  GraphStore(...))
        resolver.add_repo("service-api",   Path("/repos/api"),   GraphStore(...))
        fed_graph = resolver.link_all()
    """

    def __init__(self, fed_db_path: Path):
        self.fed_db = FederationDB(fed_db_path)
        self._stores: dict[str, GraphStore] = {}

    def add_repo(self, repo_id: str, repo_path: Path, store: GraphStore):
        """Register a repo and extract its boundary nodes."""
        self.fed_db.register_repo(repo_id, str(repo_path), str(store.db_path))
        self._stores[repo_id] = store

        # extract boundary nodes from this repo
        boundary_nodes = _extract_boundary_nodes(store, repo_id)
        for bn in boundary_nodes:
            self.fed_db.upsert_boundary_node(**bn)
        self.fed_db.commit()
        logger.info("Repo %s: %d boundary nodes extracted", repo_id, len(boundary_nodes))

    def link_all(self) -> FederatedGraph:
        """
        Match boundary nodes across all repos.
        Adds cross-repo edges to federation DB.
        Returns a FederatedGraph with full NetworkX DiGraph.
        """
        cross_edges = []

        # for each boundary node, find matches in other repos
        all_repos = self.fed_db.get_repos()
        for repo in all_repos:
            repo_id = repo["repo_id"]
            store = self._stores.get(repo_id)
            if not store:
                continue

            # get boundary nodes for this repo
            rows = self.fed_db.conn.execute(
                "SELECT * FROM fed_boundary_nodes WHERE repo_id=?", (repo_id,)
            ).fetchall()

            for _row in rows:
                row = dict(_row)
                link_key = row["link_key"]
                matches = self.fed_db.find_matching_nodes(link_key, exclude_repo=repo_id)

                for match in matches:
                    edge = FedEdge(
                        src_repo=repo_id,
                        src_node=row["node_id"],
                        dst_repo=match["repo_id"],
                        dst_node=match["node_id"],
                        link_type=row["link_type"],
                        confidence=_compute_confidence(row, match),
                    )
                    self.fed_db.upsert_cross_edge(edge)
                    cross_edges.append(edge)

        self.fed_db.commit()

        # build unified NetworkX graph
        G = self._build_federated_graph()

        return FederatedGraph(
            repos=[r["repo_id"] for r in all_repos],
            nodes=G.number_of_nodes(),
            cross_edges=cross_edges,
            G=G,
        )

    def trace_cross_repo(self, node_id: str, repo_id: str, max_hops: int = 5) -> list[dict]:
        """
        Follow call chain from node_id across repo boundaries.
        Returns list of {repo, node_id, name, file, hop} dicts.
        """
        visited = []
        frontier = [(repo_id, node_id, 0)]
        seen = set()

        while frontier:
            curr_repo, curr_node, hop = frontier.pop(0)
            if (curr_repo, curr_node) in seen or hop > max_hops:
                continue
            seen.add((curr_repo, curr_node))

            store = self._stores.get(curr_repo)
            if store:
                node = store.get_node(curr_node)
                if node:
                    visited.append({
                        "repo": curr_repo,
                        "node_id": curr_node,
                        "name": node["name"],
                        "file": node["file"],
                        "hop": hop,
                    })

            # same-repo callees (intra-repo)
            if store:
                callees = store.get_callees(curr_node)
                for c in callees:
                    frontier.append((curr_repo, c["id"], hop + 1))

            # cross-repo edges
            cross_rows = self.fed_db.conn.execute(
                "SELECT * FROM fed_cross_edges WHERE src_repo=? AND src_node=?",
                (curr_repo, curr_node),
            ).fetchall()
            for row in cross_rows:
                frontier.append((row["dst_repo"], row["dst_node"], hop + 1))

        return visited

    def _build_federated_graph(self) -> nx.DiGraph:
        """Build unified DiGraph from all repo graphs + cross-repo edges."""
        G = nx.DiGraph()

        for repo_id, store in self._stores.items():
            for nid in store.all_node_ids():
                G.add_node(f"{repo_id}:{nid}", repo=repo_id, node_id=nid)

            conn = store.conn
            for row in conn.execute("SELECT src, dst, relation FROM edges"):
                src = f"{repo_id}:{row['src']}"
                dst = f"{repo_id}:{row['dst']}"
                if G.has_node(src) and G.has_node(dst):
                    G.add_edge(src, dst, relation=row["relation"])

        # add cross-repo edges
        for edge in self.fed_db.get_cross_edges():
            src = f"{edge.src_repo}:{edge.src_node}"
            dst = f"{edge.dst_repo}:{edge.dst_node}"
            G.add_edge(src, dst, relation=f"CROSS_REPO:{edge.link_type}",
                       confidence=edge.confidence)

        return G

    def close(self):
        self.fed_db.close()


# ── Boundary node detection ────────────────────────────────────────────────

def _extract_boundary_nodes(store: GraphStore, repo_id: str) -> list[dict]:
    """
    Find nodes that cross repo boundaries:
    - Functions that look like API endpoints (Flask/FastAPI route handlers)
    - Exported symbols (in __init__.py or __all__)
    - Functions matching patterns that suggest cross-service use
    """
    boundary_nodes = []

    rows = store.conn.execute(
        "SELECT * FROM nodes WHERE type IN ('function', 'method', 'class')"
    ).fetchall()

    for row in rows:
        node = dict(row)
        link_types = _detect_link_types(node)

        for link_type, link_key in link_types:
            boundary_nodes.append({
                "node_id": node["id"],
                "repo_id": repo_id,
                "name": node["name"],
                "file": node["file"],
                "node_type": node["type"],
                "link_type": link_type,
                "link_key": link_key,
            })

    return boundary_nodes


def _detect_link_types(node: dict) -> list[tuple[str, str]]:
    """
    Detect if a node is a boundary node and what type.
    Returns list of (link_type, link_key) pairs.
    """
    results = []
    name = node.get("name", "")
    file_path = node.get("file", "")
    sig = node.get("signature", "") or ""
    doc = node.get("docstring", "") or ""

    # ── Pattern 1: FastAPI/Flask route handlers ────────────────────────────
    # Functions in files with "routes", "views", "endpoints", "api" in name
    path = Path(file_path)
    if any(kw in path.stem.lower() for kw in ("route", "view", "endpoint", "api", "handler")):
        # link_key: normalized function name (HTTP verb + resource)
        link_key = f"endpoint:{name.lower()}"
        results.append(("ENDPOINT", link_key))

    # ── Pattern 2: __init__.py exports ────────────────────────────────────
    if path.name == "__init__.py":
        link_key = f"export:{name}"
        results.append(("EXPORT", link_key))

    # ── Pattern 3: gRPC service methods ───────────────────────────────────
    # classes inheriting from Servicer or methods named like RPC methods (CamelCase)
    if any(kw in sig for kw in ("Servicer", "grpc")):
        link_key = f"grpc:{name}"
        results.append(("GRPC", link_key))

    # ── Pattern 4: Direct name match (universal fallback) ─────────────────
    # Any function that appears in multiple repos with same name
    # This enables cross-repo matching by name alone
    if node["type"] in ("function", "method") and not name.startswith("_"):
        link_key = f"fn:{name}"
        results.append(("NAME_MATCH", link_key))

    return results


def _compute_confidence(src_node: dict, dst_node: dict) -> float:
    """
    Confidence score for a cross-repo link.
    - Same link_type = higher confidence
    - ENDPOINT/GRPC/EXPORT links = 0.9
    - NAME_MATCH = 0.6 (may be coincidental)
    """
    link_type = src_node.get("link_type", "")
    if link_type in ("ENDPOINT", "GRPC", "EXPORT"):
        return 0.9
    if link_type == "NAME_MATCH":
        # higher confidence if same file pattern
        if Path(src_node["file"]).stem == Path(dst_node["file"]).stem:
            return 0.75
        return 0.6
    return 0.5
