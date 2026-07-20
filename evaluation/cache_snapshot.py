"""Listing-cache snapshot infrastructure for reproducible benchmark runs.

The customer search path (:mod:`app.core.scraping.on_demand`) reads and writes a
persistent SQLite listing cache. For a benchmark to be reproducible — and for the
warm-cache guard protocol to be a *pure* routing test (a warm hit is ~46ms, no live
scrape) — the exact cache contents a run started from must be pinnable and restorable.

This module provides that pin:

* :func:`make_snapshot` copies a live cache sqlite file to ``out_path`` and writes two
  ALWAYS-COMMITTED sidecars next to it — ``<out>.sha256`` (integrity anchor) and
  ``<out>.meta.json`` (created_at wall time, source path, row count).
* :func:`restore_snapshot` copies a snapshot back onto a destination cache path and
  verifies its SHA256 against the ``.sha256`` sidecar before returning, so a corrupted
  or truncated snapshot fails loudly instead of silently seeding a wrong dataset.

Nothing here touches the network or reads secrets. The sqlite copy is a plain file copy
(the store is a self-contained single file; snapshots are taken/restored while the app is
NOT concurrently writing the same path, which the runner guarantees by restoring into a
run-scoped namespace before the graph is invoked).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]

SHA256_SUFFIX = ".sha256"
META_SUFFIX = ".meta.json"


# --------------------------------------------------------------------------- #
# Digest + row-count helpers
# --------------------------------------------------------------------------- #
def sha256_of(path: PathLike) -> str:
    """Streaming SHA256 hex digest of an existing file. Raises if the file is missing."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_count(path: PathLike) -> Optional[int]:
    """Count rows in the ``listings`` table of a listing-cache sqlite file.

    Returns None if the file is not a readable sqlite DB or has no ``listings`` table —
    the snapshot is still valid (a fresh/empty cache legitimately has zero rows), so a
    row count that cannot be determined is recorded as null rather than failing."""
    try:
        with sqlite3.connect(str(path), timeout=10) as db:
            row = db.execute("SELECT COUNT(*) FROM listings").fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return None


def sidecar_paths(snapshot_path: PathLike) -> tuple[Path, Path]:
    """Return the (``.sha256``, ``.meta.json``) sidecar paths for a snapshot file."""
    p = Path(snapshot_path)
    return (p.with_name(p.name + SHA256_SUFFIX), p.with_name(p.name + META_SUFFIX))


# --------------------------------------------------------------------------- #
# make / restore
# --------------------------------------------------------------------------- #
def integrity_check(path: PathLike) -> None:
    """Run ``PRAGMA integrity_check`` on a sqlite file; raise ValueError unless 'ok'.

    A snapshot frozen from a corrupted db would poison every warm gate round that
    restores it, so freezing fails loudly instead."""
    with sqlite3.connect(str(path), timeout=10) as db:
        rows = db.execute("PRAGMA integrity_check").fetchall()
    verdicts = [str(r[0]) for r in rows]
    if verdicts != ["ok"]:
        raise ValueError(f"sqlite integrity_check failed for {path}: {verdicts[:5]}")


def make_snapshot(src_cache_path: PathLike, out_path: PathLike,
                  provenance: Optional[dict] = None) -> dict:
    """Copy a live listing-cache sqlite file to ``out_path`` and write its sidecars.

    Runs ``PRAGMA integrity_check`` on the source first (hard error unless 'ok').
    Writes ``<out>.sha256`` (the hex digest, single line) and ``<out>.meta.json``
    (``created_at`` wall time ISO-ish + epoch, ``source_path``, ``row_count``, ``sha256``,
    ``size_bytes``, plus an optional ``provenance`` block: candidate commit, case-file
    SHAs, warm-up commands, non-default budget envs — everything needed to re-derive the
    warm-up). Returns the metadata dict. Raises FileNotFoundError if the source cache
    does not exist (there is nothing to snapshot)."""
    src = Path(src_cache_path)
    out = Path(out_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"source listing cache not found: {src}")
    integrity_check(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)

    digest = sha256_of(out)
    sha_path, meta_path = sidecar_paths(out)
    sha_path.write_text(digest + "\n", encoding="utf-8")

    meta = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "created_at_epoch": time.time(),
        "source_path": str(src),
        "snapshot_path": str(out),
        "row_count": _row_count(out),
        "sha256": digest,
        "size_bytes": out.stat().st_size,
    }
    if provenance:
        meta["provenance"] = provenance
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def read_snapshot_sha256(snapshot_path: PathLike) -> Optional[str]:
    """Return the recorded digest from the ``.sha256`` sidecar, or None if absent."""
    sha_path, _ = sidecar_paths(snapshot_path)
    if not sha_path.exists():
        return None
    return sha_path.read_text(encoding="utf-8").strip().split()[0]


def restore_snapshot(snapshot_path: PathLike, dest_cache_path: PathLike) -> Path:
    """Copy ``snapshot_path`` onto ``dest_cache_path`` and verify its SHA256.

    The digest of the snapshot file is checked against its ``.sha256`` sidecar BEFORE the
    copy (a missing sidecar is a hard error — an unverifiable snapshot must not silently
    seed a run). Returns the destination path. Raises FileNotFoundError / ValueError on a
    missing snapshot or a digest mismatch."""
    snap = Path(snapshot_path)
    dest = Path(dest_cache_path)
    if not snap.exists() or not snap.is_file():
        raise FileNotFoundError(f"snapshot not found: {snap}")
    expected = read_snapshot_sha256(snap)
    if expected is None:
        raise FileNotFoundError(
            f"snapshot integrity sidecar missing: {sidecar_paths(snap)[0]}")
    actual = sha256_of(snap)
    if actual != expected:
        raise ValueError(
            f"snapshot sha256 mismatch for {snap}: sidecar={expected} actual={actual}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snap, dest)
    return dest
