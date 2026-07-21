"""Tests for scripts/canary_report.py — the offline fc_loop canary gate aggregator.

Covers: green fc-vs-legacy (exit 0), p95 stage-pause breach (exit 2), zero-tolerance write
(exit 3), both-minima stage logic (enough turns / not enough hours -> not eligible), and
tolerant line parsing (bare JSON + "timestamp level name: {json}" prefixed). The aggregation
functions are invoked directly by importing the script as a module — no subprocesses.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("canary_report", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load_module()

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


_RID = 0


def _rec(arch, latency_ms, *, ts=None, soft=False, partial=False, tbt=False,
         audit="clean", conv="c", **extra):
    """Build a schema-v2 canary.turn record.

    Migrated from the v1 shape. `audit` is kept as an ergonomic shorthand for the
    call sites below and is mapped onto the STRUCTURED security object that v2
    requires (v1's free-form string was never actually parseable by the report —
    ``{"denied_writes": N}`` normalised to "" and scored clean).
    """
    sec = {"denied_write_count": 0, "tainted_write_executed_count": 0,
           "forbidden_write_executed_count": 0}
    a = (audit or "clean").lower()
    if "denied" in a:
        sec["denied_write_count"] = 1
    elif "forbidden_write_executed" in a:
        sec["forbidden_write_executed_count"] = 1
    elif "tainted" in a or "unauthorized" in a:
        sec["tainted_write_executed_count"] = 1

    # Every turn has its OWN request_id in reality; the v1 helper hardcoded "r",
    # which the v2 contract now (correctly) rejects as duplicate records.
    global _RID
    _RID += 1
    arch_full = "fc_loop" if arch in ("fc", "fc_loop") else "legacy"
    stamp = ts if isinstance(ts, str) else (ts.isoformat() if ts is not None
                                            else NOW.isoformat())
    r = {
        "event": "canary.turn",
        "telemetry_schema_version": 2,
        "ts": stamp,
        "endpoint": "alex",
        "agent_arch": arch_full,
        "candidate_sha": "7db03e7",
        "strict": arch_full == "fc_loop",
        "request_id": f"r{_RID}",
        "conversation_id": conv,
        "user_id_hash": "h" * 32,
        "user_id_hash_status": "keyed",
        "http_status": 200,
        "turn_outcome": "ok",
        "soft_wrapped": soft,
        "partial": partial,
        "tool_budget_timeout": tbt,
        "security": sec,
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
        "turn_latency_ms": latency_ms,
        "llm_calls": 2,
        "tool_batches": 1,
        "llm_usage": None,
        # Layer B: required, because "cost us nothing" and "we did not measure the
        # cost" must not render as the same record.
        "llm_usage_status": "complete",
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": ["forbidden_read", "no_evidence_numbers"],
    }
    r.update(extra)
    return r


def test_green_fc_vs_legacy_exit0():
    records = []
    for i in range(60):
        records.append(_rec("fc", 5000.0, conv=f"fc{i % 12}"))
        records.append(_rec("legacy", 5200.0, conv=f"lg{i % 12}"))
    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["fc"]
    assert fc["turns"] == 60
    assert fc["p50_ms"] == 5000.0 and fc["p95_ms"] == 5000.0
    assert fc["degraded_rate"] == 0.0
    v = report["verdict"]
    assert v["decision"] == "PROCEED"
    assert v["exit_code"] == 0
    assert v["zero_tolerance"]["breached"] is False
    assert v["stage_pause"]["breached"] is False
    # v2: eval-only metrics stay advisory notes, but nothing else may be "not
    # instrumented" — a prod-observable metric that is missing now HOLDs instead.
    joined = " ".join(v["stage_pause"]["notes"])
    assert "requires eval sweep" in joined
    assert "not instrumented" not in joined
    assert v["instrumentation"]["failed"] is False


# --------------------------------------------------------------------------- #
# 2) p95 breach -> exit 2                                                     #
# --------------------------------------------------------------------------- #

def test_p95_breach_exit2():
    # 20 fc turns: 15 fast @ 5000ms + 5 slow @ 40000ms. nearest-rank p95 index =
    # ceil(0.95*20)-1 = 18 -> a slow (40000ms) sample > 30000ms limit. p50 stays fast.
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(15)]
    records += [_rec("fc", 40000.0, conv=f"fs{i}") for i in range(5)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(20)]
    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["fc"]
    assert fc["p95_ms"] == 40000.0
    assert fc["p50_ms"] == 5000.0
    v = report["verdict"]
    assert v["exit_code"] == 2
    assert v["decision"] == "STAGE-PAUSE"
    assert v["zero_tolerance"]["breached"] is False
    assert any("p95" in r for r in v["stage_pause"]["reasons"])


def test_degraded_rate_breach_exit2():
    # 12/100 fc turns soft_wrapped OR partial -> 12% > 10% -> stage-pause.
    records = [_rec("fc", 5000.0, conv=f"fc{i}", soft=(i < 7), partial=(7 <= i < 12))
               for i in range(100)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(100)]
    report = cr.build_report(records, now_override=NOW)
    assert report["arches"]["fc"]["degraded_rate"] == 0.12
    v = report["verdict"]
    assert v["exit_code"] == 2
    assert any("partial+soft_wrapped" in r for r in v["stage_pause"]["reasons"])


# --------------------------------------------------------------------------- #
# 3) zero-tolerance write -> exit 3 (dominates a simultaneous p95 breach)     #
# --------------------------------------------------------------------------- #

def test_zero_tolerance_write_exit3():
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(40)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(40)]
    # one tainted write that actually executed on the fc pool
    records.append(_rec("fc", 5000.0, conv="bad", audit="tainted_write_executed"))
    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["fc"]
    assert fc["tainted_unauth_write_count"] == 1
    assert fc["security_non_clean_count"] == 1
    v = report["verdict"]
    assert v["exit_code"] == 3
    assert v["decision"] == "CANARY-BLOCK"
    assert v["zero_tolerance"]["breached"] is True


def test_denied_write_is_non_clean_but_not_zero_tolerance():
    # A denied tainted-write attempt is the SAFE A+ path: non-clean, but never executed.
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(30)]
    records.append(_rec("fc", 5000.0, conv="d", audit="tainted_write_denied"))
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(30)]
    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["fc"]
    assert fc["security_non_clean_count"] == 1
    assert fc["tainted_unauth_write_count"] == 0
    assert report["verdict"]["exit_code"] == 0


def test_forbidden_write_and_dsml_and_400_zero_tolerance():
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(20)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(20)]
    records.append(_rec("fc", 5000.0, conv="fw", audit="forbidden_write_executed",
                        dsml_leak=1))
    records.append(_rec("fc", 5000.0, conv="b4", audit="clean",
                        provider_schema_400_count=3))
    report = cr.build_report(records, now_override=NOW)
    fc = report["arches"]["fc"]
    assert fc["forbidden_write_count"] == 1
    assert fc["dsml_leak_count"] == 1
    assert fc["api_400_count"] == 3
    v = report["verdict"]
    assert v["exit_code"] == 3
    reasons = " ".join(v["zero_tolerance"]["reasons"])
    assert "forbidden write" in reasons
    assert "DSML" in reasons
    assert "400" in reasons


# --------------------------------------------------------------------------- #
# 4) both-minima stage logic                                                  #
# --------------------------------------------------------------------------- #

def test_stage_enough_turns_not_enough_hours_not_eligible():
    # c1 needs 200 turns AND 24h. We have 250 turns but only 10h elapsed -> HOLD, exit 0.
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(250)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(250)]
    since = NOW - timedelta(hours=10)
    report = cr.build_report(records, now_override=NOW, stage="c1", since=since)
    sp = report["verdict"]["stage_progress"]
    assert sp["turns_ok"] is True
    assert sp["hours_ok"] is False
    assert sp["eligible"] is False
    v = report["verdict"]
    assert v["decision"] == "HOLD"
    assert v["exit_code"] == 0


def test_stage_both_minima_satisfied_eligible():
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(250)]
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(250)]
    since = NOW - timedelta(hours=30)   # >= 24h for c1
    report = cr.build_report(records, now_override=NOW, stage="c1", since=since)
    sp = report["verdict"]["stage_progress"]
    assert sp["turns_ok"] is True and sp["hours_ok"] is True
    assert sp["eligible"] is True
    assert report["verdict"]["decision"] == "STAGE-PROGRESS-OK"
    assert report["verdict"]["exit_code"] == 0


def test_stage_enough_hours_not_enough_turns_not_eligible():
    records = [_rec("fc", 5000.0, conv=f"fc{i}") for i in range(120)]  # < 200 for c1
    records += [_rec("legacy", 5000.0, conv=f"lg{i}") for i in range(120)]
    since = NOW - timedelta(hours=48)
    report = cr.build_report(records, now_override=NOW, stage="c1", since=since)
    sp = report["verdict"]["stage_progress"]
    assert sp["hours_ok"] is True and sp["turns_ok"] is False
    assert sp["eligible"] is False
    assert report["verdict"]["decision"] == "HOLD"


# --------------------------------------------------------------------------- #
# 5) tolerant line parsing (bare JSON + prefixed)                             #
# --------------------------------------------------------------------------- #

def test_parse_bare_json_line():
    line = json.dumps({"event": "canary.turn", "agent_arch": "fc", "turn_latency_ms": 1200})
    rec = cr.parse_line(line)
    assert rec is not None
    assert rec["agent_arch"] == "fc"
    assert cr.canonical_arch(rec) == "fc"


def test_parse_prefixed_line_and_ts():
    payload = {"event": "canary.turn", "agent_arch": "legacy", "turn_latency_ms": 900}
    line = f"2026-07-20T11:59:58.500Z INFO canary.turn: {json.dumps(payload)}"
    rec = cr.parse_line(line)
    assert rec is not None
    assert rec["agent_arch"] == "legacy"
    ts = cr.record_ts(rec)
    assert ts == datetime(2026, 7, 20, 11, 59, 58, 500000, tzinfo=timezone.utc)


def test_parse_prefixed_comma_millis_space_sep():
    payload = {"agent_arch": "fc", "turn_latency_ms": 5}
    line = f"2026-07-20 11:00:00,250 WARNING canary.turn: {json.dumps(payload)}"
    rec = cr.parse_line(line)
    ts = cr.record_ts(rec)
    assert ts == datetime(2026, 7, 20, 11, 0, 0, 250000, tzinfo=timezone.utc)


def test_parse_blank_and_garbage_returns_none():
    assert cr.parse_line("") is None
    assert cr.parse_line("   ") is None
    assert cr.parse_line("no json here") is None


def test_load_records_mixed_shapes_and_window(tmp_path):
    p = tmp_path / "canary.jsonl"
    lines = [
        json.dumps(_rec("fc", 5000.0, ts=NOW.isoformat(), conv="a")),
        f"2026-07-20T11:00:00Z INFO canary.turn: {json.dumps(_rec('legacy', 5100.0, conv='b'))}",
        "",  # blank
        "garbage line without json",
        json.dumps({"event": "other.event", "foo": 1}),  # non-canary -> dropped
        json.dumps(_rec("fc", 6000.0, ts=(NOW - timedelta(hours=50)).isoformat(), conv="old")),
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    records, skipped = cr.load_records([str(p)])
    assert skipped == 1                    # only the garbage line
    assert len(records) == 3               # 2 fc + 1 legacy (other.event dropped)
    # 24h window relative to NOW drops the 50h-old fc record.
    report = cr.build_report(records, window_hours=24.0, now_override=NOW)
    assert report["arches"]["fc"]["turns"] == 1
    assert report["arches"]["legacy"]["turns"] == 1


# --------------------------------------------------------------------------- #
# percentile convention + ts parsing units                                    #
# --------------------------------------------------------------------------- #

def test_percentile_nearest_rank_matches_repo_convention():
    import math
    values = [5000.0] * 91 + [31000, 33000, 36500, 40000, 42000, 44000, 46000]
    assert cr.percentile(values, 0.95) == sorted(values)[math.ceil(0.95 * 98) - 1]
    assert cr.percentile([1.0, 2.0], 0.5) == 1.0     # ceil(1.0)-1 = 0
    assert cr.percentile([], 0.5) is None


def test_parse_ts_epoch_seconds_and_millis():
    assert cr.parse_ts(1_700_000_000) == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert cr.parse_ts(1_700_000_000_000) == datetime.fromtimestamp(1_700_000_000,
                                                                     tz=timezone.utc)


def test_run_cli_end_to_end_exit_and_json(tmp_path):
    p = tmp_path / "c.jsonl"
    recs = [_rec("fc", 5000.0, ts=NOW.isoformat(), conv=f"fc{i}") for i in range(10)]
    recs += [_rec("legacy", 5000.0, ts=NOW.isoformat(), conv=f"lg{i}") for i in range(10)]
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    out = tmp_path / "report.json"
    code = cr.run(["--input", str(p), "--json", str(out), "--quiet"])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["arches"]["fc"]["turns"] == 10
    assert data["verdict"]["exit_code"] == 0


def test_missing_input_errors():
    assert cr.run([]) == 1


# =========================================================================== #
# --expect-turns: the external anchor                                        #
# =========================================================================== #
#
# Every other check in this report describes records that EXIST. None of them can
# see a turn that emitted nothing at all — and when turns vanish, every rate
# divides by a denominator that has already excluded the failures, so a run that
# lost a third of its traffic reports as a clean run of a smaller sample. This is
# the one blind spot the gate cannot close from the inside, which is why the count
# has to come from the driver.


def _fc_turns(n, **kw):
    return [_rec("fc", 1000, **kw) for _ in range(n)]


def _anchor(records, expected):
    return cr.build_report(records, expect_turns=expected)


def test_exactly_the_expected_count_passes():
    rep = _anchor(_fc_turns(50) + [_rec("legacy", 1000) for _ in range(50)], 50)
    et = rep["verdict"]["expected_turns"]
    assert et["matched"] is True
    assert (et["observed"], et["unique_request_ids"]) == (50, 50)
    assert rep["verdict"]["exit_code"] == 0


@pytest.mark.parametrize("n", [49, 51])
def test_off_by_one_in_either_direction_holds(n):
    """49 is a turn that produced no record; 51 is a record with no turn behind it.
    Both mean the telemetry does not describe the run, and neither is a rounding
    detail — they are the difference between a measurement and a guess."""
    rep = _anchor(_fc_turns(n), 50)
    et = rep["verdict"]["expected_turns"]
    assert et["matched"] is False
    assert et["observed"] == n
    assert rep["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert rep["verdict"]["exit_code"] == 2


def test_a_duplicate_request_id_cannot_stand_in_for_a_missing_turn():
    """The case a bare count cannot see: 50 records, but one turn emitted twice and
    another emitted nothing. By count that is indistinguishable from 50 clean turns,
    which is exactly why the ids are reconciled one for one."""
    recs = _fc_turns(49)
    dup = dict(recs[0])
    dup["conversation_id"] = "other"
    recs.append(dup)
    rep = _anchor(recs, 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 50
    assert et["unique_request_ids"] == 49
    assert et["duplicate_request_ids"]
    assert et["matched"] is False
    assert rep["verdict"]["exit_code"] == 2


def test_legacy_turns_cannot_make_up_the_count():
    """The control pool ran too. If its turns counted, a candidate pool that
    received no traffic at all would still reconcile."""
    rep = _anchor(_fc_turns(30) + [_rec("legacy", 1000) for _ in range(20)], 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 30
    assert et["matched"] is False
    assert any("not fc_loop" in k for k in et["ineligible_records"])
    assert rep["verdict"]["exit_code"] == 2


def test_search_direct_turns_cannot_make_up_the_count():
    """search_direct is LLM-free and deterministic. Counting it would let 50 form
    submissions certify an agent that was never exercised."""
    rep = _anchor(_fc_turns(45) + _fc_turns(5, endpoint="search_direct"), 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 45
    assert et["matched"] is False
    assert rep["verdict"]["exit_code"] == 2


def test_old_v1_records_cannot_make_up_the_count():
    """A stale log rotated in. v1 records assert none of the v2 facts, so counting
    them would let the window be padded with turns from a build under no test."""
    old = _fc_turns(10)
    for r in old:
        r["telemetry_schema_version"] = 1
    rep = _anchor(_fc_turns(40) + old, 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 40
    assert any("contract" in k for k in et["ineligible_records"])
    assert rep["verdict"]["exit_code"] == 2


def test_malformed_records_cannot_pad_the_count():
    """A record missing a required field holds the gate on its own. It must not
    ALSO be counted toward 50, or the two failures would cancel: a padded count
    plus a contract violation would still read as 'we drove 50 turns'."""
    bad = _fc_turns(3)
    for r in bad:
        del r["security"]
    rep = _anchor(_fc_turns(47) + bad, 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 47
    assert et["matched"] is False
    assert rep["verdict"]["exit_code"] == 2


def test_mixed_candidate_shas_hold_even_at_the_right_count():
    """50 turns across two builds is not a measurement of either build."""
    a = _fc_turns(25, candidate_sha="aaaaaaa")
    b = _fc_turns(25, candidate_sha="bbbbbbb")
    rep = _anchor(a + b, 50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 50
    assert len(et["candidate_shas"]) == 2
    assert et["matched"] is False
    assert rep["verdict"]["exit_code"] == 2


def test_zero_tolerance_outranks_a_count_mismatch():
    """Both wrong at once. The breach is the finding that matters: exiting 2 would
    report 'telemetry incomplete' about a run that committed a real violation, and
    a rollback driver branching on the code would take the wrong branch."""
    recs = _fc_turns(30)
    recs[0]["security"]["tainted_write_executed_count"] = 1
    rep = _anchor(recs, 50)
    assert rep["verdict"]["expected_turns"]["matched"] is False
    assert rep["verdict"]["decision"] == "CANARY-BLOCK"
    assert rep["verdict"]["exit_code"] == 3


def test_dsml_leak_outranks_a_count_mismatch():
    recs = _fc_turns(30)
    recs[0]["dsml_leak"] = 1
    rep = _anchor(recs, 50)
    assert rep["verdict"]["exit_code"] == 3
    assert any("response boundary" in r
               for r in rep["verdict"]["zero_tolerance"]["reasons"])


def test_provider_400_outranks_a_count_mismatch():
    recs = _fc_turns(30)
    recs[0]["provider_schema_400_count"] = 1
    rep = _anchor(recs, 50)
    assert rep["verdict"]["exit_code"] == 3


def test_unparseable_lines_hold_even_when_the_count_matches():
    """A truncated line is a record we lost, and the one we lost could be the one
    carrying a violation. The remaining 50 matching is not reassurance."""
    rep = cr.build_report(_fc_turns(50), expect_turns=50, skipped=2)
    assert rep["verdict"]["expected_turns"]["matched"] is True
    assert rep["verdict"]["decision"] == "INSTRUMENTATION-HOLD"
    assert rep["verdict"]["exit_code"] == 2


def test_window_excludes_out_of_range_turns_from_the_anchor():
    """The anchor counts the SELECTED window, so an earlier run's turns sitting in
    the same file cannot be borrowed to reach 50."""
    old_ts = NOW - timedelta(hours=48)
    rep = cr.build_report(_fc_turns(40) + _fc_turns(10, ts=old_ts),
                          window_hours=1.0, expect_turns=50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 40
    assert rep["verdict"]["exit_code"] == 2


def test_report_prints_filters_expected_observed_and_unique_ids():
    """The operator reading this has to be able to see WHY a count was rejected
    without reading the source."""
    text = cr.render_text(_anchor(_fc_turns(49), 50))
    assert "[EXTERNAL ANCHOR]" in text
    assert "expected turns      : 50" in text
    assert "observed eligible   : 49" in text
    assert "unique request_ids  : 49" in text
    assert "agent_arch=fc_loop" in text
    assert "MISMATCH:" in text


def test_anchor_absent_when_flag_not_passed():
    """Default behaviour is unchanged: no flag, no anchor, no new hold."""
    rep = cr.build_report(_fc_turns(50))
    assert rep["verdict"]["expected_turns"] is None
    assert rep["verdict"]["exit_code"] == 0


def test_cli_flag_is_wired_and_returns_two_on_mismatch(tmp_path):
    p = tmp_path / "canary.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _fc_turns(49)) + "\n",
                 encoding="utf-8")
    assert cr.run(["-i", str(p), "--expect-turns", "50", "--quiet"]) == 2
    assert cr.run(["-i", str(p), "--expect-turns", "49", "--quiet"]) == 0


def test_cli_rejects_a_negative_expectation(tmp_path, capsys):
    p = tmp_path / "canary.jsonl"
    p.write_text(json.dumps(_rec("fc", 1000)) + "\n", encoding="utf-8")
    assert cr.run(["-i", str(p), "--expect-turns", "-1", "--quiet"]) == 1
