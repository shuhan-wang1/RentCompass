"""Behavioural tests for the recursive persistent-runtime permission gate."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "preflight_runtime_permissions.sh"
RUNTIME_ROOTS = (
    ".runtime",
    "chroma_db",
    "chroma_db_area",
    "app/chroma_db_agent_memory",
    "app/data",
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("find") is None,
    reason="runtime permission gate requires bash and GNU/POSIX find",
)


def _make_repo(path: Path) -> Path:
    for relative in RUNTIME_ROOTS:
        (path / relative).mkdir(parents=True)
    return path


def _run(repo: Path, *args: str, sudo_cmd: Path | None = None):
    env = {
        **os.environ,
        "RUNTIME_PREFLIGHT_REPO": str(repo),
        "RUNTIME_PREFLIGHT_UID": str(os.getuid()),
        "RUNTIME_PREFLIGHT_GID": str(os.getgid()),
    }
    if sudo_cmd is not None:
        env["RUNTIME_PREFLIGHT_SUDO_CMD"] = str(sudo_cmd)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_recursive_audit_accepts_owner_writable_nested_state(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / ".runtime" / "logs" / "canary.jsonl"
    nested.parent.mkdir()
    nested.write_text("ok\n", encoding="utf-8")

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert "owned and owner-writable" in result.stdout


def test_recursive_audit_reports_a_nested_nonwritable_file(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / ".runtime" / "checkpoints_fc.sqlite3"
    nested.write_bytes(b"sqlite")
    nested.chmod(0o400)

    result = _run(repo)

    assert result.returncode != 0
    assert "checkpoints_fc.sqlite3" in result.stderr
    assert "bash deploy/release.sh" in result.stderr


def test_repair_restores_nested_owner_write_and_reaudits(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / ".runtime" / "checkpoints_fc.sqlite3"
    nested.write_bytes(b"sqlite")
    nested.chmod(0o400)
    # `env` is a no-privilege command prefix: all test inodes already have the
    # requested uid/gid, so real chown/chmod can exercise the exact repair path.
    env_cmd = Path(shutil.which("env"))

    result = _run(repo, "--repair", sudo_cmd=env_cmd)

    assert result.returncode == 0, result.stderr
    assert nested.stat().st_mode & 0o600 == 0o600
    assert "Repairing 1 incompatible" in result.stdout


def test_repair_refuses_a_symlinked_bind_root(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("unchanged", encoding="utf-8")
    (repo / ".runtime").rmdir()
    (repo / ".runtime").symlink_to(outside, target_is_directory=True)

    result = _run(repo, "--repair")

    assert result.returncode != 0
    assert "refusing symlinked writable bind root" in result.stderr
    assert marker.read_text(encoding="utf-8") == "unchanged"
