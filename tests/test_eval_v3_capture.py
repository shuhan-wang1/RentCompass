"""Regression tests for held-out v3 structured-output capture."""
from __future__ import annotations

import json

from evaluation import run_benchmark as rb
from evaluation.metrics.graders import GradeContext


def _result() -> rb.RunResult:
    rr = rb.RunResult(
        case_id="V3-X", category="E_multi_constraint",
        config="routed_models", mode="live", run_id="V3-X#r1", repeat=1,
    )
    rr.response_type = "search"
    rr.tool_data = {
        "eligible_recommendations": [{"url": "https://example.test/listing/v3-x-a"}],
        "candidate_states": [
            {"candidate_key": "url:https://example.test/listing/v3-x-a",
             "status": "eligible"}
        ],
    }
    return rr


def test_v3_structured_output_round_trips_through_raw_run_loader():
    original = _result()
    restored = rb._runresult_from_dict(original.to_dict())
    assert restored.response_type == "search"
    assert restored.tool_data == original.tool_data


def test_v3_structured_output_is_in_replayable_grader_packet(tmp_path):
    rr = _result()
    runner = object.__new__(rb.CaseRunner)
    runner.events_log = tmp_path / "events.jsonl"
    ctx = GradeContext(
        final_answer="One eligible option.", tools_called=["search_properties"],
        tool_call_events=[], evidence=[], user_texts=["Find a flat"],
    )

    runner._persist_grader_input(rr, {"case_id": rr.case_id}, ctx, [])
    rec = json.loads((tmp_path / "grader_input.jsonl").read_text().strip())

    assert rec["grader_input"]["response_type"] == "search"
    assert rec["grader_input"]["tool_data"] == rr.tool_data


def test_v3_fixture_directory_is_explicit_and_does_not_touch_default(tmp_path):
    fixture = tmp_path / "v3.json"
    fixture.write_text(json.dumps({"tool_name": "search_properties", "success": True,
                                   "data": {"recommendations": []}}), encoding="utf-8")
    queue = rb.load_fixture_queue({"fixture": "v3.json"}, fixtures_dir=tmp_path)
    assert list(queue) == ["search_properties"]
    assert queue["search_properties"][0]["data"]["recommendations"] == []
