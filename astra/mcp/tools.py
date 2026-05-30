"""All 7 MCP tool handlers. Pure functions, no server state here."""
import logging
import time
import uuid
from pathlib import Path

from astra.graph.store import GraphStore
from astra.query.engine import get_context, search_symbols
from astra.query.serializer import build_context
from astra.memory.session import SessionMemory
from astra.dashboard.snapshot import save_snapshot

logger = logging.getLogger("astra.mcp.tools")


def tool_get_context(
    store: GraphStore,
    task: str,
    max_tokens: int = 4000,
) -> dict:
    """
    Main tool: task description → minimal relevant code context.
    This is the primary token-saving tool.
    """
    t0 = time.perf_counter()
    result = get_context(store, task, max_tokens=max_tokens)
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    snapshot_id = ""
    try:
        snapshot_id = save_snapshot(store, task, result, entry={
            "astra_tokens": result["tokens"],
            "naive_tokens": 0,
            "reduction_pct": 0,
            "latency_ms": latency_ms,
            "source": "mcp:get_context",
        })
    except Exception as e:
        logger.warning("snapshot write failed: %s", e)

    return {
        "context": result["context"],
        "token_estimate": result["tokens"],
        "symbols_included": result["nodes"],
        "snapshot": snapshot_id,
        "usage": f"Injected ~{result['tokens']} tokens (vs full codebase read)",
    }


def tool_search(
    store: GraphStore,
    query: str,
    top_k: int = 10,
) -> list[dict]:
    """Semantic symbol search across entire indexed codebase."""
    results = search_symbols(store, query, top_k=top_k)

    # Also save a snapshot for visual feedback
    try:
        node_ids = [r["id"] for r in results if "id" in r]
        if node_ids:
            save_snapshot(store, f"search: {query}", {
                "node_ids": node_ids,
                "seeds": node_ids[:3],
                "tokens": 0,
            }, entry={
                "astra_tokens": 0, "naive_tokens": 0,
                "reduction_pct": 0, "latency_ms": 0,
                "source": "mcp:search",
            })
    except Exception as e:
        logger.warning("snapshot write failed: %s", e)

    return [
        {
            "name": r["name"],
            "type": r["type"],
            "file": r["file"],
            "line": r["line_start"],
            "signature": r.get("signature", ""),
            "score": r.get("score", 0.0),
        }
        for r in results
    ]


def tool_get_callers(
    store: GraphStore,
    function_name: str,
    file: str = None,
) -> list[dict]:
    """Who calls this function. Use for impact analysis before changing a function."""
    # find the node
    candidates = store.get_nodes_by_name(function_name)
    if file:
        candidates = [c for c in candidates if file in c["file"]]

    if not candidates:
        return []

    target = candidates[0]
    callers = store.get_callers(target["id"])
    return [
        {
            "name": c["name"],
            "type": c["type"],
            "file": c["file"],
            "line": c["line_start"],
            "signature": c.get("signature", ""),
        }
        for c in callers
    ]


def tool_get_callees(
    store: GraphStore,
    function_name: str,
    file: str = None,
) -> list[dict]:
    """What does this function call. Use for understanding dependencies."""
    candidates = store.get_nodes_by_name(function_name)
    if file:
        candidates = [c for c in candidates if file in c["file"]]

    if not candidates:
        return []

    target = candidates[0]
    callees = store.get_callees(target["id"])
    return [
        {
            "name": c["name"],
            "type": c["type"],
            "file": c["file"],
            "line": c["line_start"],
            "signature": c.get("signature", ""),
        }
        for c in callees
    ]


def tool_get_file_map(
    store: GraphStore,
    file: str,
) -> str:
    """All symbols in a file, signatures only. No bodies. Use before editing a file."""
    nodes = store.get_nodes_by_file(file)
    if not nodes:
        return f"# No indexed symbols for: {file}"

    lines = [f"# Symbol map: {file}\n"]
    for n in sorted(nodes, key=lambda x: x.get("line_start", 0)):
        if n["type"] == "file":
            continue
        sig = n.get("signature") or f"{n['type']} {n['name']}"
        lines.append(f"L{n.get('line_start', '?')}  {sig}")
        if n.get("docstring"):
            doc = n["docstring"][:100]
            lines.append(f"      # {doc}")
    return "\n".join(lines)


def tool_session_memory(
    memory: SessionMemory,
    query: str,
    project: str,
) -> str:
    """Recall what was done in past sessions relevant to current task."""
    sessions = memory.recall(query, project, top_k=3)
    return memory.format_for_injection(sessions)


def tool_trace_cross_repo(
    store: GraphStore,
    function_name: str,
    fed_db_path: str = None,
) -> dict:
    """
    Trace a function call across repository boundaries.
    Requires: astra federate to have been run first.
    Returns call chain spanning multiple repos.
    """
    from pathlib import Path as P
    from astra.federation.resolver import FederatedResolver, FEDERATION_SCHEMA

    default_fed_db = P.home() / ".astra" / "federation.db"
    fed_path = P(fed_db_path) if fed_db_path else default_fed_db

    if not fed_path.exists():
        return {"error": "No federation DB found. Run: astra federate <repo1> <repo2>"}

    # find the function node
    candidates = store.get_nodes_by_name(function_name)
    if not candidates:
        return {"error": f"No indexed node: {function_name}"}

    node = candidates[0]

    # minimal resolver with just this store to do cross-repo trace
    resolver = FederatedResolver(fed_path)
    # detect which repo_id this store belongs to
    repos = resolver.fed_db.get_repos()
    repo_id = None
    for r in repos:
        if r["db_path"] == str(store.db_path):
            repo_id = r["repo_id"]
            resolver._stores[repo_id] = store
            break

    if not repo_id:
        resolver.close()
        return {"error": "This repo is not federated. Run: astra federate"}

    trace = resolver.trace_cross_repo(node["id"], repo_id)
    resolver.close()
    return {"trace": trace, "hops": len(trace)}


def tool_get_volatility(
    store: GraphStore,
    function_name: str = None,
    top_k: int = 10,
) -> dict:
    """
    Get temporal volatility data. Which functions change most often?
    If function_name given: history for that specific function.
    Otherwise: top-k most volatile functions across the codebase.
    Requires: astra timeline to have been run first.
    """
    from astra.temporal.indexer import TemporalIndexer
    indexer = TemporalIndexer(store)

    if not indexer.is_indexed():
        return {"error": "Temporal index not built. Run: astra timeline"}

    if function_name:
        # find node_id for this function name
        candidates = store.get_nodes_by_name(function_name)
        if not candidates:
            return {"error": f"No indexed node: {function_name}"}
        history = indexer.get_node_history(candidates[0]["id"])
        return history or {"error": f"No temporal data for: {function_name}"}
    else:
        nodes = indexer.get_volatile_nodes(top_k=top_k)
        return {"top_volatile": nodes, "count": len(nodes)}


def tool_semantic_audit(
    store: GraphStore,
    file: str = None,
    threshold: float = 0.35,
) -> list[dict]:
    """
    Scan for semantic drift: functions whose name/docstring doesn't match their behavior.
    Use before a major refactor to find misnamed functions.
    Returns list of drift warnings sorted by severity.
    """
    from astra.semantics.drift import SemanticDriftDetector
    detector = SemanticDriftDetector(store, threshold=threshold)
    warnings = detector.scan(file_filter=file)
    return [w.to_dict() for w in warnings]


def tool_impact_analysis(
    store: GraphStore,
    function_names: list[str],
    file: str = None,
) -> dict:
    """
    Blast radius analysis: what breaks if these functions change?
    Returns risk score, affected callers, untested high-risk nodes.
    Use before editing a critical function.
    """
    from astra.impact.analyzer import ImpactAnalyzer

    node_ids = []
    for name in function_names:
        candidates = store.get_nodes_by_name(name)
        if file:
            candidates = [c for c in candidates if file in c["file"]]
        node_ids.extend(c["id"] for c in candidates)

    if not node_ids:
        return {"error": f"No indexed nodes found for: {function_names}"}

    analyzer = ImpactAnalyzer(store)
    report = analyzer.compute_blast_radius(node_ids)
    return report.to_dict()


def tool_index_status(store: GraphStore) -> dict:
    """Graph stats: nodes, edges, files indexed. Use to verify index is fresh."""
    stats = store.stats()
    return {
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "files_indexed": stats["files"],
        "db_size_kb": round(Path(store.db_path).stat().st_size / 1024, 1) if Path(store.db_path).exists() else 0,
    }
