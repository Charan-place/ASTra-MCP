"""Standalone graph snapshot writer. Used by dashboard /api/query AND MCP tools."""
import html as _html
import json
import os
import time
from pathlib import Path

from astra.graph.store import GraphStore


def save_snapshot(
    store: GraphStore,
    task: str,
    result: dict,
    entry: dict | None = None,
) -> str:
    """Write standalone HTML snapshot of subgraph used for a query. Returns snapshot id."""
    data_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    graphs_dir = data_dir / "graphs"
    graphs_dir.mkdir(parents=True, exist_ok=True)

    node_ids = result.get("node_ids") or []
    seed_ids = set(result.get("seeds") or [])
    if not node_ids:
        return ""

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

    meta = entry or {
        "astra_tokens": result.get("tokens", 0),
        "naive_tokens": 0,
        "reduction_pct": 0,
        "latency_ms": 0,
    }
    meta["ts"] = ts
    html = _render_html(task, nodes, edges, meta)

    # Single rolling file — overwritten each call. No disk bloat.
    current = graphs_dir / "current.html"
    current.write_text(html)

    # Optional history ring buffer (last N).
    history_max = int(os.environ.get("ASTRA_GRAPH_HISTORY", "10"))
    if history_max > 0:
        hist_dir = graphs_dir / "history"
        hist_dir.mkdir(exist_ok=True)
        (hist_dir / f"{snapshot_id}.html").write_text(html)
        _prune_history(hist_dir, history_max)

    # Clean up any pre-ring-buffer flat files (old format)
    for old in graphs_dir.glob("*.html"):
        if old.name != "current.html":
            try: old.unlink()
            except OSError: pass

    # Sidecar JSON pointer
    (graphs_dir / "latest.json").write_text(json.dumps({
        "id": snapshot_id,
        "task": task,
        "ts": ts,
        "nodes": len(nodes),
        "edges": len(edges),
        "source": meta.get("source", "mcp"),
    }))
    return snapshot_id


def _prune_history(hist_dir: Path, keep: int):
    files = sorted(hist_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try: old.unlink()
        except OSError: pass


def _render_html(task: str, nodes: list, edges: list, meta: dict) -> str:
    payload = json.dumps({"nodes": nodes, "edges": edges, "task": task, "meta": meta})
    safe_task = _html.escape(task)
    return _TPL.replace("__TASK__", safe_task).replace("__PAYLOAD__", payload)


_TPL = """<!DOCTYPE html>
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
  <div class="stat"><span>Source</span><b>${m.source||'mcp'}</b></div>
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

// Auto-reload when MCP writes new snapshot.
// Works for /graphs/current via HTTP. Skips file:// (no fetch possible).
(function(){
  if (location.protocol === 'file:') {
    const w = document.createElement('div');
    w.style.cssText = 'position:fixed;bottom:8px;right:8px;background:#7c6af7;color:#fff;padding:6px 10px;border-radius:5px;font-size:11px;z-index:9999';
    w.innerHTML = '⚠ Static file view — auto-refresh disabled. Open via <code>http://localhost:7865/graphs/current</code> for live updates.';
    document.body.appendChild(w);
    return;
  }
  const initialTs = (D.meta && D.meta.ts) || 0;
  const isCurrent = location.pathname.endsWith('/current') || location.pathname.endsWith('/current/');

  // SSE first (real-time). Polling fallback.
  let connected = false;
  try {
    const es = new EventSource('/api/stream');
    es.onmessage = (e) => {
      connected = true;
      const d = JSON.parse(e.data);
      const ls = d.latest_snapshot;
      if (ls && ls.ts && initialTs && ls.ts > initialTs && isCurrent) {
        es.close();
        location.reload();
      }
    };
    es.onerror = () => { es.close(); };
  } catch(e){}

  // Fallback: poll latest.json every 3s
  setInterval(async () => {
    if (!isCurrent) return;
    try {
      const r = await fetch('/api/latest_snapshot', {cache:'no-store'});
      const ls = await r.json();
      if (ls && ls.ts && initialTs && ls.ts > initialTs) location.reload();
    } catch(e){}
  }, 3000);
})();
</script></body></html>"""
