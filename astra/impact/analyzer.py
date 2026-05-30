"""
Impact Analyzer — blast radius computation.

Given a set of changed node IDs, computes:
- All callers (reverse call graph traversal)
- PageRank-weighted risk score per affected node
- Untested high-risk nodes
- Summary risk score (0–100)

Used by:
- `astra impact` CLI command
- `astra daemon` via "impact" socket command
- Pre-commit hook integration
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx

from astra.graph.store import GraphStore


@dataclass
class ImpactReport:
    changed_nodes: list[str]
    affected_nodes: list[dict]     # {id, name, file, line, risk_score, has_test}
    untested_high_risk: list[dict]
    risk_score: int                # 0-100
    blast_radius: int              # total affected count
    summary: str

    def to_dict(self) -> dict:
        return {
            "changed_nodes": self.changed_nodes,
            "affected_nodes": self.affected_nodes,
            "untested_high_risk": self.untested_high_risk,
            "risk_score": self.risk_score,
            "blast_radius": self.blast_radius,
            "summary": self.summary,
        }

    def to_text(self) -> str:
        lines = [
            f"Impact Analysis",
            f"  Changed nodes  : {len(self.changed_nodes)}",
            f"  Blast radius   : {self.blast_radius} functions affected",
            f"  Risk score     : {self.risk_score}/100",
        ]
        if self.untested_high_risk:
            lines.append(f"  ⚠ Untested high-risk nodes:")
            for n in self.untested_high_risk[:5]:
                lines.append(f"    - {n['name']} ({n['file']}:{n['line']})")
        if self.affected_nodes:
            lines.append(f"\n  Top affected (by risk):")
            for n in self.affected_nodes[:8]:
                tested = "✓" if n["has_test"] else "✗"
                lines.append(f"    [{tested}] {n['name']:30} risk={n['risk_score']:.3f}  {n['file']}")
        return "\n".join(lines)


class ImpactAnalyzer:
    def __init__(self, store: GraphStore, graph: Optional[nx.DiGraph] = None):
        self.store = store
        self._graph = graph  # can be pre-built by daemon

    def _get_graph(self) -> nx.DiGraph:
        if self._graph is not None:
            return self._graph
        from astra.graph.pagerank import build_nx_graph
        return build_nx_graph(self.store)

    def compute_blast_radius(
        self,
        changed_node_ids: list[str],
        max_depth: int = 10,
    ) -> ImpactReport:
        """
        BFS over reverse call graph from changed nodes.
        Weights by personalized PageRank to surface highest-risk affected nodes.
        """
        G = self._get_graph()

        if not changed_node_ids:
            return ImpactReport(
                changed_nodes=[],
                affected_nodes=[],
                untested_high_risk=[],
                risk_score=0,
                blast_radius=0,
                summary="No changed nodes provided.",
            )

        # ── Step 1: reverse BFS to find all callers ────────────────────────
        affected: set[str] = set()
        frontier = set(changed_node_ids)

        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                if nid not in G:
                    continue
                for pred in G.predecessors(nid):
                    # only follow CALLS_REV edges (callers) or CALLS edges in reverse
                    edge_data = G.get_edge_data(pred, nid, default={})
                    rel = edge_data.get("relation", "")
                    if "CALLS" in rel or rel == "":
                        if pred not in affected and pred not in changed_node_ids:
                            next_frontier.add(pred)
            if not next_frontier:
                break
            affected.update(next_frontier)
            frontier = next_frontier

        if not affected:
            # changed nodes have no callers but they themselves are affected
            affected = set(changed_node_ids)

        # ── Step 2: Personalized PageRank from changed nodes ───────────────
        try:
            weight = 1.0 / len(changed_node_ids)
            personalization = {nid: weight for nid in changed_node_ids if nid in G}
            if personalization:
                ppr_scores = nx.pagerank(G, alpha=0.85, personalization=personalization, max_iter=100)
            else:
                ppr_scores = {}
        except Exception:
            ppr_scores = {}

        # ── Step 3: Annotate affected nodes with risk + test coverage ──────
        annotated = []
        for nid in affected:
            node = self.store.get_node(nid)
            if not node:
                continue
            risk = ppr_scores.get(nid, 0.0)
            has_test = _node_has_test_coverage(node)
            annotated.append({
                "id": nid,
                "name": node["name"],
                "file": node["file"],
                "line": node.get("line_start", 0),
                "type": node["type"],
                "risk_score": round(risk, 6),
                "has_test": has_test,
            })

        # sort by risk descending
        annotated.sort(key=lambda x: -x["risk_score"])

        # ── Step 4: Untested high-risk nodes ──────────────────────────────
        if annotated:
            threshold = annotated[0]["risk_score"] * 0.3  # top 30% of max risk
            untested_high_risk = [
                n for n in annotated
                if not n["has_test"] and n["risk_score"] >= threshold
            ]
        else:
            untested_high_risk = []

        # ── Step 5: Compute summary risk score (0-100) ─────────────────────
        risk_score = _compute_risk_score(annotated, changed_node_ids, G)

        # ── Step 6: Human-readable summary ────────────────────────────────
        summary = _build_summary(changed_node_ids, annotated, untested_high_risk, risk_score)

        return ImpactReport(
            changed_nodes=changed_node_ids,
            affected_nodes=annotated,
            untested_high_risk=untested_high_risk,
            risk_score=risk_score,
            blast_radius=len(affected),
            summary=summary,
        )

    def compute_from_diff(self, diff_text: str) -> ImpactReport:
        """
        Parse unified diff text to extract changed function names,
        find their node IDs, then compute blast radius.

        Usage: git diff HEAD | astra impact --stdin
        """
        changed_names = _extract_changed_names_from_diff(diff_text)
        node_ids = []
        for name in changed_names:
            candidates = self.store.get_nodes_by_name(name)
            node_ids.extend(c["id"] for c in candidates)
        return self.compute_blast_radius(node_ids)


# ── Helpers ────────────────────────────────────────────────────────────────

def _node_has_test_coverage(node: dict) -> bool:
    """Heuristic: node is considered tested if it's in a test file or called by a test function."""
    file_path = node.get("file", "")
    name = node.get("name", "")
    # file-level check
    p = Path(file_path)
    if "test" in p.stem.lower() or "test" in str(p.parent).lower():
        return True
    # name-level check
    if name.startswith("test_") or name.startswith("Test"):
        return True
    return False


def _compute_risk_score(
    annotated: list[dict],
    changed_node_ids: list[str],
    G: nx.DiGraph,
) -> int:
    """
    Risk score 0-100:
    - blast_radius: more affected = higher risk
    - untested_ratio: fraction of affected nodes with no test
    - centrality: how central are the changed nodes
    """
    if not annotated:
        return 0

    blast = len(annotated)
    untested = sum(1 for n in annotated if not n["has_test"])
    untested_ratio = untested / blast if blast else 0

    # centrality of changed nodes
    centrality_sum = 0.0
    for nid in changed_node_ids:
        if nid in G:
            centrality_sum += G.in_degree(nid) + G.out_degree(nid)
    avg_centrality = centrality_sum / max(len(changed_node_ids), 1)

    # normalize components to 0-1
    blast_score = min(blast / 50, 1.0)          # 50+ affected = max
    centrality_score = min(avg_centrality / 20, 1.0)  # 20+ connections = max

    raw = (0.4 * blast_score + 0.4 * untested_ratio + 0.2 * centrality_score)
    return min(round(raw * 100), 100)


def _build_summary(
    changed_node_ids: list[str],
    annotated: list[dict],
    untested_high_risk: list[dict],
    risk_score: int,
) -> str:
    level = "LOW" if risk_score < 30 else ("MEDIUM" if risk_score < 60 else "HIGH")
    parts = [
        f"Risk: {level} ({risk_score}/100).",
        f"This change touches {len(changed_node_ids)} node(s),",
        f"affecting {len(annotated)} downstream caller(s).",
    ]
    if untested_high_risk:
        parts.append(
            f"{len(untested_high_risk)} high-risk nodes have no test coverage:"
            f" {', '.join(n['name'] for n in untested_high_risk[:3])}."
        )
    return " ".join(parts)


def _extract_changed_names_from_diff(diff_text: str) -> list[str]:
    """
    Extract function/class names from unified diff `+` lines.
    Looks for `def NAME` or `class NAME` patterns.
    """
    import re
    names = []
    pattern = re.compile(r"^\+\s*(?:def|class)\s+(\w+)")
    for line in diff_text.splitlines():
        m = pattern.match(line)
        if m:
            names.append(m.group(1))
    return list(set(names))
