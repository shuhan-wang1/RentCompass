"""Pure-unit tests for the Phase-2 fc_loop eval metrics (design §Phase 2, §2.3).

Covers the new deterministic metric functions in ``evaluation.metrics.graders``:
``route_matches`` (batch set-insensitivity, cross-batch order sensitivity, multiple
allowed paths, expected/forbidden fallback, vacuous case), ``extract_tool_trace``
(artifact -> batch reconstruction), the four independent failure metrics
(``forbidden_tool_used`` / ``has_duplicate_calls`` / ``loop_exhausted`` /
``schema_failure_detected``), and the ``summarize_route_metrics`` aggregation incl. the
hard-gate block (every failed id listed, NEVER averaged away).

No graph build, no model, no network — every function under test is pure.
"""
from __future__ import annotations

from evaluation.metrics import graders


# --------------------------------------------------------------------------- #
# route_matches — allowed_tool_paths (per-batch SET, cross-batch ORDER)
# --------------------------------------------------------------------------- #
def test_route_matches_batch_is_set_insensitive():
    # One batch, two tools; trace lists them in the OTHER order -> still matches.
    case = {"allowed_tool_paths": [[["check_safety", "get_weather"]]]}
    assert graders.route_matches([["get_weather", "check_safety"]], case) is True


def test_route_matches_cross_batch_order_is_significant():
    case = {"allowed_tool_paths": [[["search_properties"], ["check_safety"]]]}
    # Correct order matches.
    assert graders.route_matches([["search_properties"], ["check_safety"]], case) is True
    # Swapped batch order does NOT match (order across batches is significant).
    assert graders.route_matches([["check_safety"], ["search_properties"]], case) is False


def test_route_matches_batch_count_must_equal():
    case = {"allowed_tool_paths": [[["a"], ["b"]]]}
    # Same tools but collapsed into one batch -> different batch count -> no match.
    assert graders.route_matches([["a", "b"]], case) is False


def test_route_matches_multiple_allowed_paths():
    # A short direct path OR a recovery path that re-asks; either is acceptable.
    case = {"allowed_tool_paths": [
        [["search_properties"]],
        [["ask_user"], ["search_properties"]],
    ]}
    assert graders.route_matches([["search_properties"]], case) is True
    assert graders.route_matches([["ask_user"], ["search_properties"]], case) is True
    assert graders.route_matches([["check_safety"]], case) is False


def test_route_matches_empty_allowed_paths_falls_through_to_expected():
    # An empty allowed list is treated as absent -> fallback semantics apply.
    case = {"allowed_tool_paths": [], "expected_tools": ["search_properties"]}
    assert graders.route_matches([["search_properties"]], case) is True
    assert graders.route_matches([["check_safety"]], case) is False


# --------------------------------------------------------------------------- #
# route_matches — fallback (expected ⊆ called, no forbidden)
# --------------------------------------------------------------------------- #
def test_route_matches_fallback_expected_subset():
    case = {"expected_tools": ["search_properties"], "forbidden_tools": ["web_search"]}
    # Expected tool present (plus an extra allowed tool) -> matches.
    assert graders.route_matches([["search_properties"], ["check_safety"]], case) is True
    # Expected tool absent -> no match.
    assert graders.route_matches([["check_safety"]], case) is False


def test_route_matches_fallback_forbidden_tool_fails():
    case = {"expected_tools": ["search_properties"], "forbidden_tools": ["web_search"]}
    # Expected present but a forbidden tool also ran -> no match.
    assert graders.route_matches([["search_properties"], ["web_search"]], case) is False


def test_route_matches_vacuous_true_when_no_expected_and_no_forbidden_called():
    # No expected_tools, no allowed_tool_paths, nothing forbidden ran -> vacuously true.
    case = {"forbidden_tools": ["web_search"]}
    assert graders.route_matches([], case) is True
    assert graders.route_matches([["check_safety"]], case) is True
    # ...but a forbidden call still fails the vacuous case.
    assert graders.route_matches([["web_search"]], case) is False


def test_route_matches_empty_everything_true():
    assert graders.route_matches([], {}) is True


# --------------------------------------------------------------------------- #
# route_matches — allowed_tool_paths applies to BOTH archs (README (d) contract).
# Under --arch legacy the runner reconstructs the trace one-tool-per-batch, in call
# order; the SAME grader consumes it (allowed_tool_paths is NOT arch-gated).
# --------------------------------------------------------------------------- #
def test_route_matches_allowed_paths_apply_to_legacy_one_tool_per_batch():
    # H1-style multi-step path; legacy executed [compare_or_rank_areas, search_properties]
    # is reconstructed as one tool per batch, matching the path of single-tool batches.
    case = {"allowed_tool_paths": [
        [["compare_or_rank_areas"]],
        [["compare_or_rank_areas"], ["search_properties"]],
    ]}
    legacy_trace = [["compare_or_rank_areas"], ["search_properties"]]
    assert graders.route_matches(legacy_trace, case) is True
    # A single-batch legacy trace also matches the short allowed path.
    assert graders.route_matches([["compare_or_rank_areas"]], case) is True


def test_route_matches_allowed_paths_legacy_empty_trace_matches_empty_path():
    # H8/H10-style: legacy ran no tools -> empty trace matches the empty allowed path.
    case = {"allowed_tool_paths": [[[]]], "expected_tools": [],
            "forbidden_tools": ["search_properties"]}
    assert graders.route_matches([], case) is True


# --------------------------------------------------------------------------- #
# route_matches — recall_memory detours are ignored (IGNORABLE_TOOLS)
# --------------------------------------------------------------------------- #
def test_route_matches_ignores_leading_recall_memory_batch():
    # A leading recall_memory-only batch is a harmless detour: the trace still matches
    # a path that does not itself call for recall_memory.
    case = {"allowed_tool_paths": [[["search_properties"]]]}
    assert graders.route_matches(
        [["recall_memory"], ["search_properties"]], case) is True


def test_route_matches_ignores_interleaved_recall_memory_batch():
    case = {"allowed_tool_paths": [[["search_properties"], ["check_safety"]]]}
    assert graders.route_matches(
        [["search_properties"], ["recall_memory"], ["check_safety"]], case) is True


def test_route_matches_strips_recall_memory_from_mixed_batch():
    # recall_memory sharing a batch with a real tool is stripped, not the whole batch.
    case = {"allowed_tool_paths": [[["search_properties"]]]}
    assert graders.route_matches(
        [["recall_memory", "search_properties"]], case) is True


def test_route_matches_path_with_recall_memory_stays_authoritative():
    # A path that EXPLICITLY lists recall_memory still matches a recall_memory trace.
    case = {"allowed_tool_paths": [[["recall_memory"], ["search_properties"]]]}
    assert graders.route_matches(
        [["recall_memory"], ["search_properties"]], case) is True
    # ...and a bare recall_memory path matches a bare recall_memory trace.
    case2 = {"allowed_tool_paths": [[["recall_memory"]]]}
    assert graders.route_matches([["recall_memory"]], case2) is True


def test_route_matches_recall_memory_only_trace_matches_empty_path():
    # H14-style: a pure recall_memory detour then a TEXT clarification (no real tools)
    # matches the explicitly-empty allowed path.
    case = {"allowed_tool_paths": [[["ask_user"]], []]}
    assert graders.route_matches([["recall_memory"]], case) is True
    assert graders.route_matches([], case) is True
    assert graders.route_matches([["ask_user"]], case) is True


# --------------------------------------------------------------------------- #
# extract_tool_trace — artifacts grouped into batches by turn
# --------------------------------------------------------------------------- #
def test_extract_tool_trace_groups_by_turn_in_order():
    artifacts = [
        {"turn": 0, "tool": "check_safety", "raw_data": {}, "params_digest": "d1"},
        {"turn": 0, "tool": "get_weather", "raw_data": {}, "params_digest": "d2"},
        {"turn": 1, "tool": "search_properties", "raw_data": {}, "params_digest": "d3"},
    ]
    assert graders.extract_tool_trace(artifacts) == [
        ["check_safety", "get_weather"],
        ["search_properties"],
    ]


def test_extract_tool_trace_empty():
    assert graders.extract_tool_trace([]) == []
    assert graders.extract_tool_trace(None) == []


def test_extract_tool_trace_out_of_order_turns_sorted():
    artifacts = [
        {"turn": 2, "tool": "b", "params_digest": "x"},
        {"turn": 0, "tool": "a", "params_digest": "y"},
    ]
    assert graders.extract_tool_trace(artifacts) == [["a"], ["b"]]


# --------------------------------------------------------------------------- #
# Four independent failure metrics
# --------------------------------------------------------------------------- #
def test_forbidden_tool_used():
    case = {"forbidden_tools": ["web_search"]}
    assert graders.forbidden_tool_used([["search_properties"], ["web_search"]], case) is True
    assert graders.forbidden_tool_used([["search_properties"]], case) is False
    assert graders.forbidden_tool_used([], {"forbidden_tools": []}) is False


def test_has_duplicate_calls():
    # Same (tool, digest) twice -> duplicate.
    assert graders.has_duplicate_calls(
        [("search_properties", "d1"), ("search_properties", "d1")]) is True
    # Same tool, DIFFERENT digest -> not a duplicate.
    assert graders.has_duplicate_calls(
        [("search_properties", "d1"), ("search_properties", "d2")]) is False
    # Falsy digests are ignored (unknown params are not evidence of duplication).
    assert graders.has_duplicate_calls(
        [("check_safety", None), ("check_safety", None)]) is False
    assert graders.has_duplicate_calls([]) is False


def test_loop_exhausted():
    # Degraded path sets loop_turn = MAX+1 = 11 -> exhausted.
    assert graders.loop_exhausted({"loop_turn": 11}) is True
    assert graders.loop_exhausted({"loop_turn": 10}) is False
    assert graders.loop_exhausted({"loop_turn": 3}) is False
    assert graders.loop_exhausted({}) is False
    # Custom cap.
    assert graders.loop_exhausted({"loop_turn": 6}, max_turns=5) is True


def test_schema_failure_detected():
    ev_bad = [{"tool": "search_properties", "error": "ValidationError: area is required"}]
    ev_ok = [{"tool": "search_properties", "error": None},
             {"tool": "check_safety", "error": "timed out"}]
    assert graders.schema_failure_detected(ev_bad) is True
    assert graders.schema_failure_detected(ev_ok) is False
    assert graders.schema_failure_detected([]) is False


# --------------------------------------------------------------------------- #
# summarize_route_metrics — aggregation + hard-gate block
# --------------------------------------------------------------------------- #
def _row(case_id, **kw):
    base = {"case_id": case_id, "route_matched": True, "forbidden_tool": False,
            "duplicate_call": False, "loop_exhaustion": False, "schema_failure": False,
            "hard_gate": False, "passed": True}
    base.update(kw)
    return base


def test_summarize_route_accuracy_and_failure_rates():
    rows = [
        _row("A", route_matched=True),
        _row("B", route_matched=False, forbidden_tool=True),
        _row("C", route_matched=True, duplicate_call=True),
        _row("D", route_matched=True, loop_exhaustion=True, schema_failure=True),
    ]
    s = graders.summarize_route_metrics(rows)
    assert s["route_accuracy"]["num"] == 3 and s["route_accuracy"]["den"] == 4
    assert s["route_accuracy"]["rate"] == 0.75
    assert s["forbidden_tool_rate"]["num"] == 1
    assert s["duplicate_call_rate"]["num"] == 1
    assert s["loop_exhaustion_rate"]["num"] == 1
    assert s["schema_failure_rate"]["num"] == 1


def test_summarize_hard_gate_lists_failed_ids_never_averaged():
    rows = [
        _row("G1", hard_gate=True, passed=True),
        _row("G2", hard_gate=True, passed=False),   # a hard-gate FAILURE
        _row("N1", hard_gate=False, passed=False),  # non-gate failure: not in the block
    ]
    s = graders.summarize_route_metrics(rows)
    hg = s["hard_gate"]
    assert hg["total"] == 2
    assert hg["passed"] == 1
    # The failed id is listed explicitly — not averaged into a rate.
    assert hg["failed_case_ids"] == ["G2"]
    # Any hard-gate failure flips the overall gate.
    assert s["gate_passed"] is False


def test_summarize_gate_passed_true_when_all_hard_gates_pass():
    rows = [
        _row("G1", hard_gate=True, passed=True),
        _row("G2", hard_gate=True, passed=True),
        _row("N1", hard_gate=False, passed=False),  # ordinary failure doesn't gate
    ]
    s = graders.summarize_route_metrics(rows)
    assert s["hard_gate"]["failed_case_ids"] == []
    assert s["gate_passed"] is True


def test_summarize_gate_not_passed_when_no_cases():
    # route_accuracy uncomputable (den 0) -> gate cannot pass even with zero hard gates.
    s = graders.summarize_route_metrics([])
    assert s["route_accuracy"]["rate"] is None
    assert s["gate_passed"] is False
    assert s["hard_gate"]["total"] == 0
