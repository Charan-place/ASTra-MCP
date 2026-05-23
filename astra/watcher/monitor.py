"""File watcher: detect changes → re-index only changed files."""
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
from watchdog.observers import Observer

from astra.indexer.parser import SUPPORTED, SKIP_DIRS
from astra.indexer.graph_builder import index_single_file
from astra.graph.store import GraphStore
from astra.query.engine import invalidate_graph_cache


class _AstraHandler(FileSystemEventHandler):
    def __init__(self, store: GraphStore):
        self.store = store

    def _should_handle(self, path_str: str) -> bool:
        p = Path(path_str)
        if p.suffix.lower() not in SUPPORTED:
            return False
        return not any(skip in p.parts for skip in SKIP_DIRS)

    def on_modified(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self._reindex(Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self._reindex(Path(event.src_path))

    def on_deleted(self, event):
        if not event.is_directory and self._should_handle(event.src_path):
            self.store.delete_file(event.src_path)
            self.store.commit()
            invalidate_graph_cache(self.store)

    def _reindex(self, path: Path):
        count = index_single_file(path, self.store)
        invalidate_graph_cache(self.store)


def start_watcher(root: Path, store: GraphStore) -> Observer:
    """Start background file watcher. Returns observer (call .stop() to halt)."""
    handler = _AstraHandler(store)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    return observer
