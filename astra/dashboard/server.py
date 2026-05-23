"""
ASTra Dashboard — FastAPI server.
Serves metrics API + SSE stream + static HTML.
Run: astra dashboard
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from astra.graph.store import GraphStore
from astra.query.engine import get_context, search_symbols
from astra.indexer.parser import iter_source_files

app = FastAPI(title="ASTra Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── State ──────────────────────────────────────────────────────────────────

_state = {
    "queries": [],          # list of {task, naive_tokens, astra_tokens, latency_ms, ts}
    "total_saved": 0,
    "total_naive": 0,
    "total_astra": 0,
    "store": None,
    "project_root": None,
}


def _get_store() -> GraphStore:
    if _state["store"] is None:
        data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
        _state["store"] = GraphStore(data_dir / "graph.db")
    return _state["store"]


_naive_cache: dict = {"count": 0, "root": None, "ts": 0.0}
_NAIVE_TTL_S = 60


def _naive_token_count(root: Path) -> int:
    now = time.time()
    if _naive_cache["root"] == str(root) and now - _naive_cache["ts"] < _NAIVE_TTL_S:
        return _naive_cache["count"]
    total = 0
    for path in iter_source_files(root):
        try:
            total += len(path.read_text(errors="replace")) // 4
        except Exception:
            pass
    _naive_cache["count"] = total
    _naive_cache["root"] = str(root)
    _naive_cache["ts"] = now
    return total


# ── API ────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    store = _get_store()
    graph_stats = store.stats()
    root = Path(os.environ.get("ASTRA_PROJECT", "."))
    # Run file scan in thread pool so we don't block the event loop
    loop = asyncio.get_event_loop()
    naive = await loop.run_in_executor(None, _naive_token_count, root)
    return {
        "graph": graph_stats,
        "naive_tokens": naive,
        "queries": _state["queries"][-20:],
        "total_saved": _state["total_saved"],
        "total_naive": _state["total_naive"],
        "total_astra": _state["total_astra"],
    }


@app.post("/api/query")
async def api_query(body: dict):
    task = body.get("task", "")
    max_tokens = body.get("max_tokens", 4000)
    if not task:
        return {"error": "task required"}

    store = _get_store()
    root = Path(os.environ.get("ASTRA_PROJECT", "."))
    loop = asyncio.get_event_loop()
    naive = await loop.run_in_executor(None, _naive_token_count, root)

    t0 = time.perf_counter()
    result = get_context(store, task, max_tokens=max_tokens)
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    entry = {
        "task": task[:80],
        "naive_tokens": naive,
        "astra_tokens": result["tokens"],
        "reduction_pct": round((1 - result["tokens"] / max(naive, 1)) * 100, 1),
        "latency_ms": latency_ms,
        "symbols": result["nodes"],
        "ts": time.time(),
    }
    _state["queries"].append(entry)
    _state["total_saved"] += naive - result["tokens"]
    _state["total_naive"] += naive
    _state["total_astra"] += result["tokens"]

    return {**entry, "context": result["context"]}


@app.get("/api/search")
def api_search(q: str = "", k: int = 10):
    if not q:
        return []
    store = _get_store()
    return search_symbols(store, q, top_k=k)


@app.get("/api/stream")
async def api_stream():
    """SSE stream: push stats every 2 seconds."""
    async def generator() -> AsyncGenerator[str, None]:
        while True:
            store = _get_store()
            stats = store.stats()
            payload = json.dumps({
                "nodes": stats["nodes"],
                "edges": stats["edges"],
                "files": stats["files"],
                "total_saved": _state["total_saved"],
                "query_count": len(_state["queries"]),
                "last_query": _state["queries"][-1] if _state["queries"] else None,
            })
            yield f"data: {payload}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


def run(host: str = "127.0.0.1", port: int = 7865):
    uvicorn.run(app, host=host, port=port, log_level="error")
