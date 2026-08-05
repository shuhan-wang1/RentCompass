"""Static-gate tests for v3, using no formal benchmark cases."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "evaluation" / "results" / "_harness"
sys.path.insert(0, str(HARNESS))
import holdout_v3_preflight as gate  # noqa: E402


def _case(i: int, *, metric="task_completion", contract=None):
    return {
        "case_id": f"HO3-{i:03d}", "schema_version": "rentcompass/benchmark/v3",
        "task_category": "clarify", "user_id": f"u{i}",
        "user_query": f"Please clarify preference {i}.", "conversation_history": [],
        "expected_tools": [], "forbidden_tools": ["search_properties"], "expected_constraints": [],
        "hard_constraint_slots": [], "correct_completion": "Ask one focused question.",
        "completion_oracle": {"kind": "clarification", "markers_any": ["which"]},
        "metric_eligibility": [metric], "failure_conditions": ["Searches instead of clarifying."],
        "allowed_evidence_sources": ["user request"], "reference_calculations": None,
        "novelty_note": f"synthetic gate test {i}", "fixture": "empty.json"
    }


def test_v3_preflight_reports_fixed_denominator_shortfall(tmp_path):
    (tmp_path / "empty.json").write_text(json.dumps({"tool_name": "ask_user", "data": {}}))
    report = gate.check([_case(i) for i in range(1, 30)], tmp_path)
    assert report["gate_passed"] is False
    assert any("task_completion denominator 29 < 30" in x for x in report["problems"]["__quota__"])


def test_v3_preflight_rejects_missing_completion_oracle_and_bad_tool_contract(tmp_path):
    (tmp_path / "empty.json").write_text(json.dumps({"tool_name": "ask_user", "data": {}}))
    case = _case(1, metric="required_tool_completion", contract={"kind": "not_a_contract"})
    case["required_tool_contract"] = {"kind": "not_a_contract"}
    del case["completion_oracle"]
    report = gate.check([case], tmp_path)
    failures = report["problems"]["HO3-001"]
    assert any("completion_oracle" in item for item in failures)
    assert any("invalid required_tool_contract" in item for item in failures)


def test_v6_preflight_requires_explicit_text_question_contract(tmp_path):
    (tmp_path / "empty.json").write_text(json.dumps({"tool_name": "ask_user", "data": {}}))
    case = _case(1)
    case["schema_version"] = "rentcompass/benchmark/v6"
    report = gate.check([case], tmp_path, schema_version="rentcompass/benchmark/v6")
    assert any("C6 v6 clarify oracle" in item for item in report["problems"]["HO3-001"])
