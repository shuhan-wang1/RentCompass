"""Deterministic scoring contract for the preregistered held-out v3 benchmark.

This module deliberately scores the product's structured response contract, rather than
trying to infer recommendation membership from answer prose.  A missing run, missing
structured field, unknown listing ID, duplicate ID or runtime error is a recorded FAIL
in the frozen denominator; it is never silently removed as N/A.

The hard-constraint predicates are the already-frozen v2 predicates.  What changes in
v3 is the *measurement unit*: unique ``eval_listing_id`` values and explicit tool/output
contracts replace price/text matching.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping

from constraint_schema_v2 import FAIL, PASS, UNKNOWN, TYPES, user_hard_constraints

METRICS = (
    "eligible_recall",
    "recommendation_precision",
    "complete_constraint_satisfaction",
    "required_tool_completion",
    "unsupported_numeric_control",
    "task_completion",
)
PRIMARY_MIN_DENOMINATOR = 30

_MONEY = re.compile(r"(?:GBP\s*|£\s*)([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
_MINUTES = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:minutes?|mins?)\b", re.I)
_NO_RESULT = ("no match", "no results", "no listings", "none found", "couldn't find",
              "could not find", "没有", "无匹配", "查不到", "无法找到")


def listing_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("eval_listing_id")
    return str(value).strip() if isinstance(value, str) and str(value).strip() else None


def candidate_key(row: Mapping[str, Any]) -> str:
    url = str(row.get("url") or row.get("URL") or "").strip().casefold().rstrip("/")
    if url:
        return f"url:{url}"
    address = " ".join(str(row.get("address") or row.get("Address") or "").split()).casefold()
    price = " ".join(str(row.get("price") or row.get("Price") or "").split()).casefold()
    return f"address:{address}|price:{price}" if address else ""


def _fixture_data(records: Iterable[Mapping[str, Any]]) -> List[dict]:
    out: List[dict] = []
    for raw in records or []:
        data = raw.get("data") if isinstance(raw, Mapping) else None
        if isinstance(data, dict):
            out.append(data)
    return out


def _fixture_listings(records: Iterable[Mapping[str, Any]]) -> List[dict]:
    rows: List[dict] = []
    for data in _fixture_data(records):
        for key in ("recommendations", "over_budget_alternatives"):
            rows.extend(row for row in (data.get(key) or []) if isinstance(row, dict))
    return rows


def _commute_records(records: Iterable[Mapping[str, Any]]) -> List[dict]:
    out: List[dict] = []
    for raw in records or []:
        if raw.get("tool_name") == "calculate_commute" and isinstance(raw.get("data"), dict):
            out.append(raw["data"])
    return out


def _commute_for(listing: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> List[dict]:
    lid, key = listing_id(listing), candidate_key(listing)
    address = " ".join(str(listing.get("address") or "").split()).casefold()
    out = []
    for rec in records:
        if lid and rec.get("origin_eval_listing_id") == lid:
            out.append(rec)
        elif key and rec.get("candidate_key") == key:
            out.append(rec)
        elif address and " ".join(str(rec.get("from_address") or "").split()).casefold() == address:
            out.append(rec)
    return out


def truth(case: Mapping[str, Any], fixture_records: Iterable[Mapping[str, Any]]) -> dict:
    """Classify every frozen listing independently of the product's own state."""
    listings = _fixture_listings(fixture_records)
    commutes = _commute_records(fixture_records)
    constraints = user_hard_constraints(dict(case))
    eligible, unknown, excluded, malformed = set(), set(), set(), []
    for row in listings:
        lid = listing_id(row)
        if not lid:
            malformed.append("fixture listing missing eval_listing_id")
            continue
        verdicts = []
        for con in constraints:
            spec = TYPES[con["type"]]
            if spec["scope"] == "listing":
                verdicts.append(spec["predicate"](row, con))
            else:
                matches = _commute_for(row, commutes)
                if not matches:
                    verdicts.append(UNKNOWN)
                else:
                    # A listing is safely eligible only when every returned commute
                    # observation relevant to it meets the bound.  Any unknown stays unknown.
                    vals = [spec["predicate"](rec, con) for rec in matches]
                    verdicts.append(FAIL if FAIL in vals else (PASS if PASS in vals else UNKNOWN))
        if not constraints or all(v == PASS for v in verdicts):
            eligible.add(lid)
        elif FAIL in verdicts:
            excluded.add(lid)
        else:
            unknown.add(lid)
    return {"eligible": eligible, "excluded": excluded, "unknown": unknown,
            "listings": listings, "malformed": malformed}


def _output_ids(run: Mapping[str, Any], require_payload: bool) -> tuple[set[str], List[str]]:
    data = run.get("tool_data")
    if not isinstance(data, dict):
        return set(), (["tool_data is not an object"] if require_payload else [])
    if "eligible_recommendations" not in data:
        return set(), (["eligible_recommendations missing"] if require_payload else [])
    rows = data.get("eligible_recommendations")
    if not isinstance(rows, list):
        return set(), ["eligible_recommendations is not a list"]
    ids, errors = [], []
    for row in rows:
        if not isinstance(row, dict) or not listing_id(row):
            errors.append("eligible recommendation missing eval_listing_id")
        else:
            ids.append(listing_id(row))
    if len(ids) != len(set(ids)):
        errors.append("duplicate eval_listing_id in eligible recommendations")
    return set(ids), errors


def _successful_event(run: Mapping[str, Any], tool: str) -> bool:
    return any(e.get("tool") == tool and e.get("success") is True and not e.get("timeout")
               for e in (run.get("tool_call_events") or []) if isinstance(e, dict))


def _commute_bound(run: Mapping[str, Any], targets: Iterable[Mapping[str, Any]]) -> bool:
    data = run.get("tool_data") if isinstance(run.get("tool_data"), dict) else {}
    records = data.get("commute_evidence") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return False
    for target in targets:
        wanted_id, wanted_key = listing_id(target), candidate_key(target)
        wanted_address = " ".join(str(target.get("address") or "").split()).casefold()
        found = False
        for rec in records:
            if not isinstance(rec, dict):
                continue
            same = ((wanted_id and rec.get("origin_eval_listing_id") == wanted_id) or
                    (wanted_key and rec.get("candidate_key") == wanted_key) or
                    (wanted_address and " ".join(str(rec.get("from_address") or "").split()).casefold()
                     == wanted_address))
            if same and rec.get("success") is True and str(rec.get("evidence_status") or "success") == "success":
                found = True
                break
        if not found:
            return False
    return _successful_event(run, "calculate_commute")


def _numbers_from_text(text: str) -> tuple[List[float], List[float]]:
    money = [float(m.group(1).replace(",", "")) for m in _MONEY.finditer(text or "")]
    minutes = [float(m.group(1)) for m in _MINUTES.finditer(text or "")]
    return money, minutes


def _walk_numbers(obj: Any, money: List[float], minutes: List[float], key: str = "") -> None:
    if isinstance(obj, dict):
        for k, value in obj.items():
            _walk_numbers(value, money, minutes, str(k).casefold())
    elif isinstance(obj, list):
        for value in obj:
            _walk_numbers(value, money, minutes, key)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if any(x in key for x in ("price", "rent", "deposit", "cost", "amount", "budget")):
            money.append(float(obj))
        if any(x in key for x in ("minute", "duration", "commute", "travel_time")):
            minutes.append(float(obj))
    elif isinstance(obj, str):
        got_money, got_minutes = _numbers_from_text(obj)
        if any(x in key for x in ("price", "rent", "deposit", "cost", "amount", "budget")):
            money.extend(got_money)
        if any(x in key for x in ("minute", "duration", "commute", "travel_time")):
            minutes.extend(got_minutes)


def _allowed_numbers(case: Mapping[str, Any], run: Mapping[str, Any], records: Iterable[Mapping[str, Any]],
                     evidence: Iterable[Mapping[str, Any]]) -> tuple[List[float], List[float]]:
    money: List[float] = []
    minutes: List[float] = []
    for text in [case.get("user_query", "")] + [x.get("content", "") for x in case.get("conversation_history", []) if isinstance(x, dict)]:
        m, t = _numbers_from_text(str(text))
        money.extend(m); minutes.extend(t)
    for obj in [list(records or []), list(evidence or []), run.get("tool_data") or {},
                case.get("reference_calculations") or {}]:
        _walk_numbers(obj, money, minutes)
    # The only permitted derived money figures are the frozen weekly/monthly conversion.
    money.extend([round(x * 52 / 12, 2) for x in list(money)])
    money.extend([round(x * 12 / 52, 2) for x in list(money) if x])
    return money, minutes


def _near(value: float, candidates: Iterable[float], tolerance: float) -> bool:
    return any(abs(value - allowed) <= max(tolerance, abs(allowed) * tolerance) for allowed in candidates)


def _numeric_control(case: Mapping[str, Any], run: Mapping[str, Any], records, evidence) -> tuple[bool, dict]:
    allowed_money, allowed_minutes = _allowed_numbers(case, run, records, evidence)
    claimed_money, claimed_minutes = _numbers_from_text(str(run.get("final_answer") or ""))
    bad_money = [x for x in claimed_money if not _near(x, allowed_money, 0.01)]
    bad_minutes = [x for x in claimed_minutes if not _near(x, allowed_minutes, 0.5)]
    return not bad_money and not bad_minutes, {"unsupported_money": bad_money,
                                                "unsupported_minutes": bad_minutes}


def _task_completion(case: Mapping[str, Any], run: Mapping[str, Any], truth_set: dict,
                     output: set[str], parse_errors: List[str], required_tool: bool) -> bool:
    oracle = case.get("completion_oracle") or {}
    kind = oracle.get("kind")
    answer = str(run.get("final_answer") or "").casefold()
    if kind == "retrieval_exact_set":
        if truth_set["eligible"]:
            return not parse_errors and output == truth_set["eligible"] and _successful_event(run, "search_properties")
        return (not output and _successful_event(run, "search_properties") and
                any(marker in answer for marker in _NO_RESULT))
    if kind == "calculation":
        expected = oracle.get("result")
        if not isinstance(expected, (int, float)):
            return False
        nums = [float(x.replace(",", "")) for x in re.findall(r"[0-9][0-9,]*(?:\.[0-9]+)?", answer)]
        return any(abs(x - float(expected)) <= max(1.0, abs(float(expected)) * .01) for x in nums)
    if kind == "memory_write":
        markers = [str(x).casefold() for x in (oracle.get("ack_markers_any") or ["saved", "记住"])]
        return required_tool and any(marker in answer for marker in markers)
    if kind == "clarification":
        markers = [str(x).casefold() for x in (oracle.get("markers_any") or [])]
        return (str(run.get("response_type") or "") in {"clarification", "question"} and
                (not markers or any(marker in answer for marker in markers)))
    return False


def grade_case(case: Mapping[str, Any], run: Mapping[str, Any] | None, fixture_records,
               evidence: Iterable[Mapping[str, Any]] = ()) -> dict:
    """Grade one frozen case.  Returned ``applicable`` keys are its fixed denominator."""
    applicable = list(case.get("metric_eligibility") or [])
    unknown_metrics = sorted(set(applicable) - set(METRICS))
    errors = list(unknown_metrics)
    if run is None:
        run = {"error": "missing run"}
    if run.get("error"):
        errors.append(f"run error: {run['error']}")
    truth_set = truth(case, fixture_records)
    errors.extend(truth_set["malformed"])
    require_payload = bool(truth_set["eligible"])
    output, parse_errors = _output_ids(run, require_payload=require_payload)
    errors.extend(parse_errors)
    outcomes: Dict[str, dict] = {}
    contract = case.get("required_tool_contract") or {}
    tool_ok = True
    if contract.get("kind") == "commute_per_search_candidate":
        tool_ok = _commute_bound(run, truth_set["listings"])
    elif contract.get("kind") == "remember_write":
        tool_ok = _successful_event(run, "remember")

    for metric in applicable:
        if metric == "eligible_recall":
            den = len(truth_set["eligible"])
            passed = bool(den) and not errors and truth_set["eligible"] <= output
            outcomes[metric] = {"pass": passed, "numerator": len(truth_set["eligible"] & output), "denominator": den}
        elif metric == "recommendation_precision":
            fp = output - truth_set["eligible"]
            outcomes[metric] = {"pass": not errors and not fp, "false_positive_ids": sorted(fp),
                                "numerator": len(output & truth_set["eligible"]), "denominator": len(output)}
        elif metric == "complete_constraint_satisfaction":
            passed = not errors and bool(output) and output <= truth_set["eligible"]
            outcomes[metric] = {"pass": passed, "recommended_ids": sorted(output)}
        elif metric == "required_tool_completion":
            outcomes[metric] = {"pass": not errors and tool_ok, "kind": contract.get("kind")}
        elif metric == "unsupported_numeric_control":
            passed, detail = _numeric_control(case, run, fixture_records, evidence)
            outcomes[metric] = {"pass": not errors and passed, **detail}
        elif metric == "task_completion":
            outcomes[metric] = {"pass": not errors and _task_completion(case, run, truth_set, output, parse_errors, tool_ok),
                                "oracle": (case.get("completion_oracle") or {}).get("kind")}
    composite = bool(applicable) and all(outcomes[m]["pass"] for m in applicable if m in outcomes)
    return {"case_id": case.get("case_id"), "applicable": applicable, "outcomes": outcomes,
            "composite_pass": composite, "errors": errors,
            "truth": {k: sorted(v) if isinstance(v, set) else v for k, v in truth_set.items()},
            "output_eligible_ids": sorted(output)}


def _beta_quantile(a: int, b: int, p: float) -> float:
    """Small dependency-free bisection for Clopper-Pearson via binomial CDF."""
    # P(X <= k) is monotonic; math.comb is fine at the planned denominators.
    k, n = a - 1, a + b - 1
    if a <= 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        cdf = sum(math.comb(n, i) * mid ** i * (1 - mid) ** (n - i) for i in range(k + 1))
        if cdf > p:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def exact_ci(successes: int, n: int) -> dict | None:
    if n <= 0:
        return None
    # Equivalent to Beta quantiles, implemented as binomial inversion.
    def cdf(k: int, p: float) -> float:
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
    def solve(fn):
        lo, hi = 0.0, 1.0
        for _ in range(120):
            mid = (lo + hi) / 2
            if fn(mid) > 0:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2
    lo = 0.0 if successes == 0 else solve(lambda p: (1 - cdf(successes - 1, p)) - .025)
    hi = 1.0 if successes == n else solve(lambda p: cdf(successes, p) - .025)
    return {"successes": successes, "n": n, "rate": successes / n, "lo": lo, "hi": hi,
            "method": "Clopper-Pearson exact binomial 95%"}


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict:
    buckets: Dict[str, List[bool]] = {metric: [] for metric in METRICS}
    composite: List[bool] = []
    by_tool: Dict[str, List[bool]] = {}
    for row in rows:
        for metric, result in (row.get("outcomes") or {}).items():
            buckets.setdefault(metric, []).append(bool(result.get("pass")))
            if metric == "required_tool_completion":
                by_tool.setdefault(str(result.get("kind")), []).append(bool(result.get("pass")))
        if row.get("applicable"):
            composite.append(bool(row.get("composite_pass")))
    def pack(values):
        return exact_ci(sum(values), len(values))
    return {"metrics": {metric: pack(values) for metric, values in buckets.items()},
            "required_tool_by_kind": {kind: pack(values) for kind, values in by_tool.items()},
            "composite_case_success": pack(composite),
            "minimum_primary_denominator": PRIMARY_MIN_DENOMINATOR}
