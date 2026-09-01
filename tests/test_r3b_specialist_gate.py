"""The specialist SLO must be able to see a specialist runtime that never ran.

Two defects, one subject: the manager_v1 specialist block.

R2-1 (the gate could not see a dead runtime).  ``_note_specialist_terminal_once``
rewrites the terminal of any task that never started to ``skipped`` — deliberately,
so the ``started == completed+partial+failed`` invariant stays satisfiable.  The
consumer then scored the only specialist SLO as ``failed / planned``.  ``skipped``
was in no numerator and in no printed row, and ``--require-specialists`` asserted
only that a PLAN existed.  So a candidate whose every specialist dispatch was
refused reported ``planned=180, started=0, skipped=180``, printed
``specialist failed rate 0.00%``, and exited PROCEED — a fail-open on the one new
metric, guarding the one feature the PR ships.

R2-2 (the producer truncated silently and the consumer convicted the record for
it).  ``turn_observations`` keeps two rings (64 lifecycle + 8 denials) and computes
``events_truncated`` per ring; ``_specialist_diagnostics`` then re-truncated their
CONCATENATION at the bare literal 64 without touching the flag.  A busy turn
(>= 19 tasks and >= 1 refused dispatch) therefore shipped ``events_truncated:
false`` with denial events dropped off the end, and the consumer reported
``denied_calls=8 but events contain 7`` — an INSTRUMENTATION-HOLD whose stated
reason never happened.

These tests drive the REAL producer (the accumulator and the record builder) and
the REAL consumer (build_report), because the bug in each case was the seam
between them.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ.setdefault("CANARY_USER_HASH_KEY", "test-key")

from core import canary_telemetry as ct  # noqa: E402
from core import turn_observations as tobs  # noqa: E402


def _load_report():
    spec = importlib.util.spec_from_file_location(
        "r3b_canary_report", _ROOT / "scripts" / "canary_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_report()
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Helpers — records come out of the real builder, never hand-written JSON.     #
# --------------------------------------------------------------------------- #

_BASE_SIGNALS = {
    "soft_wrapped": False, "wrapped_by": None, "partial": False,
    "tool_budget_timeout": False,
    "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                 "forbidden_write_executed_count": 0, "write_audit": []},
    "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
    "llm_usage_status": "no_llm_calls", "llm_usage": None,
    "llm_calls": 0, "tool_batches": 0,
    "tool_ledger_status": ct.TOOL_LEDGER_COMPLETE,
}


def _record(index: int, arch: str, *, specialist=None):
    signals = dict(_BASE_SIGNALS)
    signals["security"] = dict(_BASE_SIGNALS["security"])
    if specialist is not None:
        signals["specialist"] = specialist
    return ct.build_canary_turn_record(
        endpoint="alex", agent_arch=arch, candidate_sha="c" * 12,
        strict=(arch != "legacy"), request_id=f"{arch}-{index}",
        conversation_id=f"conv-{arch}-{index}", user_id=f"user-{index}",
        http_status=200, turn_outcome="ok", turn_latency_ms=500.0,
        signals=signals, ts=NOW,
        manager_v1_specialists=(True if arch == "manager_v1" else None),
    )


def _lifecycle(*, planned: int, started: int, completed: int = 0, partial: int = 0,
               failed: int = 0, skipped: int = 0, skip_code: str = "dispatch_denied"):
    """A contract-valid specialist block with a matching event stream."""
    events = []
    task = 0
    roles = ("listings", "mobility", "area_evidence")

    def _event(status, **extra):
        return {"plan_id": "plan-1", "task_id": f"task-{task}",
                "parent_task_id": "turn:root", "role": roles[task % 3],
                "status": status, "duration_ms": None, "call_count": 1, **extra}

    for _ in range(completed):
        events += [_event("planned"), _event("started"), _event("completed")]
        task += 1
    for _ in range(partial):
        events += [_event("planned"), _event("started"),
                   _event("partial", error_code="incomplete_evidence")]
        task += 1
    for _ in range(failed):
        events += [_event("planned"), _event("started"),
                   _event("failed", error_code="tool_error")]
        task += 1
    for _ in range(skipped):
        events += [_event("planned"), _event("skipped", error_code=skip_code)]
        task += 1
    return {
        "planned": planned, "started": started, "completed": completed,
        "partial": partial, "failed": failed, "skipped": skipped,
        "denied_calls": 0, "dropped_error_codes": 0,
        "max_in_flight": (1 if started else 0),
        "events_truncated": False, "events": events,
    }


def _report(candidate_blocks, *, require_specialists=True, controls=3):
    records = [_record(i, "manager_v1", specialist=block)
               for i, block in enumerate(candidate_blocks)]
    records += [_record(i, "legacy") for i in range(controls)]
    return cr.build_report(
        records, now_override=NOW, candidate_arch="manager_v1",
        control_arch="legacy", require_specialists=require_specialists)


# --------------------------------------------------------------------------- #
# R2-1                                                                        #
# --------------------------------------------------------------------------- #

def test_a_specialist_runtime_that_never_starts_cannot_reach_proceed():
    """The reviewer's PoC, at gate scale: 60 turns, every task planned then skipped."""
    dead = _lifecycle(planned=3, started=0, skipped=3)
    report = _report([dead] * 60)
    verdict = report["verdict"]
    specialist = report["arches"]["candidate"]["specialist"]

    assert specialist["planned"] == 180 and specialist["started"] == 0
    assert specialist["skipped"] == 180
    # The old metric still reads 0.00% — it is not wrong, it is answering a
    # different question. The new one is what the gate acts on.
    assert specialist["failure_rate"] == 0.0
    assert specialist["non_success_rate"] == 1.0
    assert verdict["decision"] != "PROCEED"
    assert verdict["exit_code"] != 0
    assert any("non-delivery" in reason for reason in verdict["stage_pause"]["reasons"])


def test_the_skipped_reason_is_reported_not_just_the_count():
    """"180 tasks vanished" is not a diagnosis; "dispatch_denied x180" is."""
    report = _report([_lifecycle(planned=3, started=0, skipped=3,
                                 skip_code="budget_exhausted")] * 4)
    specialist = report["arches"]["candidate"]["specialist"]

    assert specialist["skipped_error_codes"] == {"budget_exhausted": 12}
    text = cr.render_text(report)
    assert "specialist started" in text and "specialist skipped" in text
    assert "budget_exhausted" in text


def test_a_skipped_task_with_no_error_code_is_still_attributed():
    """A3 gives never-started tasks a specific code; a record written before that
    must still be counted, under a name that says the code is missing rather than
    disappearing from the breakdown."""
    block = _lifecycle(planned=1, started=0, skipped=1)
    for event in block["events"]:
        event.pop("error_code", None)
    report = _report([block] * 4)

    assert report["arches"]["candidate"]["specialist"]["skipped_error_codes"] == {
        "unspecified": 4}


def test_require_specialists_needs_a_task_that_started_and_one_that_delivered():
    """Three independently-false conditions, three distinct reasons."""
    planned_only = _report([_lifecycle(planned=2, started=0, skipped=2)])
    assert "no specialist task ever STARTED" in " ".join(
        planned_only["verdict"]["instrumentation"]["reasons"])

    # Started, ran, and every one of them failed: the runtime is alive but useless.
    all_failed = _report([_lifecycle(planned=2, started=2, failed=2)])
    assert "no specialist task completed or partially completed" in " ".join(
        all_failed["verdict"]["instrumentation"]["reasons"])

    # No plan at all keeps the original message.
    none_planned = _report([None])
    assert "no planned specialist task" in " ".join(
        none_planned["verdict"]["instrumentation"]["reasons"])


def test_a_healthy_specialist_window_still_proceeds():
    """The guard against over-correcting: delivery is the norm, not the exception."""
    report = _report([_lifecycle(planned=3, started=3, completed=3)] * 60)
    verdict = report["verdict"]

    assert report["arches"]["candidate"]["specialist"]["non_success_rate"] == 0.0
    assert verdict["decision"] == "PROCEED", verdict
    assert verdict["exit_code"] == 0


def test_the_non_delivery_threshold_is_the_specialist_failure_limit():
    """Below the limit passes, above it pauses — and skipped counts either way."""
    limit = cr.SPECIALIST_FAILURE_RATE_LIMIT
    assert limit == 0.05

    # 4 skipped in 100 planned = 4% <= 5%.
    under = _report([_lifecycle(planned=25, started=24, completed=24, skipped=1)] * 4)
    assert under["verdict"]["decision"] == "PROCEED", under["verdict"]

    # 8 in 100 = 8% > 5%. The same shortfall, split between failed and skipped, so
    # neither bucket alone would trip a 5% threshold.
    over = _report([_lifecycle(planned=25, started=24, completed=23, failed=1,
                               skipped=1)] * 4)
    assert over["verdict"]["decision"] != "PROCEED"
    assert any("non-delivery" in reason
               for reason in over["verdict"]["stage_pause"]["reasons"])


def test_partial_is_still_not_scored_as_non_delivery():
    """A partial task answered the user with a stated gap. Scoring it as a failure
    would stage-pause a release for behaving honestly."""
    report = _report([_lifecycle(planned=4, started=4, completed=1, partial=3)] * 30)

    assert report["arches"]["candidate"]["specialist"]["non_success_rate"] == 0.0
    assert report["arches"]["candidate"]["specialist"]["partial_rate"] == 0.75
    assert report["verdict"]["decision"] == "PROCEED", report["verdict"]


# --------------------------------------------------------------------------- #
# R2-2                                                                        #
# --------------------------------------------------------------------------- #

@pytest.fixture
def turn_window():
    prev, prev_raw = tobs._observer_installed, tobs._raw_observer_installed
    tobs._observer_installed = True
    tobs.begin_turn()
    yield
    tobs.end_turn()
    tobs._observer_installed, tobs._raw_observer_installed = prev, prev_raw


def _drive_specialists(tasks: int, denials: int):
    roles = ("listings", "mobility", "area_evidence")
    for i in range(tasks):
        fields = dict(plan_id=f"plan-{i // 3}", task_id=f"task-{i}",
                      parent_task_id="turn:root", role=roles[i % 3], call_count=1)
        assert tobs.note_specialist_plan(**fields)
        assert tobs.note_specialist_start(**fields)
        assert tobs.note_specialist_complete(duration_ms=1.0, **fields)
    for _ in range(denials):
        assert tobs.note_specialist_call_denied(tool="search_properties",
                                                error_code="dispatch_denied")


def test_denied_events_are_not_silently_pushed_off_the_end(turn_window):
    """57 lifecycle events + 8 denials = 65: over the old literal-64 cap, under the
    two rings' real capacity. Nothing was truncated, so nothing may be dropped."""
    _drive_specialists(tasks=19, denials=8)
    snapshot = tobs.specialist_snapshot()
    assert len(snapshot["events"]) == 65 and snapshot["events_truncated"] is False

    record = _record(0, "manager_v1", specialist=snapshot)
    block = record["specialist"]

    assert len(block["events"]) == 65
    assert sum(1 for e in block["events"] if e.get("status") == "denied") == 8
    assert block["events_truncated"] is False
    assert cr.validate_record(record) == [], "a busy turn is not a broken turn"


def test_a_genuinely_over_cap_stream_says_so(turn_window):
    """Past the two rings' combined capacity the list IS incomplete, and the flag
    must say so — that is what switches the consumer's reconciliation off instead
    of convicting the record."""
    _drive_specialists(tasks=30, denials=8)  # 90 lifecycle events, ring holds 64
    snapshot = tobs.specialist_snapshot()
    assert snapshot["events_truncated"] is True

    record = _record(0, "manager_v1", specialist=snapshot)
    assert record["specialist"]["events_truncated"] is True
    assert len(record["specialist"]["events"]) <= 72
    assert cr.validate_record(record) == []


def test_the_cap_is_the_sum_of_the_two_rings_not_the_lifecycle_ring():
    """A source-level guard: the two constants exist and the cap is their sum, so a
    later edit cannot quietly reintroduce the bare literal."""
    assert ct._MAX_SPECIALIST_EVENTS == (
        ct._MAX_SPECIALIST_LIFECYCLE_EVENTS + ct._MAX_SPECIALIST_DENIED_EVENTS)
    assert ct._MAX_SPECIALIST_LIFECYCLE_EVENTS == tobs._MAX_SPECIALIST_EVENTS
    assert ct._MAX_SPECIALIST_DENIED_EVENTS == tobs._MAX_DENIED_EVENTS


def test_an_event_dropped_by_sanitisation_also_flips_the_flag():
    """`events_truncated` means "this list is not the complete stream", whatever
    removed the entry. Under-reporting it turns a bounded diagnostic into a
    fabricated contract violation."""
    block = _lifecycle(planned=1, started=1, completed=1)
    block["events"].append({"plan_id": "plan-1", "task_id": "task-0",
                            "parent_task_id": "turn:root", "role": "not-a-role",
                            "status": "completed", "duration_ms": None,
                            "call_count": 1})
    record = _record(0, "manager_v1", specialist=block)

    assert record["specialist"]["events_truncated"] is True
    assert len(record["specialist"]["events"]) == 3
    assert cr.validate_record(record) == []
