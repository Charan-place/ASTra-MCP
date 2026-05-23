"""SQLite-backed graph store. Nodes = symbols, Edges = relationships."""
import sqlite3
import struct
import time
from pathlib import Path
from typing import Optional

import numpy as np

from astra.indexer.symbol_table import Symbol, Edge


_SCHEMA = Path(__file__).parent / "schema.sql"


class GraphStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        schema = _SCHEMA.read_text()
        self.conn.executescript(schema)
        self.conn.commit()

    # ── Write ──────────────────────────────────────────────────────────────

    def upsert_node(self, symbol: Symbol, embedding: Optional[np.ndarray] = None):
        emb_bytes = None
        if embedding is not None:
            emb_bytes = embedding.astype(np.float32).tobytes()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO nodes
              (id, type, name, file, signature, docstring,
               line_start, line_end, raw_text, embedding, indexed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                symbol.id, symbol.type, symbol.name, symbol.file,
                symbol.signature, symbol.docstring,
                symbol.line_start, symbol.line_end,
                symbol.raw_text, emb_bytes, time.time(),
            ),
        )

    def upsert_edge(self, edge: Edge):
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (src, dst, relation) VALUES (?,?,?)",
            (edge.src, edge.dst, edge.relation),
        )

    def upsert_file_hash(self, file: str, hash_val: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO file_hashes (file, hash, indexed_at) VALUES (?,?,?)",
            (file, hash_val, time.time()),
        )

    def delete_file(self, file: str):
        """Remove all nodes and edges belonging to a file."""
        cur = self.conn.execute("SELECT id FROM nodes WHERE file=?", (file,))
        node_ids = [r["id"] for r in cur.fetchall()]
        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            self.conn.execute(f"DELETE FROM edges WHERE src IN ({placeholders})", node_ids)
            self.conn.execute(f"DELETE FROM edges WHERE dst IN ({placeholders})", node_ids)
        self.conn.execute("DELETE FROM nodes WHERE file=?", (file,))
        self.conn.execute("DELETE FROM file_hashes WHERE file=?", (file,))

    def commit(self):
        self.conn.commit()

    # ── Read ───────────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def get_nodes_by_name(self, name: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM nodes WHERE name=?", (name,)).fetchall()
        return [dict(r) for r in rows]

    def get_nodes_by_file(self, file: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM nodes WHERE file=?", (file,)).fetchall()
        return [dict(r) for r in rows]

    def get_callers(self, node_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT n.* FROM nodes n JOIN edges e ON n.id=e.src WHERE e.dst=? AND e.relation='CALLS'",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_callees(self, node_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT n.* FROM nodes n JOIN edges e ON n.id=e.dst WHERE e.src=? AND e.relation='CALLS'",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_file_hash(self, file: str) -> Optional[str]:
        row = self.conn.execute("SELECT hash FROM file_hashes WHERE file=?", (file,)).fetchone()
        return row["hash"] if row else None

    def all_embeddings(self) -> list[tuple[str, np.ndarray]]:
        """Return (node_id, embedding) for all nodes that have embeddings."""
        rows = self.conn.execute("SELECT id, embedding FROM nodes WHERE embedding IS NOT NULL").fetchall()
        result = []
        for r in rows:
            arr = np.frombuffer(r["embedding"], dtype=np.float32).copy()
            result.append((r["id"], arr))
        return result

    def all_node_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT id FROM nodes").fetchall()
        return [r["id"] for r in rows]

    def stats(self) -> dict:
        n_nodes = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_files = self.conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0]
        return {"nodes": n_nodes, "edges": n_edges, "files": n_files}

    def close(self):
        self.conn.close()
