"""Reproducible results package for a benchmark run.

A committed A/B result must be reproducible from the tree alone. This writer emits, into
the run's ``--out`` dir, the small self-describing artifacts that pin exactly how a run
was produced:

* ``per_case.csv`` — one lean row per case-run (the columns a reviewer scans to see WHAT
  passed and WHY it failed), companion to the deeper ``per_case_detail.csv``.
* ``manifest.json`` — the exact invocation (argv), the relevant environment
  (AGENT_ARCH / DEEPSEEK_STRICT / config), the code commit, a timestamp, and the SHA256
  of the case file AND the event log.

``events.jsonl`` itself stays OUT of git (it is large and PII-adjacent); only its digest
is recorded, so a committed package can be integrity-checked without shipping the raw
stream. Nothing here makes a network call.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Union


def sha256_of(path: Union[str, Path]) -> Optional[str]:
    """Streaming SHA256 of a file, or None if it does not exist."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_num(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return v


def _failed_constraints(rr: Any) -> str:
    """Pipe-joined machine names of the constraints (and forbidden-tool uses) that made
    this case FAIL — the at-a-glance 'why' column. Empty when the case passed clean."""
    verdict = getattr(rr, "verdict", None) or {}
    fails = [c.get("type") for c in verdict.get("constraints", []) if not c.get("passed")]
    for t in verdict.get("forbidden_tool_violations", []):
        fails.append(f"forbidden:{t}")
    if getattr(rr, "error", None):
        fails.append("run_error")
    return "|".join(str(x) for x in fails if x)


PER_CASE_COLUMNS = [
    "case_id", "category", "arch", "passed", "route_matched", "hard_gate",
    "llm_calls", "tool_batches", "tools_executed", "tools_denied", "tools_requested",
    "latency_ms", "cost_usd", "failed_constraints",
]


def _join_tools(rr: Any, attr: str) -> str:
    """Pipe-join a RunResult tool-name list (executed/denied/requested)."""
    return "|".join(str(t) for t in (getattr(rr, attr, None) or []))


def write_per_case(out: Union[str, Path], runs: List[Any], *, arch: str) -> Path:
    """Write the lean, task-specified ``per_case.csv``. Deterministic column order.

    The three tool columns record the requested/executed/denied split (H13): a memory-write
    the gate refused shows in ``tools_denied`` but NOT ``tools_executed``, so a reviewer can
    see the write was attempted, shown, and blocked without it counting as a call the model
    made."""
    path = Path(out) / "per_case.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(PER_CASE_COLUMNS)
        for rr in runs:
            w.writerow([
                getattr(rr, "case_id", ""),
                getattr(rr, "category", ""),
                arch,
                getattr(rr, "passed", ""),
                getattr(rr, "route_matched", ""),
                getattr(rr, "hard_gate", ""),
                getattr(rr, "llm_calls", 0),
                getattr(rr, "tool_batches", 0),
                _join_tools(rr, "tools_executed"),
                _join_tools(rr, "tools_denied"),
                _join_tools(rr, "tools_requested"),
                _fmt_num(getattr(rr, "turn_latency_ms", None)),
                _fmt_num(getattr(rr, "cost_usd", None)),
                _failed_constraints(rr),
            ])
    return path


def write_events_gz(out: Union[str, Path], events_log: Union[str, Path]) -> Optional[Path]:
    """Gzip the raw event stream into ``<out>/events.jsonl.gz`` so a committed package
    carries the events for verification (raw + gz SHA256 both go in the manifest). Written
    with a fixed gzip mtime so the archive is byte-deterministic for a given raw stream.
    Returns the gz path, or None if the raw log is absent."""
    src = Path(events_log)
    if not src.exists() or not src.is_file():
        return None
    dst = Path(out) / "events.jsonl.gz"
    with src.open("rb") as f_in, dst.open("wb") as raw_out:
        with gzip.GzipFile(filename="", fileobj=raw_out, mode="wb", mtime=0) as gz:
            shutil.copyfileobj(f_in, gz)
    return dst


def build_manifest(
    *,
    argv: List[str],
    arch: str,
    config: str,
    timestamp: str,
    case_file: Union[str, Path],
    events_log: Union[str, Path],
    mode: Optional[str] = None,
    git_commit: Union[str, Callable[[], Optional[str]], None] = None,
    git_dirty: Union[bool, Callable[[], Optional[bool]], None] = None,
    events_gz: Union[str, Path, None] = None,
    extra_env: Optional[List[str]] = None,
) -> dict:
    """Assemble (but do not write) the run manifest. ``git_commit`` and ``git_dirty`` may
    each be a value OR a zero-arg callable (so a test can stub them and stay free of any git
    dependency). ``git_dirty`` records whether the working tree had uncommitted changes when
    the run was produced — a committed A/B result should be reproduced from a CLEAN tree
    (``git_dirty`` False). ``events_gz`` (when the caller has written ``events.jsonl.gz``
    into the package) pins the gz path + SHA256 alongside the raw event digest."""
    commit = git_commit() if callable(git_commit) else git_commit
    dirty = git_dirty() if callable(git_dirty) else git_dirty
    gz_sha = sha256_of(events_gz) if events_gz else None
    env_keys = ["AGENT_ARCH", "DEEPSEEK_STRICT", "LLM_PROVIDER", "USE_MCP_TOOLS",
                "RENTCOMPASS_EVAL"]
    for k in (extra_env or []):
        if k not in env_keys:
            env_keys.append(k)
    return {
        "argv": list(argv),
        "command": " ".join(str(a) for a in argv),
        "arch": arch,
        "config": config,
        "mode": mode,
        "timestamp": timestamp,
        "git_commit": commit,
        "git_dirty": dirty,
        "python": sys.version.split()[0],
        "env": {k: os.environ.get(k) for k in env_keys},
        "case_file": {"path": str(case_file), "sha256": sha256_of(case_file)},
        # The raw events.jsonl SHA256 is the reproducibility anchor. The gzip copy
        # (events.jsonl.gz) is shipped IN the package for verification; both digests are
        # recorded so the archive can be integrity-checked and re-expanded.
        "events_log": {
            "path": str(events_log),
            "sha256": sha256_of(events_log),
            "gz_path": str(events_gz) if events_gz else None,
            "sha256_gz": gz_sha,
            "committed": bool(events_gz),
            "note": ("events.jsonl.gz preserved in the package for verification; raw + gz "
                     "SHA256 both recorded"),
        },
    }


def write_manifest(out: Union[str, Path], **kwargs: Any) -> dict:
    """Build the manifest (see :func:`build_manifest`) and write ``manifest.json``."""
    manifest = build_manifest(**kwargs)
    (Path(out) / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def write_results_package(
    out: Union[str, Path],
    runs: List[Any],
    *,
    argv: List[str],
    arch: str,
    config: str,
    timestamp: str,
    case_file: Union[str, Path],
    events_log: Union[str, Path],
    mode: Optional[str] = None,
    git_commit: Union[str, Callable[[], Optional[str]], None] = None,
    git_dirty: Union[bool, Callable[[], Optional[bool]], None] = None,
) -> dict:
    """Write the full reproducible package (per_case.csv + events.jsonl.gz + manifest.json)
    and return the manifest. Called at the end of every run so a result dir is always
    self-describing: the gzipped event stream travels WITH the package, and the manifest
    pins the git commit + clean/dirty state and the raw + gz event digests."""
    write_per_case(out, runs, arch=arch)
    events_gz = write_events_gz(out, events_log)
    return write_manifest(
        out, argv=argv, arch=arch, config=config, timestamp=timestamp,
        case_file=case_file, events_log=events_log, mode=mode, git_commit=git_commit,
        git_dirty=git_dirty, events_gz=events_gz)
