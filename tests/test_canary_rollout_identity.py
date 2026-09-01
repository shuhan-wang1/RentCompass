"""Focused rollout-identity producer/consumer contract tests.

These tests keep the public weighted-rollout boundary fail closed: only complete,
edge-attributed records for one rollout may clear the gate, and the nginx access
log denominator must reconcile to unique telemetry request IDs.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_APP = str(_ROOT / "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from core.canary_telemetry import build_canary_turn_record  # noqa: E402


def _load_report_module():
    path = _ROOT / "scripts" / "canary_report.py"
    spec = importlib.util.spec_from_file_location("canary_rollout_identity_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_report_module()
NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
os.environ.setdefault("CANARY_USER_HASH_KEY", "rollout-identity-test-key")


def _signals(*, forbidden_write: int = 0) -> dict:
    return {
        "soft_wrapped": False,
        "partial": False,
        "tool_budget_timeout": False,
        "security": {
            "denied_write_count": 0,
            "tainted_write_executed_count": 0,
            "forbidden_write_executed_count": forbidden_write,
        },
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
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
    }


def _turn(
    sequence: int,
    arch: str,
    *,
    rollout: dict | None = None,
    forbidden_write: int = 0,
    request_id: str | None = None,
) -> dict:
    return build_canary_turn_record(
        endpoint="alex",
        agent_arch=arch,
        candidate_sha="a" * 40,
        strict=arch != "legacy",
        request_id=request_id or f"rollout-request-{sequence}",
        conversation_id=f"rollout-conversation-{sequence}",
        user_id=f"rollout-user-{sequence}",
        http_status=200,
        turn_outcome="ok",
        turn_latency_ms=1000.0,
        signals=_signals(forbidden_write=forbidden_write),
        ts=NOW + timedelta(seconds=sequence),
        manager_v1_specialists=True if arch == "manager_v1" else None,
        rollout=rollout,
    )


def _edge(pool: str, *, rollout_id: str = "manager-r1", stage: str = "c2", weight=20):
    return {
        "rollout_id": rollout_id,
        "rollout_stage": stage,
        "configured_candidate_percent": weight,
        "traffic_source": "edge",
        "assigned_pool": pool,
    }


def _rollout_report(records: list[dict], *, expected: int, **overrides) -> dict:
    options = {
        "now_override": NOW + timedelta(minutes=1),
        "candidate_arch": "fc_loop",
        "control_arch": "legacy",
        "rollout_id": "manager-r1",
        "expect_rollout_turns": expected,
    }
    options.update(overrides)
    return cr.build_report(records, **options)


def _instrumentation_text(report: dict) -> str:
    verdict = report["verdict"]
    contract = verdict["instrumentation"]["contract"] or {}
    return " ".join(
        [*verdict["instrumentation"]["reasons"], *(contract.get("violations") or {})]
    )


def test_producer_defaults_untrusted_traffic_to_direct():
    record = _turn(1, "fc_loop")

    assert record["traffic_source"] == "direct"
    assert record["assigned_pool"] == "direct"
    assert record["rollout_id"] is None
    assert record["rollout_stage"] is None
    assert record["configured_candidate_percent"] is None
    assert cr.validate_record(record) == []


def test_complete_edge_identity_round_trips_through_producer_and_gate():
    candidate = _turn(1, "fc_loop", rollout=_edge("candidate"))
    control = _turn(2, "legacy", rollout=_edge("legacy"))

    assert cr.validate_record(candidate) == []
    assert cr.validate_record(control) == []
    report = _rollout_report([candidate, control], expected=2)

    assert report["records_in_window"] == 2
    assert report["verdict"]["exit_code"] == 0
    assert report["verdict"]["expected_rollout_turns"]["matched"] is True


@pytest.mark.parametrize(
    ("field", "bad_value", "reason_fragment"),
    [
        ("rollout_id", None, "rollout_id"),
        ("rollout_stage", "invalid stage", "rollout_stage"),
        ("configured_candidate_percent", 10, "configured_candidate_percent"),
        ("traffic_source", "spoofed", "traffic_source"),
        ("assigned_pool", None, "assigned_pool"),
    ],
)
def test_edge_missing_or_invalid_identity_holds(field, bad_value, reason_fragment):
    candidate = _turn(1, "fc_loop", rollout=_edge("candidate"))
    control = _turn(2, "legacy", rollout=_edge("legacy"))
    candidate[field] = bad_value

    report = cr.build_report(
        [candidate, control],
        now_override=NOW + timedelta(minutes=1),
        candidate_arch="fc_loop",
        control_arch="legacy",
    )

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert reason_fragment in _instrumentation_text(report)


def test_rollout_id_filter_excludes_direct_and_other_rollout_records():
    selected = [
        _turn(1, "fc_loop", rollout=_edge("candidate")),
        _turn(2, "legacy", rollout=_edge("legacy")),
    ]
    # Neither untrusted direct traffic nor another rollout may affect this gate,
    # even if those records carry a real breach in their own population.
    direct = _turn(3, "fc_loop", forbidden_write=1)
    other = _turn(
        4,
        "fc_loop",
        rollout=_edge("candidate", rollout_id="manager-r2"),
        forbidden_write=1,
    )

    report = _rollout_report([*selected, direct, other], expected=2)

    assert report["records_total"] == 4
    assert report["records_in_window"] == 2
    assert report["zero_tolerance_global"]["forbidden_write_count"] == 0
    assert report["verdict"]["exit_code"] == 0


def test_mixed_stage_and_weight_for_one_rollout_holds():
    records = [
        _turn(1, "fc_loop", rollout=_edge("candidate", stage="c1", weight=5)),
        _turn(2, "legacy", rollout=_edge("legacy", stage="c1", weight=5)),
        _turn(3, "fc_loop", rollout=_edge("candidate", stage="c2", weight=20)),
        _turn(4, "legacy", rollout=_edge("legacy", stage="c2", weight=20)),
    ]

    report = _rollout_report(
        records,
        expected=4,
        rollout_stage="c1",
        configured_weight=5,
    )

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    text = _instrumentation_text(report)
    assert "rollout_stage" in text
    assert "configured weight" in text


def test_mixed_stage_and_weight_holds_without_expected_value_flags():
    records = [
        _turn(1, "fc_loop", rollout=_edge("candidate", stage="c1", weight=5)),
        _turn(2, "legacy", rollout=_edge("legacy", stage="c1", weight=5)),
        _turn(3, "fc_loop", rollout=_edge("candidate", stage="c2", weight=20)),
        _turn(4, "legacy", rollout=_edge("legacy", stage="c2", weight=20)),
    ]

    report = _rollout_report(records, expected=4)

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    assert report["observed_rollout_stages"] == ["c1", "c2"]
    assert report["observed_rollout_weights"] == [5, 20]
    text = _instrumentation_text(report)
    assert "mixes rollout_stage values" in text
    assert "mixes configured weight values" in text


@pytest.mark.parametrize(
    ("arch", "reported_pool", "expected_pool"),
    [
        ("fc_loop", "legacy", "candidate"),
        ("legacy", "candidate", "legacy"),
    ],
)
def test_assigned_pool_must_match_architecture(arch, reported_pool, expected_pool):
    record = _turn(1, arch, rollout=_edge(reported_pool))
    companion_arch = "legacy" if arch == "fc_loop" else "fc_loop"
    companion_pool = "legacy" if companion_arch == "legacy" else "candidate"
    companion = _turn(2, companion_arch, rollout=_edge(companion_pool))

    report = cr.build_report(
        [record, companion],
        now_override=NOW + timedelta(minutes=1),
        candidate_arch="fc_loop",
        control_arch="legacy",
    )

    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2
    text = _instrumentation_text(report)
    assert "assigned_pool" in text
    assert expected_pool in text


def test_expect_rollout_turns_requires_exact_unique_denominator():
    records = [
        _turn(1, "fc_loop", rollout=_edge("candidate")),
        _turn(2, "legacy", rollout=_edge("legacy")),
    ]

    exact = _rollout_report(records, expected=2)
    missing = _rollout_report(records, expected=3)

    assert exact["verdict"]["expected_rollout_turns"]["matched"] is True
    assert exact["verdict"]["exit_code"] == 0
    assert missing["verdict"]["expected_rollout_turns"]["matched"] is False
    assert missing["verdict"]["exit_code"] == 2
    assert "found 2" in " ".join(
        missing["verdict"]["expected_rollout_turns"]["reasons"]
    )


def test_rollout_id_without_external_denominator_is_rejected():
    records = [
        _turn(1, "fc_loop", rollout=_edge("candidate")),
        _turn(2, "legacy", rollout=_edge("legacy")),
    ]

    with pytest.raises(ValueError, match="requires expect_rollout_turns"):
        cr.build_report(records, rollout_id="manager-r1")


def test_duplicate_rollout_request_id_holds_even_when_raw_count_matches():
    candidate = _turn(
        1,
        "fc_loop",
        rollout=_edge("candidate"),
        request_id="duplicate-request",
    )
    duplicate = copy.deepcopy(candidate)
    duplicate["conversation_id"] = "another-conversation"
    control = _turn(2, "legacy", rollout=_edge("legacy"))

    report = _rollout_report([candidate, duplicate, control], expected=3)
    denominator = report["verdict"]["expected_rollout_turns"]

    assert denominator["observed"] == 3
    assert denominator["unique_request_ids"] == 2
    assert denominator["duplicate_request_ids"] == {"duplicate-request": 2}
    assert denominator["matched"] is False
    assert report["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert report["verdict"]["exit_code"] == 2


def test_manager_candidate_zero_tolerance_still_outranks_rollout_holds():
    candidate = _turn(
        1,
        "manager_v1",
        rollout=_edge("candidate"),
        forbidden_write=1,
    )
    control = _turn(2, "legacy", rollout=_edge("legacy"))

    report = _rollout_report(
        [candidate, control],
        expected=3,  # Also force an external-denominator instrumentation failure.
        candidate_arch="manager_v1",
    )

    assert report["verdict"]["instrumentation"]["failed"] is True
    assert report["verdict"]["zero_tolerance"]["breached"] is True
    assert report["verdict"]["decision"] == "CANARY-BLOCK"
    assert report["verdict"]["exit_code"] == 3
