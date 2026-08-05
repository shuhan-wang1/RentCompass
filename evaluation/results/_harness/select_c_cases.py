"""Experiment C case selection — applies the rule frozen in PROGRESS.log before any C run.

RULE (verbatim from the design freeze): a case QUALIFIES when, in Experiment A's ROUTED
(production) runs, at least one executed tool batch contained >=2 tool calls, i.e.
RunResult.tool_trace has a batch of length >=2. Independence is implied by construction:
the model emitted those calls in ONE batch, so none of them needed another's output.
Primary threshold: qualifies in >=2 of its 3 repeats.

Reports the qualifying counts at >=1/3, >=2/3 and 3/3, plus a purely descriptive
secondary view (batches holding >=2 DISTINCT tool names) and a static cross-check against
each case's declared expected_tools. The secondary views do NOT change the rule.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--arm", default="routed_models")
    p.add_argument("--cases", default="evaluation/benchmark/cases.jsonl")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)

    rows = []
    seen = set()
    for path in a.runs:
        pth = Path(path)
        if not pth.exists():
            continue
        for line in pth.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            k = d.get("ab_run_key")
            if k in seen:
                continue
            seen.add(k)
            rows.append(d)

    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
    by_id = {c["case_id"]: c for c in cases}

    per_case = defaultdict(lambda: {"repeats_ok": 0, "repeats_multi": 0,
                                    "repeats_multi_distinct": 0, "max_batch": 0,
                                    "batch_sizes": [], "repeats_seen": []})
    for r in rows:
        if r.get("arm") != a.arm or not r.get("ab_ok"):
            continue
        cid = r.get("case_id")
        e = per_case[cid]
        e["repeats_ok"] += 1
        e["repeats_seen"].append(r.get("repeat"))
        trace = r.get("tool_trace") or []
        sizes = [len(b) for b in trace]
        e["batch_sizes"].extend(sizes)
        biggest = max(sizes, default=0)
        e["max_batch"] = max(e["max_batch"], biggest)
        if biggest >= 2:
            e["repeats_multi"] += 1
        if any(len(set(b)) >= 2 for b in trace):
            e["repeats_multi_distinct"] += 1

    qual_1, qual_2, qual_3 = [], [], []
    for cid, e in per_case.items():
        if e["repeats_multi"] >= 1:
            qual_1.append(cid)
        if e["repeats_multi"] >= 2:
            qual_2.append(cid)
        if e["repeats_multi"] >= 3:
            qual_3.append(cid)

    static = [c["case_id"] for c in cases if len(c.get("expected_tools") or []) >= 2]

    out = {
        "rule": ("a case qualifies when, in Experiment A's routed_models runs, at least one "
                 "executed tool batch held >=2 tool calls (RunResult.tool_trace batch length "
                 ">=2); primary threshold = qualifies in >=2 of its 3 repeats"),
        "arm_examined": a.arm,
        "cases_with_at_least_one_ok_run": len(per_case),
        "qualifying_at_least_1_of_3": sorted(qual_1),
        "qualifying_at_least_2_of_3": sorted(qual_2),
        "qualifying_3_of_3": sorted(qual_3),
        "counts": {"ge1_of_3": len(qual_1), "ge2_of_3": len(qual_2), "3_of_3": len(qual_3)},
        "secondary_view_distinct_tool_names": {
            "ge1_of_3": sorted(cid for cid, e in per_case.items()
                               if e["repeats_multi_distinct"] >= 1),
            "ge2_of_3": sorted(cid for cid, e in per_case.items()
                               if e["repeats_multi_distinct"] >= 2),
        },
        "static_crosscheck_expected_tools_ge2": sorted(static),
        "static_crosscheck_count": len(static),
        "batch_size_histogram_all_routed_runs": dict(
            Counter(s for e in per_case.values() for s in e["batch_sizes"])),
        "per_case": {cid: {k: v for k, v in e.items() if k != "batch_sizes"}
                     for cid, e in sorted(per_case.items())},
        "category_of_qualifying_ge2": dict(Counter(
            by_id.get(cid, {}).get("category") for cid in qual_2)),
        "source_files": list(a.runs),
    }
    Path(a.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out["counts"], indent=2))
    print("qualifying (>=2/3):", ",".join(sorted(qual_2)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
