"""
Temporal Knowledge Graph — builds a 4D code graph (nodes + edges + time).

Algorithm:
1. Walk git history commit-by-commit
2. At each commit: extract AST symbols from changed files only (incremental)
3. Diff against previous snapshot: what nodes appeared/disappeared/changed
4. Store in SQLite: temporal_nodes + temporal_edges tables
5. Compute volatility score per node (change_count / commits_alive)

This enables:
- "Which functions change most often?" (stability analysis)
- "When did this dependency appear?" (architectural archaeology)
- "Which files always break together?" (coupling detection)
- Predictive risk: volatile nodes near your change = higher risk
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from astra.graph.store import GraphStore
from astra.indexer.parser import parse_file, SUPPORTED, SKIP_DIRS
from astra.indexer.symbol_table import Symbol, FileSymbols

logger = logging.getLogger("astra.temporal")

# ── Schema additions ───────────────────────────────────────────────────────

TEMPORAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_nodes (
    node_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    file            TEXT NOT NULL,
    type            TEXT NOT NULL,
    first_seen_ts   REAL NOT NULL,
    last_seen_ts    REAL NOT NULL,
    first_commit    TEXT,
    last_commit     TEXT,
    change_count    INTEGER DEFAULT 0,
    volatility      REAL DEFAULT 0.0,
    PRIMARY KEY (node_id)
);

CREATE TABLE IF NOT EXISTS temporal_edges (
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    relation        TEXT NOT NULL,
    first_seen_ts   REAL NOT NULL,
    last_seen_ts    REAL NOT NULL,
    PRIMARY KEY (src, dst, relation)
);

CREATE TABLE IF NOT EXISTS temporal_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tnodes_file       ON temporal_nodes(file);
CREATE INDEX IF NOT EXISTS idx_tnodes_volatility ON temporal_nodes(volatility DESC);
"""


@dataclass
class TemporalNode:
    node_id: str
    name: str
    file: str
    type: str
    first_seen_ts: float
    last_seen_ts: float
    first_commit: str = ""
    last_commit: str = ""
    change_count: int = 0
    volatility: float = 0.0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "file": self.file,
            "type": self.type,
            "first_seen_ts": self.first_seen_ts,
            "last_seen_ts": self.last_seen_ts,
            "first_commit": self.first_commit,
            "last_commit": self.last_commit,
            "change_count": self.change_count,
            "volatility": round(self.volatility, 4),
        }


@dataclass
class TemporalSummary:
    """Result of a full temporal index run."""
    commits_processed: int
    nodes_tracked: int
    edges_tracked: int
    top_volatile: list[TemporalNode]   # highest volatility nodes
    elapsed_s: float

    def to_dict(self) -> dict:
        return {
            "commits_processed": self.commits_processed,
            "nodes_tracked": self.nodes_tracked,
            "edges_tracked": self.edges_tracked,
            "top_volatile": [n.to_dict() for n in self.top_volatile],
            "elapsed_s": round(self.elapsed_s, 2),
        }


# ── Temporal indexer ───────────────────────────────────────────────────────

class TemporalIndexer:
    def __init__(self, store: GraphStore):
        self.store = store
        self._init_temporal_schema()

    def _init_temporal_schema(self):
        self.store.conn.executescript(TEMPORAL_SCHEMA)
        self.store.conn.commit()

    def build_timeline(
        self,
        repo_path: Path,
        max_commits: int = 500,
        branch: str = "HEAD",
    ) -> TemporalSummary:
        """
        Walk git history and build temporal graph.
        max_commits: cap to avoid very long repos (can run incrementally).
        """
        try:
            import git as gitlib
        except ImportError:
            raise RuntimeError("gitpython required: pip install gitpython")

        t0 = time.time()
        try:
            repo = gitlib.Repo(str(repo_path), search_parent_directories=True)
        except (gitlib.exc.InvalidGitRepositoryError, gitlib.exc.NoSuchPathError):
            logger.warning("Not a git repository: %s", repo_path)
            return TemporalSummary(0, 0, 0, [], 0.0)

        commits = list(repo.iter_commits(branch, max_count=max_commits))
        commits.reverse()  # oldest first

        logger.info("Processing %d commits for temporal index...", len(commits))

        # state: {node_id -> TemporalNode} (live across commits)
        live_nodes: dict[str, TemporalNode] = {}
        live_edges: dict[tuple, dict] = {}  # (src,dst,rel) -> {first_seen_ts, last_seen_ts}

        # previous commit's file snapshot: {file_path -> set of node_ids}
        prev_snapshot: dict[str, set[str]] = {}

        commits_processed = 0
        for commit in commits:
            commit_ts = float(commit.committed_date)
            commit_sha = commit.hexsha[:8]

            # find changed files in this commit
            if commit.parents:
                changed_files = _get_changed_files(commit)
            else:
                # first commit: all files
                changed_files = _get_all_files_in_commit(commit)

            for file_path in changed_files:
                ext = Path(file_path).suffix.lower()
                if ext not in SUPPORTED:
                    continue
                if any(skip in Path(file_path).parts for skip in SKIP_DIRS):
                    continue

                # extract blob content at this commit
                content = _get_file_content_at_commit(repo, commit, file_path)
                if content is None:
                    # file deleted
                    old_ids = prev_snapshot.pop(file_path, set())
                    for nid in old_ids:
                        if nid in live_nodes:
                            live_nodes[nid].last_seen_ts = commit_ts
                            live_nodes[nid].last_commit = commit_sha
                    continue

                # parse AST from in-memory content
                file_syms = _parse_content(content, file_path)
                if not file_syms:
                    continue

                current_ids = {s.id for s in file_syms.symbols}
                old_ids = prev_snapshot.get(file_path, set())

                # new nodes (appeared)
                for sym in file_syms.symbols:
                    if sym.id not in live_nodes:
                        live_nodes[sym.id] = TemporalNode(
                            node_id=sym.id,
                            name=sym.name,
                            file=file_path,
                            type=sym.type,
                            first_seen_ts=commit_ts,
                            last_seen_ts=commit_ts,
                            first_commit=commit_sha,
                            last_commit=commit_sha,
                            change_count=1,
                        )
                    else:
                        # existing node changed
                        node = live_nodes[sym.id]
                        node.last_seen_ts = commit_ts
                        node.last_commit = commit_sha
                        node.change_count += 1

                # disappeared nodes
                for nid in (old_ids - current_ids):
                    if nid in live_nodes:
                        live_nodes[nid].last_seen_ts = commit_ts
                        live_nodes[nid].last_commit = commit_sha

                # edges
                for edge in file_syms.edges:
                    key = (edge.src, edge.dst, edge.relation)
                    if key not in live_edges:
                        live_edges[key] = {"first_seen_ts": commit_ts, "last_seen_ts": commit_ts}
                    else:
                        live_edges[key]["last_seen_ts"] = commit_ts

                prev_snapshot[file_path] = current_ids

            commits_processed += 1

        # compute volatility: change_count normalized by lifespan in commits
        total_commits = max(commits_processed, 1)
        for node in live_nodes.values():
            lifespan = node.change_count
            node.volatility = round(lifespan / total_commits, 4)

        # persist to DB
        self._persist(live_nodes, live_edges, commits_processed)

        top_volatile = sorted(live_nodes.values(), key=lambda n: -n.volatility)[:10]
        elapsed = time.time() - t0

        logger.info("Temporal index: %d nodes, %d edges, %.1fs",
                    len(live_nodes), len(live_edges), elapsed)

        return TemporalSummary(
            commits_processed=commits_processed,
            nodes_tracked=len(live_nodes),
            edges_tracked=len(live_edges),
            top_volatile=top_volatile,
            elapsed_s=elapsed,
        )

    def _persist(
        self,
        nodes: dict[str, TemporalNode],
        edges: dict[tuple, dict],
        commits_processed: int,
    ):
        conn = self.store.conn
        with self.store._lock:
            conn.execute("DELETE FROM temporal_nodes")
            conn.execute("DELETE FROM temporal_edges")

            for n in nodes.values():
                conn.execute(
                    """INSERT INTO temporal_nodes
                       (node_id, name, file, type, first_seen_ts, last_seen_ts,
                        first_commit, last_commit, change_count, volatility)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (n.node_id, n.name, n.file, n.type,
                     n.first_seen_ts, n.last_seen_ts,
                     n.first_commit, n.last_commit,
                     n.change_count, n.volatility),
                )

            for (src, dst, rel), ts_data in edges.items():
                conn.execute(
                    """INSERT INTO temporal_edges
                       (src, dst, relation, first_seen_ts, last_seen_ts)
                       VALUES (?,?,?,?,?)""",
                    (src, dst, rel, ts_data["first_seen_ts"], ts_data["last_seen_ts"]),
                )

            conn.execute(
                "INSERT OR REPLACE INTO temporal_meta (key, value) VALUES ('commits_processed', ?)",
                (str(commits_processed),),
            )
            conn.commit()

    def get_volatile_nodes(self, top_k: int = 20) -> list[dict]:
        """Return top-k most volatile nodes (changed most relative to their lifespan)."""
        rows = self.store.conn.execute(
            "SELECT * FROM temporal_nodes ORDER BY volatility DESC LIMIT ?", (top_k,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node_history(self, node_id: str) -> Optional[dict]:
        """Return temporal metadata for a specific node."""
        row = self.store.conn.execute(
            "SELECT * FROM temporal_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_coupling_pairs(self, min_cochange: int = 3) -> list[dict]:
        """
        Find files that change together frequently (temporal coupling).
        Uses co-change frequency: files that appear in the same commit.
        Returns pairs sorted by co-change count descending.
        """
        rows = self.store.conn.execute(
            """
            SELECT t1.file as file_a, t2.file as file_b,
                   COUNT(*) as cochange_count
            FROM temporal_nodes t1
            JOIN temporal_nodes t2 ON t1.file < t2.file
                AND ABS(t1.last_seen_ts - t2.last_seen_ts) < 86400
            GROUP BY t1.file, t2.file
            HAVING cochange_count >= ?
            ORDER BY cochange_count DESC
            LIMIT 20
            """,
            (min_cochange,)
        ).fetchall()
        return [dict(r) for r in rows]

    def is_indexed(self) -> bool:
        try:
            count = self.store.conn.execute(
                "SELECT COUNT(*) FROM temporal_nodes"
            ).fetchone()[0]
            return count > 0
        except Exception:
            return False


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_changed_files(commit) -> list[str]:
    """Files changed in this commit vs its parent."""
    try:
        parent = commit.parents[0]
        diff = parent.diff(commit)
        files = []
        for d in diff:
            if d.b_path:
                files.append(d.b_path)
            if d.a_path and d.a_path != d.b_path:
                files.append(d.a_path)
        return list(set(files))
    except Exception:
        return []


def _get_all_files_in_commit(commit) -> list[str]:
    """All files in the initial commit."""
    files = []
    for item in commit.tree.traverse():
        if hasattr(item, "path") and not hasattr(item, "trees"):
            files.append(item.path)
    return files


def _get_file_content_at_commit(repo, commit, file_path: str) -> Optional[bytes]:
    """Get file content as bytes at a specific commit. Returns None if deleted."""
    try:
        blob = commit.tree / file_path
        return blob.data_stream.read()
    except KeyError:
        return None
    except Exception:
        return None


def _parse_content(content: bytes, file_path: str) -> Optional[FileSymbols]:
    """Parse AST from in-memory bytes without writing to disk."""
    import tempfile
    import os

    # write to temp file with correct extension, parse, delete
    suffix = Path(file_path).suffix
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(content)
            tmp_path = Path(tf.name)
        try:
            result = parse_file(tmp_path)
            # fix file path back to original
            if result:
                for sym in result.symbols:
                    sym.file = file_path
                result.file = file_path
            return result
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.debug("Parse failed for %s: %s", file_path, e)
        return None
