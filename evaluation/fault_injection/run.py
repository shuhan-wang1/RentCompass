"""Fault-injection entry point.

    python -m evaluation.fault_injection.run [--out evaluation/results] [--timestamp TS]

Runs all 15 scenarios OFFLINE (unbilled) against the REAL tool/graph/idempotency/
guardrail code with a faked (or deliberately raising) model, then writes:

* ``results/fault_injection.csv`` — one row per scenario.
* ``results/fault_summary.json`` — aggregate resilience metrics (with denominators).

The numbers are GENUINE: only the LLM is mocked; every resilience mechanism
exercised is production code.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bootstrap():
    """Reuse the benchmark's env bootstrap (sys.path + temp state + eval flag)."""
    for p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from evaluation.run_benchmark import _bootstrap_env

    state_dir = Path(tempfile.mkdtemp(prefix="rc_fault_state_"))
    events_log = state_dir / "fault_events.jsonl"
    _bootstrap_env(state_dir, events_log)
    return state_dir, events_log


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


async def _run_all(out: Path, timestamp: str) -> dict:
    state_dir, events_log = _bootstrap()
    from evaluation.metrics import collector
    from evaluation.fault_injection.scenarios import ALL_SCENARIOS, ScenarioContext
    from evaluation.fault_injection.injectors import ScenarioResult

    results: List[ScenarioResult] = []
    ctx = ScenarioContext(tmp=state_dir)
    for fn in ALL_SCENARIOS:
        sid = fn.__name__
        try:
            with collector.capture_run(f"fault#{sid}", sid, "fault_injection",
                                       log_path=str(events_log)):
                res = await fn(ctx)
        except Exception as exc:  # a scenario harness bug must not abort the suite
            res = ScenarioResult(
                scenario_id=sid.split("_")[1] if "_" in sid else sid,
                name=sid, fault="harness error",
                task_completed_after_fault=False, fault_surfaced=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(res)
        flag = "OK" if res.error is None else f"ERR({res.error})"
        print(f"[fault] {res.scenario_id} {res.name:<26} "
              f"completed={res.task_completed_after_fault} surfaced={res.fault_surfaced} {flag}",
              flush=True)

    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "fault_injection.csv", results)
    summary = _aggregate(results, timestamp)
    (out / "fault_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    _print_summary(summary)
    return summary


def _write_csv(path: Path, results: List[ScenarioResult]) -> None:
    cols = ["scenario_id", "name", "fault", "retry_recovered", "fallback_succeeded",
            "idempotency_held", "duplicate_write_count", "task_completed_after_fault",
            "produced_ungrounded_answer_after_fault", "fault_surfaced", "detail", "error"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in results:
            d = r.to_dict()
            w.writerow([d.get(c, "") if d.get(c) is not None else "" for c in cols])


def _ratio(num: int, den: int) -> dict:
    return {"num": num, "den": den, "display": f"{num}/{den}",
            "rate": (num / den if den else None)}


def _aggregate(results: List[ScenarioResult], timestamp: str) -> dict:
    def count(pred):
        return sum(1 for r in results if pred(r))

    retry_scen = [r for r in results if r.retry_recovered is not None]
    fallback_scen = [r for r in results if r.fallback_succeeded is not None]
    idem_scen = [r for r in results if r.idempotency_held is not None]
    surfaced_scen = [r for r in results if r.fault_surfaced is not None]

    return {
        "framework": "fault_injection",
        "mode": "offline (fake model + REAL tool/graph/idempotency/guardrail code)",
        "note": ("Resilience mechanics are GENUINE: only the LLM is mocked. "
                 "Numbers below are real observations of production error paths."),
        "n_scenarios": len(results),
        "retry_recovery_rate": _ratio(
            sum(1 for r in retry_scen if r.retry_recovered), len(retry_scen)),
        "fallback_success_rate": _ratio(
            sum(1 for r in fallback_scen if r.fallback_succeeded), len(fallback_scen)),
        "idempotency_pass_rate": _ratio(
            sum(1 for r in idem_scen if r.idempotency_held), len(idem_scen)),
        "total_duplicate_writes": sum(r.duplicate_write_count for r in results),
        "post_fault_completion_rate": _ratio(
            count(lambda r: r.task_completed_after_fault), len(results)),
        "post_fault_ungrounded_rate": _ratio(
            count(lambda r: r.produced_ungrounded_answer_after_fault), len(results)),
        "faults_correctly_surfaced": _ratio(
            sum(1 for r in surfaced_scen if r.fault_surfaced), len(surfaced_scen)),
        "harness_errors": count(lambda r: r.error is not None),
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "scenarios": [r.to_dict() for r in results],
    }


def _print_summary(s: dict) -> None:
    print("\n=== fault-injection summary ===")
    for k in ("retry_recovery_rate", "fallback_success_rate", "idempotency_pass_rate",
              "post_fault_completion_rate", "post_fault_ungrounded_rate",
              "faults_correctly_surfaced"):
        print(f"  {k:<32} {s[k]['display']}")
    print(f"  total_duplicate_writes           {s['total_duplicate_writes']}")
    print(f"  harness_errors                   {s['harness_errors']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.fault_injection.run")
    p.add_argument("--out", default="evaluation/results")
    p.add_argument("--timestamp", default=None)
    args = p.parse_args(argv)
    ts = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")
    summary = asyncio.run(_run_all(Path(args.out), ts))
    # Non-zero exit only on harness errors (not on scenarios that *expect* a failure).
    return 1 if summary.get("harness_errors", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
