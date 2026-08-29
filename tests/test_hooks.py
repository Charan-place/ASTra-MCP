"""Tests for the ASTra git pre-commit hook installer."""
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from astra.hooks.installer import install_hook, uninstall_hook, HooksInstallError, _MARKER


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _is_executable(path: Path) -> bool:
    mode = path.stat().st_mode
    return bool(mode & stat.S_IXUSR and mode & stat.S_IXGRP and mode & stat.S_IXOTH)


def test_install_no_prior_hook(tmp_path):
    repo = _init_repo(tmp_path)

    hook_path = install_hook(repo)

    assert hook_path.exists()
    assert hook_path == repo / ".git" / "hooks" / "pre-commit"
    content = hook_path.read_text()
    assert "astra" in content.lower()
    assert _MARKER in content
    assert _is_executable(hook_path)
    # no backup should have been created since there was no prior hook
    assert not (repo / ".git" / "hooks" / "pre-commit.backup").exists()


def test_uninstall_no_prior_hook(tmp_path):
    repo = _init_repo(tmp_path)
    install_hook(repo)

    removed = uninstall_hook(repo)

    assert removed is True
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    assert not hook_path.exists()
    assert not (repo / ".git" / "hooks" / "pre-commit.backup").exists()


def test_install_backs_up_existing_hook(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/sh\necho 'my custom hook'\n")
    existing_hook.chmod(0o755)

    install_hook(repo)

    backup_path = hooks_dir / "pre-commit.backup"
    assert backup_path.exists()
    assert "my custom hook" in backup_path.read_text()

    hook_path = hooks_dir / "pre-commit"
    assert _MARKER in hook_path.read_text()
    assert _is_executable(hook_path)


def test_uninstall_restores_prior_hook(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    original_content = "#!/bin/sh\necho 'my custom hook'\n"
    existing_hook.write_text(original_content)
    existing_hook.chmod(0o755)

    install_hook(repo)
    removed = uninstall_hook(repo)

    assert removed is True
    hook_path = hooks_dir / "pre-commit"
    assert hook_path.exists()
    assert hook_path.read_text() == original_content
    assert _is_executable(hook_path)
    assert not (hooks_dir / "pre-commit.backup").exists()


def test_install_no_git_dir_raises(tmp_path):
    with pytest.raises(HooksInstallError):
        install_hook(tmp_path)


def test_uninstall_no_git_dir_raises(tmp_path):
    with pytest.raises(HooksInstallError):
        uninstall_hook(tmp_path)


def test_uninstall_leaves_foreign_hook_alone(tmp_path):
    """If a non-ASTra hook exists and install was never run, uninstall is a no-op."""
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/sh\necho 'unrelated hook'\n")
    existing_hook.chmod(0o755)

    removed = uninstall_hook(repo)

    assert removed is False
    assert "unrelated hook" in existing_hook.read_text()
