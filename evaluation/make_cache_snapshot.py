"""CLI: snapshot the live listing cache into a committable, integrity-pinned file.

Usage::

    python -m evaluation.make_cache_snapshot --from PATH \
        --out evaluation/benchmark/cache_snapshots/warm_v2.sqlite3 \
        --warmup-cmd "<the exact run_benchmark command used to warm PATH>" \
        --case-file evaluation/benchmark/cases.jsonl \
        --case-file evaluation/benchmark/cases_guard_regression.jsonl

``--from-runtime`` sources the repo's real ``.runtime/listing_cache.sqlite3`` (the default
customer cache path). ``--from PATH`` sources an explicit cache file instead — for warm-up
snapshots ALWAYS use ``--from`` with the exact ``--cache-path`` the warm-up shards shared:
the benchmark runner redirects the default cache to a throwaway temp dir, so
``--from-runtime`` on a warm-up would freeze the wrong (stale) database.

The source db must pass ``PRAGMA integrity_check``. Writes the snapshot plus its
always-committed ``.sha256`` and ``.meta.json`` sidecars; the meta gains a ``provenance``
block (candidate git commit + dirty flag, case-file SHA256s, warm-up commands, and every
non-default budget env var present at freeze time) so the warm-up is re-derivable. A
snapshot file <=20MB may be committed; the sidecars are committed regardless.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CACHE = REPO_ROOT / ".runtime" / "listing_cache.sqlite3"

from evaluation.cache_snapshot import make_snapshot  # noqa: E402

# Budget/tuning envs that materially shape what a warm-up scraped. Any of these present in
# the environment at freeze time is recorded verbatim in provenance.
_BUDGET_ENV_PREFIXES = ("FC_", "SEARCH_", "POI_", "TOOL_TIMEOUT", "AREA_RECO_")


def _git(args: List[str]) -> Optional[str]:
    try:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except Exception:
        return None


def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _budget_env() -> dict:
    return {k: v for k, v in sorted(os.environ.items())
            if k.startswith(_BUDGET_ENV_PREFIXES)}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evaluation.make_cache_snapshot",
        description="Snapshot the listing cache for reproducible benchmark runs.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from-runtime", action="store_true",
                     help=f"source the repo runtime cache ({DEFAULT_RUNTIME_CACHE}) — "
                          "NOT for warm-up snapshots (the runner redirects the default "
                          "cache away from .runtime; use --from with the warm-up "
                          "--cache-path instead)")
    src.add_argument("--from", dest="source", default=None,
                     help="source an explicit listing-cache sqlite path (use the exact "
                          "--cache-path the warm-up shards shared)")
    p.add_argument("--out", required=True,
                   help="destination snapshot path (e.g. "
                        "evaluation/benchmark/cache_snapshots/warm_v2.sqlite3)")
    p.add_argument("--warmup-cmd", action="append", default=[],
                   help="exact warm-up command line (repeatable, one per shard); "
                        "recorded in provenance")
    p.add_argument("--case-file", action="append", default=[],
                   help="case file the warm-up covered (repeatable); path + SHA256 "
                        "recorded in provenance")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    source = Path(args.source) if args.source else DEFAULT_RUNTIME_CACHE
    out = Path(args.out)
    provenance = {
        "git_commit": _git(["rev-parse", "--short", "HEAD"]),
        "git_dirty": bool(_git(["status", "--porcelain"])),
        "case_files": [{"path": cf, "sha256": _sha256_file(Path(cf))}
                       for cf in args.case_file],
        "warmup_commands": list(args.warmup_cmd),
        "budget_env": _budget_env(),
    }
    try:
        meta = make_snapshot(source, out, provenance=provenance)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    size_mb = meta["size_bytes"] / (1024 * 1024)
    print(f"snapshot written: {out}")
    print(f"  source     : {meta['source_path']}")
    print(f"  rows       : {meta['row_count']}")
    print(f"  size       : {size_mb:.2f} MB "
          f"({'committable (<=20MB)' if size_mb <= 20 else 'TOO LARGE to commit (>20MB)'})")
    print(f"  sha256     : {meta['sha256']}")
    print(f"  commit     : {provenance['git_commit']}"
          f"{' (DIRTY)' if provenance['git_dirty'] else ' (clean)'}")
    print(f"  provenance : {len(provenance['case_files'])} case file(s), "
          f"{len(provenance['warmup_commands'])} warm-up cmd(s), "
          f"{len(provenance['budget_env'])} budget env(s)")
    print(f"  sidecars   : {out.name}.sha256, {out.name}.meta.json (commit these)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
