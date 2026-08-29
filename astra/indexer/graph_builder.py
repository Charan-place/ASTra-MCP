"""Index a codebase: parse → embed → store in GraphStore."""
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from astra.indexer.parser import parse_file, iter_source_files
from astra.indexer.embedder import embed_texts
from astra.indexer.symbol_table import Symbol, Edge
from astra.graph.store import GraphStore

console = Console()
logger = logging.getLogger("astra.indexer")


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def _update_ann_index(store: GraphStore, ids: list[str], embeddings: list) -> None:
    """Incrementally update the on-disk HNSW index, if the hnsw backend is enabled.

    Best-effort: any failure (e.g. hnswlib not installed) is logged and ignored,
    since the brute-force path in astra/query/engine.py always remains correct.
    """
    if os.environ.get("ASTRA_INDEX_BACKEND", "brute").lower() != "hnsw":
        return
    pairs = [(nid, emb) for nid, emb in zip(ids, embeddings) if emb is not None]
    if not pairs:
        return
    try:
        from astra.graph.ann_index import HNSWIndex

        index_path = store.db_path.parent / "hnsw.index"
        meta_path = index_path.with_suffix(index_path.suffix + ".meta.npy")
        idx = HNSWIndex()
        if index_path.exists() and meta_path.exists():
            idx.load(index_path)
        for nid, emb in pairs:
            idx.add(nid, emb)
        idx.save(index_path)

        # invalidate the query engine's in-memory cache so it reloads from disk
        from astra.query import engine as _engine
        _engine._ann_index_cache.pop(str(store.db_path), None)
    except Exception:
        logger.warning("Failed to update HNSW index incrementally", exc_info=True)


def _resolve_cross_file_calls(store: GraphStore) -> int:
    """
    Second pass: add CALLS edges for cross-file calls.
    Uses stored calls_json on each node to find callees in other files.
    Returns count of new edges added.
    """
    name_to_ids = store.get_name_to_ids()
    symbol_calls = store.get_all_symbol_calls()
    added = 0
    for row in symbol_calls:
        calls = json.loads(row["calls_json"])
        src_file = row["file"]
        src_id = row["id"]
        for callee_name in calls:
            if callee_name not in name_to_ids:
                continue
            for callee_id, callee_file in name_to_ids[callee_name]:
                if callee_file != src_file:
                    store.upsert_edge(Edge(src=src_id, dst=callee_id, relation="CALLS"))
                    added += 1
    return added


def index_codebase(root: Path, store: GraphStore, force: bool = False) -> dict:
    """Parse every source file, embed all symbols, write to store."""
    start = time.time()
    if force:
        with store._lock:
            store.conn.execute("DELETE FROM nodes")
            store.conn.execute("DELETE FROM edges")
            store.conn.execute("DELETE FROM file_hashes")
            store.conn.commit()
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

    # cross-file CALLS resolution (second pass over stored calls_json)
    cross_edges = _resolve_cross_file_calls(store)
    store.commit()

    # full corpus scan (initial build or `--force`) → rebuild the ANN index once
    if os.environ.get("ASTRA_INDEX_BACKEND", "brute").lower() == "hnsw":
        try:
            from astra.graph.ann_index import HNSWIndex
            import numpy as np

            corpus = store.all_embeddings()
            if corpus:
                ids, vecs = zip(*corpus)
                idx = HNSWIndex()
                idx.build(list(ids), np.stack(vecs))
                idx.save(store.db_path.parent / "hnsw.index")
                from astra.query import engine as _engine
                _engine._ann_index_cache.pop(str(store.db_path), None)
        except Exception:
            logger.warning("Failed to (re)build HNSW index", exc_info=True)

    stats["elapsed_s"] = round(time.time() - start, 2)
    stats["cross_file_edges"] = cross_edges
    logger.info(
        "Indexed %d files (%d skipped), %d symbols, %d cross-file edges in %.2fs",
        stats["files_indexed"], stats["skipped"], stats["symbols"], cross_edges, stats["elapsed_s"],
    )
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

    # resolve cross-file calls for this file only
    name_to_ids = store.get_name_to_ids()
    for sym in file_syms.symbols:
        for callee_name in sym.calls:
            if callee_name in name_to_ids:
                for callee_id, callee_file in name_to_ids[callee_name]:
                    if callee_file != file_str:
                        store.upsert_edge(Edge(src=sym.id, dst=callee_id, relation="CALLS"))

    store.commit()
    _update_ann_index(store, [s.id for s in file_syms.symbols], list(embeddings))
    logger.debug("Re-indexed %s: %d symbols", file_str, len(file_syms.symbols))
    return len(file_syms.symbols)
