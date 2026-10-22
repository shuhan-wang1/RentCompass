#!/usr/bin/env python3
"""Remove TEST-FIXTURE rows from a canary telemetry log. Nothing else.

Why this exists
---------------
A canary telemetry log such as `.runtime/logs/canary-legacy.jsonl` can carry rows
that no pool ever wrote: `canary.turn` records stamped with the fixture shas
`shaLEGACY` / `shaSER`, and `canary.arch_mismatch` events with no `ts`, no
`request_id` and no `telemetry_schema_version`. They come from tests pointed at the
live sink. They cost the report twice: the fixture shas inflate the "window mixes N
candidate builds" list, and the undated events can be placed in no window, so
every run reports them as records that would be silently dropped.

What it will NOT do
-------------------
It is not a log cleaner. It deletes a row only when the row proves it is a
fixture, by one of exactly two tests:

1. ``candidate_sha`` is one of the literal fixture shas; or
2. the row is missing ALL THREE of ``ts``, ``request_id`` and
   ``telemetry_schema_version`` — the shape of the `arch_mismatch` events, and a
   shape no turn record can have.

Anything else is kept, including rows this script cannot parse. An unparseable
line may be a truncated write of a record that carried a real violation, and
deleting evidence to tidy a log is the opposite of the job. Short-sha rows and
`unknown`-sha rows are GENUINE production turns and are never touched — they are
also where every zero-tolerance counter in the file lives, so
pruning fixtures changes no gate verdict. Do not run this expecting one to move.

Safety
------
* ``--dry-run`` is the DEFAULT. Writing requires ``--apply``.
* A backup is written first, to ``<log>.bak-<UTC date>``, and the script refuses
  to start if that path already exists. The suffix is deliberately not
  ``.jsonl``: `canary_report.LOG_SUFFIXES` would otherwise pick the backup up on
  a directory walk and double the record population. Prefer ``--backup-dir``
  outside ``.runtime/logs/`` all the same (docs/canary_runbook.md section 5).
* The file's identity (inode, size, mtime) is captured before the read and
  re-checked before the swap. If anything changed in between — i.e. a pool is
  still appending — the script aborts and writes nothing.
* It never renames or truncates a file a live pool holds open safely: STOP THE
  POOL FIRST. The logger opens its sink once and holds the fd, so replacing the
  file underneath a running pool leaves it writing into the old inode.

Usage
-----
    python3 scripts/canary_prune_fixture_rows.py .runtime/logs/canary-legacy.jsonl
    python3 scripts/canary_prune_fixture_rows.py <log> --apply --backup-dir ~/canary-backups
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import List, Optional, Tuple

FIXTURE_SHAS = frozenset({"shaLEGACY", "shaSER"})
# A row missing every one of these carries no identity, no place in a window and
# no contract version. That is the `canary.arch_mismatch` event shape.
IDENTITY_FIELDS = ("ts", "request_id", "telemetry_schema_version")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_RACE = 3          # the file changed under us; nothing was written


def _stat_signature(path: str) -> Tuple[int, int, int]:
    st = os.stat(path)
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def classify_line(line: str) -> Tuple[bool, str]:
    """Return ``(drop, reason)`` for one raw log line.

    Unparseable and blank lines are KEPT (``drop=False``): this script removes
    rows it can prove are fixtures, never rows it merely cannot read.
    """
    stripped = line.strip()
    if not stripped:
        return False, "blank"
    try:
        rec = json.loads(stripped)
    except ValueError:
        return False, "unparseable (kept: may be a truncated real record)"
    if not isinstance(rec, dict):
        return False, "not an object (kept)"
    sha = rec.get("candidate_sha")
    if isinstance(sha, str) and sha in FIXTURE_SHAS:
        return True, f"fixture candidate_sha={sha}"
    if all(rec.get(field) is None for field in IDENTITY_FIELDS):
        event = rec.get("event") or rec.get("name") or "<no event>"
        return True, f"no ts/request_id/telemetry_schema_version (event={event})"
    return False, "kept"


def prune(path: str, *, apply: bool, backup_dir: Optional[str],
          out=sys.stdout) -> int:
    if not os.path.isfile(path):
        out.write(f"error: not a file: {path}\n")
        return EXIT_ERROR
    before = _stat_signature(path)

    kept_lines: List[str] = []
    reasons: dict = {}
    kept = dropped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            drop, reason = classify_line(line)
            if drop:
                dropped += 1
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                kept += 1
                kept_lines.append(line)

    out.write(f"file            : {path}\n")
    out.write(f"rows read       : {kept + dropped}\n")
    out.write(f"rows kept       : {kept}\n")
    out.write(f"rows to remove  : {dropped}\n")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        out.write(f"  x{count}  {reason}\n")

    if not dropped:
        out.write("nothing to do.\n")
        return EXIT_OK
    if not apply:
        out.write("DRY RUN — nothing written. Re-run with --apply to rewrite "
                  "the file (stop the pool that holds the sink fd first).\n")
        return EXIT_OK

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_name = f"{os.path.basename(path)}.bak-{stamp}"
    backup_path = os.path.join(backup_dir or os.path.dirname(path) or ".", backup_name)
    if os.path.exists(backup_path):
        out.write(f"error: backup already exists, refusing to overwrite it: "
                  f"{backup_path}\n")
        return EXIT_ERROR
    if _stat_signature(path) != before:
        out.write("error: the log changed while it was being read (a pool is "
                  "still writing to it). Nothing was written.\n")
        return EXIT_RACE
    os.makedirs(os.path.dirname(backup_path) or ".", exist_ok=True)
    shutil.copy2(path, backup_path)
    out.write(f"backup written  : {backup_path}\n")

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".prune-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.writelines(kept_lines)
            tmp.flush()
            os.fsync(tmp.fileno())
        # LAST check before the swap: between the backup and now is the only
        # remaining window in which an appender could have added a record we did
        # not read, and replacing the file would silently delete it.
        if _stat_signature(path) != before:
            out.write("error: the log changed after the backup was taken "
                      "(a pool is still writing to it). Nothing was replaced; "
                      f"the backup at {backup_path} is intact.\n")
            return EXIT_RACE
        shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    out.write(f"rewritten       : {path} ({kept} rows)\n")
    out.write("The pool holds the sink fd by inode. If it was running, it is "
              "still writing to the OLD file — restart it and verify with: "
              "docker exec <container> sh -c 'ls -l /proc/1/fd | grep canary'\n")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    # allow_abbrev=False: a typo such as "--appl" is an error, never a silent "--apply".
    ap = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Remove test-fixture rows from a canary telemetry log.")
    ap.add_argument("log", help="path to the canary jsonl log")
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the file (default: dry run)")
    ap.add_argument("--backup-dir", default=None,
                    help="directory for the .bak-<date> copy "
                         "(default: beside the log; prefer a path OUTSIDE "
                         ".runtime/logs/)")
    args = ap.parse_args(argv)
    return prune(args.log, apply=args.apply, backup_dir=args.backup_dir)


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    sys.exit(main())
