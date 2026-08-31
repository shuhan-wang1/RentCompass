from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from core import turn_observations as tobs
from core.canary_telemetry import (
    build_canary_turn_record,
    unknown_turn_signals,
)
from evaluation.metrics import collector


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


def _labels(index: int = 1, *, role: str = "listings") -> dict:
    return {
        "plan_id": "plan:1",
        "task_id": f"task:{index}",
        "parent_task_id": "turn:req-1",
        "role": role,
    }


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
    assert tobs.snapshot()["multi_agent"] == trace


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
        "failed": 0,
        "skipped": 0,
        "max_in_flight": 0,
        "events_truncated": False,
        "events": [],
    }
    # Unused legacy/fc windows retain the old additive snapshot shape.
    assert "multi_agent" not in tobs.snapshot()


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
        {**_labels(), "call_count": True},
        {**_labels(), "call_count": 1, "duration_ms": float("nan")},
    ],
)
def test_bad_lifecycle_input_is_a_silent_noop(fields):
    tobs.begin_turn()
    assert tobs.note_specialist_event("planned", **fields) is False
    assert tobs.specialist_snapshot()["events"] == []


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


def test_canary_projects_multi_agent_only_for_manager_v1():
    tobs.begin_turn()
    labels = _labels()
    tobs.note_specialist_plan(**labels, call_count=1)
    tobs.note_specialist_start(**labels)
    tobs.note_specialist_complete(**labels, duration_ms=4.25)
    trace = tobs.specialist_snapshot()

    manager = _canary("manager_v1", {"multi_agent": trace})
    assert manager["multi_agent"] == trace
    assert "multi_agent" not in _canary("fc_loop", {"multi_agent": trace})
    assert "multi_agent" not in _canary("legacy", {"multi_agent": trace})


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
    crashed = unknown_turn_signals({"multi_agent": trace})
    record = _canary("manager_v1", crashed)
    assert len(record["multi_agent"]["events"]) == 1
    assert set(record["multi_agent"]["events"][0]) == _EVENT_FIELDS
    assert "objective" not in json.dumps(record["multi_agent"])
