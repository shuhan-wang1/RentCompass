"""CLI: snapshot the live listing cache into a committable, integrity-pinned file.

Usage::

    python -m evaluation.make_cache_snapshot --from-runtime \
        --out evaluation/benchmark/cache_snapshots/warm_v1.sqlite3

``--from-runtime`` sources the repo's real ``.runtime/listing_cache.sqlite3`` (the default
customer cache path). ``--from PATH`` sources an explicit cache file instead. Writes the
snapshot plus its always-committed ``.sha256`` and ``.meta.json`` sidecars (see
:mod:`evaluation.cache_snapshot`). A snapshot file <=20MB may be committed; the sidecars
are committed regardless (the ``cache_snapshots/.gitignore`` re-includes them over the
repo-root ``*.sqlite3`` ignore).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CACHE = REPO_ROOT / ".runtime" / "listing_cache.sqlite3"

from evaluation.cache_snapshot import make_snapshot  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evaluation.make_cache_snapshot",
        description="Snapshot the listing cache for reproducible benchmark runs.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from-runtime", action="store_true",
                     help=f"source the repo runtime cache ({DEFAULT_RUNTIME_CACHE})")
    src.add_argument("--from", dest="source", default=None,
                     help="source an explicit listing-cache sqlite path")
    p.add_argument("--out", required=True,
                   help="destination snapshot path (e.g. "
                        "evaluation/benchmark/cache_snapshots/warm_v1.sqlite3)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    source = Path(args.source) if args.source else DEFAULT_RUNTIME_CACHE
    out = Path(args.out)
    try:
        meta = make_snapshot(source, out)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    size_mb = meta["size_bytes"] / (1024 * 1024)
    print(f"snapshot written: {out}")
    print(f"  source     : {meta['source_path']}")
    print(f"  rows       : {meta['row_count']}")
    print(f"  size       : {size_mb:.2f} MB "
          f"({'committable (<=20MB)' if size_mb <= 20 else 'TOO LARGE to commit (>20MB)'})")
    print(f"  sha256     : {meta['sha256']}")
    print(f"  sidecars   : {out.name}.sha256, {out.name}.meta.json (commit these)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
