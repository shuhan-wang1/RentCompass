"""Analyze a completed held-out v5 run using the frozen deterministic metric module.

This utility only loads immutable cases, fixtures, saved run records and saved evidence.
It does not invoke a model or alter the metric definitions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import holdout_v3_metrics as metrics


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fixture_records(root: Path, case: dict[str, Any]) -> list[dict[str, Any]]:
    names = case.get("fixture")
    names = [names] if isinstance(names, str) else list(names or [])
    out: list[dict[str, Any]] = []
    for name in names:
        raw = json.loads((root / name).read_text(encoding="utf-8"))
        out.extend(raw.get("results", [raw]) if isinstance(raw, dict) else [])
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--raw-runs", type=Path, required=True)
    parser.add_argument("--grader-input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    cases = load_jsonl(args.cases)
    runs = load_jsonl(args.raw_runs)
    packets = load_jsonl(args.grader_input)
    expected = {case["case_id"] for case in cases}
    run_counts = Counter(row.get("case_id") for row in runs)
    packet_counts = Counter(row.get("case_id") for row in packets)
    bad_runs = sorted(case_id for case_id, count in run_counts.items() if count != 1)
    bad_packets = sorted(case_id for case_id, count in packet_counts.items() if count != 1)
    if set(run_counts) != expected or set(packet_counts) != expected or bad_runs or bad_packets:
        raise SystemExit("run or evidence records are not exactly one per frozen case")
    run_by_case = {row["case_id"]: row for row in runs}
    packet_by_case = {row["case_id"]: row for row in packets}
    scored = []
    for case in cases:
        packet = packet_by_case[case["case_id"]]
        scored.append(metrics.grade_case(
            case,
            run_by_case[case["case_id"]],
            fixture_records(args.fixtures, case),
            packet.get("evidence") or [],
        ))
    summary = metrics.summarize(scored)
    summary["run_validity"] = {
        "n_cases": len(cases),
        "n_runs": len(runs),
        "n_evidence_packets": len(packets),
        "runner_errors": sum(bool(run.get("error")) for run in runs),
        "case_sha256": sha(args.cases),
        "raw_runs_sha256": sha(args.raw_runs),
        "grader_input_sha256": sha(args.grader_input),
    }
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "per_case_metrics.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in scored), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
