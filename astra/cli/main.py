"""ASTra CLI: astra init | status | watch | query | memory | bench"""
import json
import os
import time
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

app = typer.Typer(help="ASTra — AST-powered codebase memory for AI agents")
console = Console()


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
    """Test context retrieval for a task. Shows what gets injected into agent."""
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


@app.command()
def memory(
    action: str = typer.Argument("ls", help="ls | show | save"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    session_id: str = typer.Option(None, "--id"),
    summary: str = typer.Option(None, "--summary", "-s"),
    tags: str = typer.Option("", "--tags"),
):
    """Manage session memory. ls=list, show=detail, save=store new delta."""
    from astra.memory.session import SessionMemory

    root, astra_dir, _ = _resolve_dirs(project)
    mem = SessionMemory(astra_dir / "sessions.db")
    project_str = str(root)

    if action == "ls":
        sessions = mem.list_sessions(project_str)
        if not sessions:
            console.print("[dim]No sessions stored.[/]")
        for s in sessions:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
            console.print(f"[cyan]{s['id'][:8]}[/]  {ts}  {s['summary'][:80]}")

    elif action == "show" and session_id:
        sessions = mem.list_sessions(project_str, limit=100)
        for s in sessions:
            if s["id"].startswith(session_id):
                console.print(s["summary"])
                break

    elif action == "save" and summary:
        sid = session_id or str(uuid.uuid4())[:8]
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        mem.save_session(sid, project_str, summary, tag_list)
        console.print(f"[green]Saved session:[/] {sid}")

    else:
        console.print("Usage: astra memory [ls|show|save] [options]")

    mem.close()


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


def main():
    app()


if __name__ == "__main__":
    main()
