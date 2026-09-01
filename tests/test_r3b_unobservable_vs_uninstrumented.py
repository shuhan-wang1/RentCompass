"""Unmeasurable is not uninstrumented. Four cells, one flag.

The instrumentation gate exists to catch ONE class of defect: telemetry produced by
a process that was not watching. That is the 2026-07-25 incident — a stale
``DEEPSEEK_MODEL`` broke both pools for a day and the zero-call records looked
clean. A turn that CRASHED while the callback observer was attached is not evidence
of that class: no instrumentation can price a call that never reached its completion
callback. Charging it as a contract violation held every window containing a crash —
at the 9.6%-40.9% crash rates in the real logs, that is every window — and a gate
that always holds is a gate operators switch off, which weakens it far more than a
narrow exemption does.

So the record now carries ``llm_observer_installed`` (the CALLBACK flag from R2-4,
not the raw-SDK one) and the consumer reads the two facts separately:

    outcome        observer   ->  llm_usage_status   report
    crash/5xx      true       ->  partial            UNMEASURED, not a violation
    crash/5xx      false/absent -> not_instrumented  VIOLATION (the incident class)
    ok             true       ->  no_llm_calls       measured zero, unchanged
    ok             false      ->  not_instrumented   VIOLATION (the incident class)

The spend stays unmeasured in ``canary_cost`` in every unmeasured cell — the turn is
never priced at zero — and the crash keeps counting against the outcome/5xx rates it
already counted against. Only the "your telemetry is broken" verdict is withdrawn,
and only on positive evidence that it is not.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ.setdefault("CANARY_USER_HASH_KEY", "test-key")

from core import canary_telemetry as ct  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load("r3b_cells_report", "canary_report.py")
cc = _load("r3b_cells_cost", "canary_cost.py")
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

_OK_SIGNALS = {
    "soft_wrapped": False, "wrapped_by": None, "partial": False,
    "tool_budget_timeout": False,
    "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                 "forbidden_write_executed_count": 0, "write_audit": []},
    "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
    "llm_usage": None, "llm_usage_status": "no_llm_calls",
    "llm_calls": 0, "tool_batches": 0,
    "tool_ledger_status": ct.TOOL_LEDGER_COMPLETE,
}


def _observed(*, installed: bool):
    """What turn_observations.snapshot() hands the producer in each cell."""
    if installed:
        return {"llm_usage_status": "no_llm_calls", "llm_calls": 0,
                "provider_schema_400_count": 0, "llm_observer_installed": True,
                "denied_write_count": 0, "tainted_write_executed_count": 0,
                "forbidden_write_executed_count": 0, "write_audit": [],
                "dsml_blocked": 0, "dsml_leak": 0}
    # No observer: every counter it owns is null, exactly as snapshot() reports.
    return {"llm_usage_status": "not_instrumented", "llm_calls": None,
            "provider_schema_400_count": None, "llm_observer_installed": False,
            "denied_write_count": 0, "tainted_write_executed_count": 0,
            "forbidden_write_executed_count": 0, "write_audit": [],
            "dsml_blocked": 0, "dsml_leak": 0}


def _record(index: int, *, outcome: str, installed: bool, arch: str = "fc_loop",
            ts=None):
    if outcome in ct.UNOBSERVABLE_OUTCOMES:
        signals = ct.unknown_turn_signals(_observed(installed=installed))
        status = 502 if outcome == ct.OUTCOME_CRASH else 500
    else:
        signals = dict(_OK_SIGNALS)
        signals["security"] = dict(_OK_SIGNALS["security"])
        signals[ct.OBSERVER_INSTALLED_FIELD] = installed
        if not installed:
            # An uninstrumented process cannot state these; snapshot() returns null.
            signals["llm_usage_status"] = "not_instrumented"
            signals["provider_schema_400_count"] = None
            signals["llm_calls"] = None
        status = 200
    return ct.build_canary_turn_record(
        endpoint="alex", agent_arch=arch, candidate_sha="c" * 12,
        strict=(arch != "legacy"), request_id=f"{arch}-{outcome}-{index}",
        conversation_id=f"conv-{index}", user_id=f"user-{index}",
        http_status=status, turn_outcome=outcome, turn_latency_ms=120.0,
        signals=signals, ts=ts or NOW)


# --------------------------------------------------------------------------- #
# The four cells                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("outcome", [ct.OUTCOME_CRASH, ct.OUTCOME_SERVER_ERROR])
def test_cell1_unobservable_outcome_with_the_observer_installed_is_unmeasured(outcome):
    record = _record(1, outcome=outcome, installed=True)

    assert record[ct.OBSERVER_INSTALLED_FIELD] is True
    assert record["llm_usage_status"] == "partial", "never a certified zero"
    assert record["llm_usage"] is None
    # NOT a contract violation...
    assert cr.validate_record(record) == []
    # ...and still not priced at zero: the spend is unknown, and unknown it stays.
    priced = cc.sum_usage([record])
    assert priced["_unmeasured_turns"]["count"] == 1
    assert priced["_unobservable_turns"]["count"] == 1
    assert priced["_no_llm_call_turns"]["count"] == 0
    assert cc.compute_cost(priced, {"version": 1, "unverified": False,
                                    "models": {}})["total_cost"] is None


@pytest.mark.parametrize("outcome", [ct.OUTCOME_CRASH, ct.OUTCOME_SERVER_ERROR])
def test_cell2_unobservable_outcome_without_an_observer_is_still_a_violation(outcome):
    """The 2026-07-25 class. The exemption must not reach it."""
    record = _record(2, outcome=outcome, installed=False)

    assert record[ct.OBSERVER_INSTALLED_FIELD] is False
    problems = cr.validate_record(record)
    assert any("undercount of unknown size" in p for p in problems), problems


def test_cell3_a_completed_zero_call_turn_with_an_observer_is_unchanged():
    record = _record(3, outcome=ct.OUTCOME_OK, installed=True)

    assert record["llm_usage_status"] == "no_llm_calls"
    assert cr.validate_record(record) == []
    priced = cc.sum_usage([record])
    assert priced["_no_llm_call_turns"]["count"] == 1
    assert priced["_unmeasured_turns"]["count"] == 0


def test_cell4_a_completed_turn_without_an_observer_still_holds():
    record = _record(4, outcome=ct.OUTCOME_OK, installed=False)

    problems = cr.validate_record(record)
    assert any("undercount of unknown size" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# The edges of the exemption                                                  #
# --------------------------------------------------------------------------- #

def test_an_older_record_without_the_flag_keeps_the_stricter_reading():
    """The exemption is granted on positive evidence only. Absent is not true."""
    record = _record(5, outcome=ct.OUTCOME_CRASH, installed=True)
    record.pop(ct.OBSERVER_INSTALLED_FIELD)

    assert any("undercount of unknown size" in p for p in cr.validate_record(record))


def test_not_instrumented_is_never_forgiven_even_on_a_crash():
    """`not_instrumented` asserts nothing was watching, which contradicts the flag
    that would grant the exemption. A record cannot have it both ways."""
    record = _record(6, outcome=ct.OUTCOME_CRASH, installed=True)
    record["llm_usage_status"] = "not_instrumented"

    assert any("undercount of unknown size" in p for p in cr.validate_record(record))


def test_a_healthy_turn_cannot_borrow_the_exemption():
    """Otherwise `partial` becomes an opt-out from the cost side of the A/B."""
    record = _record(7, outcome=ct.OUTCOME_OK, installed=True)
    record["llm_usage_status"] = "partial"

    assert any("undercount of unknown size" in p for p in cr.validate_record(record))


def test_the_flag_must_be_a_boolean():
    record = _record(8, outcome=ct.OUTCOME_CRASH, installed=True)
    record[ct.OBSERVER_INSTALLED_FIELD] = "true"

    assert any("is not a boolean" in p for p in cr.validate_record(record))


# --------------------------------------------------------------------------- #
# The window the owner asked for: 30% crashes, both ways                      #
# --------------------------------------------------------------------------- #

def _window(*, installed: bool, crash_fraction: float = 0.30, n: int = 100):
    records = []
    for i in range(n):
        crashed = i < int(n * crash_fraction)
        records.append(_record(
            i, outcome=(ct.OUTCOME_CRASH if crashed else ct.OUTCOME_OK),
            installed=installed, ts=NOW - timedelta(minutes=i)))
        records.append(_record(
            i, outcome=ct.OUTCOME_OK, installed=installed, arch="legacy",
            ts=NOW - timedelta(minutes=i)))
    return records


def test_a_v3_window_with_30pct_crashes_and_an_observer_does_not_hold():
    report = cr.build_report(_window(installed=True), now_override=NOW)
    verdict = report["verdict"]
    candidate = report["arches"]["fc"]

    assert verdict["instrumentation"]["contract"]["violating"] == 0, (
        verdict["instrumentation"]["contract"]["violations"])
    assert not any("telemetry contract" in reason
                   for reason in verdict["instrumentation"]["reasons"]), verdict
    # Forgiven, not hidden: the crashes are still counted and still visible.
    assert candidate["unmeasured_spend_turns"] == 30
    assert candidate["unobservable_unmeasured_turns"] == 30
    assert candidate["http_5xx_count"] == 30
    assert "unmeasured spend turns" in cr.render_text(report)


def test_the_same_window_without_an_observer_holds():
    report = cr.build_report(_window(installed=False), now_override=NOW)
    verdict = report["verdict"]

    assert verdict["instrumentation"]["contract"]["violating"] > 0
    assert verdict["decision"] in ("INSTRUMENTATION-HOLD", "CANARY-BLOCK")
    assert verdict["exit_code"] != 0
    assert report["arches"]["fc"]["unobservable_unmeasured_turns"] == 0
