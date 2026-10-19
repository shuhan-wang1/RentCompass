"""Separate diagnostic execution, scored checks, and release evidence."""
from __future__ import annotations


def benchmark_verdict(summary: dict, *, min_pass_rate: float = 1.0) -> dict:
    total = int(summary.get("n_runs", 0))
    selected = int(summary.get("n_cases_selected", 0)) * int(summary.get("repeats", 1))
    score = summary.get("passed", {}).get("rate")
    complete = total > 0 and total == selected and not summary.get("stopped_reason")
    mechanism = bool(summary.get("gate_passed"))
    scored = score is not None and score >= min_pass_rate
    slo = bool(summary.get("slo_ok"))
    cold = summary.get("cold_resilience", {}).get("gate_passed", True)
    passed = complete and mechanism and scored and slo and cold
    return {
        "complete": complete,
        "mechanism_passed": mechanism,
        "hard_gate_cases": int(summary.get("hard_gate", {}).get("cases", 0)),
        "scored_checks_passed": scored,
        "min_pass_rate": min_pass_rate,
        "slo_passed": slo,
        "cold_resilience_passed": bool(cold),
        "quality_evidence": (
            "not_measurable_offline" if summary.get("offline") else "live_diagnostic"
        ),
        "release_evidence": "not_evaluated_use_sealed_evidence_gate",
        "passed": passed,
        "exit_code": 0 if passed else 1,
    }
