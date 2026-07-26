"""Reproducible results package for a benchmark run.

A committed A/B result must be reproducible from the tree alone. This writer emits, into
the run's ``--out`` dir, the small self-describing artifacts that pin exactly how a run
was produced:

* ``per_case.csv`` — one lean row per case-run (the columns a reviewer scans to see WHAT
  passed and WHY it failed), companion to the deeper ``per_case_detail.csv``.
* ``manifest.json`` — the exact invocation (argv), the relevant environment
  (AGENT_ARCH / DEEPSEEK_STRICT / config), the code commit AND WHERE THAT COMMIT CAME FROM
  (git vs an operator's PRODUCT_SHA assertion) with the working tree's clean/dirty state,
  a timestamp, and the SHA256 of the case file AND the event log.

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
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


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


# --------------------------------------------------------------------------- #
# Commit binding: WHICH commit produced this measurement, and WHO says so
# --------------------------------------------------------------------------- #
# This project's whole method is "a measurement belongs to exactly one SHA". Until
# 2026-07-26 the harness recorded ``git_commit: null`` / ``git_dirty: null`` whenever it
# ran where git was unavailable (the normal case: it runs INSIDE a container, off a bind
# mount, with no .git) — even when the operator had pinned PRODUCT_SHA. The binding was
# therefore external and manual, and one file move away from being unattributable.
#
# The fix records the commit AND its PROVENANCE, because "git read this off the tree" and
# "the operator asserted this in an env var" do not carry the same evidential weight and
# must never be conflated:
#   git             — git reported the commit; the tree that ran is VERIFIED
#   env:PRODUCT_SHA — git was unavailable; the operator ASSERTED the commit, unverified
#   unavailable     — neither; the package cannot name its own commit (say so loudly)
#   unrecorded      — READ-ONLY value: an older package that predates this field
SOURCE_GIT = "git"
SOURCE_ENV = "env:PRODUCT_SHA"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_UNRECORDED = "unrecorded"

# commit_trust is the single field a human skims. Only one value is quiet; every value
# that means "do not attribute this measurement to that SHA without thinking" SHOUTS.
TRUST_CLEAN = "git-clean"
TRUST_DIRTY = "GIT-DIRTY"
TRUST_ASSERTED = "ENV-ASSERTED"
TRUST_UNKNOWN = "UNKNOWN"
TRUST_UNRECORDED = "UNRECORDED-LEGACY"

WRITER_SOURCES = (SOURCE_GIT, SOURCE_ENV, SOURCE_UNAVAILABLE)


def _git_stdout(args: List[str], cwd: str) -> Optional[str]:
    """Raw stdout of ``git <args>`` in ``cwd``, or None if git could not answer (binary
    absent, not a repository, timeout). None and "" are DIFFERENT here: "" is a real
    answer from ``git status --porcelain`` (a clean tree)."""
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def probe_git(repo_root: Union[str, Path, None] = None) -> Tuple[Optional[str], Optional[bool]]:
    """``(short_commit, dirty)`` read off the tree, ``(None, None)`` when git cannot
    answer. Never raises: the harness normally runs in a container where the git dir is
    absent, and a measurement must not die because it could not name itself."""
    root = str(repo_root or Path(__file__).resolve().parents[1])
    commit = (_git_stdout(["rev-parse", "--short", "HEAD"], root) or "").strip() or None
    dirty: Optional[bool] = None
    if commit:
        status = _git_stdout(["status", "--porcelain"], root)
        if status is not None:
            dirty = bool(status.strip())
    return commit, dirty


def assert_identity_consistent(rec: Dict[str, Any]) -> None:
    """SOURCE GUARD (not a promise in a docstring): refuse to emit an identity record that
    misrepresents its own provenance. Raises ValueError.

    Each clause is the specific lie this project must never publish:
      * a commit with no source, or a source claiming a commit that is not there;
      * a dirty/clean claim attached to a commit git did not report — an env-asserted SHA
        cannot know anything about the tree that ran;
      * a DIRTY tree that is not shouted about in identity_warnings (rule 5 is "build only
        from clean checkouts", so a dirty measurement must be self-evidently suspect);
      * ``git-clean`` trust claimed without a clean, git-sourced commit.
    """
    src = rec.get("git_commit_source")
    if src not in WRITER_SOURCES:
        raise ValueError(f"git_commit_source {src!r} is not one of {WRITER_SOURCES}")
    commit, dirty = rec.get("git_commit"), rec.get("git_dirty")
    if (commit is None) != (src == SOURCE_UNAVAILABLE):
        raise ValueError(
            f"identity provenance disagrees with the value it describes: "
            f"git_commit={commit!r} with git_commit_source={src!r}")
    if src != SOURCE_GIT and dirty is not None:
        raise ValueError(
            f"git_dirty={dirty!r} recorded for a commit sourced from {src!r}: only git can "
            f"observe the working tree, so an asserted SHA must leave dirtiness UNKNOWN")
    warnings = rec.get("identity_warnings") or []
    if dirty is True and not any("DIRTY" in str(w) for w in warnings):
        raise ValueError(
            "a dirty tree must be recorded LOUDLY: git_dirty is True but "
            "identity_warnings carries no DIRTY warning")
    if rec.get("commit_trust") == TRUST_CLEAN and not (src == SOURCE_GIT and dirty is False):
        raise ValueError(
            f"commit_trust {TRUST_CLEAN!r} claimed without a clean git-sourced commit "
            f"(source={src!r}, dirty={dirty!r})")


def _identity_record(commit: Optional[str], dirty: Optional[bool],
                     source: str) -> Dict[str, Any]:
    """Build + self-check one identity record. The warnings are written into the package
    itself, not just printed, so a summary read months later still says why it is weak."""
    warnings: List[str] = []
    if source == SOURCE_GIT:
        if dirty is True:
            trust = TRUST_DIRTY
            warnings.append(
                f"DIRTY TREE: this measurement was taken at git commit {commit} with "
                f"UNCOMMITTED CHANGES in the working tree. Rule 5 is 'build only from "
                f"clean checkouts' — the run is NOT attributable to {commit} alone and "
                f"must be treated as suspect.")
        elif dirty is False:
            trust = TRUST_CLEAN
        else:
            trust = TRUST_UNKNOWN
            warnings.append(
                f"git reported commit {commit} but could not report the working-tree "
                f"state, so clean-vs-dirty is UNKNOWN and rule 5 is UNVERIFIED here.")
    elif source == SOURCE_ENV:
        trust = TRUST_ASSERTED
        warnings.append(
            f"OPERATOR-ASSERTED COMMIT: git was unavailable, so {commit} comes from the "
            f"PRODUCT_SHA environment variable and NOT from the tree. The code that "
            f"actually ran was never verified and its clean/dirty state is UNKNOWN.")
    else:
        trust = TRUST_UNKNOWN
        warnings.append(
            "NO COMMIT BINDING: git was unavailable and PRODUCT_SHA was unset, so this "
            "package cannot name the commit that produced it. Attribution is EXTERNAL and "
            "manual — do not cite this run as evidence about any SHA.")
    rec = {
        "git_commit": commit,
        "git_dirty": dirty,
        "git_commit_source": source,
        "git_dirty_source": SOURCE_GIT if dirty is not None else SOURCE_UNAVAILABLE,
        "commit_trust": trust,
        "identity_warnings": warnings,
        "self_identifying": commit is not None,
    }
    assert_identity_consistent(rec)
    return rec


def resolve_commit_identity(*, repo_root: Union[str, Path, None] = None,
                            env: Optional[Dict[str, str]] = None,
                            git_probe: Optional[Callable[[], Tuple[Optional[str], Optional[bool]]]] = None,
                            ) -> Dict[str, Any]:
    """Resolve which commit produced this run: git first, PRODUCT_SHA second, and record
    WHICH of the two answered. ``git_probe`` is injectable so tests can simulate "no git
    in this container" without touching the environment."""
    environ = os.environ if env is None else env
    probe = git_probe or (lambda: probe_git(repo_root))
    try:
        commit, dirty = probe()
    except Exception:
        commit, dirty = None, None
    if commit:
        return _identity_record(commit, dirty, SOURCE_GIT)
    asserted = (environ.get("PRODUCT_SHA") or "").strip()
    if asserted:
        # Deliberately drops any dirty flag: without git's commit, a dirty observation
        # would be a claim about a tree we cannot tie to the asserted SHA.
        return _identity_record(asserted, None, SOURCE_ENV)
    return _identity_record(None, None, SOURCE_UNAVAILABLE)


def identity_from_values(git_commit: Union[str, Callable[[], Optional[str]], None],
                         git_dirty: Union[bool, Callable[[], Optional[bool]], None],
                         *, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Wrap an ALREADY-PROBED ``(commit, dirty)`` pair — what legacy callers hand
    :func:`build_manifest`, which by contract is the git probe's result (or a test stub
    standing in for it) — in a provenance record, falling back to PRODUCT_SHA when the
    probe came up empty. Values may be zero-arg callables, as before."""
    commit = git_commit() if callable(git_commit) else git_commit
    dirty = git_dirty() if callable(git_dirty) else git_dirty
    return resolve_commit_identity(env=env, git_probe=lambda: (commit, dirty))


def describe_identity(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """READ the identity out of ANY package summary/manifest, including every package on
    disk written before provenance existed (``git_commit: null``, no source fields, e.g.
    the 8793c0b round of record). NEVER raises and never invents provenance: a package
    that predates the fields is reported as ``unrecorded``, which is NOT the same claim as
    a new package that recorded ``unavailable``."""
    d = doc if isinstance(doc, dict) else {}
    commit = d.get("git_commit")
    dirty = d.get("git_dirty")
    recorded_source = d.get("git_commit_source")
    if recorded_source in WRITER_SOURCES:
        source = recorded_source
        trust = d.get("commit_trust") or TRUST_UNKNOWN
        dirty_source = d.get("git_dirty_source") or (
            SOURCE_GIT if dirty is not None else SOURCE_UNAVAILABLE)
        warnings = list(d.get("identity_warnings") or [])
    else:
        source = SOURCE_UNRECORDED
        dirty_source = SOURCE_UNRECORDED
        warnings = []
        if commit is None:
            trust = TRUST_UNKNOWN
            warnings.append(
                "NO COMMIT BINDING (legacy package): this package predates commit binding "
                "and does not name the commit that produced it — attribution is EXTERNAL.")
        else:
            trust = TRUST_DIRTY if dirty is True else TRUST_UNRECORDED
            if dirty is True:
                warnings.append(
                    f"DIRTY TREE (legacy package): recorded at {commit} with uncommitted "
                    f"changes; not attributable to {commit} alone.")
            else:
                warnings.append(
                    f"legacy package: {commit} was recorded before provenance was, so "
                    f"whether git or an operator supplied it is UNRECORDED.")
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "git_commit_source": source,
        "git_dirty_source": dirty_source,
        "commit_trust": trust,
        "identity_warnings": warnings,
        "self_identifying": commit is not None,
    }


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
    "case_id", "category", "arch", "repeat", "passed", "route_matched", "hard_gate",
    "llm_calls", "tool_batches", "tools_executed", "tools_denied", "tools_requested",
    "latency_ms", "cost_usd", "cache_hit_rate", "budget_timeout_tools", "soft_wrapped",
    "failed_constraints", "violation_kinds",
]


def _join_tools(rr: Any, attr: str) -> str:
    """Pipe-join a RunResult tool-name list (executed/denied/requested)."""
    return "|".join(str(t) for t in (getattr(rr, attr, None) or []))


def _cache_hit_rate(rr: Any) -> str:
    """Per-run cache-hit rate ``hits/(hits+misses)`` as a 4dp string, or '' when the run saw
    no cache_stats (hits+misses==0) — an empty cell reads as 'not measured', not '0%'."""
    hits = getattr(rr, "cache_hits", 0) or 0
    misses = getattr(rr, "cache_misses", 0) or 0
    total = hits + misses
    return f"{hits / total:.4f}" if total else ""


def _budget_timeout_tools(rr: Any) -> str:
    """Semicolon-joined ``tool:phase`` entries for every tool_budget_timeout on this run."""
    return ";".join(
        f"{e.get('tool')}:{e.get('phase')}"
        for e in (getattr(rr, "budget_timeout_events", None) or []))


def write_per_case(out: Union[str, Path], runs: List[Any], *, arch: str) -> Path:
    """Write the lean, task-specified ``per_case.csv``. Deterministic column order.

    The three tool columns record the requested/executed/denied split (H13): a memory-write
    the gate refused shows in ``tools_denied`` but NOT ``tools_executed``, so a reviewer can
    see the write was attempted, shown, and blocked without it counting as a call the model
    made. ``repeat`` disambiguates the K rows of a ``--repeat K`` case; ``violation_kinds``
    is the pipe-joined zero-tolerance kinds that fired on that specific run (empty = clean)."""
    path = Path(out) / "per_case.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(PER_CASE_COLUMNS)
        for rr in runs:
            w.writerow([
                getattr(rr, "case_id", ""),
                getattr(rr, "category", ""),
                arch,
                getattr(rr, "repeat", ""),
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
                _cache_hit_rate(rr),
                _budget_timeout_tools(rr),
                bool(getattr(rr, "soft_wrapped", False)),
                _failed_constraints(rr),
                "|".join(str(k) for k in (getattr(rr, "violation_kinds", None) or [])),
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
    cache_protocol: Optional[dict] = None,
    identity: Optional[dict] = None,
) -> dict:
    """Assemble (but do not write) the run manifest. ``identity`` is a resolved commit
    identity (see :func:`resolve_commit_identity`) so the summary and the manifest of one
    run cannot disagree about the commit; when it is omitted, ``git_commit``/``git_dirty``
    are used instead and may each be a value OR a zero-arg callable (so a test can stub
    them and stay free of any git dependency). ``git_dirty`` records whether the working
    tree had uncommitted changes when the run was produced — a committed A/B result should
    be reproduced from a CLEAN tree (``git_dirty`` False). ``events_gz`` (when the caller
    has written ``events.jsonl.gz`` into the package) pins the gz path + SHA256 alongside
    the raw event digest."""
    ident = dict(identity) if identity else identity_from_values(git_commit, git_dirty)
    assert_identity_consistent(ident)
    commit = ident["git_commit"]
    dirty = ident["git_dirty"]
    # THREE-LAYER IDENTITY (capture branch, 2026-07-23). The measurement probe must never
    # masquerade as the product under test:
    #   product_sha   — the PRODUCT whose behaviour this run measures (PRODUCT_SHA env,
    #                   pinned explicitly on a capture tree; falls back to the tree commit)
    #   capture_sha   — the tree that actually ran, i.e. product + evidence probe
    #   evaluator_sha — who SCORED it. Left null here on purpose: this tree's local
    #                   verdicts are NOT the gate; evaluation/rescore.py stamps the real
    #                   evaluator when it re-scores both arms together.
    product_sha = os.environ.get("PRODUCT_SHA") or commit
    # capture_sha stays strictly GIT-DERIVED (2026-07-26). The commit-binding fallback lets
    # ``git_commit`` be satisfied by PRODUCT_SHA, but the capture tree is a claim about what
    # code RAN — only git can witness that. Promoting an asserted SHA here would make
    # capture_sha == product_sha and flip capture_is_product to True, i.e. the exact
    # masquerade the three-layer identity exists to prevent.
    capture_sha = commit if ident["git_commit_source"] == SOURCE_GIT else None
    capture_is_product = bool(capture_sha) and product_sha == capture_sha
    evaluator_sha = os.environ.get("EVALUATOR_SHA")
    gz_sha = sha256_of(events_gz) if events_gz else None
    env_keys = ["AGENT_ARCH", "DEEPSEEK_STRICT", "LLM_PROVIDER", "USE_MCP_TOOLS",
                "RENTCOMPASS_EVAL", "SEARCH_CACHE_TTL_HOURS"]
    for k in (extra_env or []):
        if k not in env_keys:
            env_keys.append(k)
    return {
        "argv": list(argv),
        "command": " ".join(str(a) for a in argv),
        # --- identity: product vs capture tree vs evaluator (see above) -------
        "product_sha": product_sha,
        "capture_sha": capture_sha,
        "evaluator_sha": evaluator_sha,
        "capture_is_product": capture_is_product,
        "grader_sha256": sha256_of(Path(__file__).parent / "metrics" / "graders.py"),
        "case_contract_sha256": sha256_of(case_file),
        "arch": arch,
        "config": config,
        "mode": mode,
        # Cache protocol (warm/cold/none): which snapshot was restored, its digest, that a
        # fresh cache was restored per repeat, and the pinned TTL — so a committed A/B result
        # records exactly the cache state it started each repeat from.
        "cache_protocol": cache_protocol or {"mode": "none"},
        "timestamp": timestamp,
        # Commit binding + its PROVENANCE (2026-07-26). git_commit answers "which commit
        # produced this measurement"; git_commit_source answers "who says so" — a git read
        # off the tree and an operator's PRODUCT_SHA assertion are not the same evidence.
        # commit_trust/identity_warnings make a dirty or unverified run self-evidently
        # suspect to whoever reads this file later.
        "git_commit": commit,
        "git_dirty": dirty,
        "git_commit_source": ident["git_commit_source"],
        "git_dirty_source": ident["git_dirty_source"],
        "commit_trust": ident["commit_trust"],
        "identity_warnings": list(ident["identity_warnings"]),
        "self_identifying": ident["self_identifying"],
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
    cache_protocol: Optional[dict] = None,
    identity: Optional[dict] = None,
) -> dict:
    """Write the full reproducible package (per_case.csv + events.jsonl.gz + manifest.json)
    and return the manifest. Called at the end of every run so a result dir is always
    self-describing: the gzipped event stream travels WITH the package, and the manifest
    pins the commit + WHERE THE COMMIT CAME FROM + clean/dirty state, the raw + gz event
    digests, and the cache protocol."""
    write_per_case(out, runs, arch=arch)
    events_gz = write_events_gz(out, events_log)
    return write_manifest(
        out, argv=argv, arch=arch, config=config, timestamp=timestamp,
        case_file=case_file, events_log=events_log, mode=mode, git_commit=git_commit,
        git_dirty=git_dirty, events_gz=events_gz, cache_protocol=cache_protocol,
        identity=identity)
