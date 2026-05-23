"""Hot/cold session memory. Brain's incremental compression analog."""
import sqlite3
import time
from pathlib import Path
from typing import Optional

import numpy as np

from astra.indexer.embedder import embed_text, top_k_similar


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT    PRIMARY KEY,
    project     TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    tags        TEXT,
    created_at  REAL    NOT NULL,
    embedding   BLOB
);
"""


class SessionMemory:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SESSION_SCHEMA)
        self.conn.commit()

    def save_session(self, session_id: str, project: str, summary: str, tags: list[str] = None):
        """Store session delta. Summary should be LLM-compressed (<300 tokens)."""
        emb = embed_text(summary).tobytes()
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (id, project, summary, tags, created_at, embedding) VALUES (?,?,?,?,?,?)",
            (session_id, project, summary, ",".join(tags or []), time.time(), emb),
        )
        self.conn.commit()

    def recall(self, query: str, project: str, top_k: int = 3) -> list[dict]:
        """Retrieve most relevant past sessions for current task."""
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE project=? ORDER BY created_at DESC LIMIT 50",
            (project,),
        ).fetchall()

        if not rows:
            return []

        corpus = []
        for r in rows:
            if r["embedding"]:
                arr = np.frombuffer(r["embedding"], dtype=np.float32).copy()
                corpus.append((r["id"], arr))

        if not corpus:
            return [dict(r) for r in rows[:top_k]]

        query_vec = embed_text(query)
        top = top_k_similar(query_vec, corpus, k=top_k)
        top_ids = {nid for nid, _ in top}

        return [dict(r) for r in rows if r["id"] in top_ids]

    def list_sessions(self, project: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, project, summary, tags, created_at FROM sessions WHERE project=? ORDER BY created_at DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def format_for_injection(self, sessions: list[dict]) -> str:
        """Serialize past session deltas for LLM context. ~500 tokens max."""
        if not sessions:
            return ""
        parts = ["# ASTra prior session memory"]
        for s in sessions:
            ts = time.strftime("%Y-%m-%d", time.localtime(s["created_at"]))
            parts.append(f"\n## Session {s['id'][:8]} ({ts})\n{s['summary']}")
        return "\n".join(parts)

    def close(self):
        self.conn.close()
