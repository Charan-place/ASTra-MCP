"""Index a codebase: parse → embed → store in GraphStore."""
import hashlib
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from astra.indexer.parser import parse_file, iter_source_files
from astra.indexer.embedder import embed_texts
from astra.indexer.symbol_table import Symbol
from astra.graph.store import GraphStore

console = Console()


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def index_codebase(root: Path, store: GraphStore, force: bool = False) -> dict:
    """Parse every source file, embed all symbols, write to store."""
    start = time.time()
    files = list(iter_source_files(root))
    stats = {"files_total": len(files), "files_indexed": 0, "symbols": 0, "skipped": 0}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Indexing...", total=len(files))

        for path in files:
            progress.update(task, advance=1, description=f"[blue]{path.name}")
            file_str = str(path)

            # skip unchanged files
            if not force:
                current_hash = _file_hash(path)
                stored_hash = store.get_file_hash(file_str)
                if stored_hash == current_hash:
                    stats["skipped"] += 1
                    continue

            file_syms = parse_file(path)
            if not file_syms or not file_syms.symbols:
                continue

            # embed all symbols in batch
            texts = [s.embed_text for s in file_syms.symbols]
            try:
                embeddings = embed_texts(texts)
            except Exception:
                embeddings = [None] * len(texts)

            # remove old nodes for this file, then insert fresh
            store.delete_file(file_str)

            for sym, emb in zip(file_syms.symbols, embeddings):
                store.upsert_node(sym, emb if emb is not None else None)

            for edge in file_syms.edges:
                store.upsert_edge(edge)

            store.upsert_file_hash(file_str, _file_hash(path))
            stats["files_indexed"] += 1
            stats["symbols"] += len(file_syms.symbols)

    store.commit()
    stats["elapsed_s"] = round(time.time() - start, 2)
    return stats


def index_single_file(path: Path, store: GraphStore) -> int:
    """Re-index one file (called by watcher). Returns symbol count."""
    file_str = str(path)

    if not path.exists():
        store.delete_file(file_str)
        store.commit()
        return 0

    file_syms = parse_file(path)
    if not file_syms:
        return 0

    texts = [s.embed_text for s in file_syms.symbols]
    try:
        embeddings = embed_texts(texts)
    except Exception:
        embeddings = [None] * len(texts)

    store.delete_file(file_str)
    for sym, emb in zip(file_syms.symbols, embeddings):
        store.upsert_node(sym, emb)
    for edge in file_syms.edges:
        store.upsert_edge(edge)

    h = hashlib.md5(path.read_bytes()).hexdigest()
    store.upsert_file_hash(file_str, h)
    store.commit()
    return len(file_syms.symbols)
