"""A crash record must be VALID and HONEST, not valid-by-fabrication.

The defect this file pins, end to end (real accumulators -> ``unknown_turn_signals``
-> ``build_canary_turn_record`` -> ``canary_report.validate_record``):

``tool_batches`` is folded from the artifact ledger inside ``final_state``. A turn
that crashed has no ``final_state``, and there is no out-of-band accumulator for
that count, so ``unknown_turn_signals`` emits ``null`` and no overlay can ever fill
it. Schema v3 then required it non-null on every record — which made EVERY
crash/5xx record a guaranteed violation of a contract it is structurally incapable
of satisfying. In the real ``canary-legacy.jsonl`` that was 11 of 11 v3 crash
records failing, with crash+server_error being ~14% of the log's history: a
permanent INSTRUMENTATION-HOLD whose stated reason had nothing to do with the
candidate under test. That is how operators are taught to ignore the hold.

The two rules that replace it:

* the v3 requirement applies only to outcomes that COULD state the count; and
* ``tool_ledger_status`` lets the producer say so explicitly, checked in both
  directions so it can never become an opt-out.

What is deliberately NOT relaxed: a crashed turn still holds the gate for
everything it genuinely failed to observe — null security counters, an
``llm_usage_status`` of ``not_instrumented``. Those are the honest reasons a crash
was never promotable evidence, and every test below asserts they survive.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(_ROOT / "app"))

from core import canary_telemetry as ct  # noqa: E402
from core import turn_observations as tobs  # noqa: E402

os.environ.setdefault("CANARY_USER_HASH_KEY", "crash-path-test-key")


def _load_report():
    spec = importlib.util.spec_from_file_location(
        "crash_path_canary_report", _ROOT / "scripts" / "canary_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_report()


@pytest.fixture(autouse=True)
def _fresh_turn():
    tobs.end_turn()
    yield
    tobs.end_turn()


def _crashed_observations() -> dict:
    """Exactly what ``app._crashed_turn_observations`` merges, from the real
    accumulators. Building this by hand would prove nothing about the producer."""
    merged = dict(tobs.snapshot())
    merged.update(tobs.write_audit_snapshot("legacy"))
    merged.update(tobs.dsml_snapshot())
    return merged


def _crash_record(observed: dict, *, outcome: str = ct.OUTCOME_CRASH) -> dict:
    return ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="abc1234",
        strict=False, request_id="req-crash-1", conversation_id="conv-crash-1",
        user_id="u1", http_status=200 if outcome == ct.OUTCOME_CRASH else 500,
        turn_outcome=outcome, turn_latency_ms=1200.0,
        signals=ct.unknown_turn_signals(observed),
    )


# --------------------------------------------------------------------------- #
# The producer/consumer contradiction itself.                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("outcome", [ct.OUTCOME_CRASH, ct.OUTCOME_SERVER_ERROR])
def test_a_fully_observed_crash_turn_emits_a_conformant_v3_record(outcome):
    """The worst case for the old rule: the observer saw EVERYTHING it could.

    One billed call with usage, a clean write audit, dsml counters — a record whose
    only null is the one field a crash can never carry. Under the previous contract
    this validated as broken, so the most completely instrumented crash in the fleet
    still held the gate.
    """
    tobs.begin_turn()
    tobs.register_write_auditor("legacy")
    tobs._mark_observer_installed()
    tobs.note_raw_llm_call(
        "rawds:0",
        usage_blob={"prompt_tokens": 100, "completion_tokens": 20},
        configured_model="deepseek-chat")
    tobs.note_write_decision(tool="remember", decision="allow", context_tainted=False,
                             user_authorized=True, audit_key="k1")

    rec = _crash_record(_crashed_observations(), outcome=outcome)

    assert rec["telemetry_schema_version"] == 3
    assert rec["turn_outcome"] == outcome
    assert rec["llm_calls"] == 1, "calls that finished before the crash are observed"
    assert rec["tool_batches"] is None
    assert rec["tool_ledger_status"] == ct.TOOL_LEDGER_UNAVAILABLE
    assert rec["llm_usage_status"] == ct.USAGE_COMPLETE
    assert cr.validate_record(rec) == []


def test_the_crash_record_reports_the_spend_it_observed():
    """A crashed turn's completed calls cost real money. Dropping it out of the
    cost denominator would understate the arm, so the exemption must not turn into
    "crashes are free"."""
    tobs.begin_turn()
    tobs.register_write_auditor("legacy")
    tobs._mark_observer_installed()
    tobs.note_raw_llm_call(
        "rawds:0",
        usage_blob={"prompt_tokens": 903, "completion_tokens": 9},
        configured_model="deepseek-chat")
    tobs.note_write_decision(tool="remember", decision="allow", context_tainted=False,
                             user_authorized=True, audit_key="k1")

    rec = _crash_record(_crashed_observations())

    assert rec["llm_usage"]["input_tokens"] == 903
    assert rec["llm_usage"]["calls"] == rec["llm_calls"] == 1
    assert cr.validate_record(rec) == []


# --------------------------------------------------------------------------- #
# ...without becoming an escape hatch.                                         #
# --------------------------------------------------------------------------- #

def test_an_uninstrumented_crash_still_holds_the_gate(monkeypatch):
    """No observer, no write auditor: the record is honestly full of nulls and it
    HOLDS — for the security counters and the usage status, which is what a crash
    genuinely failed to observe. This is the behaviour the exemption must preserve.
    """
    # Both are module-level by design (the observer is installed once, long before
    # any request), so an earlier test in this process leaves them set.
    monkeypatch.setattr(tobs, "_observer_installed", False)
    monkeypatch.setattr(tobs, "_write_auditors", set())
    tobs.begin_turn()

    rec = _crash_record(_crashed_observations())
    problems = cr.validate_record(rec)

    assert problems, "an unobserved crash must never validate clean"
    assert any("security.denied_write_count" in p for p in problems)
    assert any("llm_usage_status" in p for p in problems)
    assert not any("tool_batches" in p for p in problems), (
        "the one thing it must NOT be charged with is the field it cannot have")


def test_a_healthy_turn_cannot_declare_its_own_ledger_unavailable():
    """Otherwise the marker is an opt-out: any turn could stop being measured on
    tool overhead by claiming it kept no ledger, while still reporting outcome=ok."""
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="abc1234",
        strict=False, request_id="req-2", conversation_id="conv-2", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=10.0,
        signals={
            "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                         "forbidden_write_executed_count": 0, "write_audit": []},
            "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
            "llm_usage_status": "no_llm_calls", "llm_calls": 0,
            "tool_batches": None,
            "tool_ledger_status": ct.TOOL_LEDGER_UNAVAILABLE,
        },
    )
    problems = cr.validate_record(rec)

    assert any("only ['crash', 'server_error']" in p for p in problems), problems
    assert any("tool_batches" in p for p in problems), problems


def test_the_marker_and_the_value_may_not_contradict_each_other():
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="abc1234",
        strict=False, request_id="req-3", conversation_id="conv-3", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_CRASH, turn_latency_ms=10.0,
        signals={
            "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                         "forbidden_write_executed_count": 0, "write_audit": []},
            "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
            "llm_usage_status": "no_llm_calls", "llm_calls": 0,
            "tool_batches": 4,
            "tool_ledger_status": ct.TOOL_LEDGER_UNAVAILABLE,
        },
    )
    assert any("contradict" in p for p in cr.validate_record(rec))


def test_a_v3_non_crash_record_still_requires_both_redefined_fields():
    """The exemption is keyed on the outcome and nothing else."""
    for field in ("llm_calls", "tool_batches"):
        rec = ct.build_canary_turn_record(
            endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="abc1234",
            strict=False, request_id="req-4", conversation_id="conv-4", user_id="u1",
            http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=10.0,
            signals={
                "security": {"denied_write_count": 0,
                             "tainted_write_executed_count": 0,
                             "forbidden_write_executed_count": 0, "write_audit": []},
                "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
                "llm_usage_status": "no_llm_calls",
                "llm_calls": 0, "tool_batches": 0,
                "tool_ledger_status": ct.TOOL_LEDGER_COMPLETE,
                field: None,
            },
        )
        assert any(f"required field {field!r} is null" in p
                   for p in cr.validate_record(rec)), field


# --------------------------------------------------------------------------- #
# The marker is additive.                                                      #
# --------------------------------------------------------------------------- #

def test_the_marker_is_omitted_rather_than_defaulted():
    """A caller that does not state it emits no key at all.

    A default would be worse than silence in both directions: "unavailable" beside a
    real count is a self-contradicting record, and "complete" beside a null is a
    claim nobody made. Absent, the consumer falls back to the outcome rule — which
    is exactly how it must already read the v3 records written before this field.
    """
    rec = ct.build_canary_turn_record(
        endpoint=ct.ENDPOINT_ALEX, agent_arch="legacy", candidate_sha="abc1234",
        strict=False, request_id="req-5", conversation_id="conv-5", user_id="u1",
        http_status=200, turn_outcome=ct.OUTCOME_OK, turn_latency_ms=10.0,
        signals={
            "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                         "forbidden_write_executed_count": 0, "write_audit": []},
            "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
            "llm_usage_status": "no_llm_calls", "llm_calls": 0, "tool_batches": 0,
        },
    )
    assert "tool_ledger_status" not in rec
    assert cr.validate_record(rec) == []


def test_a_historical_v3_crash_record_without_the_marker_validates():
    """69 v3 records already exist in canary-legacy.jsonl, written before this
    field. They must not become violations of a rule invented after them — the same
    principle that produced the per-version validation in the first place."""
    tobs.begin_turn()
    tobs.register_write_auditor("legacy")
    tobs._mark_observer_installed()
    tobs.note_raw_llm_call(
        "rawds:0", usage_blob={"prompt_tokens": 10, "completion_tokens": 2},
        configured_model="deepseek-chat")
    tobs.note_write_decision(tool="remember", decision="allow", context_tainted=False,
                             user_authorized=True, audit_key="k1")
    rec = _crash_record(_crashed_observations())
    rec.pop("tool_ledger_status")

    assert cr.validate_record(rec) == []


def test_the_search_direct_path_states_a_complete_ledger():
    """Its 0 is a fact — the endpoint dispatches no tools at all — so it says so
    rather than inheriting the crash exemption by looking the same as one."""
    signals = ct.search_direct_signals()
    assert signals["tool_batches"] == 0
    assert signals["tool_ledger_status"] == ct.TOOL_LEDGER_COMPLETE


def test_the_crash_record_carries_no_user_text():
    """The crash path overlays whatever the accumulators hold; a regression here
    would ship a user's words into ops telemetry on the least-tested path."""
    tobs.begin_turn()
    tobs.register_write_auditor("legacy")
    tobs._mark_observer_installed()
    tobs.note_write_decision(tool="remember", decision="deny", context_tainted=True,
                             user_authorized=False, audit_key="k1",
                             reason="policy")

    rec = _crash_record(_crashed_observations())
    blob = json.dumps(rec, ensure_ascii=False)

    assert "£1400" not in blob and "Camden" not in blob
    assert "conv-crash-1" in blob
