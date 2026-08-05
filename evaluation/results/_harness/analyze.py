"""Paired analysis + cluster bootstrap for the 2026-08-04 unattended evaluation.

Reads the runs.jsonl shards written by ab_runner.py and emits a JSON block per study:
per-arm aggregates, per-case paired differences, and percentile bootstrap 95% CIs.

Bootstrap unit = the CASE, not the run. Three repeats of one case are correlated, so
resampling runs would understate the interval. Each resample draws len(cases) case ids
WITH replacement and recomputes the statistic from those cases' runs only.

Nothing here invents a number: a metric with no usable denominator comes back as null and
the report prints INCOMPLETE.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path
from typing import Callable, Dict, List, Optional

PRO_MODEL = "deepseek-v4-pro"


# --------------------------------------------------------------------------- #
def load_runs(paths: List[Path]) -> List[dict]:
    rows: List[dict] = []
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    # de-dup on ab_run_key (a resumed shard can re-append if it crashed mid-write)
    seen, out = set(), []
    for r in rows:
        k = r.get("ab_run_key")
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def percentile(xs: List[float], q: float) -> Optional[float]:
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(xs[int(pos)])
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def mean(xs: List[float]) -> Optional[float]:
    xs = [v for v in xs if v is not None]
    return statistics.fmean(xs) if xs else None


# --------------------------------------------------------------------------- #
# per-run metric extraction
# --------------------------------------------------------------------------- #
RETRIEVAL_NODES = {"execute_tools", "execute_tool", "dispatch_searches",
                   "search_worker", "gather_searches"}


def _node_ms(v) -> float:
    if isinstance(v, dict):
        return float(v.get("sum_ms") or 0.0)
    return float(v or 0.0)


def run_metrics(r: dict) -> dict:
    usage = r.get("model_usage") or []
    thinking = sum(1 for e in usage if str(e.get("purpose") or "").endswith("#think"))
    pro_calls = sum(1 for e in usage if e.get("model") == PRO_MODEL)
    untagged = sum(1 for e in usage
                   if "#" not in str(e.get("purpose") or "") )
    g = r.get("grounding") or {}
    v = r.get("verdict") or {}
    tool_ev = r.get("tool_call_events") or []
    nl = r.get("node_latencies") or {}
    retrieval_ms = sum(_node_ms(val) for k, val in nl.items() if k in RETRIEVAL_NODES)
    trace = r.get("tool_trace") or []
    return {
        "case_id": r.get("case_id"),
        "category": r.get("category"),
        "arm": r.get("arm"),
        "repeat": r.get("repeat"),
        "ok": bool(r.get("ab_ok")),
        "error": r.get("ab_error"),
        "llm_calls": len(usage),
        "thinking_calls": thinking,
        "pro_calls": pro_calls,
        "unrouted_calls": untagged,
        "tokens_in": r.get("tokens_in") or 0,
        "tokens_out": r.get("tokens_out") or 0,
        "tokens_total": (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0),
        "cached_tokens": sum((e.get("cached_tokens") or 0) for e in usage),
        "cost_usd": r.get("cost_usd"),
        "turn_latency_ms": r.get("turn_latency_ms"),
        "wall_ms": r.get("ab_wall_ms"),
        "retrieval_stage_ms": retrieval_ms,
        "grounded": g.get("grounded_claims") or 0,
        "verifiable": g.get("total_verifiable_claims") or 0,
        "contradicted": g.get("contradicted") or 0,
        "money_grounded": g.get("money_grounded") or 0,
        "money_total": g.get("money_total") or 0,
        "task_completed": bool(v.get("task_completed")),
        "task_completed_int": 1 if v.get("task_completed") else 0,
        "passed": bool(r.get("passed")),
        "passed_int": 1 if r.get("passed") else 0,
        "one": 1,
        "cache_hits": r.get("cache_hits") or 0,
        "cache_misses": r.get("cache_misses") or 0,
        "tool_calls": len(tool_ev),
        "tool_fail": sum(1 for e in tool_ev if not e.get("success")),
        "tools_executed": list(r.get("tools_executed") or r.get("tools_called") or []),
        "n_tools_executed": len(r.get("tools_executed") or r.get("tools_called") or []),
        "max_batch_size": max((len(b) for b in trace), default=0),
        "tool_batches": len(trace),
        "timed_out": bool(r.get("tools_timed_out")),
        "soft_wrapped": bool(r.get("soft_wrapped")),
        "started_at": r.get("ab_started_at"),
        "finished_at": r.get("ab_finished_at"),
    }


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def cluster_bootstrap(by_case: Dict[str, dict], stat: Callable[[List[dict]], Optional[float]],
                      n_boot: int = 2000, seed: int = 20260804) -> dict:
    """Percentile bootstrap over CASES. ``by_case`` maps case_id -> payload consumed by
    ``stat``; ``stat`` receives the list of payloads for one resample."""
    cases = sorted(by_case)
    point = stat([by_case[c] for c in cases])
    if point is None or not cases:
        return {"point": point, "ci_low": None, "ci_high": None, "n_cases": len(cases),
                "n_boot": n_boot, "seed": seed}
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        sample = [by_case[cases[rng.randrange(len(cases))]] for _ in cases]
        val = stat(sample)
        if val is not None:
            draws.append(val)
    return {"point": point,
            "ci_low": percentile(draws, 0.025), "ci_high": percentile(draws, 0.975),
            "n_cases": len(cases), "n_boot": n_boot, "seed": seed,
            "n_draws": len(draws)}


def crosses_zero(ci: dict) -> Optional[bool]:
    lo, hi = ci.get("ci_low"), ci.get("ci_high")
    if lo is None or hi is None:
        return None
    return lo <= 0.0 <= hi


# --------------------------------------------------------------------------- #
def build_study(runs: List[dict], arm_base: str, arm_test: str, *,
                n_boot: int = 2000, seed: int = 20260804) -> dict:
    """arm_base = the control (baseline / serial); arm_test = the treatment
    (routed / parallel). Differences are reported as test - base."""
    m = [run_metrics(r) for r in runs]
    ok = [x for x in m if x["ok"]]
    per_arm: Dict[str, List[dict]] = {arm_base: [], arm_test: []}
    for x in ok:
        if x["arm"] in per_arm:
            per_arm[x["arm"]].append(x)

    def arm_block(rows: List[dict]) -> dict:
        lat = [x["turn_latency_ms"] for x in rows]
        rstage = [x["retrieval_stage_ms"] for x in rows]
        gr_num = sum(x["grounded"] for x in rows)
        gr_den = sum(x["verifiable"] for x in rows)
        mo_num = sum(x["money_grounded"] for x in rows)
        mo_den = sum(x["money_total"] for x in rows)
        ch = sum(x["cache_hits"] for x in rows)
        cm = sum(x["cache_misses"] for x in rows)
        tc = sum(x["tool_calls"] for x in rows)
        tf = sum(x["tool_fail"] for x in rows)
        return {
            "n_runs": len(rows),
            "llm_calls_total": sum(x["llm_calls"] for x in rows),
            "thinking_calls_total": sum(x["thinking_calls"] for x in rows),
            "pro_calls_total": sum(x["pro_calls"] for x in rows),
            "unrouted_calls_total": sum(x["unrouted_calls"] for x in rows),
            "tokens_in_total": sum(x["tokens_in"] for x in rows),
            "tokens_out_total": sum(x["tokens_out"] for x in rows),
            "tokens_total": sum(x["tokens_total"] for x in rows),
            "cached_tokens_total": sum(x["cached_tokens"] for x in rows),
            "cost_usd_total": sum((x["cost_usd"] or 0.0) for x in rows),
            "cost_usd_mean_per_run": mean([x["cost_usd"] for x in rows]),
            "e2e_ms": {"mean": mean(lat), "p50": percentile(lat, 0.5),
                       "p95": percentile(lat, 0.95), "n": len([v for v in lat if v is not None])},
            "retrieval_stage_ms": {"mean": mean(rstage), "p50": percentile(rstage, 0.5),
                                   "p95": percentile(rstage, 0.95), "n": len(rstage)},
            # Same metric restricted to runs that ACTUALLY executed a tool batch. Runs with
            # no execute_tools span contribute a structural 0 that drags the median to ~0
            # and makes the p50 unreadable; both views are reported, with their denominators.
            "retrieval_stage_ms_toolruns_only": {
                "mean": mean([v for v in rstage if v]),
                "p50": percentile([v for v in rstage if v], 0.5),
                "p95": percentile([v for v in rstage if v], 0.95),
                "n": len([v for v in rstage if v])},
            "grounded_rate": {"num": gr_num, "den": gr_den,
                              "rate": (gr_num / gr_den) if gr_den else None},
            "money_grounded_rate": {"num": mo_num, "den": mo_den,
                                    "rate": (mo_num / mo_den) if mo_den else None},
            "contradicted_total": sum(x["contradicted"] for x in rows),
            "task_completed": {"num": sum(1 for x in rows if x["task_completed"]),
                               "den": len(rows)},
            "constraint_pass": {"num": sum(1 for x in rows if x["passed"]), "den": len(rows)},
            "cache_hit_rate": {"num": ch, "den": ch + cm,
                               "rate": (ch / (ch + cm)) if (ch + cm) else None},
            "tool_failure_rate": {"num": tf, "den": tc,
                                  "rate": (tf / tc) if tc else None},
            "tool_calls_total": tc,
        }

    # ---- paired per case -------------------------------------------------- #
    cases = sorted({x["case_id"] for x in ok})
    by_case: Dict[str, dict] = {}
    for c in cases:
        b = [x for x in per_arm[arm_base] if x["case_id"] == c]
        t = [x for x in per_arm[arm_test] if x["case_id"] == c]
        if not b or not t:
            continue          # unpaired case: excluded from the paired statistics
        by_case[c] = {"base": b, "test": t}

    def paired_mean_diff(field: str, transform=lambda v: v):
        def stat(sample: List[dict]) -> Optional[float]:
            diffs = []
            for s in sample:
                mb = mean([transform(x[field]) for x in s["base"]])
                mt = mean([transform(x[field]) for x in s["test"]])
                if mb is None or mt is None:
                    continue
                diffs.append(mt - mb)
            return mean(diffs)
        return stat

    def paired_pct_change(field: str):
        """(test - base)/base computed on the POOLED sums of the resample (a ratio of
        totals, not a mean of per-case ratios, so a near-zero denominator case cannot
        blow the statistic up)."""
        def stat(sample: List[dict]) -> Optional[float]:
            sb = sum(sum((x[field] or 0) for x in s["base"]) for s in sample)
            st = sum(sum((x[field] or 0) for x in s["test"]) for s in sample)
            if not sb:
                return None
            return (st - sb) / sb
        return stat

    def pooled_percentile_diff(field: str, q: float):
        def stat(sample: List[dict]) -> Optional[float]:
            vb = [x[field] for s in sample for x in s["base"]]
            vt = [x[field] for s in sample for x in s["test"]]
            pb, pt = percentile(vb, q), percentile(vt, q)
            if pb is None or pt is None:
                return None
            return pt - pb
        return stat

    def pooled_rate_diff(num_field: str, den_field: str):
        def stat(sample: List[dict]) -> Optional[float]:
            nb = sum(x[num_field] for s in sample for x in s["base"])
            db = sum(x[den_field] for s in sample for x in s["base"])
            nt = sum(x[num_field] for s in sample for x in s["test"])
            dt = sum(x[den_field] for s in sample for x in s["test"])
            if not db or not dt:
                return None
            return (nt / dt) - (nb / db)
        return stat

    boots = {
        "e2e_mean_ms_diff": cluster_bootstrap(by_case, paired_mean_diff("turn_latency_ms"),
                                              n_boot, seed),
        "e2e_p50_ms_diff": cluster_bootstrap(by_case, pooled_percentile_diff("turn_latency_ms", 0.5),
                                             n_boot, seed),
        "e2e_p95_ms_diff": cluster_bootstrap(by_case, pooled_percentile_diff("turn_latency_ms", 0.95),
                                             n_boot, seed),
        "retrieval_mean_ms_diff": cluster_bootstrap(by_case, paired_mean_diff("retrieval_stage_ms"),
                                                    n_boot, seed),
        "retrieval_p50_ms_diff": cluster_bootstrap(by_case, pooled_percentile_diff("retrieval_stage_ms", 0.5),
                                                   n_boot, seed),
        "retrieval_p95_ms_diff": cluster_bootstrap(by_case, pooled_percentile_diff("retrieval_stage_ms", 0.95),
                                                   n_boot, seed),
        "cost_pct_change": cluster_bootstrap(by_case, paired_pct_change("cost_usd"), n_boot, seed),
        "tokens_total_pct_change": cluster_bootstrap(by_case, paired_pct_change("tokens_total"),
                                                     n_boot, seed),
        "tokens_out_pct_change": cluster_bootstrap(by_case, paired_pct_change("tokens_out"),
                                                   n_boot, seed),
        "llm_calls_pct_change": cluster_bootstrap(by_case, paired_pct_change("llm_calls"),
                                                  n_boot, seed),
        "grounded_rate_diff": cluster_bootstrap(by_case, pooled_rate_diff("grounded", "verifiable"),
                                                n_boot, seed),
        "money_grounded_rate_diff": cluster_bootstrap(
            by_case, pooled_rate_diff("money_grounded", "money_total"), n_boot, seed),
        "task_completed_rate_diff": cluster_bootstrap(
            by_case, pooled_rate_diff("task_completed_int", "one"), n_boot, seed),
        "constraint_pass_rate_diff": cluster_bootstrap(
            by_case, pooled_rate_diff("passed_int", "one"), n_boot, seed),
        "n_tools_executed_diff": cluster_bootstrap(by_case, paired_mean_diff("n_tools_executed"),
                                                   n_boot, seed),
    }
    boots = {k: v for k, v in boots.items() if v is not None}
    for k, v in boots.items():
        v["crosses_zero"] = crosses_zero(v)

    failures = [x for x in m if not x["ok"]]
    return {
        "arm_base": arm_base, "arm_test": arm_test,
        "runs_total": len(m), "runs_ok": len(ok), "runs_failed": len(failures),
        "failures_by_arm": {a: sum(1 for x in failures if x["arm"] == a)
                            for a in (arm_base, arm_test)},
        "failure_reasons": _reason_counts(failures),
        "cases_seen": len(cases), "cases_paired": len(by_case),
        "per_arm": {arm_base: arm_block(per_arm[arm_base]),
                    arm_test: arm_block(per_arm[arm_test])},
        "bootstrap_test_minus_base": boots,
    }


def _reason_counts(failures: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for f in failures:
        e = (f.get("error") or "unknown")
        key = e.split(":")[0][:60]
        if "reasoning_content" in e:
            key = "HTTP400_reasoning_content_not_echoed"
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--arm-base", required=True)
    p.add_argument("--arm-test", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20260804)
    a = p.parse_args(argv)
    runs = load_runs([Path(x) for x in a.runs])
    study = build_study(runs, a.arm_base, a.arm_test, n_boot=a.n_boot, seed=a.seed)
    study["source_files"] = list(a.runs)
    Path(a.out).write_text(json.dumps(study, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: study[k] for k in
                      ("runs_total", "runs_ok", "runs_failed", "cases_paired")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
