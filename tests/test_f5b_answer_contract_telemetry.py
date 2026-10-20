"""`answer_contract` end to end: producer whitelist -> consumer validation -> report.

The block was populated on every fc turn and reached no consumer at all — not the
canary record, not the API response — so the one signal saying "the runtime knew
this answer was degraded" could only be read by opening the checkpoint SQLite. It
is additive and NON-GATING: absent is always legal, present must be well-formed,
and the rate it produces is reported next to the arms, never thresholded.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CANARY_USER_HASH_KEY", "f5b-answer-contract-key")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ct = _load("canary_telemetry_f5b_ac", _ROOT / "app" / "core" / "canary_telemetry.py")
cr = _load("canary_report_f5b_ac", _ROOT / "scripts" / "canary_report.py")

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

_SIGNALS = {
    "soft_wrapped": False, "partial": False, "wrapped_by": None,
    "tool_budget_timeout": False,
    "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                 "forbidden_write_executed_count": 0, "write_audit": []},
    "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
    "llm_calls": 2, "tool_batches": 1,
    "llm_usage": {"calls": 2, "input_tokens": 20, "output_tokens": 10,
                  "cache_read_tokens": 0,
                  "models": {"deepseek-v4-flash": {
                      "calls": 2, "input_tokens": 20, "output_tokens": 10,
                      "cache_read_tokens": 0}}},
    "llm_usage_status": ct.USAGE_COMPLETE,
    "tool_ledger_status": ct.TOOL_LEDGER_COMPLETE,
    ct.OBSERVER_INSTALLED_FIELD: True,
}

_RID = 0


def _build(signals_extra=None, arch="fc_loop"):
    global _RID
    _RID += 1
    signals = {**_SIGNALS, **(signals_extra or {})}
    return ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch=arch,
        candidate_sha="1df0d4f7140f366e42b120fc9629fdb87e738428",
        strict=arch != "legacy", request_id=f"ac-{_RID}",
        conversation_id=f"c{_RID}", user_id=f"u{_RID}", http_status=200,
        turn_outcome=ct.OUTCOME_OK, turn_latency_ms=5000.0, signals=signals,
        ts=NOW,
    )


# ---- producer -------------------------------------------------------------- #

def test_absent_answer_contract_keeps_the_historical_record_shape():
    record = _build()

    assert "answer_contract" not in record
    assert cr.validate_record(record) == []


def test_producer_emits_the_three_whitelisted_keys_only():
    record = _build({"answer_contract": {
        "valid": True, "limitations": ["commute times unavailable"],
        "final_response_truncated": False,
        "raw_prompt": "SHOULD NOT SURVIVE", "user_message": "neither should this",
    }})

    assert set(record["answer_contract"]) == {
        "valid", "limitations", "final_response_truncated"}
    assert record["answer_contract"]["limitations"] == ["commute times unavailable"]
    assert cr.validate_record(record) == []


def test_producer_caps_and_declares_limitation_truncation():
    record = _build({"answer_contract": {
        "valid": False,
        "limitations": [f"limitation {i}" for i in range(20)],
        "final_response_truncated": True,
    }})

    block = record["answer_contract"]
    assert len(block["limitations"]) == ct.ANSWER_CONTRACT_MAX_LIMITATIONS
    assert block["limitations_truncated"] is True
    assert block["final_response_truncated"] is True
    assert cr.validate_record(record) == []


def test_producer_caps_a_single_long_limitation_and_declares_it():
    record = _build({"answer_contract": {
        "valid": True, "limitations": ["x" * 900], "final_response_truncated": False}})

    block = record["answer_contract"]
    assert len(block["limitations"][0]) == ct.ANSWER_CONTRACT_MAX_LIMITATION_CHARS
    assert block["limitations_truncated"] is True


def test_producer_fails_closed_on_a_missing_valid_flag():
    record = _build({"answer_contract": {"limitations": []}})

    assert record["answer_contract"]["valid"] is False
    assert record["answer_contract"]["final_response_truncated"] is False


def test_producer_omits_the_key_for_a_non_object_contract():
    assert "answer_contract" not in _build({"answer_contract": "valid"})


# ---- consumer -------------------------------------------------------------- #

@pytest.mark.parametrize("block, fragment", [
    ("not-an-object", "not an object"),
    ({"limitations": [], "final_response_truncated": False}, "valid"),
    ({"valid": True, "final_response_truncated": False}, "limitations"),
    ({"valid": True, "limitations": []}, "final_response_truncated"),
    ({"valid": True, "limitations": [1], "final_response_truncated": False},
     "non-string"),
    ({"valid": True, "limitations": ["x" * 500], "final_response_truncated": False},
     "longer than"),
    ({"valid": True, "limitations": ["x"] * 20, "final_response_truncated": False},
     "entries"),
])
def test_consumer_rejects_a_malformed_block_that_was_emitted_anyway(block, fragment):
    record = _build()
    record["answer_contract"] = block

    problems = cr.validate_record(record)

    assert any(fragment in p for p in problems), problems


def test_consumer_is_tolerant_when_the_block_is_absent():
    record = _build()
    record.pop("answer_contract", None)

    assert cr.validate_record(record) == []


# ---- report ---------------------------------------------------------------- #

def _valid_contract(valid=True, limitations=()):
    return {"valid": valid, "limitations": list(limitations),
            "final_response_truncated": False}


def test_report_aggregates_the_limitations_rate_per_arch_without_gating():
    records = []
    for i in range(10):
        records.append(_build({"answer_contract": _valid_contract(
            limitations=["partial area evidence"] if i < 3 else [])}))
        records.append(_build(arch="legacy"))

    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["candidate"]
    legacy = report["arches"]["control"]

    assert fc["answer_contract_turns"] == 10
    assert fc["answer_contract_limitations_rate"] == pytest.approx(0.3)
    assert fc["answer_contract_valid_rate"] == pytest.approx(1.0)
    # The control arm states no contract: n/a, never a clean 0%.
    assert legacy["answer_contract_turns"] == 0
    assert legacy["answer_contract_limitations_rate"] is None
    # Informational only.
    assert report["verdict"]["exit_code"] == 0
    assert report["verdict"]["stage_pause"]["breached"] is False
    assert "answer_contract" in cr.render_text(report)
