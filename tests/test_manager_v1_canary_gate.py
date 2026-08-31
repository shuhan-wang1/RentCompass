"""Focused fail-closed contract tests for the manager_v1 canary gate.

These tests deliberately construct telemetry at the report boundary.  They do not
import the application producer, so a producer and consumer regression cannot make
the same mistake and let the gate test pass accidentally.
"""
from __future__ import annotations

import copy
import importlib.util
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
    """Return one independently attributable, schema-v2 agent turn."""
    global _REQUEST_SEQUENCE
    _REQUEST_SEQUENCE += 1
    record = {
        "event": "canary.turn",
        "telemetry_schema_version": 2,
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
        "failed": 0,
        "skipped": 0,
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
    assert "lifecycle incomplete" in _instrumentation_text(report)


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
