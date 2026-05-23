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


def tool_index_status(store: GraphStore) -> dict:
    """Graph stats: nodes, edges, files indexed. Use to verify index is fresh."""
    stats = store.stats()
    return {
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "files_indexed": stats["files"],
        "db_size_kb": round(Path(store.db_path).stat().st_size / 1024, 1) if Path(store.db_path).exists() else 0,
    }
