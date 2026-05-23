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
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
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
    # Save snapshot of subgraph used for this query
    snapshot_id = _save_query_snapshot(store, task, result, entry)
    entry["snapshot"] = snapshot_id

    _state["queries"].append(entry)
    _state["total_saved"] += naive - result["tokens"]
    _state["total_naive"] += naive
    _state["total_astra"] += result["tokens"]

    return {**entry, "context": result["context"], "node_ids": result.get("node_ids", [])}


def _save_query_snapshot(store: GraphStore, task: str, result: dict, entry: dict) -> str:
    """Save standalone HTML graph snapshot to .astra/graphs/."""
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    graphs_dir = data_dir / "graphs"
    graphs_dir.mkdir(parents=True, exist_ok=True)

    node_ids = result.get("node_ids", [])
    seed_ids = set(result.get("seeds", []))
    if not node_ids:
        return ""

    # Fetch the subgraph nodes + their edges
    with store._lock:
        placeholders = ",".join("?" * len(node_ids))
        node_rows = store.conn.execute(
            f"SELECT id, name, type, file, signature, line_start FROM nodes WHERE id IN ({placeholders})",
            node_ids,
        ).fetchall()
        edge_rows = store.conn.execute(
            f"SELECT src, dst, relation FROM edges WHERE src IN ({placeholders}) AND dst IN ({placeholders})",
            node_ids + node_ids,
        ).fetchall()

    id_set = {r["id"] for r in node_rows}
    nodes = [
        {
            "id": r["id"], "name": r["name"], "type": r["type"],
            "file": r["file"], "signature": r["signature"] or "",
            "line": r["line_start"],
            "seed": r["id"] in seed_ids,
        } for r in node_rows
    ]
    edges = [
        {"src": r["src"], "dst": r["dst"], "relation": r["relation"]}
        for r in edge_rows if r["src"] in id_set and r["dst"] in id_set
    ]

    ts = int(time.time())
    safe_task = "".join(c if c.isalnum() or c in "-_ " else "_" for c in task[:40]).strip().replace(" ", "_") or "query"
    snapshot_id = f"{ts}_{safe_task}"
    html_path = graphs_dir / f"{snapshot_id}.html"
    html_path.write_text(_render_snapshot_html(task, nodes, edges, entry))
    return snapshot_id


def _render_snapshot_html(task: str, nodes: list, edges: list, entry: dict) -> str:
    """Render standalone HTML graph viewer. D3 inlined via CDN with local fallback path."""
    import html as _html
    payload = json.dumps({"nodes": nodes, "edges": edges, "task": task, "meta": entry})
    safe_task = _html.escape(task)
    return _SNAPSHOT_TEMPLATE.replace("__TASK__", safe_task).replace("__PAYLOAD__", payload)


_SNAPSHOT_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>ASTra Graph — __TASK__</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
body{margin:0;background:#0d0d0f;color:#e2e8f0;font-family:'SF Mono',monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.hdr{padding:14px 22px;border-bottom:1px solid #2a2a35;background:#16161a;display:flex;justify-content:space-between;align-items:center}
.hdr h1{font-size:14px;font-weight:700;margin:0}
.hdr .meta{font-size:11px;color:#64748b}
.body{flex:1;display:flex}
.side{width:300px;background:#16161a;border-right:1px solid #2a2a35;padding:16px;overflow-y:auto;font-size:12px}
.side h3{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin:0 0 8px}
.stat{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #2a2a35;font-size:11px}
.stat b{color:#5eead4}
#canvas{flex:1;position:relative;background:#0d0d0f}
svg{width:100%;height:100%}
.nd{font-size:9px;fill:#94a3b8;pointer-events:none}
.legend{display:flex;gap:10px;margin:12px 0;font-size:10px}
.legend i{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px;vertical-align:middle}
.detail{margin-top:14px;padding:10px;background:#1e1e24;border-radius:6px;font-size:11px;min-height:80px}
.detail .n{color:#5eead4;font-weight:700}
.detail .s{color:#94a3b8;margin-top:4px;font-size:10px;white-space:pre-wrap;word-break:break-all}
.tip{position:absolute;background:#16161a;border:1px solid #2a2a35;padding:6px 9px;border-radius:5px;font-size:10px;pointer-events:none;opacity:0}
</style></head>
<body>
<div class="hdr">
  <h1>🕸 ASTra Query Graph — <span style="color:#5eead4">__TASK__</span></h1>
  <div class="meta">Generated by ASTra MCP</div>
</div>
<div class="body">
  <div class="side">
    <h3>Query Stats</h3>
    <div id="stats"></div>
    <div class="legend">
      <span><i style="background:#7c6af7"></i>seed</span>
      <span><i style="background:#eab308"></i>file</span>
      <span><i style="background:#f97316"></i>class</span>
      <span><i style="background:#5eead4"></i>function</span>
    </div>
    <h3 style="margin-top:14px">Node Detail</h3>
    <div class="detail" id="detail">Click a node…</div>
  </div>
  <div id="canvas"><svg></svg><div class="tip" id="tip"></div></div>
</div>
<script>
const D=__PAYLOAD__;
const m=D.meta||{};
document.getElementById('stats').innerHTML=`
  <div class="stat"><span>Task</span><b>${(D.task||'').slice(0,40)}</b></div>
  <div class="stat"><span>Nodes used</span><b>${D.nodes.length}</b></div>
  <div class="stat"><span>Edges</span><b>${D.edges.length}</b></div>
  <div class="stat"><span>Tokens (ASTra)</span><b>${m.astra_tokens||'—'}</b></div>
  <div class="stat"><span>Tokens (naive)</span><b>${m.naive_tokens||'—'}</b></div>
  <div class="stat"><span>Reduction</span><b>${m.reduction_pct||0}%</b></div>
  <div class="stat"><span>Latency</span><b>${m.latency_ms||0} ms</b></div>`;
const COLOR={file:'#eab308',class:'#f97316',function:'#5eead4'};
const W=window.innerWidth-300,H=window.innerHeight-60;
const svg=d3.select('svg').attr('viewBox',`0 0 ${W} ${H}`);
const g=svg.append('g');
svg.call(d3.zoom().scaleExtent([0.1,4]).on('zoom',e=>g.attr('transform',e.transform)));
const edges=D.edges.map(e=>({source:e.src,target:e.dst}));
const sim=d3.forceSimulation(D.nodes)
  .force('link',d3.forceLink(edges).id(d=>d.id).distance(70))
  .force('charge',d3.forceManyBody().strength(-120))
  .force('center',d3.forceCenter(W/2,H/2))
  .force('coll',d3.forceCollide().radius(10));
const link=g.append('g').selectAll('line').data(edges).join('line')
  .attr('stroke','#2a2a3a').attr('stroke-width',1.2);
const node=g.append('g').selectAll('g').data(D.nodes).join('g').style('cursor','pointer');
node.append('circle')
  .attr('r',d=>d.seed?9:(d.type==='file'?7:5))
  .attr('fill',d=>d.seed?'#7c6af7':(COLOR[d.type]||'#7c6af7'))
  .attr('stroke',d=>d.seed?'#a78bfa':'#1e1e24')
  .attr('stroke-width',d=>d.seed?2:1);
node.append('text').attr('class','nd').attr('dx',10).attr('dy',3).text(d=>d.name);
const tip=document.getElementById('tip');
node.on('mouseover',(e,d)=>{tip.style.opacity=1;tip.innerHTML=`<b>${d.name}</b> · ${d.type}<br>${d.file.split('/').slice(-1)[0]}:${d.line}`;});
node.on('mousemove',e=>{tip.style.left=(e.clientX-300+12)+'px';tip.style.top=(e.clientY-60+12)+'px';});
node.on('mouseout',()=>tip.style.opacity=0);
node.on('click',(e,d)=>{
  document.getElementById('detail').innerHTML=`<div class="n">${d.name}</div><div class="s">${d.signature}</div><div class="s" style="color:#64748b">📄 ${d.file.split('/').slice(-2).join('/')}:${d.line}</div>`;
});
node.call(d3.drag().on('start',(e,d)=>{if(!e.active)sim.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y;}).on('drag',(e,d)=>{d.fx=e.x;d.fy=e.y;}).on('end',(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));
sim.on('tick',()=>{link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);node.attr('transform',d=>`translate(${d.x},${d.y})`);});
</script></body></html>"""


@app.get("/api/graphs")
def api_graphs():
    """List history ring buffer + current."""
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    graphs_dir = data_dir / "graphs"
    if not graphs_dir.exists():
        return []
    items = []
    current = graphs_dir / "current.html"
    if current.exists():
        latest = _read_latest_snapshot() or {}
        items.append({
            "id": "current",
            "task": latest.get("task", "current"),
            "ts": latest.get("ts", int(current.stat().st_mtime)),
            "is_current": True,
        })
    hist_dir = graphs_dir / "history"
    if hist_dir.exists():
        for f in sorted(hist_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
            ts_str, _, name = f.stem.partition("_")
            try: ts = int(ts_str)
            except ValueError: ts = 0
            items.append({"id": f.stem, "task": name.replace("_", " "), "ts": ts, "is_current": False})
    return items


@app.get("/graphs/current", response_class=HTMLResponse)
def serve_current():
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    f = data_dir / "graphs" / "current.html"
    if not f.exists():
        return HTMLResponse("<h1>No graph yet. Run a query.</h1>", status_code=404)
    return HTMLResponse(
        f.read_text(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/graphs/{snapshot_id}", response_class=HTMLResponse)
def serve_snapshot(snapshot_id: str):
    """Serve historical snapshot from history ring."""
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    f = data_dir / "graphs" / "history" / f"{snapshot_id}.html"
    if not f.exists():
        # Back-compat: try flat path
        flat = data_dir / "graphs" / f"{snapshot_id}.html"
        if flat.exists():
            return HTMLResponse(flat.read_text())
        return HTMLResponse("<h1>Snapshot not found</h1>", status_code=404)
    return HTMLResponse(f.read_text())


@app.get("/api/graph")
def api_graph(limit: int = 400, file: str = ""):
    """Return nodes + edges for graph visualization."""
    store = _get_store()
    with store._lock:
        if file:
            node_rows = store.conn.execute(
                "SELECT id, name, type, file, signature, line_start FROM nodes WHERE file LIKE ? LIMIT ?",
                (f"%{file}%", limit),
            ).fetchall()
        else:
            node_rows = store.conn.execute(
                "SELECT id, name, type, file, signature, line_start FROM nodes LIMIT ?",
                (limit,),
            ).fetchall()
        node_ids = {r["id"] for r in node_rows}
        edge_rows = store.conn.execute("SELECT src, dst, relation FROM edges").fetchall()

    nodes = [
        {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "file": r["file"],
            "signature": r["signature"] or "",
            "line": r["line_start"],
        }
        for r in node_rows
    ]
    edges = [
        {"src": r["src"], "dst": r["dst"], "relation": r["relation"]}
        for r in edge_rows
        if r["src"] in node_ids and r["dst"] in node_ids
    ]
    return {"nodes": nodes, "edges": edges}


@app.get("/api/graph/hierarchy")
def api_graph_hierarchy():
    """
    Returns 3-level hierarchical graph: folders, files, functions.
    Aggregates edges at each level (cross-folder, cross-file, function-level).
    Used by the multi-zoom viz.
    """
    store = _get_store()
    project_root = Path(os.environ.get("ASTRA_PROJECT", ".")).resolve()
    with store._lock:
        node_rows = store.conn.execute(
            "SELECT id, name, type, file, line_start FROM nodes"
        ).fetchall()
        edge_rows = store.conn.execute(
            "SELECT src, dst, relation FROM edges"
        ).fetchall()

    # Build node → file / folder mapping
    def folder_of(p: str) -> str:
        try:
            rel = Path(p).resolve().relative_to(project_root)
            parts = rel.parts
            return parts[0] if len(parts) > 1 else "(root)"
        except Exception:
            return Path(p).parent.name or "(unknown)"

    node_index = {}
    folders: dict[str, dict] = {}
    files: dict[str, dict] = {}
    functions: list[dict] = []

    for r in node_rows:
        nid, name, ntype, fpath, line = r["id"], r["name"], r["type"], r["file"], r["line_start"]
        fold = folder_of(fpath)
        file_key = fpath

        if fold not in folders:
            folders[fold] = {"id": f"folder:{fold}", "name": fold, "type": "folder",
                             "files": 0, "symbols": 0}
        folders[fold]["symbols"] += 1

        if ntype == "file":
            if file_key not in files:
                files[file_key] = {
                    "id": f"file:{file_key}", "name": Path(file_key).name,
                    "type": "file", "path": file_key, "folder": fold, "symbols": 0,
                }
                folders[fold]["files"] += 1
        else:
            # ensure file bucket exists even if file-type node missing
            if file_key not in files:
                files[file_key] = {
                    "id": f"file:{file_key}", "name": Path(file_key).name,
                    "type": "file", "path": file_key, "folder": fold, "symbols": 0,
                }
                folders[fold]["files"] += 1
            files[file_key]["symbols"] += 1
            functions.append({
                "id": nid, "name": name, "type": ntype,
                "file": file_key, "line": line,
                "folder": fold,
            })

        node_index[nid] = {"file": file_key, "folder": fold, "type": ntype, "name": name}

    # Aggregate edges by level
    folder_edges: dict[tuple, int] = {}
    file_edges: dict[tuple, int] = {}
    func_edges: list[dict] = []

    for e in edge_rows:
        s, d = node_index.get(e["src"]), node_index.get(e["dst"])
        if not s or not d:
            continue
        if s["folder"] != d["folder"]:
            key = (s["folder"], d["folder"])
            folder_edges[key] = folder_edges.get(key, 0) + 1
        if s["file"] != d["file"]:
            key = (s["file"], d["file"])
            file_edges[key] = file_edges.get(key, 0) + 1
        func_edges.append({"src": e["src"], "dst": e["dst"], "relation": e["relation"]})

    return {
        "folders": list(folders.values()),
        "files": list(files.values()),
        "functions": functions,
        "folder_edges": [{"src": f"folder:{a}", "dst": f"folder:{b}", "weight": w}
                         for (a, b), w in folder_edges.items()],
        "file_edges": [{"src": f"file:{a}", "dst": f"file:{b}", "weight": w}
                       for (a, b), w in file_edges.items()],
        "func_edges": func_edges,
        "project_root": str(project_root),
    }


@app.get("/api/graph/node/{node_id:path}")
def api_graph_node(node_id: str):
    """Return full node detail + callers + callees."""
    store = _get_store()
    node = store.get_node(node_id)
    if not node:
        return {"error": "not found"}
    node.pop("embedding", None)
    callers = store.get_callers(node_id)
    callees = store.get_callees(node_id)
    for n in callers + callees:
        n.pop("embedding", None)
    return {"node": node, "callers": callers, "callees": callees}


@app.get("/api/search")
def api_search(q: str = "", k: int = 10):
    if not q:
        return []
    store = _get_store()
    return search_symbols(store, q, top_k=k)


def _read_latest_snapshot() -> dict | None:
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    f = data_dir / "graphs" / "latest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


@app.get("/api/latest_snapshot")
def api_latest_snapshot():
    return _read_latest_snapshot() or {}


@app.get("/api/query_trail")
def api_query_trail():
    """Last 5 queries with their node IDs — used to color-code multi-zoom graph."""
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    f = data_dir / "graphs" / "trail.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


@app.get("/api/stream")
async def api_stream():
    """SSE stream: push stats every 2 seconds. Includes latest snapshot pointer."""
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
                "latest_snapshot": _read_latest_snapshot(),
            })
            yield f"data: {payload}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/d3.min.js")
def serve_d3():
    return FileResponse(Path(__file__).parent / "d3.min.js", media_type="application/javascript")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


def run(host: str = "127.0.0.1", port: int = 7865):
    uvicorn.run(app, host=host, port=port, log_level="error")
