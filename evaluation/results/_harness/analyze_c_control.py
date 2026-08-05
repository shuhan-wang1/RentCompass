"""Experiment C — the negative control plus two pre-registered robustness views.

PRIMARY (design-frozen): every (case, repeat) pair, comparing executed tool-call count and
task_completed between the serial and parallel arms. The GOAL asks for the number of
DISAGREEING pairs, so that is what this reports first.

SECONDARY, clearly labelled as such and NOT replacing the primary latency result:
  (a) the same latency contrast restricted to pairs whose executed tool-call counts MATCH
      (i.e. the pairs where "same input, same evidence" actually held), and
  (b) the same contrast restricted to runs in which a batch of >=2 tool calls actually
      occurred -- the only runs where dispatch concurrency can do anything at all.
Both keep the same bootstrap (cluster over cases, 2000 resamples, seed 20260804).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import (cluster_bootstrap, crosses_zero, load_runs, mean,  # noqa: E402
                     percentile, run_metrics)

SERIAL, PARALLEL = "serial_tools", "parallel_tools"


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    runs_paths = [Path(p) for p in argv[:-1]]
    out = Path(argv[-1])
    rows = load_runs(runs_paths)
    m = [run_metrics(r) for r in rows]
    raw = {(r.get("case_id"), r.get("repeat"), r.get("arm")): r for r in rows}

    by_pair = defaultdict(dict)
    for x in m:
        by_pair[(x["case_id"], x["repeat"])][x["arm"]] = x

    pairs, tc_mis, comp_mis, detail = 0, 0, 0, []
    for key, v in sorted(by_pair.items()):
        if SERIAL not in v or PARALLEL not in v:
            continue
        pairs += 1
        s, p = v[SERIAL], v[PARALLEL]
        if s["n_tools_executed"] != p["n_tools_executed"]:
            tc_mis += 1
            detail.append({"case_id": key[0], "repeat": key[1],
                           "serial_tools_executed": s["n_tools_executed"],
                           "parallel_tools_executed": p["n_tools_executed"],
                           "serial_list": s["tools_executed"],
                           "parallel_list": p["tools_executed"]})
        if s["task_completed"] != p["task_completed"]:
            comp_mis += 1

    def arm_timeouts(arm):
        rs = [r for r in rows if r.get("arm") == arm]
        return {"runs": len(rs),
                "budget_timeout_events": sum(len(r.get("budget_timeout_events") or []) for r in rs),
                "tools_timed_out": sum(len(r.get("tools_timed_out") or []) for r in rs),
                "tools_denied": sum(len(r.get("tools_denied") or []) for r in rs),
                "soft_wrapped_runs": sum(1 for r in rs if r.get("soft_wrapped")),
                "retry_count_total": sum((e.get("retry_count") or 0)
                                         for r in rs for e in (r.get("model_usage") or [])),
                "tool_call_events": sum(len(r.get("tool_call_events") or []) for r in rs),
                "tool_call_failures": sum(1 for r in rs
                                          for e in (r.get("tool_call_events") or [])
                                          if not e.get("success"))}

    def subset_study(keep, label):
        by_case = defaultdict(lambda: {"base": [], "test": []})
        for key, v in by_pair.items():
            if SERIAL not in v or PARALLEL not in v or not keep(v):
                continue
            by_case[key[0]]["base"].append(v[SERIAL])
            by_case[key[0]]["test"].append(v[PARALLEL])
        by_case = {k: v for k, v in by_case.items() if v["base"] and v["test"]}

        def paired_mean(field):
            def stat(sample):
                d = []
                for s in sample:
                    a, b = mean([x[field] for x in s["base"]]), mean([x[field] for x in s["test"]])
                    if a is not None and b is not None:
                        d.append(b - a)
                return mean(d)
            return stat

        def pooled_pct(field, q):
            def stat(sample):
                vb = [x[field] for s in sample for x in s["base"]]
                vt = [x[field] for s in sample for x in s["test"]]
                pb, pt = percentile(vb, q), percentile(vt, q)
                return None if pb is None or pt is None else pt - pb
            return stat

        n_pairs = sum(len(v["base"]) for v in by_case.values())
        base_vals = [x["retrieval_stage_ms"] for v in by_case.values() for x in v["base"]]
        test_vals = [x["retrieval_stage_ms"] for v in by_case.values() for x in v["test"]]
        blocks = {
            "retrieval_mean_ms_diff": cluster_bootstrap(by_case, paired_mean("retrieval_stage_ms")),
            "retrieval_p50_ms_diff": cluster_bootstrap(by_case, pooled_pct("retrieval_stage_ms", 0.5)),
            "retrieval_p95_ms_diff": cluster_bootstrap(by_case, pooled_pct("retrieval_stage_ms", 0.95)),
            "e2e_mean_ms_diff": cluster_bootstrap(by_case, paired_mean("turn_latency_ms")),
        }
        for b in blocks.values():
            b["crosses_zero"] = crosses_zero(b)
        return {"label": label, "n_cases": len(by_case), "n_pairs": n_pairs,
                "serial_retrieval_ms": {"mean": mean(base_vals),
                                        "p50": percentile(base_vals, 0.5),
                                        "p95": percentile(base_vals, 0.95)},
                "parallel_retrieval_ms": {"mean": mean(test_vals),
                                          "p50": percentile(test_vals, 0.5),
                                          "p95": percentile(test_vals, 0.95)},
                "bootstrap_parallel_minus_serial": blocks}

    result = {
        "negative_control_primary": {
            "definition": ("per (case, repeat) pair: do the two arms agree on the number of "
                           "EXECUTED tool calls and on task_completed?"),
            "pairs_compared": pairs,
            "tool_count_mismatch_pairs": tc_mis,
            "completion_mismatch_pairs": comp_mis,
            "mismatch_detail": detail,
            "mismatch_direction": dict(Counter(
                "serial_more" if d["serial_tools_executed"] > d["parallel_tools_executed"]
                else "parallel_more" for d in detail)),
        },
        "per_arm_failure_and_budget": {SERIAL: arm_timeouts(SERIAL),
                                       PARALLEL: arm_timeouts(PARALLEL)},
        "secondary_matched_pairs_only": subset_study(
            lambda v: v[SERIAL]["n_tools_executed"] == v[PARALLEL]["n_tools_executed"],
            "pairs whose executed tool-call counts MATCH (the pairs where the "
            "same-input/same-evidence premise actually held)"),
        "secondary_multi_call_batches_only": subset_study(
            lambda v: max(v[SERIAL]["max_batch_size"], v[PARALLEL]["max_batch_size"]) >= 2,
            "pairs in which at least one arm actually produced a batch of >=2 tool calls "
            "(the only runs where dispatch concurrency can matter)"),
        "source_files": [p.as_posix() for p in runs_paths],
    }
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    nc = result["negative_control_primary"]
    print(json.dumps({k: nc[k] for k in ("pairs_compared", "tool_count_mismatch_pairs",
                                         "completion_mismatch_pairs", "mismatch_direction")},
                     indent=2))
    for k in ("secondary_matched_pairs_only", "secondary_multi_call_batches_only"):
        s = result[k]
        print(k, "n_pairs", s["n_pairs"], "n_cases", s["n_cases"],
              "retr_mean", round(s["serial_retrieval_ms"]["mean"] or 0), "->",
              round(s["parallel_retrieval_ms"]["mean"] or 0),
              "CI", s["bootstrap_parallel_minus_serial"]["retrieval_mean_ms_diff"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
