"""Adversarial tests for the preregistered v3 deterministic metrics.

The fixture is intentionally tiny.  It proves the evaluator rejects exactly the failures
that v2 could conceal by reading prose or dropping an unparseable row from a denominator.
"""
from __future__ import annotations

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "evaluation" / "results" / "_harness"
sys.path.insert(0, str(HARNESS))
import holdout_v3_metrics as m  # noqa: E402


def _case(*, kind="retrieval_exact_set", metrics=None, contract=None):
    return {
        "case_id": "V3-TEST-001",
        "user_query": "Find a flat in Alpha under £1,500 with a commute within 30 minutes.",
        "conversation_history": [],
        "expected_constraints": [
            {"type": "all_results_satisfy", "field": "monthly_rent", "op": "<=", "value": 1500},
            {"type": "commute_leq_minutes", "value": 30},
        ],
        "metric_eligibility": metrics or list(m.METRICS),
        "required_tool_contract": contract or {"kind": "commute_per_search_candidate"},
        "completion_oracle": {"kind": kind, "result": 1517, "ack_markers_any": ["saved"]},
        "reference_calculations": {"monthly_rent": {"result": 1517}},
    }


def _fixtures():
    good = {"eval_listing_id": "V3-A", "url": "https://v3.test/a", "address": "1 Alpha Way",
            "price_raw": 1400, "bedrooms": 1, "room_type_normalized": "flat"}
    bad = {"eval_listing_id": "V3-B", "url": "https://v3.test/b", "address": "2 Beta Way",
           "price_raw": 1700, "bedrooms": 1, "room_type_normalized": "flat"}
    return [
        {"tool_name": "search_properties", "data": {"recommendations": [good, bad]}},
        {"tool_name": "calculate_commute", "data": {"origin_eval_listing_id": "V3-A", "duration_minutes": 25}},
        {"tool_name": "calculate_commute", "data": {"origin_eval_listing_id": "V3-B", "duration_minutes": 28}},
    ]


def _run(ids=("V3-A",), *, commute=True, answer="V3-A costs £1,400 and takes 25 minutes."):
    recs = [{"eval_listing_id": x} for x in ids]
    evidence = ([
        {"origin_eval_listing_id": "V3-A", "success": True, "evidence_status": "success"},
        {"origin_eval_listing_id": "V3-B", "success": True, "evidence_status": "success"},
    ] if commute else [])
    events = [{"tool": "search_properties", "success": True, "timeout": False}]
    if commute:
        events += [{"tool": "calculate_commute", "success": True, "timeout": False}]
    return {"final_answer": answer, "response_type": "search", "tool_data": {
        "eligible_recommendations": recs, "commute_evidence": evidence}, "tool_call_events": events}


def test_v3_happy_path_scores_every_primary_metric():
    row = m.grade_case(_case(), _run(), _fixtures())
    assert all(result["pass"] for result in row["outcomes"].values())
    assert row["composite_pass"] is True


def test_v3_foreign_or_excluded_listing_is_a_precision_and_constraint_failure():
    row = m.grade_case(_case(), _run(("V3-A", "V3-B")), _fixtures())
    assert row["outcomes"]["eligible_recall"]["pass"] is True
    assert row["outcomes"]["recommendation_precision"]["pass"] is False
    assert row["outcomes"]["complete_constraint_satisfaction"]["pass"] is False


def test_v3_missing_structured_payload_is_a_failure_not_a_dropped_case():
    run = _run()
    run["tool_data"] = {}
    row = m.grade_case(_case(), run, _fixtures())
    assert "eligible_recommendations missing" in row["errors"]
    assert row["outcomes"]["eligible_recall"]["pass"] is False
    summary = m.summarize([row])
    assert summary["metrics"]["eligible_recall"]["n"] == 1


def test_v3_invented_money_is_not_supported_by_a_plausible_answer():
    row = m.grade_case(_case(), _run(answer="V3-A costs £1,999 and takes 25 minutes."), _fixtures())
    metric = row["outcomes"]["unsupported_numeric_control"]
    assert metric["pass"] is False
    assert metric["unsupported_money"] == [1999.0]


def test_v3_missing_per_listing_commute_evidence_fails_even_when_tool_name_exists():
    row = m.grade_case(_case(), _run(commute=False), _fixtures())
    assert row["outcomes"]["required_tool_completion"]["pass"] is False


def test_v3_memory_requires_successful_side_effect_and_acknowledgement():
    case = _case(kind="memory_write", metrics=["required_tool_completion", "task_completion"],
                 contract={"kind": "remember_write"})
    run = {"final_answer": "Saved your preference.", "response_type": "answer", "tool_data": {},
           "tool_call_events": [{"tool": "remember", "success": True, "timeout": False}]}
    assert m.grade_case(case, run, []) ["composite_pass"] is True
    run["tool_call_events"] = []
    assert m.grade_case(case, run, []) ["composite_pass"] is False


def test_v3_no_result_has_a_frozen_completion_branch_and_missing_run_fails():
    case = _case(metrics=["recommendation_precision", "task_completion"], contract={})
    empty = [{"tool_name": "search_properties", "data": {"recommendations": []}}]
    run = {"final_answer": "No listings matched those requirements.", "response_type": "answer",
           "tool_data": {"eligible_recommendations": []},
           "tool_call_events": [{"tool": "search_properties", "success": True, "timeout": False}]}
    assert m.grade_case(case, run, empty)["composite_pass"] is True
    absent = m.grade_case(case, None, empty)
    assert absent["composite_pass"] is False
    assert any("missing run" in e for e in absent["errors"])


def test_clarification_contract_accepts_explicit_text_question_without_ask_user():
    case = _case(kind="clarification", metrics=["task_completion"], contract={})
    case["completion_oracle"] = {
        "kind": "clarification", "markers_any": ["area"],
        "accept_text_question": True,
    }
    case["user_query"] = "I have not chosen an area or budget yet."
    run = {
        "final_answer": "Which area should I search first?",
        "response_type": "answer", "tool_data": {}, "tool_call_events": [],
    }
    row = m.grade_case(case, run, [])
    assert row["outcomes"]["task_completion"]["pass"] is True


def test_clarification_contract_rejects_text_question_after_search():
    case = _case(kind="clarification", metrics=["task_completion"], contract={})
    case["completion_oracle"] = {
        "kind": "clarification", "markers_any": ["area"],
        "accept_text_question": True,
    }
    run = {
        "final_answer": "Which area should I search first?",
        "response_type": "answer", "tool_data": {},
        "tool_call_events": [{"tool": "search_properties", "success": True,
                               "timeout": False}],
    }
    row = m.grade_case(case, run, [])
    assert row["outcomes"]["task_completion"]["pass"] is False


def test_v3_exact_interval_does_not_collapse_at_a_boundary():
    ci = m.exact_ci(33, 33)
    assert 0.89 < ci["lo"] < 0.90
    assert ci["hi"] == 1.0


def test_v3_exact_interval_has_ordered_nonboundary_bounds():
    ci = m.exact_ci(50, 60)
    assert 0.71 < ci["lo"] < 0.72
    assert 0.91 < ci["hi"] < 0.93
