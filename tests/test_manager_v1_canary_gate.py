"""Focused fail-closed contract tests for the manager_v1 canary gate.

These tests deliberately construct telemetry at the report boundary.  They do not
import the application producer, so a producer and consumer regression cannot make
the same mistake and let the gate test pass accidentally.
"""
from __future__ import annotations

import copy
import importlib.util

import pytest
from datetime import datetime, timezone
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("manager_v1_canary_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_module()
NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
_REQUEST_SEQUENCE = 0


def _turn(arch: str, *, lifecycle: bool = False) -> dict:
    """Return one independently attributable, schema-v3 agent turn.

    manager_v1 telemetry exists only from v3, and the multi_agent lifecycle rules
    are v3 rules — a v2 record is validated under the contract that was in force
    when it was written, which knows nothing about specialists.
    """
    global _REQUEST_SEQUENCE
    _REQUEST_SEQUENCE += 1
    record = {
        "event": "canary.turn",
        "telemetry_schema_version": 3,
        "ts": NOW.isoformat(),
        "endpoint": "alex",
        "agent_arch": arch,
        "candidate_sha": "a" * 40,
        "strict": arch == "manager_v1",
        "request_id": f"manager-gate-{_REQUEST_SEQUENCE}",
        "conversation_id": f"conversation-{_REQUEST_SEQUENCE}",
        "user_id_hash": "h" * 32,
        "user_id_hash_status": "keyed",
        "http_status": 200,
        "turn_outcome": "ok",
        "soft_wrapped": False,
        "partial": False,
        "tool_budget_timeout": False,
        "security": {
            "denied_write_count": 0,
            "tainted_write_executed_count": 0,
            "forbidden_write_executed_count": 0,
        },
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
        "turn_latency_ms": 1000.0,
        "llm_calls": 1,
        "tool_batches": 1,
        "llm_usage": {
            "calls": 1,
            "input_tokens": 20,
            "output_tokens": 10,
            "cache_read_tokens": 0,
            "models": {
                "fixture-model": {
                    "calls": 1,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cache_read_tokens": 0,
                }
            },
        },
        "llm_usage_status": "complete",
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": ["forbidden_read", "no_evidence_numbers"],
    }
    if arch == "manager_v1":
        record["manager_v1_specialists"] = True
        if lifecycle:
            record["multi_agent"] = _complete_lifecycle()
    return record


def _complete_lifecycle() -> dict:
    common = {
        "plan_id": "plan-1",
        "task_id": "task-1",
        "parent_task_id": "root-1",
        "role": "listings",
    }
    return {
        "planned": 1,
        "started": 1,
        "completed": 1,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "denied_calls": 0,
        "max_in_flight": 1,
        "events_truncated": False,
        "events": [
            {**common, "status": "planned", "duration_ms": None, "call_count": 0},
            {**common, "status": "started", "duration_ms": None, "call_count": 0},
            {**common, "status": "completed", "duration_ms": 10.0, "call_count": 1},
        ],
    }


def _report(candidate: dict, controls=None) -> dict:
    if controls is None:
        controls = [_turn("legacy")]
    return cr.build_report(
        [candidate, *controls],
        now_override=NOW,
        candidate_arch="manager_v1",
        control_arch="legacy",
        require_specialists=True,
    )


def _instrumentation_text(report: dict) -> str:
    verdict = report["verdict"]
    contract = verdict["instrumentation"]["contract"] or {}
    parts = list(verdict["instrumentation"]["reasons"])
    parts.extend((contract.get("violations") or {}).keys())
    return " ".join(parts)


def test_manager_forbidden_write_is_immediate_exit3_even_if_gate_also_holds():
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["security"]["forbidden_write_executed_count"] = 1
    # A missing control also creates an instrumentation HOLD.  A proven safety
    # breach must nevertheless retain the higher-priority rollback exit code.
    verdict = _report(candidate, controls=[])["verdict"]

    assert verdict["decision"] == "CANARY-BLOCK"
    assert verdict["exit_code"] == 3
    assert verdict["zero_tolerance"]["breached"] is True
    assert "forbidden write" in " ".join(verdict["zero_tolerance"]["reasons"])


def test_missing_manager_specialist_identity_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    candidate.pop("manager_v1_specialists")
    report = _report(candidate)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "manager_v1_specialists" in _instrumentation_text(report)


def test_broken_specialist_lifecycle_identity_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    broken = copy.deepcopy(candidate["multi_agent"])
    broken["completed"] = 0
    # Counter invariants remain authoritative when the bounded event ring truncates.
    broken["events_truncated"] = True
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "lifecycle must be balanced" in _instrumentation_text(report)


def test_require_specialists_without_any_planned_task_holds_exit2():
    report = _report(_turn("manager_v1", lifecycle=False))

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "no planned specialist task" in _instrumentation_text(report)


def test_complete_specialist_lifecycle_and_control_proceed_exit0():
    report = _report(_turn("manager_v1", lifecycle=True))

    assert report["verdict"]["decision"] == "PROCEED"
    assert report["verdict"]["exit_code"] == 0
    assert report["verdict"]["instrumentation"]["failed"] is False
    specialist = report["arches"]["candidate"]["specialist"]
    assert specialist["planned"] == 1
    assert specialist["started"] == 1
    assert specialist["completed"] == 1


def test_manager_strict_false_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["strict"] = False
    report = _report(candidate)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "strict is not true" in _instrumentation_text(report)


def test_complete_usage_status_without_usage_payload_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["llm_usage"] = None
    report = _report(candidate)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "llm_usage is not an object" in _instrumentation_text(report)


def test_empty_control_arm_holds_exit2():
    report = _report(_turn("manager_v1", lifecycle=True), controls=[])

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "control arm 'legacy' has zero gate-endpoint turns" in _instrumentation_text(report)


# --------------------------------------------------------------------------- #
# The lifecycle contract with the dispatcher (statuses, invariants, denials).  #
# --------------------------------------------------------------------------- #

def _lifecycle_with_partial() -> dict:
    """Two planned tasks: one completes, one ends `partial` and is denied one call.

        planned=2 >= started=2
        started=2 == completed(1) + partial(1) + failed(0)
        skipped(0) <= planned - started = 0
    """
    first = {"plan_id": "plan-1", "task_id": "task-1",
             "parent_task_id": "root-1", "role": "listings"}
    second = {"plan_id": "plan-1", "task_id": "task-2",
              "parent_task_id": "root-1", "role": "mobility"}
    return {
        "planned": 2, "started": 2, "completed": 1, "partial": 1, "failed": 0,
        "skipped": 0, "denied_calls": 1, "max_in_flight": 2,
        "events_truncated": False,
        "events": [
            {**first, "status": "planned", "duration_ms": None, "call_count": 0},
            {**second, "status": "planned", "duration_ms": None, "call_count": 0},
            {**first, "status": "started", "duration_ms": None, "call_count": 0},
            {**second, "status": "started", "duration_ms": None, "call_count": 0},
            {"status": "denied", "tool": "remember", "error_code": "dispatch_denied"},
            {**first, "status": "completed", "duration_ms": 10.0, "call_count": 1},
            {**second, "status": "partial", "duration_ms": 22.0, "call_count": 2,
             "error_code": "budget_exhausted"},
        ],
    }


def test_a_balanced_turn_containing_partial_and_denied_proceeds():
    """The old rules (`planned == completed+failed+skipped`,
    `started == completed+failed`) were arithmetically unsatisfiable the moment a
    task ended `partial`: a perfectly healthy turn read as "lifecycle incomplete"."""
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["multi_agent"] = _lifecycle_with_partial()
    report = _report(candidate)

    assert report["verdict"]["decision"] == "PROCEED", _instrumentation_text(report)
    assert report["verdict"]["exit_code"] == 0
    specialist = report["arches"]["candidate"]["specialist"]
    assert specialist["partial"] == 1
    assert specialist["denied_calls"] == 1
    # A denial is the control WORKING. Scoring it as a failure would stage-pause a
    # release for blocking a forbidden call, which teaches operators to disable it.
    assert specialist["failed"] == 0
    assert specialist["failure_rate"] == 0.0
    assert specialist["partial_rate"] == 0.5


@pytest.mark.parametrize("mutation,expected", [
    # started > planned: a task that ran without ever being planned.
    ({"planned": 1, "started": 2, "completed": 1, "partial": 1},
     "started=2 exceeds planned=1"),
    # started != completed + partial + failed: an outcome went missing.
    ({"completed": 0}, "completed+partial+failed"),
    # skipped > planned - started: more skips than there were unstarted tasks.
    ({"skipped": 1}, "skipped=1 exceeds planned-started=0"),
])
def test_an_unbalanced_lifecycle_holds_exit2(mutation, expected):
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken.update(mutation)
    # The counters are authoritative on their own; truncation only disables the
    # event-stream reconciliation, so this isolates the invariant under test.
    broken["events_truncated"] = True
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert expected in _instrumentation_text(report)


def test_a_record_predating_partial_and_denied_still_validates():
    """`partial` / `denied_calls` are required of the producer and OPTIONAL of the
    consumer. A record from an earlier manager_v1 build has neither, and 0 is the
    correct reading there — the same reason the schema branches by version."""
    candidate = _turn("manager_v1", lifecycle=True)
    old = copy.deepcopy(candidate["multi_agent"])
    old.pop("partial")
    old.pop("denied_calls")
    candidate["multi_agent"] = old
    report = _report(candidate)

    assert report["verdict"]["decision"] == "PROCEED", _instrumentation_text(report)
    assert report["arches"]["candidate"]["specialist"]["partial"] == 0
    assert report["arches"]["candidate"]["specialist"]["denied_calls"] == 0


def test_a_denied_event_carrying_free_text_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken["events"][4] = {"status": "denied", "tool": "remember",
                           "error_code": "refused: 我想找 Camden 的房子"}
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert "error_code" in _instrumentation_text(report)


def test_a_denied_event_with_a_task_identity_holds_exit2():
    """The denied shape is exactly {status, tool, error_code}. Anything else is a
    producer that has started attaching fields the whitelist never approved."""
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken["events"][4] = {"status": "denied", "tool": "remember",
                           "error_code": "dispatch_denied", "task_id": "task-1"}
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert "unsafe extra fields" in _instrumentation_text(report)


def test_denied_calls_must_reconcile_with_the_untruncated_event_stream():
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken["denied_calls"] = 3
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert "denied_calls=3 but events contain 1" in _instrumentation_text(report)


def test_an_error_code_on_a_non_outcome_status_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken["events"][2] = {**broken["events"][2], "error_code": "timeout"}
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert "not allowed on status" in _instrumentation_text(report)


def test_partial_before_started_holds_exit2():
    candidate = _turn("manager_v1", lifecycle=True)
    broken = _lifecycle_with_partial()
    broken["events"] = [e for e in broken["events"]
                        if not (e.get("task_id") == "task-2"
                                and e.get("status") == "started")]
    broken["started"] = 1
    broken["completed"] = 1
    broken["max_in_flight"] = 1
    candidate["multi_agent"] = broken
    report = _report(candidate)

    assert report["verdict"]["exit_code"] == 2
    assert "occurs before started" in _instrumentation_text(report)


# --------------------------------------------------------------------------- #
# Every planned task must have a fate.                                         #
# --------------------------------------------------------------------------- #

def test_planned_tasks_that_simply_vanish_hold_exit2():
    """`planned=10, started=0, skipped=0` used to VALIDATE.

    The old invariant was `planned == completed + failed + skipped`, which the
    `partial` outcome made arithmetically unsatisfiable, so it was replaced by
    `planned >= started` + `started == completed + partial + failed`. Both of those
    are true of a turn that planned ten specialist tasks and then lost all ten
    without recording a single outcome — the check that would have caught it went
    out with the rewrite. The ONLY legal way for a planned task not to start is
    `skipped`, so every planned task must land in exactly one terminal bucket.
    """
    candidate = _turn("manager_v1", lifecycle=True)
    lost = copy.deepcopy(candidate["multi_agent"])
    lost.update({"planned": 10, "events_truncated": True})
    candidate["multi_agent"] = lost

    report = _report(candidate)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert "account for every planned task" in _instrumentation_text(report)


def test_planned_tasks_that_were_all_skipped_are_legal():
    """The one legal shape of `started=0`: they were skipped, and it is recorded."""
    candidate = _turn("manager_v1", lifecycle=True)
    common = {"plan_id": "plan-1", "task_id": "task-1", "parent_task_id": "root-1",
              "role": "listings"}
    candidate["multi_agent"] = {
        "planned": 1, "started": 0, "completed": 0, "partial": 0, "failed": 0,
        "skipped": 1, "denied_calls": 0, "max_in_flight": 0,
        "events_truncated": False,
        "events": [
            {**common, "status": "planned", "duration_ms": None, "call_count": 0},
            {**common, "status": "skipped", "duration_ms": None, "call_count": 0},
        ],
    }
    report = _report(candidate)

    assert report["verdict"]["decision"] == "PROCEED"
    assert report["verdict"]["exit_code"] == 0


def test_a_crashed_turn_is_not_charged_with_an_unbalanced_lifecycle():
    """A turn that died mid-flight genuinely has a task with no terminal
    transition. Reporting "broken instrumentation" about working instrumentation
    observing a crash is the same mistake as the `tool_batches` one: it holds the
    window for a reason no amount of instrumentation could remove. The shape of
    every event is still validated."""
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["turn_outcome"] = "crash"
    mid_flight = copy.deepcopy(candidate["multi_agent"])
    mid_flight.update({"completed": 0, "events": mid_flight["events"][:2]})
    candidate["multi_agent"] = mid_flight

    assert not [p for p in cr.validate_record(candidate)
                if "lifecycle" in p], cr.validate_record(candidate)

    # ...but an unsafe event inside that same crashed record is still a violation.
    mid_flight["events"][0]["role"] = "user asked about £1400"
    assert any("role" in p for p in cr.validate_record(candidate))


def test_dropped_error_codes_is_an_accepted_optional_counter():
    """The producer counts terminal transitions whose error_code was outside the
    closed vocabulary. It is additive: a record from a build that predates it reads
    as 0, and one that carries it must not be rejected as an unknown field."""
    candidate = _turn("manager_v1", lifecycle=True)
    candidate["multi_agent"]["dropped_error_codes"] = 3

    assert cr.validate_record(candidate) == []
    assert "dropped_error_codes" in cr._MULTI_AGENT_OPTIONAL_COUNTER_FIELDS

    candidate["multi_agent"]["dropped_error_codes"] = -1
    assert any("dropped_error_codes" in p for p in cr.validate_record(candidate))
