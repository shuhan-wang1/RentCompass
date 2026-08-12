#!/usr/bin/env python3
"""Verify and explicitly retire the duplicate legacy Chroma memory files.

Run the default inspect mode first. Retirement is destructive by design and is only
allowed with the exact observed count and digest plus an explicit confirmation that
every process running the old Chroma-backed application has stopped.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from rag.sqlite_memory_store import (  # noqa: E402
    LEGACY_QUARANTINE_DIR,
    LEGACY_RETIREMENT_MARKER,
    LegacyMemoryError,
    SQLiteMemoryCollection,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(root: Path, payload: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=root, prefix=".legacy-retirement-", suffix=".tmp"
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, root / LEGACY_RETIREMENT_MARKER)
        _fsync_directory(root)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_marker(root: Path) -> dict | None:
    path = root / LEGACY_RETIREMENT_MARKER
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("legacy retirement marker is invalid")
    return value


def _assert_expected(report: dict, expected_count: int, expected_digest: str) -> None:
    if report.get("source_count") != expected_count:
        raise RuntimeError(
            f"legacy count changed: expected {expected_count}, "
            f"observed {report.get('source_count')}"
        )
    if report.get("source_digest") != expected_digest:
        raise RuntimeError("legacy digest changed; inspect again before retirement")
    accounted = int(report.get("verified_count", 0)) + int(
        report.get("tombstoned_count", 0)
    )
    if accounted != expected_count:
        raise RuntimeError(
            f"migration verification accounted for {accounted}/{expected_count} rows"
        )


def inspect_legacy(root: Path) -> dict:
    store = SQLiteMemoryCollection(root)
    report = store.verify_legacy_copy()
    return {
        **report,
        "db_path": str(root),
        "legacy_artifacts": [path.name for path in store.legacy_artifacts()],
        "health": store.health(),
    }


def _resume_pending(
    root: Path,
    marker: dict,
    expected_count: int,
    expected_digest: str,
) -> dict | None:
    if marker.get("status") != "pending":
        return None
    _assert_expected(marker, expected_count, expected_digest)
    quarantine = root / LEGACY_QUARANTINE_DIR
    source = root / "chroma.sqlite3"
    if source.exists():
        # The move did not pass the point of no return. Restore any already moved
        # artifacts and perform a fresh verification below.
        if quarantine.exists():
            for child in sorted(quarantine.iterdir(), key=lambda path: path.name):
                target = root / child.name
                if target.exists():
                    raise RuntimeError(
                        f"cannot recover pending migration; both copies exist: {target}"
                    )
                os.replace(child, target)
            quarantine.rmdir()
            _fsync_directory(root)
        (root / LEGACY_RETIREMENT_MARKER).unlink(missing_ok=True)
        _fsync_directory(root)
        return None

    # The verified source was already moved. Finish deleting a partial quarantine
    # and seal the final marker; the pending marker proves verification preceded it.
    if quarantine.exists():
        shutil.rmtree(quarantine)
        _fsync_directory(root)
    final = {
        **marker,
        "status": "retired",
        "retired_at": int(time.time()),
    }
    _write_marker(root, final)
    return final


def retire_legacy(
    root: Path,
    *,
    expected_count: int,
    expected_digest: str,
    confirmed_no_legacy_processes: bool,
) -> dict:
    root = root.expanduser()
    if root.is_symlink():
        raise RuntimeError("memory root must not be a symlink")
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"memory root is not a directory: {root}")
    if expected_count < 0 or not _DIGEST.fullmatch(expected_digest):
        raise RuntimeError("expected count/digest are invalid")
    if not confirmed_no_legacy_processes:
        raise RuntimeError(
            "refusing retirement until all legacy application processes are stopped"
        )

    lock_path = root / ".legacy-retirement.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another memory migration is running") from exc

        marker = _read_marker(root)
        if marker and marker.get("status") == "retired":
            _assert_expected(marker, expected_count, expected_digest)
            if (root / "chroma.sqlite3").exists():
                raise RuntimeError("retired legacy source unexpectedly reappeared")
            return marker
        if marker:
            resumed = _resume_pending(
                root, marker, expected_count, expected_digest
            )
            if resumed is not None:
                return resumed

        store = SQLiteMemoryCollection(root)
        report = store.verify_legacy_copy()
        if report.get("status") != "verified":
            raise RuntimeError("no legacy source is available to retire")
        _assert_expected(report, expected_count, expected_digest)
        artifacts = store.legacy_artifacts()
        if store.legacy_path not in artifacts:
            raise RuntimeError("legacy database is not in the retirement set")
        if any(path.is_symlink() for path in artifacts):
            raise RuntimeError("legacy artifacts must not be symbolic links")

        pending = {
            **report,
            "status": "pending",
            "schema": 1,
            "verified_at": int(time.time()),
        }
        _write_marker(root, pending)

        quarantine = root / LEGACY_QUARANTINE_DIR
        quarantine.mkdir(mode=0o700)
        for artifact in artifacts:
            os.replace(artifact, quarantine / artifact.name)
        _fsync_directory(quarantine)
        _fsync_directory(root)

        # An old process writing after the operator confirmation is a hard failure.
        if store.legacy_artifacts():
            raise RuntimeError(
                "legacy files reappeared during retirement; stop old processes and retry"
            )

        shutil.rmtree(quarantine)
        _fsync_directory(root)
        final = {
            **pending,
            "status": "retired",
            "retired_at": int(time.time()),
        }
        _write_marker(root, final)
        return final


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "app" / "chroma_db_agent_memory",
    )
    parser.add_argument("--retire-legacy", action="store_true")
    parser.add_argument("--expected-source-count", type=int)
    parser.add_argument("--expected-source-digest")
    parser.add_argument("--confirm-no-legacy-processes", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.retire_legacy:
            if args.expected_source_count is None or not args.expected_source_digest:
                parser.error(
                    "--retire-legacy requires --expected-source-count and "
                    "--expected-source-digest"
                )
            result = retire_legacy(
                args.db_path,
                expected_count=args.expected_source_count,
                expected_digest=args.expected_source_digest,
                confirmed_no_legacy_processes=args.confirm_no_legacy_processes,
            )
        else:
            result = inspect_legacy(args.db_path)
    except (LegacyMemoryError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"agent-memory migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
