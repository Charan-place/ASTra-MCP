"""
Benchmark: ASTra token reduction vs naive full codebase read.
Run: python benchmarks/token_reduction.py <project_root> "<task>"
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from astra.graph.store import GraphStore
from astra.query.engine import get_context
from astra.indexer.parser import iter_source_files


TASKS = [
    "fix authentication token expiry bug",
    "add pagination to user list endpoint",
    "refactor database connection pooling",
    "add unit tests for payment processing",
    "debug slow API response times",
]


def run_benchmark(project_root: Path, task: str):
    astra_dir = project_root / ".astra"
    db_path = astra_dir / "graph.db"

    if not db_path.exists():
        print(f"ERROR: {db_path} not found. Run: astra init {project_root}")
        return

    # naive token count: read every source file as text
    naive_tokens = 0
    file_count = 0
    for path in iter_source_files(project_root):
        try:
            text = path.read_text(errors="replace")
            naive_tokens += len(text) // 4
            file_count += 1
        except Exception:
            pass

    # astra context
    store = GraphStore(db_path)
    t0 = time.perf_counter()
    result = get_context(store, task, max_tokens=8000)
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    store.close()

    astra_tokens = result["tokens"]
    reduction_pct = round((1 - astra_tokens / max(naive_tokens, 1)) * 100, 1)
    cost_saved = round((naive_tokens - astra_tokens) / 1_000_000 * 3.0, 5)

    print(f"\n{'='*55}")
    print(f"Task:            {task}")
    print(f"Files:           {file_count}")
    print(f"Naive tokens:    {naive_tokens:,}")
    print(f"ASTra tokens:    {astra_tokens:,}")
    print(f"Reduction:       {reduction_pct}%")
    print(f"Symbols:         {result['nodes']}")
    print(f"Latency:         {latency_ms}ms")
    print(f"Cost saved:      ${cost_saved} per session (@$3/M tokens)")
    print(f"{'='*55}")

    return reduction_pct


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    task = sys.argv[2] if len(sys.argv) > 2 else TASKS[0]
    run_benchmark(root, task)
