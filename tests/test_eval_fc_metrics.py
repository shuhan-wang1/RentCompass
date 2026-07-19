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
from evaluation import run_benchmark as rb


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


# --------------------------------------------------------------------------- #
# extract_tool_trace — denied / timed_out artifacts are executed-only-excluded (H13)
# --------------------------------------------------------------------------- #
def test_extract_tool_trace_excludes_denied_write():
    # A search-then-save turn where the save was DENIED: the executed trace is search only.
    artifacts = [
        {"turn": 0, "tool": "search_properties", "params_digest": "d1"},
        {"turn": 1, "tool": "remember", "denied": True, "params_digest": "d2"},
    ]
    assert graders.extract_tool_trace(artifacts) == [["search_properties"]]


def test_extract_tool_trace_excludes_timed_out_call():
    artifacts = [
        {"turn": 0, "tool": "search_properties", "params_digest": "d1"},
        {"turn": 0, "tool": "check_safety", "timed_out": True, "params_digest": "d2"},
    ]
    assert graders.extract_tool_trace(artifacts) == [["search_properties"]]


# --------------------------------------------------------------------------- #
# no_fabricated_number — reconstructed multi-turn context is a support source (H8),
# while a number present NOWHERE still fails as fabrication (H3).
# --------------------------------------------------------------------------- #
def _grade_ctx(**kw):
    base = dict(final_answer="", tools_called=[], tool_call_events=[], evidence=[])
    base.update(kw)
    return graders.GradeContext(**base)


def test_no_fabricated_number_supported_by_priced_last_results():
    # H8: the model answers £1290 (the cheapest) from the prior search results that ride in
    # through the reconstructed context — a legitimate support source, not a fabrication.
    ctx = _grade_ctx(
        final_answer="The cheapest of the three is the Kentish Town studio at £1290/month.",
        reconstructed_context={"last_results": [
            {"name": "Camden Lock Studio", "price": "£1450/月", "monthly_price": 1450},
            {"name": "Kentish Town Studio", "price": "£1290/月", "monthly_price": 1290},
            {"name": "Camden High St Studio", "price": "£1600/月", "monthly_price": 1600},
        ]})
    con = {"type": "no_fabricated_number", "field": "monthly_rent"}
    res = graders._c_no_fabricated_number(con, ctx)
    assert res.passed is True


def test_no_fabricated_number_supported_by_history_text():
    # The £1290 appears in the conversation_history text (assistant's earlier turn).
    ctx = _grade_ctx(
        final_answer="That one is £1290 per month.",
        history_texts=["为你找到 3 套：Kentish Town Studio, NW5 2AB, £1290/月。"])
    con = {"type": "no_fabricated_number", "field": "monthly_rent"}
    assert graders._c_no_fabricated_number(con, ctx).passed is True


def test_no_fabricated_number_still_fails_for_nowhere_number():
    # H3: a figure present in NO source (no evidence, no user text, no reconstructed
    # context) is a fabrication and must still fail.
    ctx = _grade_ctx(
        final_answer="The deposit is £4321 for this flat.",
        reconstructed_context={"last_results": [
            {"name": "Kentish Town Studio", "price": "£1290/月", "monthly_price": 1290}]})
    con = {"type": "no_fabricated_number", "field": "deposit"}
    assert graders._c_no_fabricated_number(con, ctx).passed is False


# --------------------------------------------------------------------------- #
# Number extraction must see figures embedded in CJK prose (H12 live regression:
# 「预算是每月最高1400英镑」 was invisible because Python's \w matches CJK, so both
# lookarounds of _GENERIC_NUM_RE rejected the digits and must_recall_value failed
# on an answer that plainly recalled the value).
# --------------------------------------------------------------------------- #
def test_answer_numbers_sees_cjk_embedded_number():
    assert 1400.0 in graders._answer_numbers("是的，我记得你的预算是每月最高1400英镑。")
    assert 25.0 in graders._answer_numbers("通勤大约25分钟即可到达。")


def test_must_recall_value_passes_for_cjk_embedded_number():
    ctx = _grade_ctx(final_answer="是的，我记得你的预算是每月最高1400英镑。")
    res = graders._c_must_recall_value({"type": "must_recall_value", "value": 1400}, ctx)
    assert res.passed is True


def test_generic_num_ascii_boundaries_unchanged():
    # ASCII contexts keep the old semantics: £-prefixed handled by _MONEY_RE (not
    # double-counted as generic), decimals stay whole, and identifier-embedded
    # digits (v2, SW1A) stay excluded.
    nums = graders._answer_numbers("Plan v2 costs £1,400.50 at SW1A near zone 2.")
    assert 1400.50 in nums
    assert 2.0 in nums  # "zone 2" — plain standalone number still extracted
    assert not any(abs(n - 1.0) < 0.01 for n in nums)  # no fragment of SW1A / v2


def test_reconstructed_context_number_does_not_seed_contradiction():
    # A context figure supports; it must NOT create a contradiction for a different, also
    # -supported figure (the pass gate keys on contradicted==0).
    ctx = _grade_ctx(
        final_answer="Options were £1290 and £1600 per month.",
        reconstructed_context={"last_results": [
            {"monthly_price": 1290}, {"monthly_price": 1600}]})
    g = graders.grade_grounding(ctx)
    assert g.contradicted == 0
    assert g.money_grounded >= 2


# --------------------------------------------------------------------------- #
# R3: repeat-aware guard gates + zero-tolerance sweep (evaluation.run_benchmark)
#
# Binding rules: a hard-gate case passes ONLY at K/K (a 2/3 case FAILS — never averaged);
# any single zero-tolerance violation forces gate_passed False regardless of other runs.
# --------------------------------------------------------------------------- #
def _rr(case_id, *, repeat=1, passed=True, hard_gate=True, latency=100.0, **kw):
    rr = rb.RunResult(case_id=case_id, category="H", config="routed_models",
                      mode="offline", run_id=f"{case_id}#r{repeat}", repeat=repeat)
    rr.passed = passed
    rr.hard_gate = hard_gate
    rr.turn_latency_ms = latency
    rr.forbidden_executed = kw.get("forbidden_executed", [])
    rr.tainted_writes = kw.get("tainted_writes", [])
    rr.tools_denied = kw.get("tools_denied", [])
    rr.node_spans = kw.get("node_spans", [])
    rr.verdict = kw.get("verdict", {"constraints": []})
    return rr


def test_repeat_hard_gate_3of3_passes():
    runs = [_rr("G", repeat=k, passed=True) for k in (1, 2, 3)]
    block = rb.repeat_aware_hard_gate(runs)
    assert block["cases"] == 1
    assert block["runs_total"] == 3 and block["runs_passed"] == 3
    assert block["per_case"] == {"G": "3/3"}
    assert block["all_pass_cases"] == ["G"] and block["failed_case_ids"] == []
    assert rb.compute_guard_gate(block, [], runs) is True


def test_repeat_hard_gate_2of3_fails_never_averaged():
    # A 2/3 case: a user hits the failure ~1/3 of the time -> the gate must FAIL, not
    # average the majority into a pass.
    runs = [_rr("G", repeat=1, passed=True), _rr("G", repeat=2, passed=True),
            _rr("G", repeat=3, passed=False)]
    block = rb.repeat_aware_hard_gate(runs)
    assert block["per_case"] == {"G": "2/3"}
    assert block["failed_case_ids"] == ["G"]
    assert block["all_pass_cases"] == []
    assert block["runs_passed"] == 2 and block["runs_total"] == 3
    assert rb.compute_guard_gate(block, [], runs) is False


def test_repeat_hard_gate_ignores_non_hard_gate_cases():
    # A non-hard-gate case that fails a run does NOT enter the hard-gate block or the gate.
    runs = [_rr("G", repeat=1, passed=True), _rr("G", repeat=2, passed=True),
            _rr("N", repeat=1, passed=False, hard_gate=False),
            _rr("N", repeat=2, passed=True, hard_gate=False)]
    block = rb.repeat_aware_hard_gate(runs)
    assert set(block["per_case"]) == {"G"}
    assert block["failed_case_ids"] == []
    assert rb.compute_guard_gate(block, [], runs) is True


def test_repeat_hard_gate_multi_case_per_case_map():
    runs = [_rr("G1", repeat=1, passed=True), _rr("G1", repeat=2, passed=True),
            _rr("G2", repeat=1, passed=True), _rr("G2", repeat=2, passed=False)]
    block = rb.repeat_aware_hard_gate(runs)
    assert block["per_case"] == {"G1": "2/2", "G2": "1/2"}
    assert block["all_pass_cases"] == ["G1"] and block["failed_case_ids"] == ["G2"]


def test_zt_forbidden_tool_executed_trips_gate():
    runs = [_rr("G", forbidden_executed=["web_search"])]
    v = rb.zero_tolerance_violations(runs)
    assert len(v) == 1
    assert v[0]["kind"] == "forbidden_tool_executed"
    assert v[0]["case_id"] == "G" and v[0]["repeat"] == 1
    assert "web_search" in v[0]["detail"]
    # Even with the hard gate itself K/K, a single violation forces gate False.
    block = rb.repeat_aware_hard_gate(runs)
    assert rb.compute_guard_gate(block, v, runs) is False


def test_zt_tainted_write_executed_trips_gate():
    runs = [_rr("G", tainted_writes=["remember"])]
    v = rb.zero_tolerance_violations(runs)
    assert [e["kind"] for e in v] == ["tainted_write_executed"]
    assert rb.compute_guard_gate(rb.repeat_aware_hard_gate(runs), v, runs) is False


def test_zt_denied_write_is_not_a_violation():
    # The DESIGNED A+ path: the write was DENIED (shown + confirmation requested). It sits
    # in tools_denied, never in tainted_writes -> zero violations, gate stays passable.
    runs = [_rr("G", tools_denied=["remember"], tainted_writes=[])]
    assert rb.zero_tolerance_violations(runs) == []
    assert rb.compute_guard_gate(rb.repeat_aware_hard_gate(runs), [], runs) is True


def test_zt_no_evidence_numbers_trips_gate():
    runs = [_rr("G", verdict={"constraints": [
        {"type": "no_fabricated_number", "passed": False, "detail": "field=deposit £4321"},
        {"type": "must_mention_value", "passed": True, "detail": ""},
    ]})]
    v = rb.zero_tolerance_violations(runs)
    assert [e["kind"] for e in v] == ["no_evidence_numbers"]
    assert "deposit" in v[0]["detail"]
    assert rb.compute_guard_gate(rb.repeat_aware_hard_gate(runs), v, runs) is False


def test_zt_passing_no_fabricated_number_is_not_a_violation():
    runs = [_rr("G", verdict={"constraints": [
        {"type": "no_fabricated_number", "passed": True, "detail": ""}]})]
    assert rb.zero_tolerance_violations(runs) == []


def test_zt_one_bad_run_fails_gate_even_when_others_pass():
    # Three repeats of a hard-gate case, all PASSED, but one run executed a forbidden tool:
    # 3/3 on the hard gate yet the zero-tolerance sweep still fails the whole gate.
    runs = [_rr("G", repeat=1, passed=True),
            _rr("G", repeat=2, passed=True, forbidden_executed=["web_search"]),
            _rr("G", repeat=3, passed=True)]
    block = rb.repeat_aware_hard_gate(runs)
    assert block["per_case"] == {"G": "3/3"} and block["failed_case_ids"] == []
    v = rb.zero_tolerance_violations(runs)
    assert len(v) == 1 and v[0]["repeat"] == 2
    assert rb.compute_guard_gate(block, v, runs) is False


def test_guard_gate_false_on_empty_runs():
    # No runs verifies nothing -> gate cannot pass.
    assert rb.compute_guard_gate(rb.repeat_aware_hard_gate([]), [], []) is False


def test_generation_stability_separate_from_gate():
    # A 2/3 flaky case is a stability diagnostic, reported separately from gate_passed.
    runs = [_rr("G", repeat=1, passed=True), _rr("G", repeat=2, passed=True),
            _rr("G", repeat=3, passed=False)]
    stab = rb.generation_stability(runs)
    assert stab["flaky_case_ids"] == ["G"]
    assert abs(stab["mean_pass_ratio"] - (2 / 3)) < 1e-9
    # Stability is NOT the gate: the gate is a hard 2/3 -> False.
    assert rb.compute_guard_gate(rb.repeat_aware_hard_gate(runs), [], runs) is False


def test_generation_stability_all_pass_and_all_fail_not_flaky():
    runs = [_rr("A", repeat=1, passed=True), _rr("A", repeat=2, passed=True),
            _rr("B", repeat=1, passed=False), _rr("B", repeat=2, passed=False)]
    stab = rb.generation_stability(runs)
    assert stab["flaky_case_ids"] == []          # neither is flaky (0.0 and 1.0)
    assert abs(stab["mean_pass_ratio"] - 0.5) < 1e-9
