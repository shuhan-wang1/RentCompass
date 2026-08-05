"""Static gate for the preregistered held-out v3 benchmark.

Run before a formal model request.  A non-zero exit means replace/fix cases and run the
gate again; it is never permission to relabel failed cases N/A after observing outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import holdout_v3_metrics as metrics  # noqa: E402
import constraint_schema_v2 as hard  # noqa: E402

DEFAULT_CASE_SCHEMA = "rentcompass/benchmark/v3"
REQUIRED = {
    "case_id", "schema_version", "task_category", "user_id", "user_query",
    "conversation_history", "expected_tools", "forbidden_tools", "expected_constraints",
    "hard_constraint_slots", "correct_completion", "completion_oracle",
    "metric_eligibility", "failure_conditions", "allowed_evidence_sources",
    "reference_calculations", "novelty_note", "fixture",
}
VALID_CATEGORIES = {"retrieval_hard", "retrieval_soft", "calculation", "memory", "clarify"}
VALID_ORACLES = {"retrieval_exact_set", "calculation", "memory_write", "clarification"}
VALID_TOOL_CONTRACTS = {"commute_per_search_candidate", "remember_write"}
HARD_SLOT_MINIMUM = 30


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{no}: case must be an object")
            rows.append(row)
    return rows


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(x) for x in (value or []) if isinstance(x, str)]


def _records(case: dict, fixtures: Path) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for name in _names(case.get("fixture")):
        path = fixtures / name
        if not path.is_file():
            problems.append(f"fixture absent: {name}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append(f"fixture invalid JSON: {name}")
            continue
        items = data.get("results") if isinstance(data, dict) and isinstance(data.get("results"), list) else [data]
        rows.extend(x for x in items if isinstance(x, dict))
    return rows, problems


def _fixture_fingerprint(records: Iterable[dict]) -> tuple[set[str], set[float], set[str]]:
    ids, prices, addresses = set(), set(), set()
    for record in records:
        data = record.get("data") if isinstance(record, dict) else None
        if not isinstance(data, dict):
            continue
        for key in ("recommendations", "over_budget_alternatives"):
            for listing in data.get(key) or []:
                if not isinstance(listing, dict):
                    continue
                if metrics.listing_id(listing):
                    ids.add(metrics.listing_id(listing))
                price = listing.get("price_raw")
                if isinstance(price, (int, float)) and not isinstance(price, bool):
                    prices.add(float(price))
                address = listing.get("address")
                if isinstance(address, str) and address.strip():
                    addresses.add(address.strip().casefold())
    return ids, prices, addresses


def _check_case(case: dict, fixtures: Path, global_ids: set[str], schema_version: str) -> list[str]:
    out = []
    missing = sorted(REQUIRED - set(case))
    if missing:
        out.append(f"M1 missing required fields {missing}")
    if case.get("schema_version") != schema_version:
        out.append(f"M2 schema_version must be {schema_version!r}")
    if case.get("task_category") not in VALID_CATEGORIES:
        out.append("M3 invalid task_category")
    if not isinstance(case.get("completion_oracle"), dict) or case["completion_oracle"].get("kind") not in VALID_ORACLES:
        out.append("C1 completion_oracle kind is absent or invalid")
    eligible = case.get("metric_eligibility")
    if not isinstance(eligible, list) or not eligible or set(eligible) - set(metrics.METRICS):
        out.append("Q1 metric_eligibility must be a nonempty subset of the frozen metric vocabulary")
    contract = case.get("required_tool_contract") or {}
    if contract and contract.get("kind") not in VALID_TOOL_CONTRACTS:
        out.append("D1 invalid required_tool_contract kind")
    if "required_tool_completion" in (eligible or []) and not contract:
        out.append("D2 required_tool_completion needs a required_tool_contract")
    if case.get("task_category", "").startswith("retrieval") and case.get("completion_oracle", {}).get("kind") != "retrieval_exact_set":
        out.append("C2 retrieval task must use retrieval_exact_set completion oracle")
    if case.get("task_category") == "calculation" and case.get("completion_oracle", {}).get("kind") != "calculation":
        out.append("C3 calculation task must use calculation completion oracle")
    if case.get("task_category") == "memory" and case.get("completion_oracle", {}).get("kind") != "memory_write":
        out.append("C4 formal memory stratum is a write-completion stratum")
    if case.get("task_category") == "clarify" and case.get("completion_oracle", {}).get("kind") != "clarification":
        out.append("C5 clarify task must use clarification completion oracle")
    for c in hard.user_hard_constraints(case):
        out.extend(hard.arg_domain_problems(c))
    out.extend(hard.explicitness_problems(case))
    out.extend(hard.contradictions(hard.user_hard_constraints(case)))
    records, record_problems = _records(case, fixtures)
    out.extend("E1 " + item for item in record_problems)
    ids, _, _ = _fixture_fingerprint(records)
    if case.get("task_category", "").startswith("retrieval"):
        if not records:
            out.append("E2 retrieval case has no frozen fixture records")
        if not ids and case.get("completion_oracle", {}).get("kind") == "retrieval_exact_set" and records:
            # Empty-result retrievals are valid, but a nonempty listing payload cannot lack IDs.
            has_listing = any((r.get("data") or {}).get("recommendations") for r in records if isinstance(r, dict))
            if has_listing:
                out.append("E3 retrieval listing lacks eval_listing_id")
    duplicate = sorted(ids & global_ids)
    if duplicate:
        out.append(f"E4 eval_listing_id repeats across formal set: {duplicate}")
    global_ids.update(ids)
    if contract.get("kind") == "commute_per_search_candidate":
        commute_records = [r for r in records if r.get("tool_name") == "calculate_commute"]
        if not commute_records:
            out.append("D3 commute contract needs frozen calculate_commute evidence")
    return out


def check(cases: list[dict], fixtures: Path, comparison_cases: Iterable[dict] = (),
          schema_version: str = DEFAULT_CASE_SCHEMA) -> dict:
    problems: dict[str, list[str]] = {}
    seen, global_ids = set(), set()
    counts, tool_counts, slot_counts = Counter(), Counter(), Counter()
    source_queries, source_ids, source_prices, source_addresses = set(), set(), set(), set()
    for old in comparison_cases:
        source_queries.add(str(old.get("user_query") or "").strip().casefold())
    # The caller may place comparison fixture paths in a separate report; comparison here is
    # intentionally query-level.  v3's manifest audit adds fixture/listing fingerprint checks.
    for case in cases:
        cid = case.get("case_id")
        errs = _check_case(case, fixtures, global_ids, schema_version)
        if cid in seen:
            errs.append("M4 duplicate case_id")
        seen.add(cid)
        if str(case.get("user_query") or "").strip().casefold() in source_queries:
            errs.append("N1 verbatim query overlaps a development/earlier-holdout case")
        for metric in case.get("metric_eligibility") or []:
            counts[metric] += 1
        for con in hard.user_hard_constraints(case):
            slot = hard.slot_of(con)
            if slot:
                slot_counts[slot] += 1
        contract = (case.get("required_tool_contract") or {}).get("kind")
        if "required_tool_completion" in (case.get("metric_eligibility") or []):
            tool_counts[contract] += 1
        if errs:
            problems[str(cid)] = errs
    quota = {metric: counts[metric] for metric in metrics.METRICS}
    for metric, n in quota.items():
        if n < metrics.PRIMARY_MIN_DENOMINATOR:
            problems.setdefault("__quota__", []).append(
                f"Q2 {metric} denominator {n} < {metrics.PRIMARY_MIN_DENOMINATOR}")
    for kind in ("commute_per_search_candidate", "remember_write"):
        if tool_counts[kind] < metrics.PRIMARY_MIN_DENOMINATOR:
            problems.setdefault("__quota__", []).append(
                f"Q3 {kind} denominator {tool_counts[kind]} < {metrics.PRIMARY_MIN_DENOMINATOR}")
    for slot in hard.SLOT_MIN_COVERAGE:
        if slot_counts[slot] < HARD_SLOT_MINIMUM:
            problems.setdefault("__quota__", []).append(
                f"Q4 hard-constraint slot {slot} coverage {slot_counts[slot]} < {HARD_SLOT_MINIMUM}")
    return {"gate_passed": not problems, "n_cases": len(cases), "problems": problems,
            "metric_denominators": quota, "tool_contract_denominators": dict(tool_counts),
            "hard_slot_coverage": {slot: slot_counts[slot] for slot in hard.SLOT_MIN_COVERAGE},
            "frozen_schema": schema_version}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--compare-cases", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--schema-version", default=DEFAULT_CASE_SCHEMA,
                        help="case schema_version required by this run")
    args = parser.parse_args(argv)
    cases = _load_jsonl(args.cases)
    comparison = []
    for path in args.compare_cases:
        comparison.extend(_load_jsonl(path))
    report = check(cases, args.fixtures, comparison, args.schema_version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("gate_passed", "n_cases", "metric_denominators", "tool_contract_denominators")}, ensure_ascii=False))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
