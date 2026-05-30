"""ASTra CLI: astra init | status | watch | query | memory | bench | daemon"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

__version__ = "1.0.0"

app = typer.Typer(
    help="ASTra — AST-powered codebase memory for AI agents",
    no_args_is_help=True,
)
daemon_app = typer.Typer(help="Manage the ASTra live daemon", no_args_is_help=True)
memory_app = typer.Typer(help="Manage session memory", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")
app.add_typer(memory_app, name="memory")
console = Console()


def _version_callback(value: bool):
    if value:
        console.print(f"ASTra v{__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        None, "--version", "-V", callback=_version_callback,
        is_eager=True, help="Show version and exit",
    ),
):
    pass


def _resolve_dirs(project: Path) -> tuple[Path, Path, Path]:
    root = project.resolve()
    astra_dir = root / ".astra"
    astra_dir.mkdir(exist_ok=True)
    return root, astra_dir, astra_dir / "graph.db"


@app.command()
def init(
    project: Path = typer.Argument(Path("."), help="Project root to index"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-index all files"),
):
    """Index codebase: parse AST → embed symbols → build knowledge graph."""
    from astra.graph.store import GraphStore
    from astra.indexer.graph_builder import index_codebase

    root, astra_dir, db_path = _resolve_dirs(project)
    console.print(f"[bold green]ASTra[/] indexing: {root}")

    store = GraphStore(db_path)
    stats = index_codebase(root, store, force=force)
    store.close()

    console.print(f"\n[green]Done[/] in {stats['elapsed_s']}s")
    console.print(f"  Files indexed : {stats['files_indexed']} (skipped {stats['skipped']})")
    console.print(f"  Symbols       : {stats['symbols']}")
    console.print(f"  DB            : {db_path}")
    console.print(f"\n[bold]Next:[/] astra watch  (start MCP server + watcher)")


@app.command()
def status(
    project: Path = typer.Argument(Path("."), help="Project root"),
):
    """Show graph stats: nodes, edges, files indexed."""
    from astra.graph.store import GraphStore

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    store = GraphStore(db_path)
    s = store.stats()
    store.close()

    table = Table(title="ASTra Index Status", show_header=False)
    table.add_row("Nodes (symbols)", str(s["nodes"]))
    table.add_row("Edges (relations)", str(s["edges"]))
    table.add_row("Files indexed", str(s["files"]))
    table.add_row("DB path", str(db_path))
    table.add_row("DB size", f"{round(db_path.stat().st_size/1024,1)} KB")
    console.print(table)


@app.command()
def watch(
    project: Path = typer.Argument(Path("."), help="Project root"),
):
    """Start MCP server (stdio) + file watcher for incremental updates."""
    from astra.graph.store import GraphStore
    from astra.watcher.monitor import start_watcher

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[yellow]Index not found. Running init first...[/]")
        from astra.indexer.graph_builder import index_codebase
        store = GraphStore(db_path)
        index_codebase(root, store)
        store.close()

    os.environ["ASTRA_DATA_DIR"] = str(astra_dir)
    os.environ["ASTRA_PROJECT"] = str(root)

    store = GraphStore(db_path)
    observer = start_watcher(root, store)
    console.print(f"[bold green]ASTra watching:[/] {root}")
    console.print("[dim]Starting MCP server on stdio...[/]")

    from astra.mcp.server import main as run_mcp
    try:
        run_mcp()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        store.close()


@app.command()
def query(
    task: str = typer.Argument(..., help="Task description"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    max_tokens: int = typer.Option(4000, "--max-tokens", "-t"),
    show_tokens: bool = typer.Option(True, "--tokens/--no-tokens"),
):
    """Test context retrieval for a task (cold query — no daemon needed).

    Tip: for faster repeated queries, start the daemon first:
      astra daemon start
      astra daemon query "your task"
    """
    from astra.graph.store import GraphStore
    from astra.query.engine import get_context

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    store = GraphStore(db_path)
    result = get_context(store, task, max_tokens=max_tokens)
    store.close()

    console.print(result["context"])
    if show_tokens:
        console.print(f"\n[dim]── {result['tokens']} tokens | {result['nodes']} symbols | seeds: {result['seeds'][:3]}[/]")


@memory_app.command("ls")
def memory_ls(
    project: Path = typer.Option(Path("."), "--project", "-p", help="Project root"),
):
    """List past sessions for this project."""
    from astra.memory.session import SessionMemory
    root, astra_dir, _ = _resolve_dirs(project)
    mem = SessionMemory(astra_dir / "sessions.db")
    sessions = mem.list_sessions(str(root))
    mem.close()
    if not sessions:
        console.print("[dim]No sessions stored.[/]")
        return
    for s in sessions:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
        console.print(f"[cyan]{s['id'][:8]}[/]  {ts}  {s['summary'][:80]}")


@memory_app.command("show")
def memory_show(
    session_id: str = typer.Argument(..., help="Session ID prefix (from 'astra memory ls')"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
):
    """Show full summary for a session."""
    from astra.memory.session import SessionMemory
    root, astra_dir, _ = _resolve_dirs(project)
    mem = SessionMemory(astra_dir / "sessions.db")
    sessions = mem.list_sessions(str(root), limit=100)
    mem.close()
    for s in sessions:
        if s["id"].startswith(session_id):
            console.print(s["summary"])
            return
    console.print(f"[red]Session not found:[/] {session_id}")
    raise typer.Exit(1)


@memory_app.command("save")
def memory_save(
    summary: str = typer.Argument(..., help="Session summary text"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    session_id: str = typer.Option(None, "--id", help="Custom session ID"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
):
    """Save a new session summary."""
    from astra.memory.session import SessionMemory
    root, astra_dir, _ = _resolve_dirs(project)
    mem = SessionMemory(astra_dir / "sessions.db")
    sid = session_id or str(uuid.uuid4())[:8]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    mem.save_session(sid, str(root), summary, tag_list)
    mem.close()
    console.print(f"[green]Saved session:[/] {sid}")


@app.command()
def bench(
    task: str = typer.Argument("fix authentication bug"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
):
    """Benchmark: tokens with ASTra vs naive full-file read."""
    from astra.graph.store import GraphStore
    from astra.query.engine import get_context
    from astra.indexer.parser import iter_source_files

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    # naive: count tokens from reading all source files as text
    naive_tokens = 0
    file_count = 0
    for path in iter_source_files(root):
        try:
            text = path.read_text(errors="replace")
            naive_tokens += len(text) // 4
            file_count += 1
        except Exception:
            pass

    store = GraphStore(db_path)
    t0 = time.time()
    result = get_context(store, task, max_tokens=8000)
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    store.close()

    astra_tokens = result["tokens"]
    reduction = round((1 - astra_tokens / max(naive_tokens, 1)) * 100, 1)
    savings_cost = round((naive_tokens - astra_tokens) / 1_000_000 * 3.0, 4)  # ~$3/M tokens

    table = Table(title=f'Benchmark: "{task}"', show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Files in codebase", str(file_count))
    table.add_row("Tokens (naive full-read)", f"{naive_tokens:,}")
    table.add_row("Tokens (ASTra context)", f"{astra_tokens:,}")
    table.add_row("[green]Reduction", f"[green]{reduction}%")
    table.add_row("Symbols injected", str(result["nodes"]))
    table.add_row("Query latency", f"{elapsed_ms}ms")
    table.add_row("Cost saved (est. $3/M)", f"${savings_cost}")

    console.print(table)


@app.command()
def dashboard(
    project: Path = typer.Argument(Path("."), help="Project root"),
    port: int = typer.Option(7865, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
):
    """Launch real-time web dashboard (token savings, query history, symbol search)."""
    import webbrowser
    from astra.dashboard.server import run as run_dashboard

    root, astra_dir, db_path = _resolve_dirs(project)
    os.environ["ASTRA_DATA_DIR"] = str(astra_dir)
    os.environ["ASTRA_PROJECT"] = str(root)

    url = f"http://{host}:{port}"
    console.print(f"[bold green]ASTra Dashboard[/] → {url}")

    if open_browser:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    run_dashboard(host=host, port=port)


@app.command()
def federate(
    repos: list[Path] = typer.Argument(None, help="Repo paths to federate"),
    fed_db: Path = typer.Option(None, "--fed-db", help="Federation DB path (default: ~/.astra/federation.db)"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Link multiple repos into a federated knowledge graph. Trace calls across service boundaries."""
    from astra.graph.store import GraphStore
    from astra.federation.resolver import FederatedResolver
    import json

    if not repos:
        console.print("[red]Provide at least one repo path.[/]")
        raise typer.Exit(1)

    fed_db_path = fed_db or (Path.home() / ".astra" / "federation.db")
    fed_db_path.parent.mkdir(exist_ok=True)

    resolver = FederatedResolver(fed_db_path)
    stores = []

    for repo_path in repos:
        root, astra_dir, db_path = _resolve_dirs(repo_path)
        if not db_path.exists():
            console.print(f"[yellow]Skipping {repo_path}: not indexed.[/] Run: astra init {repo_path}")
            continue
        store = GraphStore(db_path)
        stores.append(store)
        repo_id = root.name
        resolver.add_repo(repo_id, root, store)
        console.print(f"Added repo: [cyan]{repo_id}[/]")

    with console.status("[bold]Resolving cross-repo links...[/]"):
        fed_graph = resolver.link_all()

    for s in stores:
        s.close()
    resolver.close()

    if json_out:
        console.print(json.dumps(fed_graph.to_dict(), indent=2))
        raise typer.Exit(0)

    console.print(f"\n[bold green]Federation complete:[/]")
    console.print(f"  Repos federated : {len(fed_graph.repos)}")
    console.print(f"  Total nodes     : {fed_graph.nodes}")
    console.print(f"  Cross-repo edges: {len(fed_graph.cross_edges)}")

    if fed_graph.cross_edges:
        console.print(f"\n[bold]Cross-repo links found:[/]")
        table = Table(show_header=True)
        table.add_column("From repo")
        table.add_column("To repo")
        table.add_column("Link type")
        table.add_column("Confidence", justify="right")
        for e in fed_graph.cross_edges[:15]:
            table.add_row(e.src_repo, e.dst_repo, e.link_type, f"{e.confidence:.2f}")
        console.print(table)


@app.command()
def timeline(
    project: Path = typer.Argument(Path("."), help="Project root (must be a git repo)"),
    max_commits: int = typer.Option(200, "--max-commits", "-n", help="Max commits to analyze"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Build temporal graph from git history. Reveals which functions change most."""
    try:
        import git  # noqa: F401
    except ImportError:
        console.print(
            "[red]Missing dependency:[/] gitpython is required for timeline.\n"
            "Install it with:\n\n"
            "  [bold]pip install gitpython[/]\n"
        )
        raise typer.Exit(1)

    from astra.graph.store import GraphStore
    from astra.temporal.indexer import TemporalIndexer
    import json

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    store = GraphStore(db_path)
    indexer = TemporalIndexer(store)

    with console.status(f"[bold]Replaying {max_commits} commits...[/]"):
        summary = indexer.build_timeline(root, max_commits=max_commits)

    store.close()

    if json_out:
        console.print(json.dumps(summary.to_dict(), indent=2))
        raise typer.Exit(0)

    console.print(f"\n[bold green]Temporal index built:[/]")
    console.print(f"  Commits processed : {summary.commits_processed}")
    console.print(f"  Nodes tracked     : {summary.nodes_tracked}")
    console.print(f"  Edges tracked     : {summary.edges_tracked}")
    console.print(f"  Time              : {summary.elapsed_s}s")

    if summary.top_volatile:
        console.print(f"\n[bold yellow]Top volatile nodes (change most often):[/]")
        table = Table(show_header=True)
        table.add_column("Function", style="cyan")
        table.add_column("File")
        table.add_column("Changes", justify="right")
        table.add_column("Volatility", justify="right")
        for n in summary.top_volatile[:10]:
            table.add_row(
                n.name,
                f"{Path(n.file).name}",
                str(n.change_count),
                f"{n.volatility:.3f}",
            )
        console.print(table)


@app.command()
def audit(
    project: Path = typer.Argument(Path("."), help="Project root"),
    file: str = typer.Option(None, "--file", "-f", help="Scan only this file"),
    threshold: float = typer.Option(0.35, "--threshold", "-t", help="Drift threshold 0-1"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Scan for semantic drift: functions whose behavior doesn't match their name."""
    from astra.graph.store import GraphStore
    from astra.semantics.drift import SemanticDriftDetector
    import json

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    store = GraphStore(db_path)
    detector = SemanticDriftDetector(store, threshold=threshold)

    with console.status("[bold]Scanning for semantic drift...[/]"):
        warnings = detector.scan(file_filter=file)

    store.close()

    if not warnings:
        console.print("[green]No semantic drift detected.[/]")
        raise typer.Exit(0)

    if json_out:
        console.print(json.dumps([w.to_dict() for w in warnings], indent=2))
        raise typer.Exit(0)

    console.print(f"\n[bold red]Found {len(warnings)} semantic drift warning(s):[/]\n")
    table = Table(show_header=True)
    table.add_column("Function", style="cyan")
    table.add_column("File")
    table.add_column("Drift", justify="right")
    table.add_column("Calls")

    for w in warnings[:20]:
        callee_str = ", ".join(w.actual_callees[:3])
        if len(w.actual_callees) > 3:
            callee_str += f" +{len(w.actual_callees)-3}"
        table.add_row(
            w.name,
            f"{Path(w.file).name}:{w.line}",
            f"{w.drift_score:.2f}",
            callee_str,
        )

    console.print(table)
    if len(warnings) > 20:
        console.print(f"[dim]... and {len(warnings)-20} more. Use --json for full output.[/]")


@app.command()
def impact(
    names: list[str] = typer.Argument(None, help="Function/class names to analyze"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    diff: bool = typer.Option(False, "--diff", help="Read unified diff from stdin"),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON"),
):
    """Compute blast radius: which functions break if these change."""
    import sys
    from astra.graph.store import GraphStore
    from astra.impact.analyzer import ImpactAnalyzer

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[red]Not indexed.[/] Run: astra init")
        raise typer.Exit(1)

    store = GraphStore(db_path)
    analyzer = ImpactAnalyzer(store)

    if diff:
        diff_text = sys.stdin.read()
        report = analyzer.compute_from_diff(diff_text)
    elif names:
        node_ids = []
        for name in names:
            candidates = store.get_nodes_by_name(name)
            node_ids.extend(c["id"] for c in candidates)
        if not node_ids:
            console.print(f"[yellow]No indexed nodes found for: {names}[/]")
            raise typer.Exit(1)
        report = analyzer.compute_blast_radius(node_ids)
    else:
        console.print("[red]Provide function names or use --diff[/]")
        raise typer.Exit(1)

    store.close()

    if json_out:
        import json
        console.print(json.dumps(report.to_dict(), indent=2))
    else:
        console.print(report.to_text())


@daemon_app.command("start")
def daemon_start(
    project: Path = typer.Argument(Path("."), help="Project root to watch"),
    background: bool = typer.Option(True, "--bg/--fg", help="Run in background"),
):
    """Start ASTra live daemon (persistent graph + socket server)."""
    from astra.daemon.core import is_daemon_running, PID_PATH, SOCKET_PATH

    if is_daemon_running():
        console.print("[yellow]Daemon already running.[/] Use: astra daemon status")
        raise typer.Exit(0)

    root, astra_dir, db_path = _resolve_dirs(project)
    if not db_path.exists():
        console.print("[yellow]Index not found. Running init first...[/]")
        from astra.graph.store import GraphStore
        from astra.indexer.graph_builder import index_codebase
        store = GraphStore(db_path)
        index_codebase(root, store)
        store.close()

    if background:
        log_path = astra_dir / "daemon.log"
        with open(log_path, "a") as log_f:
            proc = subprocess.Popen(
                [sys.executable, "-m", "astra.daemon.runner",
                 "--repo", str(root), "--db", str(db_path)],
                stdout=log_f, stderr=log_f,
                start_new_session=True,
            )
        # wait up to 3s for daemon to be ready
        for _ in range(30):
            time.sleep(0.1)
            if is_daemon_running():
                break
        if is_daemon_running():
            console.print(f"[bold green]Daemon started[/] (PID {proc.pid})")
            console.print(f"  Socket : {SOCKET_PATH}")
            console.print(f"  Log    : {log_path}")
            console.print(f"  Stop   : astra daemon stop")
        else:
            console.print(f"[red]Daemon failed to start.[/] Check log: {log_path}")
    else:
        # foreground — blocking
        import logging
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from astra.daemon.core import AstraDaemon
        d = AstraDaemon(root, db_path)
        d.start()


@daemon_app.command("stop")
def daemon_stop():
    """Stop the running ASTra daemon."""
    from astra.daemon.core import PID_PATH
    if not PID_PATH.exists():
        console.print("[yellow]No daemon PID found.[/]")
        raise typer.Exit(1)
    pid = int(PID_PATH.read_text().strip())
    try:
        os.kill(pid, 15)  # SIGTERM
        console.print(f"[green]Daemon stopped[/] (PID {pid})")
    except ProcessLookupError:
        console.print(f"[yellow]PID {pid} not running.[/] Cleaning up.")
        PID_PATH.unlink(missing_ok=True)


@daemon_app.command("status")
def daemon_status():
    """Show daemon status and live graph stats."""
    from astra.daemon.core import DaemonClient, SOCKET_PATH, PID_PATH

    client = DaemonClient()
    if not client.ping():
        console.print("[red]Daemon not running.[/] Start with: astra daemon start")
        raise typer.Exit(1)

    resp = client.status()
    if not resp.get("ok"):
        console.print(f"[red]Error:[/] {resp.get('error')}")
        raise typer.Exit(1)

    d = resp["data"]
    table = Table(title="ASTra Daemon Status", show_header=False)
    table.add_row("Status", "[green]running[/]")
    table.add_row("Uptime", f"{d['uptime_s']}s")
    table.add_row("Graph nodes", str(d["graph_nodes"]))
    table.add_row("DB nodes", str(d["nodes"]))
    table.add_row("DB edges", str(d["edges"]))
    table.add_row("DB files", str(d["files"]))
    table.add_row("Socket", str(SOCKET_PATH))
    if PID_PATH.exists():
        table.add_row("PID", PID_PATH.read_text().strip())
    if d.get("last_delta_ts"):
        ts = time.strftime("%H:%M:%S", time.localtime(d["last_delta_ts"]))
        table.add_row("Last delta", ts)
    console.print(table)


@daemon_app.command("query")
def daemon_query(
    task: str = typer.Argument(..., help="Task description"),
    max_tokens: int = typer.Option(4000, "--max-tokens"),
):
    """Query the live daemon (~20ms vs ~150ms cold). Graph stays in memory between calls.

    vs 'astra query': daemon query skips model reload and graph rebuild every time.
    Start daemon once with 'astra daemon start', then use this for all queries.
    """
    from astra.daemon.core import DaemonClient

    client = DaemonClient()
    if not client.ping():
        console.print("[red]Daemon not running.[/] Start with: astra daemon start")
        raise typer.Exit(1)

    t0 = time.time()
    resp = client.query(task, max_tokens=max_tokens)
    elapsed_ms = round((time.time() - t0) * 1000, 1)

    if not resp.get("ok"):
        console.print(f"[red]Error:[/] {resp.get('error')}")
        raise typer.Exit(1)

    d = resp["data"]
    console.print(d["context"])
    console.print(f"\n[dim]── {d['tokens']} tokens | {d['nodes']} symbols | {elapsed_ms}ms (daemon)[/]")


def main():
    app()


if __name__ == "__main__":
    main()
