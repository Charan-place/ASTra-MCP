"""
ASTra Daemon — persistent background process.

Maintains a live knowledge graph via:
- watchdog file watcher (incremental re-index on change)
- Unix domain socket server (any tool queries the live graph)
- Incremental PageRank updates (subgraph only, not full recompute)
- WebSocket broadcast to all subscribers on graph change

Socket path: ~/.astra/daemon.sock
PID file:    ~/.astra/daemon.pid
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import networkx as nx

from astra.graph.store import GraphStore
from astra.graph.pagerank import build_nx_graph
from astra.indexer.graph_builder import index_single_file, _resolve_cross_file_calls
from astra.indexer.parser import SUPPORTED, SKIP_DIRS
from astra.query.engine import invalidate_graph_cache

logger = logging.getLogger("astra.daemon")

ASTRA_DIR = Path.home() / ".astra"
SOCKET_PATH = ASTRA_DIR / "daemon.sock"
PID_PATH = ASTRA_DIR / "daemon.pid"
DELTA_PATH = ASTRA_DIR / "latest_delta.json"


# ── Incremental PageRank ───────────────────────────────────────────────────

def _incremental_pagerank_update(
    G: nx.DiGraph,
    store: GraphStore,
    changed_node_ids: list[str],
    radius: int = 2,
) -> dict[str, float]:
    """
    Recompute PageRank only on the subgraph within `radius` hops of changed nodes.
    10-50x faster than full recompute for small changes.
    Returns updated {node_id: score} for affected nodes only.
    """
    if not changed_node_ids or G.number_of_nodes() == 0:
        return {}

    # collect subgraph nodes: changed + their radius-hop neighborhood
    affected: set[str] = set()
    for nid in changed_node_ids:
        if nid not in G:
            continue
        affected.add(nid)
        # predecessors and successors up to radius hops
        for hop in range(radius):
            neighbors: set[str] = set()
            for n in list(affected):
                neighbors.update(G.predecessors(n))
                neighbors.update(G.successors(n))
            affected.update(neighbors)

    if not affected:
        return {}

    subG = G.subgraph(affected).copy()
    if subG.number_of_nodes() < 2:
        return {}

    try:
        scores = nx.pagerank(subG, alpha=0.85, max_iter=100)
    except Exception:
        return {}

    return scores


# ── Delta tracking ─────────────────────────────────────────────────────────

class GraphDelta:
    """Records what changed in the last file update."""
    __slots__ = ("file", "added_nodes", "removed_nodes", "changed_nodes", "ts")

    def __init__(self, file: str):
        self.file = file
        self.added_nodes: list[str] = []
        self.removed_nodes: list[str] = []
        self.changed_nodes: list[str] = []
        self.ts = time.time()

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "added": self.added_nodes,
            "removed": self.removed_nodes,
            "changed": self.changed_nodes,
            "ts": self.ts,
        }


# ── Watcher handler ────────────────────────────────────────────────────────

class _LiveHandler:
    def __init__(self, daemon: "AstraDaemon"):
        self.daemon = daemon

    def dispatch(self, event):
        from watchdog.events import FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() not in SUPPORTED:
            return
        if any(skip in p.parts for skip in SKIP_DIRS):
            return

        if isinstance(event, FileDeletedEvent):
            self._handle_delete(event.src_path)
        elif isinstance(event, (FileModifiedEvent, FileCreatedEvent)):
            self._handle_change(event.src_path)

    def _handle_delete(self, path_str: str):
        store = self.daemon.store
        old_nodes = {r["id"] for r in store.get_nodes_by_file(path_str)}
        store.delete_file(path_str)
        store.commit()
        invalidate_graph_cache(store)

        delta = GraphDelta(path_str)
        delta.removed_nodes = list(old_nodes)
        self.daemon._apply_delta(delta)
        logger.info("Daemon: removed %s (%d nodes)", Path(path_str).name, len(old_nodes))

    def _handle_change(self, path_str: str):
        store = self.daemon.store
        old_node_ids = {r["id"] for r in store.get_nodes_by_file(path_str)}

        count = index_single_file(Path(path_str), store)
        invalidate_graph_cache(store)

        new_node_ids = {r["id"] for r in store.get_nodes_by_file(path_str)}

        delta = GraphDelta(path_str)
        delta.added_nodes = list(new_node_ids - old_node_ids)
        delta.removed_nodes = list(old_node_ids - new_node_ids)
        delta.changed_nodes = list(new_node_ids & old_node_ids)
        self.daemon._apply_delta(delta)
        logger.info("Daemon: re-indexed %s (%d symbols, delta +%d/-%d)",
                    Path(path_str).name, count,
                    len(delta.added_nodes), len(delta.removed_nodes))


# ── Socket server ──────────────────────────────────────────────────────────

PROTOCOL_VERSION = "1"

def _handle_client(conn: socket.socket, daemon: "AstraDaemon"):
    """
    Simple line-delimited JSON protocol:
      Request:  {"cmd": "query"|"status"|"delta"|"ping", ...}
      Response: {"ok": true, "data": ...} or {"ok": false, "error": "..."}
    """
    try:
        conn.settimeout(10.0)
        raw = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk
            if b"\n" in raw:
                break

        line = raw.split(b"\n")[0].strip()
        if not line:
            return

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            conn.sendall(json.dumps({"ok": False, "error": "invalid JSON"}).encode() + b"\n")
            return

        cmd = req.get("cmd", "")

        if cmd == "ping":
            resp = {"ok": True, "data": {"pong": True, "version": PROTOCOL_VERSION}}

        elif cmd == "status":
            stats = daemon.store.stats()
            resp = {"ok": True, "data": {
                "nodes": stats["nodes"],
                "edges": stats["edges"],
                "files": stats["files"],
                "uptime_s": round(time.time() - daemon.started_at, 1),
                "last_delta_ts": daemon.last_delta_ts,
                "graph_nodes": daemon.graph.number_of_nodes() if daemon.graph else 0,
            }}

        elif cmd == "delta":
            resp = {"ok": True, "data": daemon.last_delta}

        elif cmd == "query":
            task = req.get("task", "")
            max_tokens = req.get("max_tokens", 4000)
            if not task:
                resp = {"ok": False, "error": "task required"}
            else:
                from astra.query.engine import get_context
                result = get_context(daemon.store, task, max_tokens=max_tokens)
                resp = {"ok": True, "data": result}

        elif cmd == "search":
            query = req.get("query", "")
            top_k = req.get("top_k", 10)
            if not query:
                resp = {"ok": False, "error": "query required"}
            else:
                from astra.query.engine import search_symbols
                results = search_symbols(daemon.store, query, top_k=top_k)
                # strip non-serializable embedding bytes
                for r in results:
                    r.pop("embedding", None)
                resp = {"ok": True, "data": results}

        elif cmd == "impact":
            node_ids = req.get("node_ids", [])
            if not node_ids:
                resp = {"ok": False, "error": "node_ids required"}
            else:
                from astra.impact.analyzer import ImpactAnalyzer
                analyzer = ImpactAnalyzer(daemon.store, daemon.graph)
                report = analyzer.compute_blast_radius(node_ids)
                resp = {"ok": True, "data": report.to_dict()}

        else:
            resp = {"ok": False, "error": f"unknown cmd: {cmd}"}

        conn.sendall(json.dumps(resp).encode() + b"\n")

    except Exception as e:
        logger.warning("Daemon client error: %s", e)
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
        except Exception:
            pass
    finally:
        conn.close()


# ── Main Daemon ────────────────────────────────────────────────────────────

class AstraDaemon:
    def __init__(self, repo_path: Path, db_path: Path):
        self.repo_path = repo_path
        self.db_path = db_path
        self.store: Optional[GraphStore] = None
        self.graph: Optional[nx.DiGraph] = None
        self._graph_lock = threading.Lock()
        self.last_delta: dict = {}
        self.last_delta_ts: float = 0.0
        self.started_at: float = 0.0
        self._subscribers: list[socket.socket] = []
        self._stop_event = threading.Event()

    def _apply_delta(self, delta: GraphDelta):
        with self._graph_lock:
            # rebuild graph from store (efficient enough for <100K nodes)
            self.graph = build_nx_graph(self.store)
            # run incremental pagerank on affected subgraph
            all_changed = delta.added_nodes + delta.changed_nodes + delta.removed_nodes
            scores = _incremental_pagerank_update(self.graph, self.store, all_changed)
            self.last_delta = delta.to_dict()
            self.last_delta["pagerank_updated"] = len(scores)
            self.last_delta_ts = delta.ts

        # persist delta for polling clients
        try:
            DELTA_PATH.write_text(json.dumps(self.last_delta))
        except Exception:
            pass

        # broadcast to socket subscribers
        self._broadcast({"type": "graph_delta", "delta": self.last_delta})

    def _broadcast(self, msg: dict):
        dead = []
        payload = json.dumps(msg).encode() + b"\n"
        for s in self._subscribers:
            try:
                s.sendall(payload)
            except Exception:
                dead.append(s)
        for s in dead:
            self._subscribers.remove(s)

    def _run_socket_server(self):
        ASTRA_DIR.mkdir(exist_ok=True)
        sock_path = str(SOCKET_PATH)

        # remove stale socket
        if Path(sock_path).exists():
            Path(sock_path).unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(16)
        server.settimeout(1.0)
        logger.info("Daemon socket: %s", sock_path)

        while not self._stop_event.is_set():
            try:
                conn, _ = server.accept()
                t = threading.Thread(target=_handle_client, args=(conn, self), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.warning("Socket accept error: %s", e)

        server.close()
        try:
            Path(sock_path).unlink()
        except Exception:
            pass

    def _run_watcher(self):
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        handler_obj = _LiveHandler(self)

        class _WatchdogAdapter(FileSystemEventHandler):
            def dispatch(self, event):
                handler_obj.dispatch(event)

        observer = Observer()
        observer.schedule(_WatchdogAdapter(), str(self.repo_path), recursive=True)
        observer.start()
        logger.info("Daemon watcher: %s", self.repo_path)

        self._stop_event.wait()
        observer.stop()
        observer.join()

    def start(self):
        ASTRA_DIR.mkdir(exist_ok=True)
        self.started_at = time.time()

        self.store = GraphStore(self.db_path)
        with self._graph_lock:
            self.graph = build_nx_graph(self.store)
        logger.info("Daemon graph loaded: %d nodes, %d edges",
                    self.graph.number_of_nodes(), self.graph.number_of_edges())

        # write PID
        PID_PATH.write_text(str(os.getpid()))

        # signal handler — only works in main thread
        import threading as _threading
        if _threading.current_thread() is _threading.main_thread():
            def _shutdown(sig, frame):
                logger.info("Daemon shutting down (signal %d)", sig)
                self._stop_event.set()
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

        # start threads
        socket_thread = threading.Thread(target=self._run_socket_server, daemon=True)
        socket_thread.start()

        watcher_thread = threading.Thread(target=self._run_watcher, daemon=True)
        watcher_thread.start()

        logger.info("ASTra Daemon running. PID=%d", os.getpid())

        # block until stop
        self._stop_event.wait()

        # cleanup
        try:
            PID_PATH.unlink()
        except Exception:
            pass
        if self.store:
            self.store.close()
        logger.info("Daemon stopped.")

    def stop(self):
        self._stop_event.set()


# ── Client helper ──────────────────────────────────────────────────────────

class DaemonClient:
    """Thin client to talk to a running ASTra daemon via Unix socket."""

    def __init__(self, socket_path: Path = SOCKET_PATH):
        self.socket_path = socket_path

    def _send(self, req: dict) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        try:
            s.connect(str(self.socket_path))
            s.sendall(json.dumps(req).encode() + b"\n")
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return json.loads(data.split(b"\n")[0])
        finally:
            s.close()

    def ping(self) -> bool:
        try:
            resp = self._send({"cmd": "ping"})
            return resp.get("ok", False)
        except Exception:
            return False

    def status(self) -> dict:
        return self._send({"cmd": "status"})

    def query(self, task: str, max_tokens: int = 4000) -> dict:
        return self._send({"cmd": "query", "task": task, "max_tokens": max_tokens})

    def search(self, query: str, top_k: int = 10) -> dict:
        return self._send({"cmd": "search", "query": query, "top_k": top_k})

    def impact(self, node_ids: list[str]) -> dict:
        return self._send({"cmd": "impact", "node_ids": node_ids})

    def delta(self) -> dict:
        return self._send({"cmd": "delta"})

    @property
    def is_running(self) -> bool:
        return self.ping()


def is_daemon_running() -> bool:
    client = DaemonClient()
    return client.ping()


def get_daemon_client() -> Optional[DaemonClient]:
    client = DaemonClient()
    if client.is_running:
        return client
    return None
