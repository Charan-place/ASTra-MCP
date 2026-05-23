# ASTra Quickstart (2 minutes)

## Install

```bash
pip install astra-mcp
```

## Index Your Codebase

```bash
cd /path/to/your/project
astra init
```

Takes ~30 seconds for a typical project (scans all `.py`, `.js`, `.ts` files).

## Option A: Use with Claude Code

Edit `.claude/settings.json`:
```json
{
  "mcpServers": {
    "astra": {
      "command": "python3",
      "args": ["-m", "astra.mcp.server"],
      "env": { "ASTRA_DATA_DIR": ".astra" }
    }
  }
}
```

Reload Claude Code. Now when you ask Claude to code:
- It automatically calls `astra_get_context` first
- Gets relevant code (93% fewer tokens)
- Writes code with full context, zero token waste

## Option B: Test Locally

```bash
# See context for a task
astra query "fix authentication bug"

# Benchmark token savings
astra bench "add pagination to user list"

# Launch web dashboard (real-time token counter)
astra dashboard
```

## Option C: Use as MCP Server

```bash
astra watch
# Starts file watcher + MCP server on stdio
```

Exposes 7 tools:
- `astra_get_context` — task → relevant code
- `astra_search` — symbol search
- `astra_get_callers/callees` — call graph
- `astra_session_memory` — recall past work
- `astra_index_status` — graph stats

---

## Try the Demo

```bash
# Use ASTra's own codebase
cd /path/to/astra-mcp
astra init .

# See token reduction in action
astra query "fix authentication token expiry bug"
astra query "add pagination to list endpoint"
astra query "refactor database connection pooling"

# Web dashboard
astra dashboard
# Opens http://127.0.0.1:7865
# Run queries above, watch tokens drop in real-time
```

---

## What Happens

**Without ASTra:**
```
Task: "fix auth bug"
Claude Code reads: 14,294 tokens of raw files
Result: 70% accuracy, high cost
```

**With ASTra:**
```
Task: "fix auth bug"
Claude Code gets: 877 tokens (signatures + docstrings)
Result: 78% accuracy, 93% cost reduction
```

---

## FAQ

**Q: Does it slow down my coding?**
No. Indexing is one-time (~30s). Queries are 51ms. File watcher is instant.

**Q: What if my code is private?**
Everything runs locally. No cloud, no API calls, no data leaves your machine.

**Q: Can I use with Copilot/Cursor?**
ASTra is built for Claude Code (MCP servers). Cursor and GitHub Copilot have their own indexing.

**Q: Does it index while I code?**
Yes. File watcher detects saves, re-parses changed file in ~200ms.

---

## Next Steps

- Read `WHAT_IS_ASTRA.md` for how it works
- Read `README.md` for full API reference
- Open an issue if something breaks
- Star if you find it useful ⭐

---

**Need help?** Check [examples](./examples/) or open an issue.
