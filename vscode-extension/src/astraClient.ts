/**
 * astraClient.ts
 *
 * Thin client for talking to a locally-running ASTra MCP backend.
 *
 * ASTra exposes two local interfaces (see astra/daemon/core.py and
 * astra/dashboard/server.py in the parent repo):
 *
 *   1. Daemon Unix domain socket (~/.astra/daemon.sock) — a tiny
 *      line-delimited-JSON protocol (`{"cmd": ...}\n` -> `{"ok": ...}\n`)
 *      with commands: ping, status, delta, query, search, impact.
 *      It does NOT currently expose "symbols for file" or "callers/callees
 *      for node" — those only exist on the dashboard's HTTP API. The daemon
 *      is therefore used here only for a lightweight "is it alive" check
 *      and for the token-budgeted `query` command.
 *
 *   2. Dashboard FastAPI HTTP server (default http://127.0.0.1:7865,
 *      started via `astra dashboard`) — this is the more complete and
 *      better-documented surface, so it's what this extension uses for
 *      symbol/caller/callee lookups:
 *
 *        GET  /api/stats
 *        GET  /api/graph?file=<path>&limit=<n>        -> {nodes, edges}
 *        GET  /api/graph/node/{node_id}                -> {node, callers, callees}
 *        GET  /api/search?q=<query>&k=<n>              -> [ {id, name, ...}, ... ]
 *        POST /api/query   { task, max_tokens }
 *
 * IMPORTANT / KNOWN GAP (see TODOs below):
 *   - There is currently no server-side endpoint that returns a node's raw
 *     PageRank score. PageRank is computed in-memory inside the daemon
 *     process (astra/daemon/core.py::_incremental_pagerank_update) but is
 *     never persisted to the SQLite store or exposed over HTTP/socket.
 *     As a stand-in, `getImportance()` below derives a rough importance
 *     tier from caller-count (in-degree), which is a reasonable but
 *     imperfect proxy. A future contributor should add a real
 *     `GET /api/graph/node/{id}/rank` (or include `pagerank` in the
 *     `/api/graph/node/{id}` response) on the Python side and swap the
 *     implementation here.
 *   - There is no endpoint exposing SemanticDriftDetector output
 *     (astra/semantics/drift.py). `getDriftWarning()` is a stub that
 *     always returns null until a `/api/drift?file=...` (or similar)
 *     route is added server-side.
 */

import * as net from 'net';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

export interface AstraNode {
  id: string;
  type: string;
  name: string;
  file: string;
  signature?: string;
  docstring?: string;
  line_start?: number;
  line_end?: number;
  line?: number;
}

export interface NodeDetail {
  node: AstraNode;
  callers: AstraNode[];
  callees: AstraNode[];
}

export interface DriftWarning {
  node_id: string;
  name: string;
  file: string;
  line: number;
  declared_intent: string;
  actual_callees: string[];
  drift_score: number;
  explanation: string;
}

export type ImportanceTier = 'low' | 'medium' | 'high';

const DEFAULT_SOCKET_PATH = path.join(os.homedir(), '.astra', 'daemon.sock');

function getDashboardUrl(): string {
  return vscode.workspace.getConfiguration('astra').get<string>('dashboardUrl', 'http://127.0.0.1:7865');
}

function getSocketPath(): string {
  const configured = vscode.workspace.getConfiguration('astra').get<string>('daemonSocketPath', '');
  return configured && configured.trim().length > 0 ? configured : DEFAULT_SOCKET_PATH;
}

/** Fire a single line-delimited JSON request at the daemon socket. Resolves to null on any failure. */
function daemonRequest(cmd: string, extra: Record<string, unknown> = {}, timeoutMs = 1500): Promise<any | null> {
  return new Promise((resolve) => {
    const socketPath = getSocketPath();
    const client = net.createConnection(socketPath);
    let buf = '';
    let settled = false;

    const finish = (value: any | null) => {
      if (settled) {
        return;
      }
      settled = true;
      client.destroy();
      resolve(value);
    };

    const timer = setTimeout(() => finish(null), timeoutMs);

    client.on('connect', () => {
      client.write(JSON.stringify({ cmd, ...extra }) + '\n');
    });
    client.on('data', (chunk) => {
      buf += chunk.toString('utf8');
      if (buf.includes('\n')) {
        clearTimeout(timer);
        try {
          const parsed = JSON.parse(buf.split('\n')[0]);
          finish(parsed);
        } catch {
          finish(null);
        }
      }
    });
    client.on('error', () => {
      clearTimeout(timer);
      finish(null);
    });
    client.on('close', () => {
      clearTimeout(timer);
      finish(null);
    });
  });
}

async function dashboardGet(pathAndQuery: string, timeoutMs = 2000): Promise<any | null> {
  const url = `${getDashboardUrl()}${pathAndQuery}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    // `fetch` is available globally in the Node.js runtime VS Code ships with
    // (Node 18+ / Electron). No extra HTTP dependency needed.
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) {
      return null;
    }
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export class AstraClient {
  private lastReachable: boolean | null = null;
  private readonly onReachabilityChangedEmitter = new vscode.EventEmitter<boolean>();
  /** Fires whenever daemon/dashboard reachability flips, so UI can react (status bar, tree view). */
  readonly onReachabilityChanged = this.onReachabilityChangedEmitter.event;

  /** Cheap liveness probe. Tries the daemon socket first (fast, local), falls back to the dashboard HTTP root. */
  async ping(): Promise<boolean> {
    const daemonResp = await daemonRequest('ping', {}, 800);
    let reachable = !!daemonResp?.ok;
    if (!reachable) {
      const stats = await dashboardGet('/api/stats', 1200);
      reachable = stats !== null;
    }
    if (this.lastReachable !== reachable) {
      this.lastReachable = reachable;
      this.onReachabilityChangedEmitter.fire(reachable);
    }
    return reachable;
  }

  /** Symbols (functions/classes/etc) defined in a given file, via the dashboard's /api/graph. */
  async getSymbolsForFile(filePath: string): Promise<AstraNode[]> {
    const data = await dashboardGet(`/api/graph?file=${encodeURIComponent(filePath)}&limit=500`);
    if (!data || !Array.isArray(data.nodes)) {
      return [];
    }
    return data.nodes as AstraNode[];
  }

  /** Full node detail (callers + callees) for a given node id. */
  async getNodeDetail(nodeId: string): Promise<NodeDetail | null> {
    const data = await dashboardGet(`/api/graph/node/${encodeURIComponent(nodeId)}`);
    if (!data || data.error || !data.node) {
      return null;
    }
    return data as NodeDetail;
  }

  /** Convenience: caller list only. */
  async getCallers(nodeId: string): Promise<AstraNode[]> {
    const detail = await this.getNodeDetail(nodeId);
    return detail?.callers ?? [];
  }

  /** Convenience: callee list only. */
  async getCallees(nodeId: string): Promise<AstraNode[]> {
    const detail = await this.getNodeDetail(nodeId);
    return detail?.callees ?? [];
  }

  /**
   * Importance tier, derived from caller count as a stand-in for real
   * PageRank (see module-level TODO — PageRank is not currently exposed
   * by either the daemon socket or the dashboard HTTP API).
   */
  async getImportance(nodeId: string): Promise<{ tier: ImportanceTier; callerCount: number; calleeCount: number } | null> {
    const detail = await this.getNodeDetail(nodeId);
    if (!detail) {
      return null;
    }
    const callerCount = detail.callers.length;
    const calleeCount = detail.callees.length;
    let tier: ImportanceTier = 'low';
    if (callerCount >= 8) {
      tier = 'high';
    } else if (callerCount >= 3) {
      tier = 'medium';
    }
    return { tier, callerCount, calleeCount };
  }

  /**
   * TODO: no backend endpoint exists yet for semantic drift warnings
   * (see astra/semantics/drift.py — SemanticDriftDetector.scan()). This
   * stub always resolves to null so the hover provider degrades cleanly.
   * Once a route like `GET /api/drift?file=<path>` is added server-side,
   * implement this as a dashboardGet() call and map the response onto
   * DriftWarning.
   */
  async getDriftWarning(_nodeId: string): Promise<DriftWarning | null> {
    return null;
  }

  /** Fuzzy/semantic symbol search, via dashboard /api/search (backed by search_symbols()). */
  async search(query: string, topK = 10): Promise<AstraNode[]> {
    const data = await dashboardGet(`/api/search?q=${encodeURIComponent(query)}&k=${topK}`);
    return Array.isArray(data) ? (data as AstraNode[]) : [];
  }
}
