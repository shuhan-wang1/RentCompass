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


def _rec(arch, latency_ms, *, ts=None, soft=False, partial=False, tbt=False,
         audit="clean", conv="c", **extra):
    r = {
        "event": "canary.turn",
        "agent_arch": arch,
        "candidate_sha": "7db03e7",
        "strict": arch == "fc",
        "request_id": "r",
        "conversation_id": conv,
        "user_id": "u",
        "soft_wrapped": soft,
        "partial": partial,
        "tool_budget_timeout": tbt,
        "security_audit": audit,
        "turn_latency_ms": latency_ms,
        "llm_calls": 2,
        "tool_batches": 1,
    }
    if ts is not None:
        r["ts"] = ts if isinstance(ts, str) else ts.isoformat()
    r.update(extra)
    return r


# --------------------------------------------------------------------------- #
# 1) green fc vs legacy -> exit 0                                             #
# --------------------------------------------------------------------------- #

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
    # forbidden-read / no-evidence / 5xx are not in prod telemetry -> noted, not breached.
    joined = " ".join(v["stage_pause"]["notes"])
    assert "requires eval sweep" in joined
    assert "not instrumented" in joined


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
                        dsml_leak=True))
    records.append(_rec("fc", 5000.0, conv="b4", audit="clean", **{"400_count": 3}))
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
