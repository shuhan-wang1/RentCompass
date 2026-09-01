from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from core import turn_observations as tobs
from core.canary_telemetry import (
    TOOL_LEDGER_COMPLETE,
    build_canary_turn_record,
    unknown_turn_signals,
)
from evaluation.metrics import collector

import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "canary_report_for_specialist_tests",
    _Path(__file__).resolve().parents[1] / "scripts" / "canary_report.py",
)
canary_report = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(canary_report)


_EVENT_FIELDS = {
    "plan_id",
    "task_id",
    "parent_task_id",
    "role",
    "status",
    "duration_ms",
    "call_count",
}


@pytest.fixture(autouse=True)
def _fresh_turn():
    tobs.end_turn()
    yield
    tobs.end_turn()


@pytest.fixture()
def turn_window():
    """An open observation window, for the tests that assert on one."""
    tobs.begin_turn()
    yield
    tobs.end_turn()


def _labels(index: int = 1, *, role: str = "listings") -> dict:
    return {
        "plan_id": "plan:1",
        "task_id": f"task:{index}",
        "parent_task_id": "turn:req-1",
        "role": role,
    }


def _record(*, specialist: dict) -> dict:
    """A fully conformant manager_v1 record carrying ``specialist``.

    The point of routing the snapshot through the real builder and the real
    consumer is that a producer-side "we recorded it" claim means nothing unless
    ``canary_report`` also reads it as conformant.
    """
    record = _canary("manager_v1", {
        "llm_calls": 0,
        "tool_batches": 0,
        "tool_ledger_status": TOOL_LEDGER_COMPLETE,
        "specialist": specialist,
    })
    record["manager_v1_specialists"] = True
    record["variant_id"] = "manager_v1:strict-1:specialists-1"
    return record


def _canary(arch: str, signals: dict) -> dict:
    return build_canary_turn_record(
        endpoint="alex",
        agent_arch=arch,
        candidate_sha="abc123",
        strict=True,
        request_id="req-1",
        conversation_id="conv-1",
        user_id=None,
        http_status=200,
        turn_outcome="ok",
        turn_latency_ms=12.5,
        signals={
            "soft_wrapped": False,
            "partial": False,
            "tool_budget_timeout": False,
            "security": {
                "denied_write_count": 0,
                "tainted_write_executed_count": 0,
                "forbidden_write_executed_count": 0,
                "write_audit": [],
            },
            "dsml_blocked": 0,
            "dsml_leak": 0,
            "provider_schema_400_count": 0,
            "llm_usage_status": "no_llm_calls",
            **signals,
        },
        ts=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )


def test_lifecycle_counts_deduplicate_and_expose_only_safe_fields():
    tobs.begin_turn()
    first = _labels(1)
    second = _labels(2, role="mobility")

    assert tobs.note_specialist_plan(**first, call_count=2, objective="private text")
    assert tobs.note_specialist_plan(**second, call_count=1, args={"postcode": "E1"})
    assert tobs.note_specialist_start(**first)
    assert tobs.note_specialist_start(**second)
    assert tobs.note_specialist_complete(**first, duration_ms=7.125)
    assert tobs.note_specialist_fail(**second, duration_ms=8.5, error="private")

    # Duplicate and conflicting terminal callbacks cannot inflate the counters.
    assert not tobs.note_specialist_complete(**first, duration_ms=9)
    assert not tobs.note_specialist_skip(**second, duration_ms=9)

    trace = tobs.specialist_snapshot()
    assert {key: trace[key] for key in (
        "planned", "started", "completed", "failed", "skipped", "max_in_flight"
    )} == {
        "planned": 2,
        "started": 2,
        "completed": 1,
        "failed": 1,
        "skipped": 0,
        "max_in_flight": 2,
    }
    assert len(trace["events"]) == 6
    assert all(set(event) == _EVENT_FIELDS for event in trace["events"])
    assert "private" not in json.dumps(trace)
    assert tobs.snapshot()["specialist"] == trace


def test_begin_turn_resets_specialist_trace_and_detail_is_bounded():
    tobs.begin_turn()
    for index in range(30):
        labels = _labels(index)
        assert tobs.note_specialist_plan(**labels, call_count=1)
        assert tobs.note_specialist_start(**labels)
        assert tobs.note_specialist_complete(**labels, duration_ms=index)

    trace = tobs.specialist_snapshot()
    assert trace["planned"] == trace["started"] == trace["completed"] == 30
    assert len(trace["events"]) == tobs._MAX_SPECIALIST_EVENTS

    tobs.begin_turn()
    assert tobs.specialist_snapshot() == {
        "planned": 0,
        "started": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "denied_calls": 0,
        "dropped_error_codes": 0,
        "max_in_flight": 0,
        "events_truncated": False,
        "events": [],
    }
    # Unused legacy/fc windows retain the old additive snapshot shape.
    assert "specialist" not in tobs.snapshot()


def test_lifecycle_is_thread_safe_across_copied_contexts():
    tobs.begin_turn()
    workers = 8
    for index in range(workers):
        assert tobs.note_specialist_plan(**_labels(index), call_count=1)

    enter = threading.Barrier(workers)
    all_started = threading.Barrier(workers)

    def run(index: int) -> None:
        enter.wait(timeout=5)
        assert tobs.note_specialist_start(**_labels(index))
        all_started.wait(timeout=5)
        assert tobs.note_specialist_complete(**_labels(index), duration_ms=1)

    contexts = [contextvars.copy_context() for _ in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(contexts[i].run, run, i) for i in range(workers)]
        for future in futures:
            future.result(timeout=10)

    trace = tobs.specialist_snapshot()
    assert trace["planned"] == workers
    assert trace["started"] == workers
    assert trace["completed"] == workers
    assert trace["max_in_flight"] == workers


@pytest.mark.parametrize(
    "fields",
    [
        {**_labels(), "plan_id": "plan containing user text", "call_count": 1},
        {**_labels(), "role": "manager", "call_count": 1},
    ],
)
def test_bad_lifecycle_identity_is_a_silent_noop(fields):
    """An unusable IDENTITY has no event to attach to, so there is nothing to record."""
    tobs.begin_turn()
    assert tobs.note_specialist_event("planned", **fields) is False
    assert tobs.specialist_snapshot()["events"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {**_labels(), "call_count": True},
        {**_labels(), "call_count": -1},
        {**_labels(), "call_count": 1, "duration_ms": float("nan")},
        {**_labels(), "call_count": 1, "duration_ms": "quickly"},
    ],
)
def test_a_bad_measurement_drops_the_value_not_the_transition(fields):
    """The transition HAPPENED; only its measurement is unusable.

    Rejecting the whole event over a malformed duration is the same defect as
    rejecting it over a malformed error_code: the counters then stop short of the
    outcome the producer believes it recorded, and the consumer's turn-end
    invariants fail on a turn where nothing was actually wrong.
    """
    tobs.begin_turn()
    assert tobs.note_specialist_event("planned", **fields) is True
    events = tobs.specialist_snapshot()["events"]
    assert len(events) == 1
    assert tobs.specialist_snapshot()["planned"] == 1
    if not isinstance(fields["call_count"], int) or isinstance(
        fields["call_count"], bool
    ) or fields["call_count"] < 0:
        assert events[0]["call_count"] == 0, "unusable count falls back, never coerced"
    # `planned` is not terminal, so duration is null here regardless; what matters
    # is that no non-finite float or string ever reaches the JSON.
    assert events[0]["duration_ms"] is None
    assert "nan" not in json.dumps(events).lower()
    assert "quickly" not in json.dumps(events)


def test_eval_collector_receives_only_the_sanitised_lifecycle_event(tmp_path):
    path = tmp_path / "events.jsonl"
    tobs.begin_turn()
    with collector.capture_run("run-1", log_path=str(path)):
        assert tobs.note_specialist_plan(
            **_labels(),
            call_count=2,
            objective="do not persist me",
            data={"address": "private"},
        )

    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["type"] == "specialist_task"
    assert {key: event[key] for key in _EVENT_FIELDS} == {
        **_labels(),
        "status": "planned",
        "duration_ms": None,
        "call_count": 2,
    }
    assert "objective" not in event
    assert "data" not in event
    assert "private" not in json.dumps(event)


def test_canary_projects_specialist_only_for_manager_v1():
    tobs.begin_turn()
    labels = _labels()
    tobs.note_specialist_plan(**labels, call_count=1)
    tobs.note_specialist_start(**labels)
    tobs.note_specialist_complete(**labels, duration_ms=4.25)
    trace = tobs.specialist_snapshot()

    manager = _canary("manager_v1", {"specialist": trace})
    assert manager["specialist"] == trace
    assert "specialist" not in _canary("fc_loop", {"specialist": trace})
    assert "specialist" not in _canary("legacy", {"specialist": trace})


def test_canary_filters_unsafe_event_fields_and_crash_projection_survives():
    trace = {
        "planned": 1,
        "started": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 1,
        "max_in_flight": 0,
        "events_truncated": False,
        "events": [
            {
                **_labels(),
                "status": "skipped",
                "duration_ms": 0,
                "call_count": 1,
                "objective": "must disappear",
            },
            {
                **_labels(2),
                "task_id": "unsafe task id with spaces",
                "status": "skipped",
                "duration_ms": 0,
                "call_count": 1,
            },
        ],
    }
    crashed = unknown_turn_signals({"specialist": trace})
    record = _canary("manager_v1", crashed)
    assert len(record["specialist"]["events"]) == 1
    assert set(record["specialist"]["events"][0]) == _EVENT_FIELDS
    assert "objective" not in json.dumps(record["specialist"])


# --------------------------------------------------------------------------- #
# `partial` and `denied` — the two lifecycle facts the first cut could not say. #
# --------------------------------------------------------------------------- #

def test_partial_is_its_own_terminal_status(turn_window):
    """A task that produced usable output AND left part of its objective unmet.

    Before this existed the dispatcher had to pick `completed` (hiding a systematic
    shortfall) or `failed` (tripping the specialist failure-rate stage-pause on
    turns that answered the user perfectly well). It also made the counters
    unsatisfiable: `started == completed + failed` could not hold.
    """
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    assert tobs.note_specialist_plan(**fields, call_count=0) is True
    assert tobs.note_specialist_start(**fields, call_count=0) is True
    assert tobs.note_specialist_partial(**fields, call_count=2, duration_ms=12.5,
                                        error_code="budget_exhausted") is True

    snap = tobs.specialist_snapshot()
    assert snap["partial"] == 1
    assert snap["completed"] == 0 and snap["failed"] == 0
    # Turn-end invariants.
    assert snap["planned"] >= snap["started"]
    assert snap["started"] == snap["completed"] + snap["partial"] + snap["failed"]
    assert snap["skipped"] <= snap["planned"] - snap["started"]
    assert snap["events"][-1]["error_code"] == "budget_exhausted"


def test_partial_is_terminal_so_nothing_follows_it(turn_window):
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    tobs.note_specialist_plan(**fields, call_count=0)
    tobs.note_specialist_start(**fields, call_count=0)
    tobs.note_specialist_partial(**fields, call_count=1)
    assert tobs.note_specialist_complete(**fields, call_count=1) is False
    assert tobs.note_specialist_fail(**fields, call_count=1) is False
    assert tobs.specialist_snapshot()["partial"] == 1


@pytest.mark.parametrize("status", ["partial", "failed", "skipped"])
def test_an_error_code_is_accepted_on_an_unsuccessful_outcome(turn_window, status):
    fields = {"plan_id": "plan-1", "task_id": f"task-{status}",
              "parent_task_id": "root-1", "role": "mobility"}
    tobs.note_specialist_plan(**fields, call_count=0)
    if status != "skipped":
        tobs.note_specialist_start(**fields, call_count=0)
    assert tobs.note_specialist_event(status, **fields, call_count=0,
                                      error_code="tool_error") is True
    assert tobs.specialist_snapshot()["events"][-1]["error_code"] == "tool_error"


@pytest.mark.parametrize("status", ["planned", "started"])
def test_an_error_code_on_a_non_outcome_status_is_dropped_not_the_event(
    turn_window, status
):
    """There is no outcome yet to explain, so the CODE is refused — but the
    transition still happened, and dropping it would unbalance the lifecycle."""
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    if status == "started":
        tobs.note_specialist_plan(**fields, call_count=0)
    assert tobs.note_specialist_event(status, **fields, call_count=0,
                                      error_code="timeout") is True
    snap = tobs.specialist_snapshot()
    assert snap[{"planned": "planned", "started": "started"}[status]] == 1
    assert "error_code" not in snap["events"][-1]
    assert snap["dropped_error_codes"] == 1


@pytest.mark.parametrize("code", [
    "Tool_Error",                  # not lowercase
    "9timeout",                    # must start with a letter
    "tool error",                  # no spaces
    "connection refused: 用户想找 Camden",   # a MESSAGE, not a code
    "x" * 65,
    "",
    123,
])
def test_a_free_text_error_reason_can_never_enter_the_record(turn_window, code):
    """The whole point of a code: an exception's str() can carry the user's query
    verbatim, and this record goes to ops telemetry.

    The code is refused; the FAILURE is not. Dropping the whole transition left
    ``started=1, failed=0``, which the consumer reads as an unbalanced lifecycle —
    so a diagnostics typo became an INSTRUMENTATION-HOLD on a turn whose telemetry
    was otherwise perfect. This asserts the consequence, not just the return value.
    """
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    tobs.note_specialist_plan(**fields, call_count=0)
    tobs.note_specialist_start(**fields, call_count=0)
    assert tobs.note_specialist_fail(**fields, call_count=0, error_code=code) is True

    snap = tobs.specialist_snapshot()
    assert snap["failed"] == 1
    assert snap["started"] == snap["completed"] + snap["partial"] + snap["failed"]
    assert snap["dropped_error_codes"] == 1
    assert "error_code" not in snap["events"][-1]
    if str(code):
        assert str(code) not in json.dumps(snap)
    record = _record(specialist=snap)
    assert canary_report.validate_record(record) == []


def test_the_closed_error_code_set_is_declared_once_and_matches_its_grammar():
    """A2's dispatcher imports THIS name. Two unlinked copies of the set is how a
    code that the grammar rejects gets added on one side and silently discarded on
    the other."""
    assert tobs.SPECIALIST_ERROR_CODES == frozenset({
        "dispatch_denied", "tool_error", "timeout", "abandoned",
        "budget_exhausted", "cancelled", "ledger_invalid", "incomplete",
    })
    for code in tobs.SPECIALIST_ERROR_CODES:
        assert tobs._ERROR_CODE_RE.fullmatch(code), code


def test_a_syntactically_valid_but_unknown_code_is_still_dropped(turn_window):
    """`_ERROR_CODE_RE` alone would have accepted this. The closed set is the check;
    the regex is only the second guard."""
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    tobs.note_specialist_plan(**fields, call_count=0)
    tobs.note_specialist_start(**fields, call_count=0)
    assert tobs.note_specialist_fail(**fields, call_count=0,
                                     error_code="upstream_gateway_hiccup") is True
    snap = tobs.specialist_snapshot()
    assert snap["failed"] == 1 and snap["dropped_error_codes"] == 1
    assert "upstream_gateway_hiccup" not in json.dumps(snap)


def test_a_denied_call_is_counted_without_touching_the_lifecycle(turn_window):
    """A refused dispatch happens INSIDE a task that is still running and will
    still reach its own terminal status. Counting it as a task outcome would both
    double-count the task and unbalance the turn-end invariants."""
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    tobs.note_specialist_plan(**fields, call_count=0)
    tobs.note_specialist_start(**fields, call_count=0)
    assert tobs.note_specialist_call_denied(tool="remember",
                                            error_code="dispatch_denied") is True
    tobs.note_specialist_complete(**fields, call_count=1)

    snap = tobs.specialist_snapshot()
    assert snap["denied_calls"] == 1
    assert snap["failed"] == 0, "a denial is the control working, not a failure"
    assert snap["started"] == snap["completed"] + snap["partial"] + snap["failed"]
    assert snap["events"][-1] == {"status": "denied", "tool": "remember",
                                  "error_code": "dispatch_denied"}


@pytest.mark.parametrize("tool,code", [
    ("remember", "not a code"),
    ("tool name with spaces", "dispatch_denied"),
    ("", "dispatch_denied"),
    ("remember", ""),
    (None, "dispatch_denied"),
    ("remember", None),
    ("search_properties", "用户请求被拒绝"),
])
def test_a_denied_event_validates_both_of_its_fields(turn_window, tool, code):
    assert tobs.note_specialist_call_denied(tool=tool, error_code=code) is False
    assert tobs.specialist_snapshot()["denied_calls"] == 0


def test_a_denial_storm_is_bounded_without_evicting_the_lifecycle(turn_window):
    """The two streams used to share one 64-slot ring, so 70 refusals inside one
    turn deleted every plan/start/finish event — exactly the detail the record
    exists to provide — while the denial itself was already summarised by a counter.
    """
    fields = {"plan_id": "plan-1", "task_id": "task-1",
              "parent_task_id": "root-1", "role": "listings"}
    tobs.note_specialist_plan(**fields, call_count=0)
    tobs.note_specialist_start(**fields, call_count=0)
    for _ in range(tobs._MAX_SPECIALIST_EVENTS + 10):
        tobs.note_specialist_call_denied(tool="remember", error_code="dispatch_denied")
    tobs.note_specialist_complete(**fields, call_count=1, duration_ms=1.0)

    snap = tobs.specialist_snapshot()
    assert snap["denied_calls"] == tobs._MAX_SPECIALIST_EVENTS + 10
    lifecycle = [e for e in snap["events"] if e.get("status") != "denied"]
    denied = [e for e in snap["events"] if e.get("status") == "denied"]
    assert [e["status"] for e in lifecycle] == ["planned", "started", "completed"]
    assert len(denied) == tobs._MAX_DENIED_EVENTS
    assert snap["events_truncated"] is True
    assert canary_report.validate_record(_record(specialist=snap)) == []


def test_a_denied_dispatch_never_records_a_model_chosen_tool_name(turn_window):
    """`agent_loop` passes ``plan[i][0].get("name")`` — the MODEL's own string. A
    shape check alone let a 128-character model-chosen identifier into ops
    telemetry, so the name is checked against the dispatchable set as well."""
    assert tobs.note_specialist_call_denied(
        tool="rememberMyBudgetIs1400PoundsInPeckham",
        error_code="dispatch_denied") is True
    snap = tobs.specialist_snapshot()
    assert snap["denied_calls"] == 1
    assert snap["events"][-1]["tool"] == tobs.UNREGISTERED_TOOL
    assert "Peckham" not in json.dumps(snap)


def test_a_real_registry_name_is_recorded_verbatim(turn_window):
    assert tobs.note_specialist_call_denied(
        tool="search_properties", error_code="dispatch_denied") is True
    assert tobs.specialist_snapshot()["events"][-1]["tool"] == "search_properties"


def test_the_denied_hook_is_a_noop_outside_a_turn():
    tobs.end_turn()
    assert tobs.note_specialist_call_denied(tool="remember",
                                            error_code="dispatch_denied") is False


def test_the_eval_collector_mirrors_partial_and_denied(tmp_path):
    """A's dispatcher mirrors events into the eval sink; a status the sink rejects
    would make the offline stream disagree with the canary record."""
    log = tmp_path / "events.jsonl"
    with collector.capture_run("run-partial", log_path=str(log)):
        collector.record_specialist_lifecycle(
            plan_id="plan-1", task_id="task-1", parent_task_id="root-1",
            role="listings", status="partial", duration_ms=3.0, call_count=1,
            error_code="budget_exhausted")
        collector.record_specialist_lifecycle(
            status="denied", tool="remember", error_code="dispatch_denied")
        # Still refused: an error MESSAGE, and a code on a non-outcome status.
        collector.record_specialist_lifecycle(
            plan_id="plan-1", task_id="task-2", parent_task_id="root-1",
            role="listings", status="failed", call_count=0,
            error_code="Connection refused for 我想找 Camden")
        collector.record_specialist_lifecycle(
            plan_id="plan-1", task_id="task-3", parent_task_id="root-1",
            role="listings", status="started", call_count=0, error_code="timeout")

    events = [json.loads(line) for line in log.read_text().splitlines()
              if '"specialist_task"' in line]
    assert len(events) == 2
    assert events[0]["status"] == "partial"
    assert events[0]["error_code"] == "budget_exhausted"
    assert events[1] == {**events[1], "status": "denied", "tool": "remember",
                         "error_code": "dispatch_denied"}
