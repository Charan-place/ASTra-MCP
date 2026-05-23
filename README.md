# ASTra MCP — Permanent Code Memory for AI Coding Assistants

> **The problem:** Your AI assistant (Claude Code, Cursor, Codex) reads entire files to understand your codebase. On a 100k-line repo, that's 500k+ tokens per session. Slow. Expensive. Hits context limits.
>
> **The fix:** ASTra builds a permanent knowledge graph of your codebase. When your AI assistant starts a task, ASTra injects **only the 5 most relevant functions** — not 50 whole files. Result: **98.9% token reduction** on real projects.

---

## What ASTra Does For You

You ask your AI assistant something. Before it reads any files, it asks ASTra: *"What in this codebase is relevant to what the user wants?"* ASTra answers in under 100ms with the minimum code needed.

You save money. You save time. Your AI assistant gets smarter because it has room in its context window to actually think.

---

## Real Numbers from Real Projects

Tested on a personal monorepo with 4 projects (Python + TypeScript):

| Metric | Without ASTra | With ASTra |
|---|---|---|
| Tokens per coding task | ~112,000 | ~1,250 |
| Cost per task (Claude Sonnet) | $0.34 | $0.004 |
| Time to context | 12–18 seconds | <1 second |
| Files the AI must read | 20–40 | 0 |

**Translation:** A developer running 50 AI-assisted tasks per day cuts spending from ~$17/day to ~$0.20/day. Across a 10-person team: roughly **$5,000+/month saved**.

---

## Who Benefits

### Individual developers
- Stop burning tokens on huge `Read` operations
- Faster AI responses (less context = less latency)
- Keep working in large codebases without hitting context limits

### Engineering teams
- Predictable AI spend per developer
- Less time spent watching AI assistants spin reading files
- Better onboarding — new engineers' AI assistants instantly "know" the codebase

### Open-source maintainers
- Contributors using AI assistants get accurate, scoped suggestions
- AI-generated PRs are more focused (smaller context = less hallucination)

### Companies with proprietary codebases
- Local-first: index runs entirely on your machine. No code sent to external services.
- Reduce Claude/OpenAI API spend without losing AI productivity

---

## What Questions ASTra Can Answer

Ask your AI assistant any of these — it pulls only the relevant code via ASTra:

### "Help me build something"
- "Add 2FA to login" → ASTra returns auth functions, session handlers, user model
- "Add rate limiting to the API" → ASTra returns middleware, existing throttle logic, where to plug it in
- "Refactor the payment flow" → all payment-touching symbols + their callers

### "Help me find something"
- "Where do we validate webhooks?" → exact file + function + line
- "Find every file that loads API keys" → security audit in seconds
- "Show me all strategy implementations" → semantic match, not just grep

### "Help me understand impact"
- "Who calls `process_order()`?" → full caller list before you rename it
- "What does `place_bracket_order` depend on?" → callee chain, no surprises
- "Will changing this signature break anything?" → blast radius mapped instantly

### "Help me skim a file"
- "Symbol map of api_client.py" → all functions + signatures, no bodies
- "What's in strategies/base.py?" → bird's-eye view in 50 tokens, not 500

### "Help me remember"
- "Did I solve this kind of bug before?" → past session recall
- "What approach did we try last week?" → memory across sessions

---

## How You'll Actually Use It Day-to-Day

### Day 1 — Install (one-time, 2 minutes)
You run the installer. ASTra adds itself as an MCP server to your AI assistant. It indexes your codebase in the background — 60 seconds for a medium repo. You forget it exists.

### Day 2 onwards — You do nothing
You keep using Claude Code / Cursor / Codex exactly as before. Behind the scenes, your AI assistant automatically calls ASTra before reading files. You never see it happen. You just notice:
- Your AI assistant responds faster
- It picks the right files on the first try
- You stop hitting context-window errors
- Your monthly API bill drops dramatically

### When the codebase changes
ASTra watches your files. You edit a function → ASTra re-indexes that file in milliseconds. The graph stays fresh forever. No "rebuild the index" step.

---

## The Knowledge Dashboard

Run `astra dashboard` → open `http://localhost:7865`.

You get a live visual brain of your codebase:

### Dashboard tab
- **Token savings counter** — running total of tokens ASTra has saved you
- **Cost saved estimate** — actual dollar value at current Claude pricing
- **Live token bars** — every AI query shown as bar chart: naive read vs ASTra
- **Reduction ring** — % saved per query, animated
- **Query history** — last 20 AI tasks with their token cost

### Knowledge Graph tab
- Interactive force-directed map of your entire codebase
- **Nodes** = files (yellow), classes (orange), functions (cyan)
- **Edges** = who calls whom, who contains what
- Click a node → see its callers, callees, signature, file location
- Filter by file or symbol name
- Per-query graph snapshots: see exactly which nodes ASTra picked for each AI task

This is useful for:
- **Code reviews** — visualize what a PR touches
- **Onboarding** — new engineers see the whole shape of the system
- **Refactoring** — spot tightly-coupled clusters
- **Demos to stakeholders** — show "this is the surface area of our codebase"

---

## Multi-Project Support

Got a monorepo? Got multiple repos? Both work.

- Point ASTra at any directory → it indexes everything recursively
- Stays per-project: each repo gets its own `.astra/` folder
- Same MCP server handles all projects — your AI assistant just picks the right context per workspace

Tested with: Python backends, Next.js frontends, FastAPI services, trading bots, ML pipelines.

---

## Privacy & Security

- **Local-first.** Your code never leaves your machine. Index lives in SQLite on disk.
- **No telemetry.** ASTra doesn't phone home.
- **No external API keys needed.** Embeddings model runs locally (sentence-transformers).
- **Self-hosted dashboard.** Runs on `localhost`. Not exposed to the internet.
- **Open source.** MIT licensed. Audit the code, fork it, ship it.

For enterprise: ASTra is safe to run on machines that touch confidential code (medical, financial, defense). Nothing leaves the box.

---

## Installation

### Via Claude Code plugin (recommended)
```bash
# In Claude Code, open Manage Plugins → Marketplace → Install "astra"
# Or:
claude plugin install astra
```
ASTra auto-installs, registers as MCP server, and indexes your current workspace.

### Via pip (universal — works with Cursor, Codex, any MCP client)
```bash
pip install astra-mcp
astra init                        # index current directory
astra dashboard                   # optional — view the graph
```

Then add to your AI assistant's MCP config:
```json
{
  "mcpServers": {
    "astra": {
      "command": "python3",
      "args": ["-m", "astra.mcp.server"]
    }
  }
}
```

Restart your AI assistant. Done.

---

## Command Reference

| Command | Purpose |
|---|---|
| `astra init` | Index current project (first-time setup) |
| `astra reindex` | Force-rebuild index (rarely needed) |
| `astra status` | Show index health: files, symbols, edges, DB size |
| `astra search "auth code"` | Quick semantic search from CLI |
| `astra dashboard` | Launch web dashboard on :7865 |
| `astra bench "fix login bug"` | Benchmark token savings on a sample task |
| `astra watch` | Run file watcher in foreground (usually auto-runs) |

---

## What Makes ASTra Different

Most "code search" tools do one of these:
- **Grep / ripgrep** — text match only. No semantic understanding.
- **GitHub Copilot index** — closed, cloud-based, your code leaves your machine.
- **Vector DBs (Chroma, Pinecone) with raw embedding** — find similar text, but miss structural relationships.
- **Tree-sitter alone** — parses syntax, doesn't rank what matters.

ASTra combines **all four signals**:
1. Semantic embeddings (what's the user asking about?)
2. AST structure (what's syntactically related?)
3. Call graph (what calls what?)
4. PageRank scoring (what's actually important in this codebase?)

Result: when you ask "fix the order placement bug," ASTra returns not just functions matching "order" but the *entire blast radius* — callers, validators, fee calculators, retry logic — ranked by relevance.

---

## Frequently Asked Questions

**Will this slow down my AI assistant?**
No. Index queries take 30–100ms. You save 10+ seconds of file-reading per task.

**Does it work on huge codebases?**
Yes. Tested on 5,000+ file projects. SQLite handles millions of rows fine. Index size is roughly 1–3% of source size.

**Languages supported?**
Python, JavaScript, TypeScript, JSX, TSX today. Go, Rust, Java planned.

**What if my code changes constantly?**
ASTra has a file watcher. Edit a file → index updates in <100ms. No manual rebuild.

**Does it work offline?**
Yes, after first install (embeddings model is downloaded once, ~80MB).

**Can I use it without an AI assistant?**
Yes. CLI commands (`astra search`, `astra dashboard`) work standalone. Useful for code archaeology in unfamiliar repos.

**How is this different from RAG over my codebase?**
Standard RAG embeds raw text chunks. ASTra embeds *parsed symbols with structural context*. You get function signatures and call graphs, not random text windows. Far higher signal density.

**Does ASTra train on my code?**
No. Nothing is sent anywhere. Embeddings are computed locally and stored in `.astra/graph.db` on your disk.

**Can I delete the index?**
Yes — `rm -rf .astra`. Rebuild with `astra init`. No persistent state outside that folder.

---

## Roadmap

- Go, Rust, Java, C++ parsers
- VS Code extension (visual graph inline with editor)
- Team mode: shared index for monorepos via S3/GCS sync
- IDE annotations: hover any symbol → see PageRank score + caller count
- Diff-aware indexing: re-rank on PRs to highlight new structural relationships

---

## Contributing

PRs welcome. Areas where help is most valuable:
- Adding language parsers (`astra/indexer/parser.py`)
- Improving the dashboard UX
- Benchmarks on more diverse codebases
- Documentation translations

---

## License

MIT. Use it, fork it, sell it.

---

## Credits

Built by Satya Sai Charan. Inspired by years of watching AI assistants burn money reading the same files over and over.

If ASTra saves you tokens, star the repo.
