"""A turn that makes NO LLM call must still emit a contract-valid canary record.

The 2026-07-25 fc smoke on candidate ``042c477`` reproduced this twice:

  turn 1  greeting, guard fast path, 0 LLM calls, FIRST request of the process
          -> llm_usage_status='not_instrumented', provider_schema_400_count=null
          -> record violates the v2 contract -> EXCLUDED from the gate population
          -> `--expect-turns 2` observed 1
  turn 2  read-only safety query -> built a model -> contract-valid
  turn 3  greeting again, 0 LLM calls, but a model now existed -> contract-VALID

So the condition is not "greeting" and not "first request" — it is precisely **a
zero-LLM-call turn in a process that has not yet constructed any model**, because
``observer_installed()`` is set as a side effect of ``ModelRouter.create()``.

Why it matters more than a miscount: an excluded record does not just fail the external
anchor, it disappears from the DENOMINATOR of p50 and of every rate the gate computes. A
pool whose cheap turns silently leave the population reports a p50 for its expensive turns
only.

``app.py:_wire_canary_llm_observer`` fixes it by installing the observer at startup. These
tests pin both halves: the broken state is genuinely broken (so the fix is measurable), and
the fixed state produces a record the gate counts.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
if os.path.join(_ROOT, "app") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "app"))

import pytest  # noqa: E402

from core import turn_observations as tobs  # noqa: E402
from core.canary_telemetry import (  # noqa: E402
    ENDPOINT_ALEX, OUTCOME_OK, USAGE_NO_CALLS, USAGE_NOT_INSTRUMENTED,
    build_canary_turn_record,
)
import canary_report  # noqa: E402

os.environ.setdefault("CANARY_USER_HASH_KEY", "zero-call-test-key")

_SHA = "042c4775670ec5f8f2260dbffb65b911a8d1b234"
_T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fresh_observer_state():
    """Save/restore the module-global observer flag and the turn window."""
    prev = tobs._observer_installed
    tobs.end_turn()
    yield
    tobs._observer_installed = prev
    tobs.end_turn()


def _zero_call_record(snapshot: dict, *, i: int = 0) -> dict:
    """Build a record the way app.py does, from a real snapshot of a 0-call turn."""
    signals = {
        "soft_wrapped": False, "wrapped_by": None,
        "partial": False, "tool_budget_timeout": False,
        "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                     "forbidden_write_executed_count": 0, "write_audit": []},
        "dsml_blocked": 0, "dsml_leak": 0,
        "provider_schema_400_count": snapshot["provider_schema_400_count"],
        "llm_usage": None,
        "llm_usage_status": snapshot["llm_usage_status"],
        "llm_calls": 0, "tool_batches": 0,
    }
    return build_canary_turn_record(
        endpoint=ENDPOINT_ALEX, agent_arch="fc_loop", candidate_sha=_SHA, strict=True,
        request_id=f"zero{i}", conversation_id=f"conv{i}", user_id=f"user{i}",
        http_status=200, turn_outcome=OUTCOME_OK, turn_latency_ms=500.0,
        signals=signals, ts=_T0,
    )


# ── the broken state, so the fix is measurable ───────────────────────────────
def test_zero_call_turn_without_observer_is_contract_invalid(fresh_observer_state):
    """Reproduces smoke turn 1: no model has ever been built, so the record is rejected."""
    tobs._observer_installed = False
    tobs.begin_turn()

    snap = tobs.snapshot()
    assert snap["llm_usage_status"] == USAGE_NOT_INSTRUMENTED
    assert snap["provider_schema_400_count"] is None

    rec = _zero_call_record(snap)
    problems = canary_report.validate_record(rec)
    assert problems, "expected the un-instrumented record to violate the v2 contract"
    assert any("provider_schema_400_count" in p for p in problems)
    assert any("not_instrumented" in p for p in problems)

    # ...and the consequence: it does not count toward the external anchor.
    verdict = canary_report.evaluate_expected_turns([rec], 1)
    assert verdict["matched"] is False
    assert verdict["observed"] == 0


# ── the fixed state ──────────────────────────────────────────────────────────
def test_zero_call_turn_with_observer_is_contract_valid(fresh_observer_state):
    """A 0-call turn in an observer-wired process reports the formal no-calls status."""
    tobs._mark_observer_installed()
    tobs.begin_turn()

    snap = tobs.snapshot()
    assert snap["llm_usage_status"] == USAGE_NO_CALLS, (
        "a turn that provably made no call must say so, not 'not instrumented'")
    assert snap["provider_schema_400_count"] == 0, (
        "zero calls means zero provider 400s — an observed fact, not an unknown")
    assert snap["llm_usage_calls"] == []

    rec = _zero_call_record(snap)
    assert canary_report.validate_record(rec) == []


def test_zero_call_turn_counts_toward_expect_turns(fresh_observer_state):
    """The anchor sees it: 3 zero-call turns are 3 eligible turns, not 0."""
    tobs._mark_observer_installed()
    recs = []
    for i in range(3):
        tobs.begin_turn()
        recs.append(_zero_call_record(tobs.snapshot(), i=i))
        tobs.end_turn()

    verdict = canary_report.evaluate_expected_turns(recs, 3)
    assert verdict["observed"] == 3
    assert verdict["matched"] is True, verdict.get("reasons")
    assert verdict["candidate_shas"] == [_SHA]
    assert not verdict["duplicate_request_ids"]


def test_building_one_model_is_what_installs_the_observer(fresh_observer_state):
    """The mechanism ``app.py:_wire_canary_llm_observer`` relies on.

    It calls ``ModelRouter().create()`` once at startup precisely because that is the only
    thing that sets the flag. Asserting it here means the startup hook cannot be silently
    invalidated by someone moving ``install_observer`` out of ``create()`` — this test would
    fail, and the whole zero-call class would regress with it.

    Construction is offline: ChatOpenAI does not contact the provider until invoked.
    """
    from uk_rent_agent.llm.router import ModelRouter

    tobs._observer_installed = False
    assert tobs.observer_installed() is False

    ModelRouter().create("intent")

    assert tobs.observer_installed() is True, (
        "ModelRouter.create() must install the canary observer — app.py's startup wiring "
        "depends on exactly this side effect")


def test_mixed_population_keeps_zero_call_turns_in_the_denominator(fresh_observer_state):
    """The point of the fix: cheap turns must not silently leave the population.

    Two 0-call turns at 500ms plus one 8s turn. With all three counted the p50 is the
    middle value; if the cheap ones were dropped the pool would report only its expensive
    turn — the exact distortion that makes a rate meaningless.
    """
    tobs._mark_observer_installed()
    recs = []
    for i in range(2):
        tobs.begin_turn()
        recs.append(_zero_call_record(tobs.snapshot(), i=i))
        tobs.end_turn()

    tobs.begin_turn()
    snap = tobs.snapshot()
    expensive = _zero_call_record(snap, i=99)
    expensive["turn_latency_ms"] = 8000.0
    expensive["llm_calls"] = 2
    expensive["llm_usage_status"] = "complete"
    tobs.end_turn()
    recs.append(expensive)

    assert all(canary_report.validate_record(r) == [] for r in recs)
    fc = canary_report.build_report(recs)["arches"]["fc"]
    assert fc["turns"] == 3
    assert fc["p50_ms"] == 500.0, (
        "with all three counted the median is a cheap turn; dropping the 0-call turns "
        "would move it to 8000ms")
