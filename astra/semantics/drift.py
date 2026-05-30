"""
Semantic Drift Detector.

Detects functions where the name/docstring (declared intent) diverges
from the actual behavior (what it calls + what calls it).

Uses the existing all-MiniLM-L6-v2 embeddings — no new ML model needed.

Algorithm:
1. declared_vec  = embed(function_name + " " + docstring)
2. behavioral_vec = aggregate embeddings of all callees (mean pool)
3. drift_score   = 1 - cosine_similarity(declared_vec, behavioral_vec)
4. flag if drift_score > threshold (default 0.35)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from astra.graph.store import GraphStore
from astra.indexer.embedder import embed_text, cosine_similarity

DRIFT_THRESHOLD = 0.35   # cosine distance above this = likely drift
MIN_CALLEES = 2          # skip functions with fewer than this many callees (insufficient signal)


@dataclass
class DriftWarning:
    node_id: str
    name: str
    file: str
    line: int
    declared_intent: str       # name + docstring
    actual_callees: list[str]  # names of what it calls
    drift_score: float         # 0=no drift, 1=complete divergence
    explanation: str

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "declared_intent": self.declared_intent,
            "actual_callees": self.actual_callees,
            "drift_score": round(self.drift_score, 4),
            "explanation": self.explanation,
        }


class SemanticDriftDetector:
    def __init__(self, store: GraphStore, threshold: float = DRIFT_THRESHOLD):
        self.store = store
        self.threshold = threshold
        self._embedding_cache: dict[str, np.ndarray] = {}

    def scan(self, file_filter: Optional[str] = None) -> list[DriftWarning]:
        """
        Scan all (or filtered) functions for semantic drift.
        Returns warnings sorted by drift_score descending.
        """
        all_nodes = self._get_function_nodes(file_filter)
        warnings = []

        for node in all_nodes:
            warning = self._check_node(node)
            if warning is not None:
                warnings.append(warning)

        warnings.sort(key=lambda w: -w.drift_score)
        return warnings

    def check_node(self, node_id: str) -> Optional[DriftWarning]:
        """Check a single node for drift."""
        node = self.store.get_node(node_id)
        if not node:
            return None
        return self._check_node(node)

    def _check_node(self, node: dict) -> Optional[DriftWarning]:
        # only check functions and methods
        if node["type"] not in ("function", "method"):
            return None

        # get callees
        callees = self.store.get_callees(node["id"])
        if len(callees) < MIN_CALLEES:
            return None  # not enough signal

        # declared intent: name + docstring
        docstring = node.get("docstring") or ""
        declared_text = f"{node['name']} {docstring}".strip()
        declared_vec = self._get_or_embed(f"declared:{node['id']}", declared_text)

        # behavioral signal: mean of callee embeddings
        behavioral_vec = self._get_behavioral_vec(callees)
        if behavioral_vec is None:
            return None

        # cosine distance (1 - similarity)
        similarity = cosine_similarity(declared_vec, behavioral_vec)
        drift_score = 1.0 - float(similarity)

        if drift_score < self.threshold:
            return None

        callee_names = [c["name"] for c in callees]
        explanation = _build_explanation(node["name"], docstring, callee_names, drift_score)

        return DriftWarning(
            node_id=node["id"],
            name=node["name"],
            file=node["file"],
            line=node.get("line_start", 0),
            declared_intent=declared_text,
            actual_callees=callee_names,
            drift_score=drift_score,
            explanation=explanation,
        )

    def _get_behavioral_vec(self, callees: list[dict]) -> Optional[np.ndarray]:
        """Mean-pool callee embeddings. Returns None if no embeddings available."""
        vecs = []
        for c in callees:
            # try to get stored embedding from DB
            emb = self._get_stored_embedding(c["id"])
            if emb is not None:
                vecs.append(emb)
            else:
                # fall back to embedding the name
                name_vec = self._get_or_embed(f"name:{c['id']}", c["name"])
                vecs.append(name_vec)

        if not vecs:
            return None

        mean_vec = np.mean(np.stack(vecs), axis=0)
        # re-normalize after mean pooling
        norm = np.linalg.norm(mean_vec)
        if norm > 0:
            mean_vec = mean_vec / norm
        return mean_vec.astype(np.float32)

    def _get_stored_embedding(self, node_id: str) -> Optional[np.ndarray]:
        row = self.store.conn.execute(
            "SELECT embedding FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        if row and row["embedding"]:
            return np.frombuffer(row["embedding"], dtype=np.float32).copy()
        return None

    def _get_or_embed(self, key: str, text: str) -> np.ndarray:
        if key not in self._embedding_cache:
            self._embedding_cache[key] = embed_text(text)
        return self._embedding_cache[key]

    def _get_function_nodes(self, file_filter: Optional[str]) -> list[dict]:
        if file_filter:
            nodes = self.store.get_nodes_by_file(file_filter)
        else:
            rows = self.store.conn.execute(
                "SELECT * FROM nodes WHERE type IN ('function', 'method')"
            ).fetchall()
            nodes = [dict(r) for r in rows]
        return [n for n in nodes if n["type"] in ("function", "method")]


# ── Helpers ────────────────────────────────────────────────────────────────

def _build_explanation(
    name: str,
    docstring: str,
    callee_names: list[str],
    drift_score: float,
) -> str:
    severity = "mild" if drift_score < 0.5 else ("moderate" if drift_score < 0.7 else "severe")
    callee_str = ", ".join(callee_names[:5])
    if len(callee_names) > 5:
        callee_str += f" +{len(callee_names)-5} more"

    parts = [f"Semantic drift ({severity}, score={drift_score:.2f})."]
    if docstring:
        parts.append(f'"{name}" declares: "{docstring[:80]}"')
    else:
        parts.append(f'"{name}" has no docstring.')
    parts.append(f"But calls: {callee_str}.")
    parts.append("Name/docstring may not reflect actual responsibilities.")
    return " ".join(parts)
