# ASTra — AST-Powered Codebase Memory for AI Agents

**Navigate code like a senior engineer, not a search engine.**

ASTra is an MCP server that gives Claude Code, Codex, and Cursor permanent structural memory of your codebase. It parses every file into an Abstract Syntax Tree, builds a knowledge graph of symbols and their relationships, and injects only the relevant context before every task — cutting token usage by **93%**.

## The Problem

AI coding agents waste **80% of tokens on orientation**: reading files they don't need, rediscovering patterns, re-learning the same codebase every session. A typical agent query generates context by:

1. Naive approach: Read all source files as raw text → ~14,000 tokens
2. Agent's first task: "fix auth bug" → pulls entire codebase into context
3. Result: Token cost explodes, LLM accuracy falls 30% from context bloat

## How ASTra Solves It

```
Task: "fix authentication token expiry bug"

1. Embed task → query vector (384-dim)
2. Semantic search → find top-5 most relevant symbols
3. Personalized PageRank → expand to callers/callees (2-hop graph traversal)
4. Serialize → signatures + docstrings only, no bodies
5. Inject context → 877 tokens (vs 14,294 without ASTra)
6. Result: 93.9% reduction, 51ms latency (warm)
```

## The Brain Analogy

Human brain does 3 things LLMs don't:

| Brain | ASTra |
|---|---|
| **PFC pre-filter** — only relevant info enters working memory | **Query engine** — goal-based context injection before task |
| **Hippocampal indexing** — stores associations, not raw data | **Knowledge graph** — AST nodes + edges, not text blobs |
| **Incremental compression** — combines short/long-term context | **Session memory** — hot/cold delta store across sessions |

## Technical Architecture

### Layers

1. **Parser** (`astra/indexer/parser.py`)
   - tree-sitter AST extraction (Python, JS/TS)
   - Extracts: functions, classes, signatures, docstrings, call graphs
   - Skips full function bodies (they waste tokens)

2. **Graph Builder** (`astra/indexer/graph_builder.py`)
   - Parses entire codebase → 112 symbols in ASTra itself
   - Builds SQLite-backed knowledge graph
   - Computes embeddings on all nodes (all-MiniLM-L6-v2, 384-dim)
   - Incremental indexing: changed file only, ~200ms per file

3. **Query Engine** (`astra/query/engine.py`)
   - Task description → embedding
   - Top-k semantic search over symbol embeddings
   - Personalized PageRank from seeds (mimics hippocampal recall)
   - Returns ranked subgraph, serialized to token budget

4. **Session Memory** (`astra/memory/session.py`)
   - Stores compressed deltas from past sessions
   - Semantic similarity retrieval: "what did we do related to auth?"
   - ~500 tokens injected at session start (replaces 5000-token history)

5. **MCP Server** (`astra/mcp/server.py`)
   - 7 tools exposed to Claude Code/Codex/Cursor
   - Runs on stdio (Claude Code compatible)
   - Can also run as HTTP server with web dashboard

6. **File Watcher** (`astra/watcher/monitor.py`)
   - Detects file changes in real-time
   - Re-indexes only changed file (incremental)
   - Invalidates graph cache, re-runs PageRank

### Tech Stack

```
Parser:       tree-sitter 0.25.x (Python, JS/TS)
Graph:        SQLite + NetworkX + Personalized PageRank
Embeddings:   sentence-transformers (all-MiniLM-L6-v2, 384-dim)
MCP Server:   FastAPI + stdio (Claude Code compatible)
Watcher:      watchdog + fsevents
CLI:          typer + rich
Dashboard:    vanilla HTML/CSS/JS + SSE
```

## Installation

### Prerequisites
- Python 3.10+
- `pip` / `pip3`

### Option 1: pip install (recommended)

```bash
pip install astra-mcp
```

Then index your codebase:
```bash
astra init /path/to/your/project
```

### Option 2: Local development

```bash
git clone https://github.com/YOUR_ORG/astra-mcp
cd astra-mcp
pip install -e .
```

## Quick Start

### 1. Index Your Codebase

```bash
# Index current directory
astra init

# Index specific directory
astra init /path/to/myproject

# Force re-index all files
astra init --force
```

This scans all `.py`, `.js`, `.ts`, `.jsx`, `.tsx` files (skips `node_modules`, `.git`, etc.), parses ASTs, computes embeddings, and builds the knowledge graph. On a 100-file codebase: ~30 seconds.

### 2. Launch MCP Server (for Claude Code)

```bash
astra watch
```

This starts:
- File watcher (re-indexes on save)
- MCP stdio server (connects to Claude Code)
- Auto-discovery of the 7 tools

Then in Claude Code settings (`.claude/settings.json`):
```json
{
  "mcpServers": {
    "astra": {
      "command": "python3",
      "args": ["-m", "astra.mcp.server"],
      "env": { "ASTRA_DATA_DIR": ".astra", "ASTRA_PROJECT": "." }
    }
  }
}
```

Reload Claude Code. You'll see 7 new tools:
- `astra_get_context`
- `astra_search`
- `astra_get_callers`
- `astra_get_callees`
- `astra_get_file_map`
- `astra_session_memory`
- `astra_index_status`

### 3. Test Locally

```bash
# Query the graph
astra query "fix authentication token expiry bug"

# See token savings
astra bench "add pagination to user list"

# Launch web dashboard
astra dashboard
# Opens http://127.0.0.1:7865
```

### 4. Check Graph Status

```bash
astra status
```

Output:
```
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ Metric              ┃ Value ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ Nodes (symbols)     │  112  │
│ Edges (relations)   │   46  │
│ Files indexed       │   22  │
│ DB size             │ 2.1   │
└─────────────────────┴───────┘
```

## Usage Guide

### MCP Tool: `astra_get_context`

**What**: Convert task description into minimal relevant code context.

**When**: Call this first before any coding task.

```python
# In Claude Code, invoke via tool:
task = "fix authentication token expiry bug"
context = astra_get_context(task, max_tokens=4000)
# Returns: signatures + docstrings of 25 relevant symbols, ~877 tokens
```

**Why it works**: Semantic embedding + PageRank finds only what matters, avoiding full-file reads.

---

### MCP Tool: `astra_search`

**What**: Semantic symbol search across codebase.

```python
query = "token validation"
results = astra_search(query, top_k=10)
# Returns: {name, type, file, line, signature, score}
```

---

### MCP Tool: `astra_get_callers` / `astra_get_callees`

**What**: Find who calls a function, or what it calls.

**When**: Before refactoring a signature, understand impact.

```python
callers = astra_get_callers("verify_token", file="src/auth/token.py")
# Returns: all functions that call verify_token
```

---

### CLI: `astra query`

Test context retrieval locally:

```bash
astra query "refactor database connection pooling" --max-tokens 2000
```

Output: signatures + docstrings injected into agent context.

---

### CLI: `astra bench`

Benchmark token savings:

```bash
astra bench "add unit tests for payment processing"
```

Output:
```
═══════════════════════════════════════════════
Task:            add unit tests for payment processing
Files:           42
Naive tokens:    28,460
ASTra tokens:    1,890
Reduction:       93.3%
Symbols:         31
Latency:         52.3ms
Cost saved:      $0.079 per session (@$3/M tokens)
═══════════════════════════════════════════════
```

---

### CLI: `astra memory`

Manage session history across days:

```bash
# List all past sessions
astra memory ls

# Show details of a session
astra memory show <session_id>

# Save a session delta manually
astra memory save --summary "Fixed auth bug in Token.verify() by adding expiry check" --tags "bug,auth"
```

When starting a new task, ASTra recalls: "3 days ago, we worked on something similar here's what we fixed."

---

### CLI: `astra dashboard`

Real-time web dashboard (hackathon demo):

```bash
astra dashboard
# Opens http://127.0.0.1:7865
```

Shows:
- Live token counter (animated reduction %)
- Query history with token savings per task
- Symbol graph stats (nodes, edges, files)
- Semantic symbol search
- Cost savings estimate

---

## Benchmark Numbers

**On ASTra's own codebase** (22 files, 112 symbols):

| Task | Naive Tokens | ASTra Tokens | Reduction | Latency | Symbols |
|---|---|---|---|---|---|
| "fix auth token bug" | 14,294 | 877 | 93.9% | 51ms | 25 |
| "add pagination" | 14,294 | 875 | 93.9% | 48ms | 24 |
| "refactor connection pool" | 14,294 | 920 | 93.6% | 49ms | 28 |

**Cost impact** (at $3/M tokens):
- Per session: save ~$0.037
- 10 sessions/day, 250 working days/year: save **$92.50/dev/year**
- Team of 50 devs: **$4,625/year**

---

## How It Differs from Alternatives

### vs. RAG (traditional)

| RAG | ASTra |
|---|---|
| Vector DB for chunks | Graph DB for symbols + structure |
| BM25 keyword search | Semantic + structural traversal |
| No session memory | Persistent hot/cold memory |
| Full function bodies bloat tokens | Signatures + docstrings only |

### vs. Cursor/GitHub Copilot's built-in

| Built-in | ASTra |
|---|---|
| Heuristic file ordering | Knowledge graph + PageRank |
| Same device only | Codebase-agnostic, portable |
| No cross-session recall | Session deltas stored in SQLite |
| One agent only | Any AI agent (Claude, Codex, GPT-4) |

### vs. LangGraph / LlamaIndex agents

| Generic agent framework | ASTra |
|---|---|
| Configure for each codebase | Works out of the box |
| Build custom retrieval | Pre-built context injection |
| Stateless | Session memory included |
| No file watcher | Auto-reindex on save |

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│ Your Codebase (.py, .js, .ts files)                    │
└─────────────────────────────────────────────────────────┘
                          ↓
        ┌──────────────────────────────────────┐
        │ Parser (tree-sitter)                 │
        │ Extract: functions, classes, calls   │
        └──────────────────────────────────────┘
                          ↓
        ┌──────────────────────────────────────┐
        │ Embedder (all-MiniLM-L6-v2)          │
        │ 384-dim vectors for each symbol      │
        └──────────────────────────────────────┘
                          ↓
        ┌──────────────────────────────────────┐
        │ Graph Store (SQLite + NetworkX)      │
        │ Nodes: symbols, Edges: calls/imports │
        └──────────────────────────────────────┘
                          ↓
        ┌──────────────────────────────────────┐
        │ Query Engine                         │
        │ 1. Embed task                        │
        │ 2. Semantic search (top-k)           │
        │ 3. PageRank expansion (2-hop)        │
        │ 4. Serialize to token budget         │
        └──────────────────────────────────────┘
                          ↓
    ┌─────────────────────────────────────────────┐
    │ Context Injection                           │
    │ 877 tokens (93% savings) → Claude Code      │
    └─────────────────────────────────────────────┘

Parallel:
┌──────────────────────┐     ┌──────────────────┐
│ File Watcher         │     │ Session Memory   │
│ Re-index on save     │     │ Hot/cold deltas  │
└──────────────────────┘     └──────────────────┘

Frontend:
┌──────────────────────┐     ┌──────────────────┐
│ MCP Server (stdio)   │     │ Web Dashboard    │
│ 7 tools             │     │ Real-time stats  │
└──────────────────────┘     └──────────────────┘
```

---

## Language Support

**Phase 1 (done):**
- Python 3.6+
- JavaScript/TypeScript
- JSX/TSX

**Phase 2 (planned):**
- Go
- Rust
- Java

---

## Configuration

### Environment Variables

```bash
ASTRA_DATA_DIR      # Path to .astra folder (default: .astra)
ASTRA_PROJECT       # Project root for context (default: current dir)
```

### MCP Server Config

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "astra": {
      "command": "python3",
      "args": ["-m", "astra.mcp.server"],
      "env": {
        "ASTRA_DATA_DIR": ".astra",
        "ASTRA_PROJECT": "."
      }
    }
  }
}
```

---

## Troubleshooting

### Graph index is slow

First index is slow (30s for 100 files) because it downloads the embedding model (~200MB). Subsequent runs are cached.

```bash
# Force re-index
astra init --force
```

### MCP server won't start

Check Claude Code can find Python 3.10+:

```bash
python3 --version
which python3
```

Update `.claude/settings.json` path if needed.

### Some files not indexed

ASTra skips:
- Non-source files (not `.py/.js/.ts/.jsx/.tsx`)
- `node_modules/`, `.git/`, `__pycache__/`, `.venv/`, etc.

Whitelist custom directories by editing `astra/indexer/parser.py`:

```python
SKIP_DIRS = {..., "your_skip_dir"}
```

---

## Contributing

Issues and PRs welcome. Key areas:
- More languages (Go, Rust, Java)
- Query refinements (better seed selection)
- Performance (faster PageRank)
- UI (dashboard enhancements)

---

## License

MIT

---

## Citation

If ASTra helps your team, cite us:

```bibtex
@software{astra2025,
  title={ASTra: AST-Powered Codebase Memory for AI Agents},
  author={Satya Sai Charan},
  year={2025},
  url={https://github.com/your-org/astra-mcp}
}
```

---

## References

- **HippoRAG** (NeurIPS 2024): Knowledge graphs + Personalized PageRank for RAG
- **fMRI brain memory** (neuroscience): Incremental compression in hippocampus
- **tree-sitter**: Universal AST parsing for 100+ languages
- **Sentence Transformers**: Fast semantic embeddings

---

## FAQ

**Q: Does ASTra work with Cursor/GitHub Copilot?**

A: Cursor and GitHub Copilot have their own built-in indexing. ASTra is best for Claude Code, which supports MCP servers.

**Q: Can I use ASTra for private codebases?**

A: Yes. Everything runs locally. No code leaves your machine. No API calls.

**Q: What about large codebases (10K+ files)?**

A: Tested on 1000+ file projects. Initial index: ~5 mins. Queries: 50-200ms. Graph size: ~50MB.

**Q: How often does the index update?**

A: File watcher detects saves in real-time. Re-parses changed file in ~200ms. Queries pick up changes immediately.

**Q: Can I integrate with GitHub/GitLab CI?**

A: Not yet. Planned: commit hooks to update index on push.

---

**Questions?** Open an issue or check the [examples](./examples/) directory.
