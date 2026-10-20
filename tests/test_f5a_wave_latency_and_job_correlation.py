"""Two cross-agent requests landed in `app/app.py`, pinned here.

**F4 (P2-4 tail).** `_tool_latency_from_artifacts` read `tool_artifacts` only. The legacy
WAVE executor (`dispatch_tasks -> task_worker -> gather_wave`) writes `task_results`
instead — `tool_artifacts` has no reducer in `agent.state`, so concurrent `Send` workers
cannot append to it — while `tool_batches` counts the wave regardless. A wave turn
therefore shipped `tool_batches: 1` with `tool_latency` absent or all-null, even though
every call in it was timed (F4's live `fixtest-f4-e2-legacy`). The rows are folded into
the latency summary only; they are deliberately NOT added to `tool_artifacts`, which the
critic and the grounding collectors read.

**F2 (P2-2).** Both outbox job kinds make a billed provider call on the consumer thread,
long after the turn returned, and a thread starts with an empty context. The turn's id now
travels WITH THE JOB and is bound around the work — correlatable without re-opening the
turn's observation window, whose canary record was written minutes earlier.
"""
from __future__ import annotations

import pytest

import app as appmod
from uk_rent_agent.observability import request_id_var, user_id_var


def _wave_row(tool, elapsed_ms, *, idx=0, tid=None, run_id="run-1", obs="ok", params=None):
    return {"id": tid if tid is not None else f"t{idx}", "index": idx, "tool": tool,
            "params": params if params is not None else {"q": tool},
            "obs": obs, "raw": {}, "run_id": run_id,
            "started_at": 100.0, "ended_at": 100.0 + elapsed_ms / 1000.0,
            "elapsed_ms": elapsed_ms}


# ------------------------------------------------------------------ F4: wave latency

def test_a_wave_only_turn_reports_real_latency_numbers():
    """The defect: timings existed, in the one place the summariser did not look."""
    signals = appmod._build_fc_signals({
        "task_results": [_wave_row("web_search", 1200, idx=0),
                         _wave_row("web_search", 800, idx=1),
                         _wave_row("search_nearby_pois", 4804, idx=2)],
    })

    latency = signals["tool_latency"]
    assert latency["web_search"]["count"] == 2
    # Nearest rank over sorted [800, 1200]: ceil(0.5*2)-1 = 0. An observed sample,
    # never an interpolated 1000.
    assert latency["web_search"]["p50_ms"] == 800.0
    assert latency["web_search"]["max_ms"] == 1200.0
    assert latency["search_nearby_pois"] == {
        "count": 1, "p50_ms": 4804.0, "max_ms": 4804.0, "timed_out": 0, "abandoned": 0}
    # And the wave is still counted as a batch, as it was before.
    assert signals["tool_batches"] == 1


def test_the_record_field_shape_is_unchanged():
    latency = appmod._build_fc_signals(
        {"task_results": [_wave_row("web_search", 900)]})["tool_latency"]
    assert set(latency["web_search"]) == {"count", "p50_ms", "max_ms", "timed_out", "abandoned"}


def test_a_timed_out_wave_task_is_counted_but_not_sampled():
    """A budget kill measures the budget, not the tool — same rule as an artifact."""
    signals = appmod._build_fc_signals({
        "task_results": [
            _wave_row("web_search", 30000, idx=0,
                      obs="Error: web_search timed out after 30s"),
            _wave_row("web_search", 1500, idx=1),
        ],
    })
    slot = signals["tool_latency"]["web_search"]
    assert slot["count"] == 2
    assert slot["timed_out"] == 1
    assert slot["p50_ms"] == slot["max_ms"] == 1500.0   # the kill is not a sample


def test_every_wave_task_killed_reports_null_not_zero():
    slot = appmod._build_fc_signals({
        "task_results": [_wave_row("web_search", 30000,
                                   obs="Error: web_search timed out after 30s")],
    })["tool_latency"]["web_search"]
    assert slot["count"] == 1 and slot["timed_out"] == 1
    assert slot["p50_ms"] is None and slot["max_ms"] is None


def test_a_reducer_duplicate_cannot_double_count_one_call():
    """`task_results` is an append reducer; a re-delivered Send must not become two calls."""
    row = _wave_row("web_search", 1000, idx=0, tid="task-a")
    slot = appmod._build_fc_signals(
        {"task_results": [row, dict(row)]})["tool_latency"]["web_search"]
    assert slot["count"] == 1


def test_a_call_present_in_both_ledgers_is_counted_once():
    """(tool, params digest) is the runtime's own definition of 'the same call'."""
    from core.langgraph_agent import _params_digest

    params = {"query": "camden pubs"}
    artifact = {"turn": 1, "tool": "web_search", "raw_data": {},
                "params_digest": _params_digest("web_search", params),
                "success": True, "error": None, "elapsed_ms": 1000}
    signals = appmod._build_fc_signals({
        "tool_artifacts": [artifact],
        "task_results": [_wave_row("web_search", 9999, params=params)],
    })
    slot = signals["tool_latency"]["web_search"]
    assert slot["count"] == 1
    assert slot["p50_ms"] == 1000.0   # the artifact's own duration, not the wave copy's


def test_artifacts_and_a_wave_both_contribute_when_they_are_different_calls():
    artifact = {"turn": 1, "tool": "search_properties", "raw_data": {},
                "params_digest": "abc123", "success": True, "error": None,
                "elapsed_ms": 12266}
    signals = appmod._build_fc_signals({
        "tool_artifacts": [artifact],
        "task_results": [_wave_row("web_search", 1400)],
    })
    assert signals["tool_latency"]["search_properties"]["p50_ms"] == 12266.0
    assert signals["tool_latency"]["web_search"]["p50_ms"] == 1400.0


@pytest.mark.parametrize("task_results", [
    None, "not a list", [None, 42, "x"], [{"tool": "web search with spaces"}],
    [{"tool": "web_search", "elapsed_ms": True}],
    [{"tool": "web_search", "elapsed_ms": float("inf")}],
    [{"tool": "web_search", "elapsed_ms": -5}],
])
def test_malformed_wave_rows_never_break_the_fold(task_results):
    signals = appmod._build_fc_signals({"task_results": task_results})
    latency = signals.get("tool_latency", {})
    for slot in latency.values():
        assert slot["p50_ms"] is None or slot["p50_ms"] >= 0


def test_an_fc_turn_with_no_wave_is_byte_for_byte_unchanged():
    artifact = {"turn": 1, "tool": "web_search", "raw_data": {}, "params_digest": "d",
                "success": True, "error": None, "elapsed_ms": 700}
    assert appmod._build_fc_signals({"tool_artifacts": [artifact]})["tool_latency"] == {
        "web_search": {"count": 1, "p50_ms": 700.0, "max_ms": 700.0,
                       "timed_out": 0, "abandoned": 0}}


# --------------------------------------------------- F2: post-turn job correlation

def _capture_job(job, worker_id="w1"):
    seen = {}
    original = appmod._apply_background_job

    def _spy(j, wid):
        seen["request_id"] = request_id_var.get()
        seen["user_id"] = user_id_var.get()
        seen["window"] = __import__("core.turn_observations", fromlist=["current"]).current()

    appmod._apply_background_job = _spy
    try:
        appmod._process_background_job(job, worker_id)
    finally:
        appmod._apply_background_job = original
    return seen


def test_a_queued_job_runs_under_its_turns_request_id():
    seen = _capture_job({"id": "job-1", "user_id": "u9", "kind": "memory_turn",
                         "payload": {"request_id": "req-abc"}})
    assert seen["request_id"] == "req-abc"
    assert seen["user_id"] == "u9"


def test_a_job_from_before_the_field_gets_a_job_scoped_sentinel():
    """Never '-', and never something that could pass for a real request id."""
    seen = _capture_job({"id": "job-2", "user_id": "u9", "kind": "memory_turn",
                         "payload": {}})
    assert seen["request_id"] == "job:job-2"


def test_the_job_does_not_reopen_the_turns_observation_window():
    """The turn's canary record was written minutes ago; a late call must not join it.

    Asserted with a window deliberately OPEN in the caller: the consumer thread
    normally has none, but the guarantee must not depend on that — an inline consumer
    or a future in-request drain would otherwise append to a record already on disk.
    """
    import core.turn_observations as tobs

    tobs.begin_turn()
    try:
        seen = _capture_job({"id": "job-3", "user_id": "u9", "kind": "memory_turn",
                             "payload": {"request_id": "req-abc"}})
        assert seen["window"] is None
        # ...and the caller's own window survives: the clear happened in a context copy.
        assert tobs.current() is not None
    finally:
        tobs.end_turn()


def test_the_binding_is_scoped_to_the_job():
    before = request_id_var.get()
    _capture_job({"id": "job-4", "user_id": "u9", "kind": "memory_turn",
                  "payload": {"request_id": "req-abc"}})
    assert request_id_var.get() == before
