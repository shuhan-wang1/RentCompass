#!/usr/bin/env python3
"""Delete stored reflection rows whose wording the R4-12 guard rejects. Nothing else.

Why this exists
---------------
Reflections are the assistant's own guesses about a user. Before R4-12 (5155cbd) nothing checked
their wording, and about half of the reflection rows written then turn a preference into a
requirement ("prioritizes", "must", "a deal-breaker", "is willing to stretch …") with words the
user never used, or say the user "stated" what was only inferred. 5155cbd hides such rows when
they are read; this tool removes them from the store.

What it judges, and against what
--------------------------------
* Only rows with ``mtype='reflection'``. Episodic and semantic rows are never deleted.
* Each reflection is judged by ``rag.agent_memory.reflection_rejection`` -- the guard 5155cbd
  added, imported, never copied -- against the user's FULL own-message history in the store:
  every episodic and semantic row of THAT user. A reflection whose escalation word the user did
  say somewhere ("pets must be allowed") is kept. ``rejected_reflections`` in the same module is
  the one implementation; the read side (``presentable_memories``) gives the same verdict.

Safety
------
* Dry run is the DEFAULT: it prints every row it would delete (id, a hashed user, the reason,
  a short excerpt) and the totals, and deletes nothing. ``--apply`` deletes.
* Before ``--apply`` deletes anything it writes a JSON backup of every row it will delete
  (id, document, metadata, reason) next to the database, or to ``--backup PATH``. The file is
  created exclusively (an existing file is never overwritten) with mode 0600, and fsynced. If it
  cannot be written the tool stops and deletes nothing.
* Rows are deleted through the store's own API (``SQLiteMemoryCollection.delete``), which
  tombstones every deleted id, so a legacy Chroma copy of a row cannot be re-imported by the
  legacy sync.
* Idempotent: a second run finds nothing to delete, writes no backup and exits 0.
* Opening the store runs the same idempotent schema check and legacy sync every application
  process runs when it starts. Pools may keep running.

Usage
-----
    python3 scripts/purge_escalating_reflections.py app/chroma_db_agent_memory/agent_memory.sqlite3
    python3 scripts/purge_escalating_reflections.py app/chroma_db_agent_memory/agent_memory.sqlite3 --apply
    python3 scripts/purge_escalating_reflections.py <db> --apply --backup /home/shuhan/memory-backups/reflections.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "app"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from rag.agent_memory import AgentMemory, rejected_reflections  # noqa: E402
from rag.sqlite_memory_store import _NATIVE_DB_NAME  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 2
_DELETE_BATCH = 200
_EXCERPT_CHARS = 90


def _memory_root(path: Path) -> Path:
    """The store directory for ``path`` (the directory, or its agent_memory.sqlite3)."""
    path = path.expanduser()
    root = path.parent if path.name == _NATIVE_DB_NAME else path
    if not (root / _NATIVE_DB_NAME).is_file():
        raise RuntimeError(f"no {_NATIVE_DB_NAME} at {root}; refusing to create a new store")
    return root.resolve()


def _user_tag(user_id) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:10]


def _excerpt(text) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= _EXCERPT_CHARS else value[:_EXCERPT_CHARS - 1] + "…"


def find_rejected(memory: AgentMemory) -> tuple[list, dict]:
    got = memory.col.get()
    records = list(zip(got.get("ids") or [], got.get("documents") or [],
                       got.get("metadatas") or []))
    doomed = rejected_reflections(records)
    reflections = [r for r in records if (r[2] or {}).get("mtype") == "reflection"]
    stats = {
        "rows": len(records),
        "reflections": len(reflections),
        "users_with_reflections": len({(r[2] or {}).get("user_id") for r in reflections}),
        "to_delete": len(doomed),
        "users_affected": len({row["user_id"] for row in doomed}),
        "kept": len(reflections) - len(doomed),
        "by_reason": {},
    }
    for row in doomed:
        stats["by_reason"][row["reason"]] = stats["by_reason"].get(row["reason"], 0) + 1
    return doomed, stats


def _write_backup(path: Path, root: Path, doomed: list) -> None:
    payload = {
        "tool": "scripts/purge_escalating_reflections.py",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": str(root / _NATIVE_DB_NAME),
        "rows": [{"id": row["id"], "document": row["text"], "metadata": row["metadata"],
                  "reason": row["reason"]} for row in doomed],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    # The backup is read back before anything is deleted: a truncated file is no backup.
    written = json.loads(path.read_text(encoding="utf-8"))
    if [row["id"] for row in written["rows"]] != [row["id"] for row in doomed]:
        raise RuntimeError("the backup does not hold every row to be deleted")


def purge(db: Path, *, apply: bool, backup: Path | None, out=None) -> int:
    out = out or sys.stdout
    try:
        root = _memory_root(db)
        memory = AgentMemory(db_path=str(root))
        doomed, stats = find_rejected(memory)
    except Exception as exc:
        out.write(f"error: {type(exc).__name__}: {exc}\n")
        return EXIT_ERROR

    out.write(f"database          : {root / _NATIVE_DB_NAME}\n")
    for row in doomed:
        out.write(f"DELETE {row['id']}  user#{_user_tag(row['user_id'])}  "
                  f"reason={row['reason']}  \"{_excerpt(row['text'])}\"\n")
    out.write(f"rows in store     : {stats['rows']}\n")
    out.write(f"reflection rows   : {stats['reflections']} "
              f"({stats['users_with_reflections']} users)\n")
    out.write(f"to delete         : {stats['to_delete']} ({stats['users_affected']} users)\n")
    out.write(f"kept              : {stats['kept']}\n")
    for reason, count in sorted(stats["by_reason"].items(), key=lambda kv: (-kv[1], kv[0])):
        out.write(f"  x{count}  {reason}\n")
    if not doomed:
        out.write("nothing to do.\n")
        return EXIT_OK
    if not apply:
        out.write("DRY RUN: nothing was deleted. Re-run with --apply to delete these rows "
                  "(a JSON backup is written first).\n")
        return EXIT_OK

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = (backup.expanduser() if backup is not None
                   else root / f"reflection-purge-backup-{stamp}.json")
    try:
        _write_backup(backup_path, root, doomed)
    except Exception as exc:
        out.write(f"error: cannot write the backup {backup_path}: {type(exc).__name__}: {exc}\n"
                  "Nothing was deleted.\n")
        return EXIT_ERROR
    out.write(f"backup written    : {backup_path}\n")

    ids = [row["id"] for row in doomed]
    try:
        with memory._lock:
            for start in range(0, len(ids), _DELETE_BATCH):
                # The where clause is a second guard: only a reflection row can be deleted.
                memory.col.delete(ids=ids[start:start + _DELETE_BATCH],
                                  where={"mtype": "reflection"})
        remaining = memory.col.get(ids=ids).get("ids") or []
    except Exception as exc:
        out.write(f"error: delete failed: {type(exc).__name__}: {exc}\n"
                  f"The backup at {backup_path} is intact; re-run to finish.\n")
        return EXIT_ERROR
    try:
        legacy = memory.col.legacy_residual_records()
    except Exception as exc:
        out.write(f"warning: the legacy copy could not be read ({type(exc).__name__}); the "
                  "deleted ids are tombstoned either way.\n")
        legacy = {}
    legacy_copies = len(set(ids) & set(legacy.get("ids") or []))
    out.write(f"deleted           : {len(ids) - len(remaining)}\n")
    if legacy_copies:
        out.write(f"legacy copies     : {legacy_copies} deleted rows still sit in the legacy "
                  "Chroma file; they are tombstoned and will not be re-imported, and go when "
                  "the legacy files are retired (scripts/migrate_agent_memory.py).\n")
    if remaining:
        out.write(f"error: {len(remaining)} rows are still present after the delete.\n")
        return EXIT_ERROR
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete reflection rows whose wording the R4-12 guard rejects "
                    "(dry run by default).")
    parser.add_argument(
        "db", nargs="?", type=Path,
        default=REPO_ROOT / "app" / "chroma_db_agent_memory" / _NATIVE_DB_NAME,
        help="agent_memory.sqlite3, or the directory holding it")
    parser.add_argument("--apply", action="store_true",
                        help="delete the rows (default: dry run)")
    parser.add_argument("--backup", type=Path, default=None,
                        help="where to write the JSON backup (default: beside the database); "
                             "an existing file is never overwritten")
    args = parser.parse_args(argv)
    return purge(args.db, apply=args.apply, backup=args.backup)


if __name__ == "__main__":
    raise SystemExit(main())
