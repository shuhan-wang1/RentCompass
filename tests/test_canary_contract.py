"""Canary telemetry CONTRACT closed-loop tests.

The point of this file: records are built by the REAL producer
(``app.core.canary_telemetry.build_canary_turn_record`` — the same function
``_emit_canary_turn`` serialises) and fed to the REAL consumer
(``scripts/canary_report.build_report``). Nothing here hand-writes a record dict
that the producer could not actually emit, so producer/consumer drift fails the
suite instead of silently turning the gate green.

Run:  python3 tests/test_canary_contract.py     (or: pytest tests/test_canary_contract.py)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# tests/conftest.py already pins app/ and src/ (tests import the flat `core.*`
# modules). Import via that SAME path the app itself uses — `app.core.…` would
# create a second module identity for canary_telemetry and collide with the
# top-level app.py module during whole-suite collection.
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
if os.path.join(_ROOT, "app") not in sys.path:          # standalone `python3 tests/…`
    sys.path.insert(0, os.path.join(_ROOT, "app"))

from core.canary_telemetry import (  # noqa: E402
    ENDPOINT_ALEX, ENDPOINT_SEARCH_DIRECT, OUTCOME_OK, OUTCOME_SERVER_ERROR,
    HASH_UNKEYED, build_canary_turn_record, search_direct_signals,
    unknown_turn_signals,
)
import canary_report  # noqa: E402

os.environ.setdefault("CANARY_USER_HASH_KEY", "contract-test-key")

_T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def make(i=0, *, arch="fc_loop", endpoint=ENDPOINT_ALEX, latency=1000.0,
         denied=0, tainted_exec=0, forbidden_exec=0,
         dsml_blocked=0, dsml_leak=0, provider_400=0, sha="d62628c",
         http_status=200, outcome=OUTCOME_OK, strict=None, **sig_over):
    """Build one record through the real producer."""
    signals = {
        "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
        "security": {
            "denied_write_count": denied,
            "tainted_write_executed_count": tainted_exec,
            "forbidden_write_executed_count": forbidden_exec,
        },
        "dsml_blocked": dsml_blocked,
        "dsml_leak": dsml_leak,
        "provider_schema_400_count": provider_400,
        "llm_calls": 1, "tool_batches": 0, "llm_usage": None,
    }
    signals.update(sig_over)
    return build_canary_turn_record(
        endpoint=endpoint, agent_arch=arch, candidate_sha=sha,
        strict=(arch == "fc_loop") if strict is None else strict,
        request_id=f"req{i}", conversation_id=f"conv{i}", user_id=f"user{i}",
        http_status=http_status, turn_outcome=outcome, turn_latency_ms=latency,
        signals=signals, ts=_T0 + timedelta(seconds=i),
    )


def baseline(n=40, **over):
    """A clean, conformant fc+legacy population."""
    recs = [make(i, arch="fc_loop", **over) for i in range(n)]
    recs += [make(1000 + i, arch="legacy") for i in range(n)]
    return recs


def verdict_of(records):
    return canary_report.build_report(records)["verdict"]


# --------------------------------------------------------------------------- #

def test_producer_output_is_contract_conformant():
    """The producer's own output must satisfy the consumer's required-field contract."""
    v = canary_report.validate_records([make(0)])
    assert v["ok"], f"producer emits a non-conformant record: {v['violations']}"


def test_clean_population_proceeds():
    vd = verdict_of(baseline())
    assert vd["exit_code"] == 0, vd
    assert not vd["instrumentation"]["failed"], vd["instrumentation"]


def test_denied_write_is_non_clean_but_not_a_breach():
    """A DENIED write is a real security event (non-clean) but the write never ran,
    so it must never trigger a zero-tolerance block."""
    recs = baseline()
    recs[0] = make(0, denied=2)
    rpt = canary_report.build_report(recs)
    assert rpt["arches"]["fc"]["security_non_clean_count"] == 1, rpt["arches"]["fc"]
    assert rpt["arches"]["fc"]["denied_write_count"] == 2
    assert rpt["verdict"]["exit_code"] != 3, rpt["verdict"]


def test_clean_records_are_not_counted_non_clean():
    """Guards the v1 bug: security_audit={'denied_writes': N} normalised to "" and
    every record scored clean. Structured parsing must count only real events."""
    rpt = canary_report.build_report(baseline())
    assert rpt["arches"]["fc"]["security_non_clean_count"] == 0
    assert rpt["arches"]["fc"]["denied_write_count"] == 0


def test_executed_tainted_write_blocks():
    recs = baseline()
    recs[0] = make(0, tainted_exec=1)
    vd = verdict_of(recs)
    assert vd["exit_code"] == 3 and vd["decision"] == "CANARY-BLOCK", vd


def test_executed_forbidden_write_blocks():
    recs = baseline()
    recs[0] = make(0, forbidden_exec=1)
    vd = verdict_of(recs)
    assert vd["exit_code"] == 3, vd


def test_dsml_leak_blocks_but_dsml_blocked_does_not():
    leaked = baseline()
    leaked[0] = make(0, dsml_leak=1)
    assert verdict_of(leaked)["exit_code"] == 3, "a real DSML leak must block"

    blocked = baseline()
    blocked[0] = make(0, dsml_blocked=5)
    vd = verdict_of(blocked)
    assert vd["exit_code"] != 3, f"blocked+recovered markup is the SAFE path: {vd}"


def test_provider_schema_400_blocks():
    recs = baseline()
    recs[0] = make(0, provider_400=1)
    vd = verdict_of(recs)
    assert vd["exit_code"] == 3, vd
    assert any("400" in r for r in vd["zero_tolerance"]["reasons"]), vd


def test_missing_required_field_holds():
    recs = baseline()
    bad = make(0)
    del bad["dsml_leak"]                     # instrumentation regressed
    recs[0] = bad
    vd = verdict_of(recs)
    assert vd["exit_code"] == 2 and vd["decision"] == "INSTRUMENTATION-HOLD", vd


def test_null_required_field_holds():
    """A null asserts nothing — it must not satisfy the gate."""
    recs = baseline()
    bad = make(0)
    bad["dsml_leak"] = None
    recs[0] = bad
    vd = verdict_of(recs)
    assert vd["exit_code"] == 2, vd


def test_null_nested_security_field_holds():
    recs = baseline()
    bad = make(0)
    bad["security"]["forbidden_write_executed_count"] = None
    recs[0] = bad
    assert verdict_of(recs)["exit_code"] == 2


def test_v1_record_holds():
    """The pre-v2 record shape must NOT be able to clear the gate."""
    v1 = {
        "event": "canary.turn", "agent_arch": "fc_loop", "candidate_sha": "d62628c",
        "strict": True, "request_id": "r", "conversation_id": "c", "user_id": "u",
        "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
        "security_audit": {"denied_writes": 0}, "turn_latency_ms": 100.0,
        "llm_calls": 1, "tool_batches": 0,
    }
    recs = baseline()
    recs[0] = v1
    vd = verdict_of(recs)
    assert vd["exit_code"] == 2, f"v1 records must HOLD, got {vd}"


def test_search_direct_excluded_from_perf_but_NOT_from_security():
    """Endpoint scoping is metric-dependent, and conflating the two is a fail-open:

      * perf / degradation -> agent endpoint only (search_direct is deterministic and
        LLM-free, so its latency profile is not comparable to an agent turn);
      * SECURITY zero-tolerance -> GLOBAL. A forbidden write is a breach on whichever
        public endpoint it executed; scoping it to the gate would ship a real violation.
    """
    recs = baseline()
    recs.append(make(9001, endpoint=ENDPOINT_SEARCH_DIRECT, forbidden_exec=1))
    rpt = canary_report.build_report(recs)
    # excluded from the A/B population...
    assert rpt["records_excluded_from_gate"] == 1
    assert "search_direct" in rpt["excluded_endpoints"]
    assert rpt["arches"]["fc"]["forbidden_write_count"] == 0, "must not enter the gate slice"
    # ...but the breach still blocks, globally
    assert rpt["verdict"]["exit_code"] == 3, "a security breach on search_direct MUST block"
    assert (rpt["zero_tolerance_global"]["by_endpoint"]["search_direct"]
            ["forbidden_write_count"] == 1)


def test_search_direct_latency_does_not_dilute_the_ab():
    """A slow deterministic search must not move the agent's percentiles."""
    recs = baseline()
    base_p50 = canary_report.build_report(recs)["arches"]["fc"]["p50_ms"]
    recs += [make(9100 + i, endpoint=ENDPOINT_SEARCH_DIRECT, latency=90000.0)
             for i in range(20)]
    assert canary_report.build_report(recs)["arches"]["fc"]["p50_ms"] == base_p50


def test_eval_only_fields_are_null_not_false():
    """forbidden_read / no_evidence_numbers are eval-only: the producer must emit
    null (not False), and the report must surface None rather than a clean 0."""
    r = make(0)
    assert r["forbidden_read"] is None and r["no_evidence_numbers"] is None
    assert "forbidden_read" in r["eval_only"]
    rpt = canary_report.build_report(baseline())
    assert rpt["arches"]["fc"]["forbidden_read_count"] is None
    assert rpt["arches"]["fc"]["no_evidence_numbers_count"] is None
    # and being eval-only must NOT by itself hold the gate
    assert not rpt["verdict"]["instrumentation"]["failed"]


def test_5xx_is_observable_and_counted():
    recs = baseline()
    recs[0] = make(0, http_status=500, outcome=OUTCOME_SERVER_ERROR)
    rpt = canary_report.build_report(recs)
    assert rpt["arches"]["fc"]["http_5xx_count"] == 1, rpt["arches"]["fc"]


def test_unkeyed_hash_holds_the_gate():
    """No stable HMAC secret => user hashes decorrelate across restarts, so every
    per-user statistic in the window is unreliable. That must HOLD, not continue."""
    saved = {k: os.environ.pop(k, None) for k in ("CANARY_USER_HASH_KEY", "FLASK_SECRET_KEY")}
    try:
        r = make(0)
        assert r["user_id_hash"] is None, "must not emit an unstable digest"
        assert r["user_id_hash_status"] == HASH_UNKEYED
        recs = baseline()
        recs[0] = r
        vd = verdict_of(recs)
        assert vd["exit_code"] == 2 and vd["decision"] == "INSTRUMENTATION-HOLD", vd
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_no_stable_secret_never_falls_back_to_a_random_salt():
    """Guard against reintroducing a per-process salt: two builds with no secret
    must both yield None, not two different digests."""
    saved = {k: os.environ.pop(k, None) for k in ("CANARY_USER_HASH_KEY", "FLASK_SECRET_KEY")}
    try:
        assert make(1)["user_id_hash"] is None and make(1)["user_id_hash"] is None
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_boundary_5xx_holds_because_state_is_unobservable():
    """A boundary 5xx must still produce a record (never vanish), but its security
    and provider fields are UNOBSERVABLE: the agent may have executed `remember`
    before dying, and a provider schema 400 may itself be the crash cause. Writing
    0 there would be fail-open, so it is null -> HOLD."""
    boundary = build_canary_turn_record(
        endpoint=ENDPOINT_ALEX, agent_arch="fc_loop", candidate_sha="d62628c", strict=True,
        request_id="unknown", conversation_id="unknown", user_id=None,
        http_status=500, turn_outcome=OUTCOME_SERVER_ERROR, turn_latency_ms=0.0,
        signals=unknown_turn_signals(), ts=_T0,
    )
    # the record EXISTS (did not vanish) and is counted as a 5xx...
    recs = baseline()
    recs.append(boundary)
    rpt = canary_report.build_report(recs)
    assert rpt["arches"]["fc"]["http_5xx_count"] == 1, rpt["arches"]["fc"]
    # ...and its unobservable fields hold the gate rather than reading as clean.
    assert rpt["verdict"]["exit_code"] == 2, rpt["verdict"]
    assert rpt["verdict"]["decision"] == "INSTRUMENTATION-HOLD"


def test_5xx_latency_excluded_from_percentiles():
    """Fast failures must not flatter p50."""
    slow = [make(i, latency=9000.0) for i in range(10)]
    slow += [make(1000 + i, arch="legacy", latency=9000.0) for i in range(10)]
    base_p50 = canary_report.build_report(slow)["arches"]["fc"]["p50_ms"]
    with_5xx = slow + [make(500 + i, latency=0.0, http_status=500,
                            outcome=OUTCOME_SERVER_ERROR) for i in range(10)]
    rpt = canary_report.build_report(with_5xx)
    assert rpt["arches"]["fc"]["p50_ms"] == base_p50, \
        f"5xx dragged p50 {base_p50} -> {rpt['arches']['fc']['p50_ms']}"
    assert rpt["arches"]["fc"]["http_5xx_count"] == 10


def test_duplicate_record_for_one_request_holds():
    """One turn must emit exactly one record. A duplicate inflates the denominator
    and halves every rate, so the contract rejects it outright."""
    recs = baseline()
    recs.append(dict(recs[0]))          # same request_id + endpoint + arch
    vd = verdict_of(recs)
    assert vd["exit_code"] == 2, vd
    assert any("duplicate" in r for r in vd["instrumentation"]["reasons"]), vd


# --------------------------------------------------------------------------- #
# Malformed-record table. Each case injects ONE defect into an otherwise clean
# window and asserts the gate HOLDs (exit 2) with a reason that names the defect.
#
# These are table-driven rather than @pytest.mark.parametrize so the file stays
# runnable as `python3 tests/test_canary_contract.py` on a box with no pytest —
# which is how it was actually run during the canary build-out.
# --------------------------------------------------------------------------- #

_DELETE = object()


def _mutate(rec: dict, **over) -> dict:
    """Hand-edit producer output. Used ONLY for defects the current producer cannot
    emit — the point is to prove the consumer catches a FUTURE producer regression,
    so these cases are deliberately not expressible through make()."""
    out = dict(rec)
    for k, v in over.items():
        if v is _DELETE:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def _held_reasons(records, **report_kw):
    rpt = canary_report.build_report(records, **report_kw)
    return rpt["verdict"], rpt["verdict"]["instrumentation"]["reasons"]


# (name, mutation over a clean baseline, substring expected in an instrumentation
#  reason, expected exit code). Exit 2 == INSTRUMENTATION-HOLD; exit 3 where the
# malformed value ALSO reads as a zero-tolerance breach, since zero-tolerance
# outranks hold — blocking is the safe resolution of that ambiguity.
_MALFORMED_CASES = [
    ("corrupt ts",
     lambda recs: [_mutate(recs[0], ts="yesterday-ish")] + recs[1:],
     "ts missing or unparseable", 2),
    ("missing ts",
     lambda recs: [_mutate(recs[0], ts=_DELETE)] + recs[1:],
     "ts missing or unparseable", 2),
    # A negative counter is the dangerous case: summed across a window it can cancel
    # a real violation, so it must be rejected rather than added. Note the two
    # aggregators differ — the write counters count RECORDS with a non-zero value (so
    # -1 reads as a breach, exit 3) while dsml_leak SUMS (so -1 stays under the
    # threshold and only the contract check catches it, exit 2). Both fail closed;
    # the asymmetry is pinned here so a future change to either one is visible.
    ("negative security counter",
     lambda recs: [make(0, forbidden_exec=-1)] + recs[1:],
     "security.forbidden_write_executed_count", 3),
    ("negative dsml_leak",
     lambda recs: [make(0, dsml_leak=-1)] + recs[1:],
     "dsml_leak", 2),
    # bool is an int subclass in Python: sum([True]) == 1, so an unchecked True would
    # both corrupt the count and (here) trip the leak gate.
    ("bool smuggled in as a count",
     lambda recs: [_mutate(recs[0], dsml_leak=True)] + recs[1:],
     "dsml_leak", 3),
    ("illegal agent_arch",
     lambda recs: [make(0, arch="fc")] + recs[1:],
     "agent_arch", 2),
    ("illegal turn_outcome",
     lambda recs: [_mutate(recs[0], turn_outcome="probably_fine")] + recs[1:],
     "turn_outcome", 2),
    ("impossible http_status",
     lambda recs: [_mutate(recs[0], http_status=999)] + recs[1:],
     "http_status", 2),
    ("fc_loop without strict",
     lambda recs: [make(0, strict=False)] + recs[1:],
     "strict", 2),
    ("keyed status with no digest",
     lambda recs: [_mutate(recs[0], user_id_hash=None)] + recs[1:],
     "user_id_hash", 2),
    ("window mixes two candidate builds",
     lambda recs: [make(0, sha="deadbee")] + recs[1:],
     "candidate_sha", 2),
    ("eval_only declared but carries a value",
     lambda recs: [_mutate(recs[0], forbidden_read=False)] + recs[1:],
     "eval_only", 2),
    ("eval-only value shipped without the declaration",
     lambda recs: [_mutate(recs[0], no_evidence_numbers=False, eval_only=[])] + recs[1:],
     "eval_only", 2),
    # A ">= 2" floor was fail-open forwards: a v3 that re-means a field would be
    # scored by a consumer that cannot read it.
    ("unknown FUTURE schema version",
     lambda recs: [_mutate(recs[0], telemetry_schema_version=3)] + recs[1:],
     "newer than this consumer knows", 2),
    ("schema version as a float",
     lambda recs: [_mutate(recs[0], telemetry_schema_version=2.0)] + recs[1:],
     "telemetry_schema_version", 2),
    # `strict` decides whether a record is the configuration under test, so the
    # CONTROL arm has to carry it too — not just fc_loop.
    ("legacy record missing strict",
     lambda recs: recs[:-1] + [_mutate(recs[-1], strict=_DELETE)],
     "strict", 2),
    ("strict as a string",
     lambda recs: [_mutate(recs[0], strict="true")] + recs[1:],
     "strict", 2),
]


def test_malformed_records_never_pass_the_gate():
    for name, mutate, expect, code in _MALFORMED_CASES:
        vd, reasons = _held_reasons(mutate(baseline()))
        assert vd["exit_code"] == code, \
            f"[{name}] expected exit {code}, got {vd['exit_code']}: {vd}"
        assert any(expect in r for r in reasons), \
            f"[{name}] no instrumentation reason mentioned {expect!r}; reasons were {reasons}"


def test_each_malformed_case_is_actually_a_mutation():
    """Guards the table itself: a case that accidentally builds a CLEAN record would
    pass the assertion above only if something else in the window were broken."""
    clean = baseline()
    assert verdict_of(clean)["exit_code"] == 0, "baseline is not clean to begin with"
    for name, mutate, _, _code in _MALFORMED_CASES:
        # repr, not ==: some mutations change only the TYPE (2 -> 2.0, True -> 1),
        # and Python's == cannot see that — which is precisely why the consumer has
        # to check types explicitly.
        assert repr(mutate(clean)) != repr(clean), f"[{name}] mutation changed nothing"


def test_provider_400s_are_counted_by_record_not_summed():
    """A negative counter elsewhere in the window must not be able to cancel a real
    provider 400 down to zero. Every other zero-tolerance signal already counted
    affected RECORDS; this one summed raw values, so 5 + (-5) read as "no 400s"
    and the verdict downgraded from CANARY-BLOCK to a misleading INSTRUMENTATION-HOLD.
    """
    recs = baseline()
    recs[0] = make(0, provider_400=5)
    recs[1] = make(1, provider_400=-5)
    rpt = canary_report.build_report(recs)
    vd = rpt["verdict"]
    assert vd["exit_code"] == 3, f"cancelled 400s must still BLOCK: {vd}"
    assert any("provider schema 400" in r for r in vd["zero_tolerance"]["reasons"]), vd
    # The magnitude is floored, never negative.
    assert rpt["zero_tolerance_global"]["api_400_count"] == 1
    assert rpt["zero_tolerance_global"]["api_400_total"] == 5


def test_unparseable_log_line_holds_the_gate():
    """A line that would not parse means a truncated/interleaved write. The lost
    record could be the one carrying a violation, so a count in a summary row is not
    an acceptable substitute for holding."""
    vd, reasons = _held_reasons(baseline(), skipped=1)
    assert vd["exit_code"] == 2, vd
    assert any("unparseable" in r for r in reasons), reasons


def test_clean_window_with_zero_skipped_still_proceeds():
    """The negative control for the case above — otherwise the test would pass even
    if the gate held on every input."""
    vd, _ = _held_reasons(baseline(), skipped=0)
    assert vd["exit_code"] == 0, vd


def test_user_id_is_hashed_not_raw():
    r = make(0)
    assert "user_id" not in r, "raw user_id must never be emitted"
    assert r["user_id_hash"] and r["user_id_hash"] != "user0"
    assert len(r["user_id_hash"]) == 32


def test_hash_is_stable_and_distinct():
    a1, a2 = make(1)["user_id_hash"], make(1)["user_id_hash"]
    b = make(2)["user_id_hash"]
    assert a1 == a2 and a1 != b


# --------------------------------------------------------------------------- #

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:  # contract/import errors are failures too
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
