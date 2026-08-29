# ASTra Code Graph (VS Code extension)

Inline code-knowledge-graph intelligence — inline caller/callee counts,
importance, and (eventually) semantic-drift warnings — powered by
[ASTra MCP](../README.md), sourced from its local daemon/dashboard.

> Status: **scaffold**. This compiles and activates cleanly, and the
> architecture matches ASTra's real daemon/dashboard API, but several
> pieces are stubs pending backend work — see "Known gaps / TODOs" below.

## Prerequisites

This extension does **not** bundle ASTra itself — it's a client for a
locally running ASTra backend. From your project root (the codebase you
want graphed), with the `astra` Python package installed:

```bash
# one-time index + start the live daemon (watches files, keeps graph in memory)
astra daemon start

# optional but recommended: the extension's primary data source is the
# dashboard's HTTP API, not the raw daemon socket (see "Architecture" below)
astra dashboard
# → serves http://127.0.0.1:7865
```

Check `astra daemon status` if the extension's status bar item shows
"ASTra offline".

## What it does

- **Status bar item** — shows whether the daemon/dashboard is reachable.
  Never crashes the extension if ASTra isn't running; providers just
  return no data.
- **CodeLens** (`src/codeLensProvider.ts`) — above each function/class
  definition: `N callers · M callees · importance: <tier>`.
- **Hover** (`src/hoverProvider.ts`) — richer markdown popup: signature,
  docstring, full caller/callee lists, importance, and a semantic-drift
  warning section (currently always empty, see TODOs).
- **Sidebar tree view** ("ASTra" activity bar icon, `src/graphTreeView.ts`)
  — top symbols in the active file (sorted by caller count), expandable
  one level to that symbol's callees. Click any entry to jump to it.

Supported languages: Python, JavaScript, TypeScript, JSX, TSX, and (listed
for forward-compat with a parallel ASTra indexer workstream) Go, Rust,
Java — those three will simply show no data until daemon-side indexing
support for them lands.

## Architecture

See the top-of-file comment in `src/astraClient.ts` for the full writeup.
Short version:

- ASTra's daemon (`astra/daemon/core.py`) listens on a Unix domain socket
  at `~/.astra/daemon.sock` with a line-delimited JSON protocol
  (`{"cmd": "ping"|"status"|"delta"|"query"|"search"|"impact", ...}` →
  `{"ok": true, "data": ...}`). It's used here only for a fast liveness
  ping — it doesn't expose "symbols for file" or "callers/callees for
  node" endpoints.
- ASTra's dashboard (`astra/dashboard/server.py`, a FastAPI app, default
  `http://127.0.0.1:7865`) is the more complete, documented surface and
  is what this extension uses for real data:
  - `GET /api/graph?file=<path>` — symbols defined in a file
  - `GET /api/graph/node/{node_id}` — a symbol's full detail + callers + callees
  - `GET /api/search?q=<query>` — semantic symbol search
  - `POST /api/query` — token-budgeted context retrieval

Both base URLs/paths are configurable via VS Code settings:

| Setting | Default |
|---|---|
| `astra.dashboardUrl` | `http://127.0.0.1:7865` |
| `astra.daemonSocketPath` | `~/.astra/daemon.sock` |
| `astra.enableCodeLens` | `true` |

## Known gaps / TODOs

These are called out with inline `TODO` comments at their exact location
too — search the `src/` tree for `TODO`.

1. **No real PageRank exposure.** PageRank scores are computed in-memory
   inside the daemon process (`_incremental_pagerank_update` in
   `astra/daemon/core.py`) but are never persisted or returned by any
   API. `AstraClient.getImportance()` currently approximates "importance"
   from caller count instead. Fix: add the score to the
   `GET /api/graph/node/{id}` response (or a dedicated endpoint) on the
   Python side, then update `astraClient.ts`.
2. **No semantic drift endpoint.** `astra/semantics/drift.py`'s
   `SemanticDriftDetector` is never wired into the dashboard or daemon
   API surface. `AstraClient.getDriftWarning()` is a stub that always
   returns `null`, and the hover provider's drift section will simply
   never render until this exists. Fix: add e.g.
   `GET /api/drift?file=<path>` returning `DriftWarning.to_dict()`
   entries, then implement the client call.
3. **Symbol-to-line matching is regex-based, not AST-based.** The
   CodeLens provider (`codeLensProvider.ts`) scans source lines with
   simple per-language regexes to find candidate definition lines, then
   matches them to ASTra nodes by name (+ nearest line on ties). This is
   good enough for a scaffold but will misfire on unusual formatting,
   decorators spanning multiple lines, etc. A more robust approach would
   use `document.line_start`/`line_end` from ASTra directly to place
   lenses, without any local regex scanning.
4. **No live-update subscription.** The daemon broadcasts a
   `{"type": "graph_delta", ...}` message to subscribed sockets after
   every reindex (`AstraDaemon._broadcast`), but this client doesn't
   subscribe to it — the tree view just polls every 15s
   (`extension.ts`). Wiring up a persistent subscriber connection would
   give near-instant updates on save.
5. **No `.vsix` packaging / marketplace metadata** (publisher icon,
   gallery banner, etc.) — this is a dev scaffold, not a marketplace
   submission.

## Development

```bash
npm install
npx tsc --noEmit   # typecheck
npm run compile    # emit to out/
```

Then in VS Code: `F5` (Run and Debug → "Launch Extension") to try it in
an Extension Development Host, with `astra daemon start` / `astra
dashboard` running in a terminal against some project.
