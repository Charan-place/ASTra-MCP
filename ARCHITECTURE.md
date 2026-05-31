# ASTra MCP — Architecture Deep Dive

<div align="center">

```
╔═══════════════════════════════════════════════════════════════════════╗
║         PERMANENT STRUCTURAL MEMORY FOR AI CODING AGENTS             ║
║     AST → Knowledge Graph → PageRank → Intelligence Layers           ║
╚═══════════════════════════════════════════════════════════════════════╝
```

</div>

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Full Module Map](#2-full-module-map)
3. [Data Flow — Indexing](#3-data-flow--indexing)
4. [Data Flow — Query](#4-data-flow--query)
5. [SQLite Schemas](#5-sqlite-schemas)
6. [Core Engine Components](#6-core-engine-components)
7. [Intelligence Layer — Impact Analyzer](#7-intelligence-layer--impact-analyzer)
8. [Intelligence Layer — Semantic Drift Detector](#8-intelligence-layer--semantic-drift-detector)
9. [Intelligence Layer — Temporal Knowledge Graph](#9-intelligence-layer--temporal-knowledge-graph)
10. [Intelligence Layer — Cross-Repo Federation](#10-intelligence-layer--cross-repo-federation)
11. [Intelligence Layer — Live Daemon](#11-intelligence-layer--live-daemon)
12. [MCP Protocol Layer](#12-mcp-protocol-layer)
13. [Dashboard Layer](#13-dashboard-layer)
14. [Performance Characteristics](#14-performance-characteristics)
15. [Configuration Reference](#15-configuration-reference)
16. [Test Suite](#16-test-suite)
17. [End-to-End Trace](#17-end-to-end-trace)
18. [Bugs Fixed & Lessons Learned](#18-bugs-fixed--lessons-learned)
19. [Quick Code Pointers](#19-quick-code-pointers)

---

## 1. System Overview

ASTra is a **4-layer system**. Clients never touch raw code — they query a living knowledge graph.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 4 — CLIENTS                                                      │
│                                                                         │
│   Claude Code · Cursor · Codex · Any MCP-compatible agent              │
│   Dashboard (browser, D3 graph)                                         │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │ MCP stdio  /  HTTP+SSE  /  Unix socket
┌─────────────────────────▼───────────────────────────────────────────────┐
│  LAYER 3 — SERVERS                                                      │
│                                                                         │
│   MCP stdio server (11 tools)        Dashboard FastAPI (:7865)          │
│   Live Daemon (AF_UNIX socket)        REST + SSE + static HTML          │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │ Python imports
┌─────────────────────────▼───────────────────────────────────────────────┐
│  LAYER 2 — INTELLIGENCE LAYERS                                          │
│                                                                         │
│   Impact Analyzer   │  Semantic Drift  │  Temporal Graph  │  Federation │
│   blast radius BFS  │  cosine drift    │  git history     │  cross-repo │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────────────────┐
│  LAYER 1 — CORE ENGINE                                                  │
│                                                                         │
│   tree-sitter Parser  →  Embedder (384-dim)  →  GraphStore (SQLite)    │
│   Personalized PageRank (NetworkX)  →  Serializer (token budget)       │
│   Session Memory  ·  File Watcher (watchdog)                            │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- SQLite for graph storage — zero infrastructure, file-portable, WAL-mode concurrent reads
- all-MiniLM-L6-v2 — 384-dim, 80MB RAM, runs CPU-only, 5ms encode
- Personalized PageRank over call graph — structurally relevant neighbors, not just semantic matches
- MCP stdio — works with any agent without HTTP servers or API keys

---

## 2. Full Module Map

```
astra/
├── cli/
│   └── main.py                 # Typer CLI: all user-facing commands
├── indexer/
│   ├── parser.py               # tree-sitter AST traversal, symbol extraction
│   ├── embedder.py             # sentence-transformers wrapper + top_k_similar
│   ├── graph_builder.py        # index_codebase(): orchestrates full indexing
│   ├── monitor.py              # watchdog file watcher (incremental updates)
│   └── symbol_table.py         # Symbol dataclass with sha256-based id property
├── graph/
│   ├── store.py                # SQLite CRUD: upsert/get/delete + stats + cache
│   └── pagerank.py             # NetworkX graph build + personalized PageRank
├── query/
│   ├── engine.py               # get_context() pipeline + search_symbols()
│   └── serializer.py           # build_context(): greedy token-budget serializer
├── memory/
│   └── session.py              # session memory store + recall by embedding
├── mcp/
│   ├── server.py               # MCP stdio server, 11 tool registrations
│   └── tools.py                # 11 pure tool handler functions
├── dashboard/
│   ├── server.py               # FastAPI app: /api/* + /graphs/* + SSE
│   ├── snapshot.py             # standalone HTML snapshot writer (D3.js)
│   ├── index.html              # dashboard SPA (vanilla JS + D3)
│   └── d3.min.js               # local D3 v7 (no CDN dependency)
├── impact/
│   └── analyzer.py             # blast radius BFS + risk scoring
├── semantics/
│   └── drift.py                # cosine drift: declared intent vs behavior
├── temporal/
│   └── indexer.py              # git history replay → volatility scores
├── federation/
│   └── resolver.py             # cross-repo boundary node linking
└── daemon/
    ├── core.py                 # AstraDaemon + DaemonClient + incremental PageRank
    └── runner.py               # CLI entrypoint: python -m astra.daemon.runner

tests/
├── test_parser.py              # AST parsing, symbol extraction
├── test_embedder.py            # embedding + cosine similarity
├── test_store.py               # SQLite operations
├── test_pagerank.py            # PageRank convergence
├── test_engine.py              # full query pipeline
├── test_tools.py               # MCP tool handlers
├── test_daemon.py              # live daemon + socket protocol
├── test_impact.py              # blast radius analysis
├── test_drift.py               # semantic drift detection
├── test_temporal.py            # git history indexing
└── test_federation.py          # cross-repo federation
```

---

## 3. Data Flow — Indexing

```
  codebase root
       │
       │  astra init  (or auto-init on MCP startup if empty)
       ▼
  ┌────────────────────────────────┐
  │  iter_source_files(root)       │  walk dir tree, skip:
  │                                │  node_modules / __pycache__ / .git
  └────────────────────────────────┘  .venv / dist / build / .astra
       │
       ▼  for each .py / .js / .ts / .tsx / .jsx
  ┌────────────────────────────────┐
  │  file_hash = sha256(content)   │  compare to stored hash → skip if same
  │  → unchanged? skip            │  ~100μs per file
  └────────────────────────────────┘
       │
       ▼
  ┌────────────────────────────────┐
  │  tree-sitter parser            │  language-specific .so via tree-sitter-languages
  │  → AST root node               │  parse: ~3ms per file
  └────────────────────────────────┘
       │
       ▼  recursive node tree traversal (no Query API in tree-sitter 0.25)
  ┌────────────────────────────────┐
  │  extract symbols per language  │
  │                                │
  │  Python: function_definition   │  → child_by_field_name("name")
  │          class_definition      │  → child_by_field_name("parameters")
  │          decorated_definition  │  → first string_literal in body = docstring
  │                                │
  │  JS/TS:  function_declaration  │
  │          method_definition     │
  │          class_declaration     │
  │          arrow_function (const)│
  │                                │
  │  Each symbol → Symbol object:  │
  │    name, type, file,           │
  │    signature, docstring,       │
  │    line_start, line_end,       │
  │    calls: list[str]            │  outgoing call names (unresolved)
  └────────────────────────────────┘
       │
       ▼  Pass 1: store all nodes
  ┌────────────────────────────────┐
  │  embed(name + sig + doc)       │  all-MiniLM-L6-v2 → 384-dim float32
  │  GraphStore.upsert_node(sym)   │  INSERT OR REPLACE, blob = vec.tobytes()
  └────────────────────────────────┘
       │
       ▼  Pass 2: resolve edges
  ┌────────────────────────────────┐
  │  for each call name in sym:    │
  │    lookup target node by name  │  file-local first, then global
  │    GraphStore.upsert_edge()    │  (src_id, dst_id, "calls")
  └────────────────────────────────┘
       │
       ▼
    SQLite: .astra/graph.db
```

**Incremental update (live watcher):**
```
  watchdog FileModifiedEvent / FileCreatedEvent / FileDeletedEvent
       │
       ▼
  store.delete_file(path)         ← remove all nodes + edges for that file
       │
       ▼
  re-parse + re-embed + re-upsert ← same pipeline as above, single file
       │
       ▼
  invalidate_graph_cache(store)   ← drop cached NetworkX DiGraph
```
~100ms per file. No full rebuild.

---

## 4. Data Flow — Query

```
  LLM tool call: astra_get_context(task="add rate limiting")
       │
       ▼
  ┌─────────────────────────────────────────┐
  │  embed_text(task)                        │  → 384-dim query vector  (~5ms)
  └─────────────────────────────────────────┘
       │
       ▼
  ┌─────────────────────────────────────────┐
  │  store.all_embeddings()                  │  → [(node_id, vec), ...]
  │                                          │    decoded from BLOB in SQLite
  │  top_k_similar(query_vec, corpus, k=5)   │  → 5 seed nodes  (~2ms)
  │                                          │    cosine = dot (L2-normed vecs)
  └─────────────────────────────────────────┘
       │
       ▼  seed_ids = 5 most semantically relevant nodes
  ┌─────────────────────────────────────────┐
  │  _get_graph(store)  [cached 300s]        │  NetworkX DiGraph from edges table
  │                                          │  bidirectional (adds reverse edges)
  │  personalized_pagerank(G, seeds,         │  α=0.85, 50 iters, tol=1e-4
  │                        top_k=25)         │  personalization: {seed: 1/k}
  │                                          │  → 25 structurally relevant nodes
  └─────────────────────────────────────────┘
       │
       ▼
  ┌─────────────────────────────────────────┐
  │  merge ranked list:                      │
  │    seeds (raw cosine score)              │  → semantic match
  │    PageRank nodes × 0.8 discount         │  → structural relevance
  │  deduplicate by node_id                  │
  └─────────────────────────────────────────┘
       │
       ▼
  ┌─────────────────────────────────────────┐
  │  build_context(store, merged,            │  serialize: sig + docstring only
  │               max_tokens=4000)           │  greedy fill until budget hit
  └─────────────────────────────────────────┘
       │
       ├──→ save_snapshot() ──────────────────────────────────────────────┐
       │       writes .astra/graphs/current.html (D3 interactive)         │
       │       writes .astra/graphs/history/{ts}_{slug}.html              │
       │       writes .astra/graphs/latest.json (SSE pointer, ~160 bytes) │
       │       prunes history ring buffer (default 10 files)              │
       │                                                                  │
       └──→ MCP response to LLM        Dashboard SSE picks up latest.json ┘
```

---

## 5. SQLite Schemas

### `graph.db` — main knowledge graph

```sql
CREATE TABLE nodes (
    id          TEXT PRIMARY KEY,  -- sha256[:16] of "file::type::name::line"
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,     -- 'file' | 'class' | 'function' | 'method'
    file        TEXT NOT NULL,     -- absolute path
    signature   TEXT,
    docstring   TEXT,              -- capped at 500 chars
    line_start  INTEGER,
    line_end    INTEGER,
    embedding   BLOB               -- numpy float32 384-dim, .tobytes()
);
CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_nodes_file ON nodes(file);
CREATE INDEX idx_nodes_type ON nodes(type);

CREATE TABLE edges (
    src       TEXT NOT NULL REFERENCES nodes(id),
    dst       TEXT NOT NULL REFERENCES nodes(id),
    relation  TEXT NOT NULL,  -- 'calls' | 'contains' | 'CALLS_REV'
    PRIMARY KEY (src, dst, relation)
);

CREATE TABLE file_hashes (
    path   TEXT PRIMARY KEY,
    hash   TEXT NOT NULL,     -- SHA256 hex of file content
    mtime  REAL NOT NULL      -- epoch float
);
```

### `sessions.db` — session memory

```sql
CREATE TABLE sessions (
    id        TEXT PRIMARY KEY,  -- UUID
    project   TEXT NOT NULL,     -- repo root path
    summary   TEXT NOT NULL,     -- LLM-generated session recap
    embedding BLOB,              -- embedded summary for cosine recall
    ts        REAL NOT NULL      -- created_at epoch
);
CREATE INDEX idx_sessions_project ON sessions(project);
```

### `temporal_nodes` / `temporal_changes` — git history

```sql
CREATE TABLE temporal_nodes (
    node_id        TEXT PRIMARY KEY,
    name           TEXT,
    file           TEXT,
    type           TEXT,
    first_seen_ts  REAL,
    last_seen_ts   REAL,
    first_commit   TEXT,
    last_commit    TEXT,
    change_count   INTEGER DEFAULT 0,
    volatility     REAL DEFAULT 0.0   -- change_count / total_commits
);

CREATE TABLE temporal_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT,
    commit_sha TEXT,
    ts         REAL,
    change_type TEXT   -- 'added' | 'modified' | 'removed'
);

CREATE TABLE temporal_coupling (
    node_a     TEXT,
    node_b     TEXT,
    co_changes INTEGER DEFAULT 0,  -- times changed in same commit
    PRIMARY KEY (node_a, node_b)
);
```

### `federation.db` — cross-repo links

```sql
CREATE TABLE fed_repos (
    repo_id    TEXT PRIMARY KEY,
    repo_path  TEXT NOT NULL,
    db_path    TEXT NOT NULL,
    indexed_at REAL NOT NULL
);

CREATE TABLE fed_boundary_nodes (
    node_id   TEXT NOT NULL,
    repo_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    file      TEXT NOT NULL,
    type      TEXT NOT NULL,
    link_type TEXT NOT NULL,  -- EXPORT | ENDPOINT | GRPC | NAME_MATCH
    link_key  TEXT NOT NULL,  -- normalized key for matching across repos
    PRIMARY KEY (node_id, repo_id)
);
CREATE INDEX idx_fbn_link_key ON fed_boundary_nodes(link_key);

CREATE TABLE fed_cross_edges (
    src_repo   TEXT NOT NULL,
    src_node   TEXT NOT NULL,
    dst_repo   TEXT NOT NULL,
    dst_node   TEXT NOT NULL,
    link_type  TEXT NOT NULL,
    confidence REAL NOT NULL,   -- 0.0–1.0
    PRIMARY KEY (src_repo, src_node, dst_repo, dst_node)
);
```

---

## 6. Core Engine Components

### 6.1 Parser (`indexer/parser.py`)

Manual AST traversal — tree-sitter 0.25 dropped `Query.captures()`.

**Per-language handlers:**
- Python: `function_definition`, `class_definition`, `decorated_definition`
- JS/TS: `function_declaration`, `method_definition`, `class_declaration`, `arrow_function`
- JSX/TSX: same as TS + PascalCase component detection

**Call resolution (2-pass):**
```
Pass 1: index all symbols into file-local table
Pass 2: for each call_node in function body:
          get function field → identifier text
          lookup in file-local table → direct edge
          fallback: lookup by name globally → approximate edge
```

**Skip rules:**
```python
SKIP_DIRS  = {"node_modules", "__pycache__", ".git", ".venv", "venv",
              ".astra", "dist", "build", ".next", ".turbo"}
SKIP_FILES = {"d3.min.js", "d3.js"}   # minified files parse as 1399 false nodes
```

### 6.2 Embedder (`indexer/embedder.py`)

```python
model = SentenceTransformer("all-MiniLM-L6-v2")   # 384-dim, ~80MB, CPU-only
embed_text = lambda s: model.encode(s, normalize_embeddings=True)
# L2-normalized → cosine similarity = dot product (fast)
```

Storage: `np.float32.tobytes()` → BLOB. Decoded via `np.frombuffer(blob, dtype=np.float32)`.

Top-k:
```python
def top_k_similar(query, corpus, k):
    matrix = np.stack([v for _, v in corpus])  # (N, 384)
    scores = matrix @ query                     # (N,) dot products = cosine
    idx    = np.argpartition(-scores, k)[:k]    # O(N) partial sort
    return sorted([(corpus[i][0], float(scores[i])) for i in idx], key=lambda x: -x[1])
```

### 6.3 GraphStore (`graph/store.py`)

**Thread safety:** `threading.Lock` guards all writes. SQLite WAL mode. `check_same_thread=False`.

**Hot paths:**
- `all_embeddings()` — pull (id, blob) tuples, decode to np arrays. ~5ms for 1k symbols
- `upsert_node()` — `INSERT OR REPLACE`. ~0.3ms per node
- `get_callers(id)` / `get_callees(id)` — single JOIN on edges table. ~1ms
- `get_nodes_by_name(name)` — indexed lookup. ~0.5ms

### 6.4 PageRank (`graph/pagerank.py`)

```python
def build_nx_graph(store) -> nx.DiGraph:
    G = nx.DiGraph()
    for src, dst in store.iter_edges():
        G.add_edge(src, dst)          # forward: caller → callee
        G.add_edge(dst, src)          # reverse: callee → caller (bidirectional PPR)
    return G

def personalized_pagerank(G, seeds, alpha=0.85, top_k=20) -> list[tuple]:
    p = {n: 0.0 for n in G.nodes}
    for s in seeds:
        if s in G: p[s] = 1.0 / len(seeds)
    scores = nx.pagerank(G, alpha=alpha, personalization=p, max_iter=50, tol=1e-4)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
```

**Cache:** `engine._nx_graph_cache[db_path] = (graph, built_at)`. TTL 300s. Invalidated on file change event.

---

## 7. Intelligence Layer — Impact Analyzer

**File:** `astra/impact/analyzer.py`

**Problem:** Before editing a critical function, understand what breaks.

**Algorithm:**
```
given: changed_node_ids (list of node IDs to analyze)

Step 1 — Reverse BFS over CALLS_REV edges:
    queue = [changed_node_ids]
    while queue and depth < max_depth:
        for each node in queue:
            callers = store.get_callers(node)
            add to affected_set, push callers to next level

Step 2 — Personalized PageRank from changed nodes:
    G = build_nx_graph(store)
    ppr_scores = personalized_pagerank(G, changed_node_ids, top_k=50)

Step 3 — Annotate with test coverage heuristic:
    for each affected node:
        _node_has_test_coverage(node):
            True if "test" in file stem/parent OR name starts with test_/Test

Step 4 — Risk score (0–100):
    blast_ratio     = len(affected) / total_nodes  (0–1)
    untested_ratio  = untested_count / len(affected)
    centrality      = max PPR score among changed nodes (0–1)
    risk = (0.4 * blast_ratio + 0.4 * untested_ratio + 0.2 * centrality) * 100
```

**Diff mode:**
```python
def compute_from_diff(diff_text: str) -> ImpactReport:
    # regex: r'^[+].*def\s+(\w+)'  → extract added/changed function names
    # regex: r'^[+].*class\s+(\w+)' → extract added/changed class names
    # lookup each name → get_nodes_by_name() → compute_blast_radius()
```

**Output:**
```json
{
  "risk_score": 73.4,
  "affected_nodes": [...],
  "untested_nodes": [...],
  "blast_radius": 12,
  "centrality": 0.84
}
```

---

## 8. Intelligence Layer — Semantic Drift Detector

**File:** `astra/semantics/drift.py`

**Problem:** Functions get renamed but their behavior doesn't match their name — hidden tech debt.

**Algorithm:**
```
for each function with docstring and ≥ MIN_CALLEES (default 2) callees:

    declared_vec  = embed(name + " " + docstring)
                    # what the function CLAIMS to do

    behavioral_vec = mean_pool([embed(callee.name + callee.sig)
                                for callee in callees])
                    # what the function ACTUALLY does (inferred from calls)

    drift_score = 1 - cosine_similarity(declared_vec, behavioral_vec)
                # 0 = perfect alignment, 1 = complete mismatch

    if drift_score > DRIFT_THRESHOLD (default 0.35):
        emit DriftWarning(node_id, name, file, drift_score, callee_names)
```

**DriftWarning output:**
```json
{
  "node_id": "abc123",
  "name": "validate_payment",
  "file": "/src/auth/tokens.py",
  "drift_score": 0.67,
  "declared": "validate_payment: Validates user credentials and...",
  "callees": ["generate_token", "create_session", "log_event"],
  "severity": "HIGH"
}
```

**Severity bands:**
- `LOW`: 0.35–0.50
- `MEDIUM`: 0.50–0.70
- `HIGH`: >0.70

---

## 9. Intelligence Layer — Temporal Knowledge Graph

**File:** `astra/temporal/indexer.py`

**Problem:** Hot files and frequently-changed functions are high-risk. No tool shows this.

**Algorithm:**
```
given: repo_path, max_commits=500, branch="HEAD"

Step 1 — Walk git history oldest → newest:
    repo = git.Repo(repo_path)
    commits = list(repo.iter_commits(branch, max_count=max_commits))
    commits.reverse()  # oldest first

Step 2 — For each commit, for each .py/.js/.ts file:
    content_bytes = _get_file_content_at_commit(repo, commit, file_path)
    # blob = commit.tree / file_path → data_stream.read()
    # no disk write — reads from git object store directly

Step 3 — Parse symbols in-memory:
    write bytes to /tmp/astra_temporal_{hash}.py
    parse with tree-sitter → symbol_ids (set)
    delete temp file

Step 4 — Diff against previous commit's symbols:
    added   = curr_ids - prev_ids
    removed = prev_ids - curr_ids
    # "modified" = removed + re-added with same name but different id (line changed)

Step 5 — Update temporal_nodes:
    change_count += 1 for each touched symbol
    record temporal_change(node_id, commit_sha, ts, change_type)
    update co-change matrix for temporal coupling

Step 6 — After all commits:
    volatility = change_count / total_commits   (0.0–1.0)
```

**Output:**
```python
TemporalSummary(
    total_commits=147,
    files_tracked=23,
    volatile_nodes=[
        {"name": "process_payment", "volatility": 0.82, "change_count": 121},
        ...
    ]
)
```

---

## 10. Intelligence Layer — Cross-Repo Federation

**File:** `astra/federation/resolver.py`

**Problem:** Microservices call each other but no tool shows cross-repo call chains.

**Boundary node detection — 4 strategies:**

| Strategy | Trigger | link_key | Confidence |
|---|---|---|---|
| `ENDPOINT` | file has route/view/api/endpoint in name | `endpoint:{fn_name}` | 0.90 |
| `EXPORT` | file is `__init__.py` | `export:{fn_name}` | 0.90 |
| `GRPC` | signature contains "Servicer" or "grpc" | `grpc:{fn_name}` | 0.90 |
| `NAME_MATCH` | public function (not `_private`) | `fn:{fn_name}` | 0.60–0.75 |

**Federation algorithm:**
```
Step 1 — Register repos:
    for each repo: extract boundary nodes → upsert into fed_boundary_nodes

Step 2 — Link all:
    for each boundary node in repo A:
        find nodes with same link_key in any other repo
        create FedEdge(src_repo=A, src_node=..., dst_repo=B, dst_node=...)
        confidence = _compute_confidence(src, dst)

Step 3 — Build unified NetworkX graph:
    node_id format: "{repo_id}:{node_id}"
    add all intra-repo edges
    add all cross-repo edges with relation="CROSS_REPO:{link_type}"

Step 4 — Cross-repo trace:
    BFS from (repo_id, node_id) following both:
    - same-repo callees (store.get_callees)
    - cross-repo edges (fed_cross_edges WHERE src_repo=? AND src_node=?)
```

**Federation DB location:** `~/.astra/federation.db` (global, shared across repos)

---

## 11. Intelligence Layer — Live Daemon

**File:** `astra/daemon/core.py`

**Problem:** MCP restarts rebuild graph from scratch. 7s cold start per session.

**Architecture:**
```
┌────────────────────────────────────────────────────────┐
│  AstraDaemon (background process)                      │
│                                                        │
│  ┌─────────────┐   ┌────────────────┐                  │
│  │ watchdog     │   │ AF_UNIX socket │                  │
│  │ file watcher │   │ ~/.astra/      │                  │
│  │             │   │ daemon.sock    │                  │
│  └──────┬──────┘   └───────┬────────┘                  │
│         │                  │                            │
│         ▼                  ▼                            │
│   GraphDelta          _handle_client()                 │
│   (file changes)      line-delimited JSON               │
│         │              ping / status / query            │
│         ▼              search / impact / delta          │
│   _apply_delta()                                        │
│   incremental PPR update                               │
└────────────────────────────────────────────────────────┘
```

**Incremental PageRank update:**
```python
def _incremental_pagerank_update(G, store, changed_node_ids, radius=2):
    # BFS from changed nodes to radius hops
    subgraph_nodes = bfs_subgraph(G, changed_node_ids, radius)
    # extract subgraph
    H = G.subgraph(subgraph_nodes)
    # local PageRank on subgraph only
    local_scores = nx.pagerank(H, alpha=0.85, max_iter=50)
    # merge into global cache
    global_ppr_cache.update(local_scores)
```

10–50x faster than full recompute. Accurate within BFS radius.

**Unix socket protocol (line-delimited JSON):**
```json
→ {"cmd": "query", "task": "add rate limiting", "max_tokens": 4000}
← {"status": "ok", "context": "...", "tokens": 992, "nodes": 30}

→ {"cmd": "ping"}
← {"status": "ok", "ts": 1717000000.0}

→ {"cmd": "impact", "node_ids": ["abc123", "def456"]}
← {"status": "ok", "risk_score": 73.4, "affected_nodes": [...]}
```

**Socket path:** `~/.astra/daemon.sock` (104-char macOS limit — path is always short)

**Signal handling:** `signal.signal()` only registered when running in main thread:
```python
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, self._shutdown)
    signal.signal(signal.SIGINT,  self._shutdown)
```

---

## 12. MCP Protocol Layer

### Tool registration (`mcp/server.py`)

```python
server = Server("astra-mcp")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS   # 11 Tool objects with JSON schemas

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    result = dispatch_table[name](store, **arguments)
    return [TextContent(type="text", text=json.dumps(result))]

async with stdio_server() as (read, write):
    await server.run(read, write, server.create_initialization_options())
```

### All 11 MCP Tools

| Tool | Description | Returns |
|---|---|---|
| `astra_get_context` | Task → minimal relevant code context (primary) | `{context, tokens, nodes, snapshot}` |
| `astra_search` | Semantic top-k symbol search | `list[{name, type, file, line, score}]` |
| `astra_get_callers` | Who calls this function | `list[{name, file, line, signature}]` |
| `astra_get_callees` | What this function calls | `list[{name, file, line, signature}]` |
| `astra_get_file_map` | All symbols in a file, signatures only | `str` (formatted) |
| `astra_session_memory` | Recall past sessions relevant to task | `str` (formatted) |
| `astra_index_status` | Nodes, edges, files, DB size | `{nodes, edges, files, db_size_kb}` |
| `astra_impact_analysis` | Blast radius before editing | `{risk_score, affected_nodes, untested}` |
| `astra_semantic_audit` | Scan for semantic drift in codebase | `list[DriftWarning]` |
| `astra_get_volatility` | Which functions change most (git history) | `{top_volatile, count}` |
| `astra_trace_cross_repo` | Follow call chain across repo boundaries | `{trace, hops}` |

### Auto-index on startup
```python
async def run_server():
    store = GraphStore(db_path)
    if store.stats()["nodes"] == 0:
        await loop.run_in_executor(None, index_codebase, project, store)
    # then start MCP server
```

---

## 13. Dashboard Layer

### FastAPI routes (`dashboard/server.py`)

```
GET  /                         → index.html (SPA)
GET  /d3.min.js                → local D3 v7 (no CDN)
GET  /api/stats                → nodes + edges + files + query history
POST /api/query                → run query → save snapshot → push history
GET  /api/search?q=…&k=…       → semantic symbol search
GET  /api/graph?limit=…        → all nodes + edges for full graph viz
GET  /api/graph/node/{id}      → node detail + callers + callees
GET  /api/stream               → SSE: stats every 2s + latest_snapshot pointer
GET  /api/graphs               → list current + history snapshots
GET  /api/latest_snapshot      → polling fallback
GET  /graphs/current           → live rolling snapshot HTML (auto-reloads)
GET  /graphs/{id}              → historical snapshot from ring buffer
```

### SSE auto-refresh flow

```
MCP tool call fires (e.g. astra_get_context from Claude Code)
     ↓
save_snapshot() writes:
  .astra/graphs/current.html   ← D3 interactive graph
  .astra/graphs/history/{ts}.html
  .astra/graphs/latest.json    ← {id, task, ts, nodes, edges}
     ↓
Dashboard SSE generator reads latest.json every 2s → pushes event
     ↓
Browser JS detects ts change → loadSavedGraphs() + flash banner
     ↓
if /graphs/current open in tab "astra-graph":
  embedded SSE listener in snapshot HTML detects ts > initialTs
  → location.reload() → fresh graph
```

### Snapshot storage strategy

| File | Role | Size |
|---|---|---|
| `current.html` | Rolling live view | ~200KB (full D3 HTML) |
| `history/{ts}_{slug}.html` | Ring buffer (10 max) | ~200KB each |
| `latest.json` | SSE pointer | ~160 bytes |

---

## 14. Performance Characteristics

| Operation | Latency | Notes |
|---|---|---|
| Index 1 file | 10–30ms | parse + embed + 2 SQLite upserts |
| Full index 1k files | 30–60s | sequential (parallel-safe, not parallelized yet) |
| Embed query text | ~5ms | model warm in RAM |
| Cosine top-k (1k nodes) | ~2ms | numpy matmul O(N·384) |
| Build NetworkX graph | ~50ms | from 1k nodes, 2k edges |
| Personalized PageRank | 20–80ms | 50 iters, varies by density |
| `astra_get_context` (warm) | 50–150ms | |
| First query (cold) | ~7s | model load + graph build |
| Daemon query (warm) | ~20ms | no model reload, incremental PPR |
| Impact analysis (1k nodes) | ~30ms | BFS + subgraph PPR |
| Semantic drift scan (100 fns) | ~200ms | embed declared + behavioral vecs |
| Git timeline (100 commits) | ~10s | blob reads + temp file parse |

**Memory footprint:**
- Model in RAM: ~400MB (all-MiniLM-L6-v2)
- Embeddings in RAM during query: 1.5KB × N symbols
- NetworkX graph: ~50 bytes × E edges
- DB on disk: ~1–3% of source size

**Token reduction (real numbers):**

| Project size | Naive (full read) | ASTra context | Reduction |
|---|---|---|---|
| 500 functions | ~120k tokens | ~800–1,200 tokens | 98.9% |
| 2,000 functions | ~480k tokens | ~1,000–2,000 tokens | 99.6% |
| 10,000 functions | ~2.4M tokens | ~2,000–4,000 tokens | 99.8% |

---

## 15. Configuration Reference

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ASTRA_DATA_DIR` | `.astra` (cwd) | where all DB + snapshot files live |
| `ASTRA_PROJECT` | cwd | root path for indexing |
| `ASTRA_GRAPH_HISTORY` | `10` | snapshot ring buffer size. `0` = disabled |
| `HF_TOKEN` | — | huggingface token (model download rate limits) |

### Files

```
{project}/
├── .astra/
│   ├── graph.db                 # main knowledge graph (nodes, edges, file_hashes)
│   ├── sessions.db              # session memory
│   ├── daemon.pid               # daemon process ID
│   ├── daemon.delta             # latest file change delta (JSON)
│   └── graphs/
│       ├── current.html         # live rolling snapshot
│       ├── latest.json          # SSE pointer
│       └── history/             # ring buffer snapshots
├── .mcp.json                    # MCP server registration (Claude Code)
└── .claude-plugin/
    └── plugin.json              # Claude Code plugin manifest

~/.astra/
├── federation.db                # cross-repo federation (global)
└── daemon.sock                  # live daemon Unix socket
```

---

## 16. Test Suite

| Test file | What it covers | Key fixtures |
|---|---|---|
| `test_parser.py` | AST traversal, symbol extraction per language | tmp source files |
| `test_embedder.py` | embed, top_k_similar, L2 normalization | — |
| `test_store.py` | upsert/get/delete/stats, thread safety | tmp SQLite |
| `test_pagerank.py` | convergence, personalization, caching | small DiGraph |
| `test_engine.py` | full query pipeline end-to-end | tmp indexed repo |
| `test_tools.py` | all 11 MCP tool handlers | mock store |
| `test_daemon.py` | socket protocol, incremental PPR, ping/query | `running_daemon` fixture (Unix socket at `/tmp/astra_test_{hash8}.sock`) |
| `test_impact.py` | BFS blast radius, risk scoring, diff mode | store with known call graph |
| `test_drift.py` | declared vs behavioral vec, threshold, severity | store with callee embeddings |
| `test_temporal.py` | git history walk, volatility, coupling | `_make_git_repo_with_history()` (3 commits via subprocess) |
| `test_federation.py` | boundary detection, link matching, confidence, trace | two GraphStores in tmp dirs |

**Run all:**
```bash
cd ASTra_MCP && python -m pytest tests/ -v
```

**Critical test patterns:**

```python
# Daemon: socket path must be under 104 chars (macOS AF_UNIX limit)
h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
test_sock = Path(f"/tmp/astra_test_{h}.sock")   # 26 chars ✓

# Symbol: id is a @property, NOT a constructor arg
sym = Symbol(type="function", name="foo", file="/x.py",
             signature="def foo()", docstring="", line_start=1, line_end=5)
# DO NOT pass id= or embed_text= to Symbol()

# Signal: only register in main thread
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, handler)
```

---

## 17. End-to-End Trace

**User types:** "Add rate limiting to the MCP server"

```
1.  Claude Code planner calls astra_get_context(task="Add rate limiting...")

2.  JSON-RPC over stdio:
    {"method":"tools/call","params":{"name":"astra_get_context",
     "arguments":{"task":"Add rate limiting to the MCP server"}}}

3.  mcp/server.py dispatch → tool_get_context(store, task)

4.  query/engine.py get_context():
    a. embed_text(task)           → 384-dim vec          [~5ms]
    b. store.all_embeddings()     → 605 (id, vec) pairs
    c. top_k_similar(vec, 605, 5) → 5 seed nodes         [~2ms]
    d. _get_graph(store) cached   → DiGraph (605N, 1.6kE)
    e. personalized_pagerank(...)  → 25 expansion nodes   [~40ms]
    f. merge + dedup              → 30 unique node_ids
    g. build_context(store, 30)   → serialized sigs       [~30ms]

5.  Returns {context, tokens:992, nodes:30, seeds:[...], node_ids:[...]}

6.  tool_get_context() calls save_snapshot():
    - SELECT 30 nodes + edges from SQLite
    - Render D3 HTML (node colors by type, edges by relation)
    - Write .astra/graphs/current.html
    - Write .astra/graphs/history/1717000000_Add_rate_limiting.html
    - Prune history to 10 files
    - Write .astra/graphs/latest.json

7.  JSON-RPC response → Claude Code
    Claude injects 992 tokens of context (vs 116,461 naive = 99.1% reduction)
    Claude generates correct rate-limiting code on first try

8.  Dashboard SSE (parallel):
    - SSE generator reads latest.json on next 2s tick
    - Pushes {type:"snapshot", ...} to browser
    - Browser: loadSavedGraphs() + flash banner
    - /graphs/current tab auto-reloads → new D3 graph rendered
```

---

## 18. Bugs Fixed & Lessons Learned

| Bug | Root cause | Fix |
|---|---|---|
| `Query.captures()` AttributeError | tree-sitter 0.25 removed this API | manual recursive node traversal |
| MCP server "Failed" in Claude Code | `${pluginRoot}` doesn't resolve in plugin context | hardcoded absolute Python path |
| `plugin.json` install error | `"author": "string"` not valid | `"author": {"name": "..."}` |
| 1399 nodes instead of 607 | d3.min.js parsed as source code | `SKIP_FILES = {"d3.min.js"}` |
| Index wiped on restart | auto-init ran even when DB populated | only auto-index if `nodes == 0` |
| D3 graph blank | CDN unreachable | bundled d3.min.js + local route |
| Stale data after re-index | `--force` didn't wipe existing rows | wipe nodes/edges/hashes when `force=True` |
| `node_ids` missing from response | engine returned count only | added `"node_ids":[...]` to return dict |
| Snapshots only from dashboard | MCP tools bypassed snapshot | extracted `save_snapshot()` → shared module |
| Disk bloat from per-query HTML | every query wrote new file | rolling `current.html` + capped ring buffer |
| `signal.signal()` in test thread | called from non-main thread | guarded with `threading.current_thread() is threading.main_thread()` |
| `OSError: AF_UNIX path too long` | pytest tmp_path = 100+ chars | `/tmp/astra_test_{md5[:8]}.sock` = 26 chars |
| `bytes not JSON serializable` | `search_symbols()` returns embedding BLOB | `r.pop("embedding", None)` before json.dumps |
| `sqlite3.Row` not a dict | passed raw Row to `_compute_confidence()` | explicit `row = dict(_row)` |
| `gitpython` import fails | not installed in user's CLI Python env | `pip3 install gitpython` |
| `Symbol() got unexpected kwarg 'id'` | `id` is a `@property`, not a constructor arg | never pass `id=` to Symbol() |

---

## 19. Quick Code Pointers

| What | File:location |
|---|---|
| CLI entry point | [astra/cli/main.py](astra/cli/main.py) |
| AST traversal (Python) | [astra/indexer/parser.py](astra/indexer/parser.py) |
| File watcher | [astra/indexer/monitor.py](astra/indexer/monitor.py) |
| Embedding + cosine | [astra/indexer/embedder.py](astra/indexer/embedder.py) |
| SQLite layer | [astra/graph/store.py](astra/graph/store.py) |
| PageRank | [astra/graph/pagerank.py](astra/graph/pagerank.py) |
| Query pipeline | [astra/query/engine.py](astra/query/engine.py) |
| Token-budget serializer | [astra/query/serializer.py](astra/query/serializer.py) |
| MCP stdio server | [astra/mcp/server.py](astra/mcp/server.py) |
| 11 tool handlers | [astra/mcp/tools.py](astra/mcp/tools.py) |
| Snapshot writer | [astra/dashboard/snapshot.py](astra/dashboard/snapshot.py) |
| Dashboard API | [astra/dashboard/server.py](astra/dashboard/server.py) |
| Dashboard SPA | [astra/dashboard/index.html](astra/dashboard/index.html) |
| **Impact analyzer** | [astra/impact/analyzer.py](astra/impact/analyzer.py) |
| **Drift detector** | [astra/semantics/drift.py](astra/semantics/drift.py) |
| **Temporal indexer** | [astra/temporal/indexer.py](astra/temporal/indexer.py) |
| **Federation resolver** | [astra/federation/resolver.py](astra/federation/resolver.py) |
| **Live daemon** | [astra/daemon/core.py](astra/daemon/core.py) |
| **Daemon runner** | [astra/daemon/runner.py](astra/daemon/runner.py) |
