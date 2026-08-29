"""Git pre-commit hook installer/uninstaller for ASTra.

Installs a `pre-commit` script into the current project's `.git/hooks/`
directory that re-indexes staged source files with ASTra's incremental
single-file indexer, keeping the knowledge graph fresh without requiring
the daemon or watcher to be running.
"""
from pathlib import Path

_MARKER = "# ASTRA-MANAGED-PRE-COMMIT-HOOK"

_HOOK_TEMPLATE = f"""#!/bin/sh
{_MARKER}
# Installed by `astra hooks install`. Re-indexes staged source files.

astra hooks run-staged
exit 0
"""


class HooksInstallError(Exception):
    """Raised when the pre-commit hook cannot be installed/uninstalled."""


def _git_hooks_dir(project: Path) -> Path:
    git_dir = project / ".git"
    if not git_dir.exists() or not git_dir.is_dir():
        raise HooksInstallError(
            f"No .git directory found in {project}. Run this inside a git repository."
        )
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def install_hook(project: Path = Path(".")) -> Path:
    """Install the ASTra pre-commit hook into project's .git/hooks/.

    If a pre-commit hook already exists and was not installed by ASTra,
    it is backed up to `pre-commit.backup` before being overwritten.

    Returns the path to the installed hook.
    """
    project = project.resolve()
    hooks_dir = _git_hooks_dir(project)
    hook_path = hooks_dir / "pre-commit"
    backup_path = hooks_dir / "pre-commit.backup"

    if hook_path.exists():
        existing = hook_path.read_text(errors="replace")
        if _MARKER not in existing:
            backup_path.write_text(existing)

    hook_path.write_text(_HOOK_TEMPLATE)
    hook_path.chmod(0o755)
    return hook_path


def uninstall_hook(project: Path = Path(".")) -> bool:
    """Remove the ASTra pre-commit hook, restoring a prior backup if present.

    Returns True if a hook was removed/restored, False if nothing to do.
    """
    project = project.resolve()
    hooks_dir = _git_hooks_dir(project)
    hook_path = hooks_dir / "pre-commit"
    backup_path = hooks_dir / "pre-commit.backup"

    if not hook_path.exists():
        return False

    existing = hook_path.read_text(errors="replace")
    if _MARKER not in existing:
        # Not our hook; leave it alone.
        return False

    hook_path.unlink()

    if backup_path.exists():
        backup_path.rename(hook_path)
        hook_path.chmod(0o755)

    return True


def run_staged_reindex(project: Path = Path(".")) -> int:
    """Re-index all staged source files. Returns count of files re-indexed.

    Intended to be called by the installed git pre-commit hook itself
    (`astra hooks run-staged`), not directly by users.
    """
    import subprocess

    from astra.indexer.parser import SUPPORTED, SKIP_DIRS
    from astra.indexer.graph_builder import index_single_file
    from astra.graph.store import GraphStore

    project = project.resolve()
    astra_dir = project / ".astra"
    db_path = astra_dir / "graph.db"
    if not db_path.exists():
        # Nothing indexed yet; nothing to keep fresh.
        return 0

    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    staged_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    targets = []
    for rel in staged_files:
        p = project / rel
        if p.suffix.lower() not in SUPPORTED:
            continue
        if any(skip in p.parts for skip in SKIP_DIRS):
            continue
        targets.append(p)

    if not targets:
        return 0

    store = GraphStore(db_path)
    try:
        for path in targets:
            index_single_file(path, store)
    finally:
        store.close()

    return len(targets)
