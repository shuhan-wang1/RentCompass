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
import hashlib
import json
import os
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
    "llm_calls", "tool_batches", "latency_ms", "cost_usd", "failed_constraints",
]


def write_per_case(out: Union[str, Path], runs: List[Any], *, arch: str) -> Path:
    """Write the lean, task-specified ``per_case.csv``. Deterministic column order."""
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
                _fmt_num(getattr(rr, "turn_latency_ms", None)),
                _fmt_num(getattr(rr, "cost_usd", None)),
                _failed_constraints(rr),
            ])
    return path


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
    extra_env: Optional[List[str]] = None,
) -> dict:
    """Assemble (but do not write) the run manifest. ``git_commit`` may be a value OR a
    zero-arg callable (so a test can stub it and stay free of any git dependency)."""
    commit = git_commit() if callable(git_commit) else git_commit
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
        "python": sys.version.split()[0],
        "env": {k: os.environ.get(k) for k in env_keys},
        "case_file": {"path": str(case_file), "sha256": sha256_of(case_file)},
        # events.jsonl is intentionally NOT committed; the digest lets a committed package
        # be integrity-checked without shipping the raw event stream.
        "events_log": {"path": str(events_log), "sha256": sha256_of(events_log),
                       "committed": False,
                       "note": "events.jsonl stays out of git; digest only"},
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
) -> dict:
    """Write the full reproducible package (per_case.csv + manifest.json) and return the
    manifest. Called at the end of every run so a result dir is always self-describing."""
    write_per_case(out, runs, arch=arch)
    return write_manifest(
        out, argv=argv, arch=arch, config=config, timestamp=timestamp,
        case_file=case_file, events_log=events_log, mode=mode, git_commit=git_commit)
