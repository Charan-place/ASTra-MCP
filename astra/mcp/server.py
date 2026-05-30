"""MCP server. Exposes 11 tools to Claude Code, Codex, Cursor via stdio."""
import asyncio
import json
import logging
import os
import traceback
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ListToolsResult,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("astra.mcp")

from astra.graph.store import GraphStore
from astra.memory.session import SessionMemory
from astra.indexer.graph_builder import index_codebase
from astra.mcp.tools import (
    tool_get_context,
    tool_search,
    tool_get_callers,
    tool_get_callees,
    tool_get_file_map,
    tool_session_memory,
    tool_index_status,
    tool_impact_analysis,
    tool_semantic_audit,
    tool_get_volatility,
    tool_trace_cross_repo,
)

_TOOLS = [
    Tool(
        name="astra_get_context",
        description=(
            "CALL THIS FIRST before any coding task. "
            "Converts a natural-language task description into the minimal relevant "
            "code context from the indexed codebase. "
            "Returns signatures and docstrings only — no full file reads needed. "
            "Cuts token usage 60-80% vs reading files directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What you are about to do"},
                "max_tokens": {"type": "integer", "default": 4000, "description": "Token budget for context"},
            },
            "required": ["task"],
        },
    ),
    Tool(
        name="astra_search",
        description="Semantic search for functions, classes, or modules by name or meaning.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="astra_get_callers",
        description="Find all functions that call a given function. Use before changing a function signature.",
        inputSchema={
            "type": "object",
            "properties": {
                "function_name": {"type": "string"},
                "file": {"type": "string", "description": "Optional: narrow to specific file"},
            },
            "required": ["function_name"],
        },
    ),
    Tool(
        name="astra_get_callees",
        description="Find all functions called by a given function. Use to understand dependencies.",
        inputSchema={
            "type": "object",
            "properties": {
                "function_name": {"type": "string"},
                "file": {"type": "string"},
            },
            "required": ["function_name"],
        },
    ),
    Tool(
        name="astra_get_file_map",
        description="Get all symbols in a file with signatures only. Use before editing a file.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Absolute or relative file path"},
            },
            "required": ["file"],
        },
    ),
    Tool(
        name="astra_session_memory",
        description="Recall what was done in past sessions relevant to the current task.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you are working on"},
                "project": {"type": "string", "description": "Project root path"},
            },
            "required": ["query", "project"],
        },
    ),
    Tool(
        name="astra_index_status",
        description="Check index freshness: how many files, symbols, and edges are indexed.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="astra_impact_analysis",
        description=(
            "Blast radius analysis: what breaks if these functions change? "
            "Returns risk score (0-100), list of affected callers, and untested high-risk nodes. "
            "Use before editing a critical function."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "function_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of functions/classes you plan to change",
                },
                "file": {"type": "string", "description": "Optional: narrow to specific file"},
            },
            "required": ["function_names"],
        },
    ),
    Tool(
        name="astra_semantic_audit",
        description=(
            "Scan for semantic drift: functions whose name/docstring doesn't match their behavior. "
            "Use before a major refactor to find misnamed or misleading functions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Optional: scan only this file"},
                "threshold": {
                    "type": "number",
                    "default": 0.35,
                    "description": "Drift score threshold 0-1 (lower = stricter)",
                },
            },
        },
    ),
    Tool(
        name="astra_get_volatility",
        description=(
            "Get temporal volatility: which functions change most often (from git history). "
            "Requires astra timeline to have been run first. "
            "High volatility = high risk, consider extra tests before changing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "function_name": {
                    "type": "string",
                    "description": "Optional: history for a specific function. Omit for top-k across codebase.",
                },
                "top_k": {"type": "integer", "default": 10},
            },
        },
    ),
    Tool(
        name="astra_trace_cross_repo",
        description=(
            "Trace a function call across repository boundaries. "
            "Requires: astra federate to have been run first. "
            "Returns full call chain spanning multiple microservices/repos."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Function to trace"},
                "fed_db_path": {
                    "type": "string",
                    "description": "Optional: path to federation.db (default: ~/.astra/federation.db)",
                },
            },
            "required": ["function_name"],
        },
    ),
]


def _get_store() -> GraphStore:
    astra_dir = Path(os.environ.get("ASTRA_DATA_DIR", ".astra"))
    astra_dir.mkdir(exist_ok=True)
    return GraphStore(astra_dir / "graph.db")


def _get_memory(store: GraphStore) -> SessionMemory:
    astra_dir = Path(store.db_path).parent
    return SessionMemory(astra_dir / "sessions.db")


def _project() -> str:
    return os.environ.get("ASTRA_PROJECT", str(Path.cwd()))


def _auto_index_if_empty(store: GraphStore, project: str):
    """Index codebase on first launch if DB is empty."""
    stats = store.stats()
    if stats["nodes"] == 0:
        logger.info("Index empty — auto-indexing %s ...", project)
        try:
            result = index_codebase(Path(project), store)
            logger.info("Auto-index done: %d files, %d symbols", result["files"], result["symbols"])
        except Exception as e:
            logger.warning("Auto-index failed: %s", e)


async def run_server():
    server = Server("astra-mcp")
    store = _get_store()
    memory = _get_memory(store)
    project = _project()
    logger.info("ASTra MCP server starting. project=%s data_dir=%s", project, store.db_path)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _auto_index_if_empty, store, project)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        logger.info("Tool called: %s args=%s", name, list(arguments.keys()))
        try:
            if name == "astra_get_context":
                result = tool_get_context(store, arguments["task"], arguments.get("max_tokens", 4000))
                text = json.dumps(result, indent=2)

            elif name == "astra_search":
                result = tool_search(store, arguments["query"], arguments.get("top_k", 10))
                text = json.dumps(result, indent=2)

            elif name == "astra_get_callers":
                result = tool_get_callers(store, arguments["function_name"], arguments.get("file"))
                text = json.dumps(result, indent=2)

            elif name == "astra_get_callees":
                result = tool_get_callees(store, arguments["function_name"], arguments.get("file"))
                text = json.dumps(result, indent=2)

            elif name == "astra_get_file_map":
                text = tool_get_file_map(store, arguments["file"])

            elif name == "astra_session_memory":
                text = tool_session_memory(memory, arguments["query"], arguments.get("project", project))

            elif name == "astra_index_status":
                result = tool_index_status(store)
                text = json.dumps(result, indent=2)

            elif name == "astra_impact_analysis":
                fn_names = arguments.get("function_names")
                if not fn_names or not isinstance(fn_names, list):
                    text = json.dumps({"error": "function_names must be a non-empty list of strings"})
                else:
                    result = tool_impact_analysis(store, fn_names, arguments.get("file"))
                    text = json.dumps(result, indent=2)

            elif name == "astra_semantic_audit":
                result = tool_semantic_audit(
                    store,
                    file=arguments.get("file"),
                    threshold=float(arguments.get("threshold", 0.35)),
                )
                text = json.dumps(result, indent=2)

            elif name == "astra_get_volatility":
                result = tool_get_volatility(
                    store,
                    function_name=arguments.get("function_name"),
                    top_k=int(arguments.get("top_k", 10)),
                )
                text = json.dumps(result, indent=2)

            elif name == "astra_trace_cross_repo":
                fn_name = arguments.get("function_name")
                if not fn_name:
                    text = json.dumps({"error": "function_name is required"})
                else:
                    result = tool_trace_cross_repo(
                        store,
                        fn_name,
                        fed_db_path=arguments.get("fed_db_path"),
                    )
                    text = json.dumps(result, indent=2)

            else:
                text = json.dumps({"error": f"Unknown tool: {name}. Available: {[t.name for t in _TOOLS]}"})

        except KeyError as e:
            logger.error("Missing required argument for %s: %s", name, e)
            text = json.dumps({"error": f"Missing required argument: {e}", "tool": name})
        except Exception as e:
            logger.error("Tool %s failed: %s\n%s", name, e, traceback.format_exc())
            text = json.dumps({"error": str(e), "type": type(e).__name__, "tool": name})

        return [TextContent(type="text", text=text)]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point. Crash log writes to ~/.astra-mcp/crash.log for diagnostics."""
    import sys, traceback
    try:
        asyncio.run(run_server())
    except Exception as e:
        try:
            log_dir = Path.home() / ".astra-mcp"
            log_dir.mkdir(exist_ok=True)
            log = log_dir / "crash.log"
            with log.open("a") as f:
                f.write(f"\n=== CRASH {os.environ.get('USER','?')} {sys.version} ===\n")
                f.write(f"argv: {sys.argv}\n")
                f.write(f"cwd: {os.getcwd()}\n")
                f.write(f"err: {e}\n")
                f.write(traceback.format_exc())
            sys.stderr.write(f"\n[ASTra crashed] See {log} for details.\n")
        except Exception:
            sys.stderr.write(f"[ASTra crashed] {e}\n{traceback.format_exc()}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
