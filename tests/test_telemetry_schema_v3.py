"""Schema v3: a redefined field gets a version, and history is judged by its own contract.

K3. Two v2 fields were REDEFINED in place while ``SCHEMA_VERSION`` stayed at 2:

  * ``llm_calls``   fc ``loop_turn`` (null on legacy)  ->  the observer's billed-call
    count on every arch, now including nested tool-internal DeepSeek calls;
  * ``tool_batches``  null on legacy  ->  artifact turns + legacy wave.

The consumer was tightened to match. Replaying the real production logs through the
old and new validators showed what that costs when the version does not move:

    canary-fc_loop.jsonl   230 records   violating OLD 0     violating NEW 81
    canary-legacy.jsonl   2748 records   violating OLD 590   violating NEW 2643

Those are not defects that were discovered. They are records being charged with a
contract that did not exist when they were written — and every window containing one
would have held. So: bump the version, and validate each record under the rules that
were in force for ITS version.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
if str(_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(_ROOT / "app"))

from core import canary_telemetry as ct  # noqa: E402

os.environ.setdefault("CANARY_USER_HASH_KEY", "schema-v3-test-key")


def _load_report():
    spec = importlib.util.spec_from_file_location(
        "schema_v3_canary_report", _ROOT / "scripts" / "canary_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_report()


def _v2_record(**over) -> dict:
    """A record with exactly the shape the v2 producer emitted.

    Note what is NOT here: llm_calls and tool_batches. A legacy v2 turn emitted
    both as null, which is 2643 of the 2748 historical legacy records.
    """
    rec = {
        "event": "canary.turn",
        "telemetry_schema_version": 2,
        "ts": "2026-08-01T12:00:00+00:00",
        "endpoint": "alex",
        "agent_arch": "legacy",
        "candidate_sha": "abc1234",
        "strict": False,
        "request_id": "req-v2-1",
        "conversation_id": "conv-v2-1",
        "user_id_hash": "h" * 32,
        "user_id_hash_status": "keyed",
        "http_status": 200,
        "turn_outcome": "ok",
        "soft_wrapped": False,
        "partial": False,
        "tool_budget_timeout": False,
        "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                     "forbidden_write_executed_count": 0},
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
        "turn_latency_ms": 1200.0,
        "llm_calls": None,
        "tool_batches": None,
        "llm_usage": None,
        "llm_usage_status": "no_llm_calls",
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": ["forbidden_read", "no_evidence_numbers"],
    }
    rec.update(over)
    return rec


# --------------------------------------------------------------------------- #
# The producer moved.                                                          #
# --------------------------------------------------------------------------- #

def test_the_producer_declares_v3():
    assert ct.SCHEMA_VERSION == 3, (
        "llm_calls and tool_batches were REDEFINED; a redefinition without a version "
        "bump is exactly the silent metric change this test exists to prevent")


def test_the_consumer_understands_v2_and_v3_and_nothing_else():
    assert cr.SUPPORTED_SCHEMA_VERSIONS == (2, 3)
    assert cr.validate_record(_v2_record(telemetry_schema_version=4))
    assert "newer than this consumer knows" in " ".join(
        cr.validate_record(_v2_record(telemetry_schema_version=4)))


# --------------------------------------------------------------------------- #
# History is validated under the contract it was written against.              #
# --------------------------------------------------------------------------- #

def test_a_historical_v2_record_with_null_llm_calls_is_still_conformant():
    """The single biggest slice of the replay: 2092 legacy records said
    llm_usage_status=no_llm_calls AND llm_calls=null, which the v2 producer had no
    way to say otherwise."""
    assert cr.validate_record(_v2_record()) == []


def test_v2_llm_usage_is_not_reconciled_against_v2_llm_calls():
    """36 fc records tripped 'llm_usage.calls=3 does not match llm_calls=2'.

    They were not inconsistent. Under v2 `llm_calls` counted agent super-steps and
    `llm_usage.calls` counted provider calls — two different quantities that were
    never supposed to be equal. Reconciling them retroactively invents a violation
    out of a definition difference.
    """
    rec = _v2_record(
        agent_arch="fc_loop", strict=True, llm_calls=2, tool_batches=1,
        llm_usage_status="complete",
        llm_usage={"calls": 3, "input_tokens": 30, "output_tokens": 9,
                   "cache_read_tokens": 0,
                   "models": {"m": {"calls": 3, "input_tokens": 30,
                                    "output_tokens": 9, "cache_read_tokens": 0}}},
    )
    assert cr.validate_record(rec) == []


@pytest.mark.parametrize("field", ["llm_calls", "tool_batches"])
def test_v3_requires_the_redefined_fields_but_v2_does_not(field):
    v3 = _v2_record(telemetry_schema_version=3, llm_calls=2, tool_batches=1)
    v3[field] = None
    problems = cr.validate_record(v3)
    assert any(field in p for p in problems), problems
    # The same record shape at v2 is fine: the v2 producer was allowed to say null.
    assert cr.validate_record(_v2_record()) == []


def test_v3_does_reconcile_llm_usage_against_llm_calls():
    """The tightening is not abandoned — it applies from the version that earned it."""
    rec = _v2_record(
        telemetry_schema_version=3, agent_arch="fc_loop", strict=True,
        llm_calls=2, tool_batches=1, llm_usage_status="complete",
        llm_usage={"calls": 3, "input_tokens": 30, "output_tokens": 9,
                   "cache_read_tokens": 0,
                   "models": {"m": {"calls": 3, "input_tokens": 30,
                                    "output_tokens": 9, "cache_read_tokens": 0}}},
    )
    assert any("does not match llm_calls" in p for p in cr.validate_record(rec))


def test_a_type_error_in_the_redefined_fields_is_caught_at_every_version():
    """Version-gating the REQUIREMENT does not version-gate the TYPE check: a
    negative counter would cancel a real value when summed, whenever it was written."""
    assert any("llm_calls" in p for p in cr.validate_record(_v2_record(llm_calls=-1)))


# --------------------------------------------------------------------------- #
# A window may not straddle the redefinition.                                  #
# --------------------------------------------------------------------------- #

def test_a_window_mixing_v2_and_v3_holds_rather_than_averaging_two_definitions():
    result = cr.validate_records([
        _v2_record(request_id="a"),
        _v2_record(request_id="b", telemetry_schema_version=3,
                   llm_calls=0, tool_batches=0),
    ])
    assert result["ok"] is False
    assert any("mixes telemetry_schema_version" in k for k in result["violations"])
    assert result["schema_versions"] == [2, 3]


def test_a_single_version_window_is_not_penalised():
    result = cr.validate_records([_v2_record(request_id="a"),
                                  _v2_record(request_id="b")])
    assert result["ok"] is True
    assert result["schema_versions"] == [2]


# --------------------------------------------------------------------------- #
# The producer's own output round-trips through the consumer.                  #
# --------------------------------------------------------------------------- #

def test_a_freshly_built_record_validates_as_v3():
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="fc_loop", candidate_sha="deadbee",
        strict=True, request_id="req-1", conversation_id="conv-1", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=900.0,
        signals={
            "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
            "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                         "forbidden_write_executed_count": 0},
            "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
            "llm_calls": 1, "tool_batches": 1,
            "llm_usage": {"calls": 1, "input_tokens": 10, "output_tokens": 5,
                          "cache_read_tokens": 0,
                          "models": {"m": {"calls": 1, "input_tokens": 10,
                                           "output_tokens": 5,
                                           "cache_read_tokens": 0}}},
            "llm_usage_status": "complete",
        },
    )
    assert rec["telemetry_schema_version"] == 3
    assert cr.validate_record(rec) == []


def test_search_direct_reports_observed_zeros_not_nulls():
    """This endpoint provably makes no LLM call and dispatches no tool, so 0 is an
    observation. Emitting null contradicted no_llm_calls and held every window that
    contained deterministic search traffic."""
    sig = ct.search_direct_signals()
    assert sig["llm_calls"] == 0 and sig["tool_batches"] == 0


# --------------------------------------------------------------------------- #
# tool_latency (K9 Stage-1 instrument) is additive and content-free.           #
# --------------------------------------------------------------------------- #

def test_tool_latency_is_whitelisted_and_survives_into_the_record():
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="fc_loop", candidate_sha="deadbee",
        strict=True, request_id="req-2", conversation_id="conv-2", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=900.0,
        signals={
            "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                         "forbidden_write_executed_count": 0},
            "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
            "llm_calls": 0, "tool_batches": 1, "llm_usage_status": "no_llm_calls",
            "tool_latency": {
                "search_properties": {"count": 2, "p50_ms": 1200.44, "max_ms": 3000,
                                      "timed_out": 0, "abandoned": 1},
                # Rejected: not a tool identifier, and carries free text.
                "user asked about £1400 in Camden": {
                    "count": 1, "p50_ms": 5, "max_ms": 5, "timed_out": 0,
                    "abandoned": 0},
            },
        },
    )
    assert rec["tool_latency"] == {
        "search_properties": {"count": 2, "p50_ms": 1200.4, "max_ms": 3000.0,
                              "timed_out": 0, "abandoned": 1},
    }
    assert cr.validate_record(rec) == []


def test_tool_latency_is_absent_when_no_tool_ran():
    """Absent, not an empty object: legacy/fc records that dispatched nothing keep
    exactly their historical shape."""
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="deadbee",
        strict=False, request_id="req-3", conversation_id="conv-3", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=900.0,
        signals={"security": {}, "llm_usage_status": "no_llm_calls",
                 "llm_calls": 0, "tool_batches": 0},
    )
    assert "tool_latency" not in rec


def test_tool_latency_never_carries_a_per_call_vector_or_arguments():
    summary = ct._tool_latency_summary({
        "search_properties": {"count": 1, "p50_ms": 10, "max_ms": 10,
                              "timed_out": 0, "abandoned": 0,
                              "samples": [10.0], "args": {"area": "Camden"}},
    })
    assert set(summary["search_properties"]) == {
        "count", "p50_ms", "max_ms", "timed_out", "abandoned"}
