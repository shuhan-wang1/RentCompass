"""Offline unit tests for the eval-protocol redesign (cache snapshots, cold protocol,
nearest-rank percentile, cold-resilience grading, budget-timeout collector events).

FAKES ONLY — no network, no paid calls, no app graph. Covers:
  * cache_snapshot make/restore roundtrip + sha256 verify + tamper detection;
  * nearest-rank percentile correctness, tied to the final3 numbers (98 samples, 7 over 30s
    -> p95 is the ~36.5s member, NOT an interpolated boundary);
  * cold-cache namespace per-repeat isolation (monkeypatched set_cache_path records calls);
  * warm-cache restore points the app at a verified copy of the snapshot;
  * timeout_claimed_no_listings pattern matcher (positive + honest-phrasing negative);
  * cold_resilience_block gate wiring;
  * collector.record_tool_budget_timeout / record_turn_soft_wrap event shape.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

import evaluation.run_benchmark as rb
from evaluation import cache_snapshot
from evaluation.metrics import collector


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_cache_db(path: Path, rows: int) -> None:
    """Build a minimal listing-cache sqlite file with ``rows`` rows in ``listings``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS listings ("
                   "key TEXT PRIMARY KEY, rows TEXT NOT NULL, fetched REAL NOT NULL)")
        for i in range(rows):
            db.execute("INSERT INTO listings(key, rows, fetched) VALUES (?,?,?)",
                       (f"k{i}", json.dumps([{"URL": f"u{i}"}]), 1234.0 + i))
        db.commit()


def _mk_run(case_id="C1", category="cold_resilience", **kw) -> rb.RunResult:
    rr = rb.RunResult(case_id=case_id, category=category, config="cfg", mode="live",
                      run_id=f"{case_id}#r1#cfg", repeat=kw.pop("repeat", 1))
    for k, v in kw.items():
        setattr(rr, k, v)
    return rr


# --------------------------------------------------------------------------- #
# 1) snapshot make / restore roundtrip + sha256
# --------------------------------------------------------------------------- #
def test_make_snapshot_writes_sidecars_and_roundtrips(tmp_path):
    src = tmp_path / "runtime" / "listing_cache.sqlite3"
    _make_cache_db(src, rows=5)
    out = tmp_path / "snapshots" / "warm_v1.sqlite3"

    meta = cache_snapshot.make_snapshot(src, out)
    sha_side, meta_side = cache_snapshot.sidecar_paths(out)

    assert out.exists()
    assert sha_side.exists() and meta_side.exists()
    # sidecar digest matches the actual file digest
    assert sha_side.read_text(encoding="utf-8").strip() == cache_snapshot.sha256_of(out)
    assert meta["row_count"] == 5
    assert meta["sha256"] == cache_snapshot.sha256_of(out)
    meta_json = json.loads(meta_side.read_text(encoding="utf-8"))
    assert meta_json["row_count"] == 5 and meta_json["source_path"] == str(src)

    # restore into a fresh dest and confirm byte-identical content
    dest = tmp_path / "restored" / "listing_cache.sqlite3"
    returned = cache_snapshot.restore_snapshot(out, dest)
    assert returned == dest and dest.exists()
    assert cache_snapshot.sha256_of(dest) == meta["sha256"]
    with sqlite3.connect(str(dest)) as db:
        assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 5


def test_restore_snapshot_detects_tamper(tmp_path):
    src = tmp_path / "listing_cache.sqlite3"
    _make_cache_db(src, rows=2)
    out = tmp_path / "snap.sqlite3"
    cache_snapshot.make_snapshot(src, out)
    # Corrupt the snapshot AFTER the sidecar was written -> digest mismatch.
    out.write_bytes(out.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        cache_snapshot.restore_snapshot(out, tmp_path / "dest.sqlite3")


def test_restore_snapshot_missing_sidecar_is_hard_error(tmp_path):
    src = tmp_path / "listing_cache.sqlite3"
    _make_cache_db(src, rows=1)
    out = tmp_path / "snap.sqlite3"
    cache_snapshot.make_snapshot(src, out)
    cache_snapshot.sidecar_paths(out)[0].unlink()  # remove .sha256
    with pytest.raises(FileNotFoundError, match="integrity sidecar"):
        cache_snapshot.restore_snapshot(out, tmp_path / "dest.sqlite3")


# --------------------------------------------------------------------------- #
# 2) nearest-rank percentile (final3 numbers)
# --------------------------------------------------------------------------- #
def test_percentile_nearest_rank_final3():
    # 98 samples: 91 fast (5000ms) + 7 slow over 30s. p95 (nearest-rank) index =
    # ceil(0.95*98)-1 = 93 -> the 3rd-slowest of the 7-strong tail = 36500ms member.
    slow = [31000, 33000, 36500, 38000, 40000, 42000, 45000]
    values = [5000.0] * 91 + [float(s) for s in slow]
    assert len(values) == 98
    assert sum(1 for v in values if v > 30000) == 7

    p95 = rb._percentile(values, 0.95)
    assert p95 == 36500  # an ACTUAL sample at rank ceil(.95*98)=94

    # A linear-interpolation percentile would instead land at 33525 (between idx92/93);
    # nearest-rank must NOT.
    assert p95 != pytest.approx(33525)

    # p50 nearest-rank: index ceil(0.5*98)-1 = 48 -> still in the fast block.
    assert rb._percentile(values, 0.5) == 5000

    # explicit index formula check
    assert rb._percentile(values, 0.95) == sorted(values)[math.ceil(0.95 * 98) - 1]


def test_percentile_edges():
    assert rb._percentile([], 0.95) is None
    assert rb._percentile([7.0], 0.95) == 7.0
    assert rb._percentile([1.0, 2.0], 0.5) == 1.0   # ceil(1.0)-1 = 0
    assert rb._percentile([1.0, 2.0], 1.0) == 2.0


def test_slo_block_reports_over_30s_and_method():
    runs = [_mk_run(f"S{i}", turn_latency_ms=2000.0) for i in range(18)]
    runs += [_mk_run("Sslow1", turn_latency_ms=45000.0),
             _mk_run("Sslow2", turn_latency_ms=45000.0)]
    slo = rb.slo_block(runs)
    assert slo["method"] == "nearest_rank"
    assert slo["over_30s_count"] == 2
    assert slo["over_30s_rate"] == pytest.approx(2 / 20)
    assert slo["p95_ms"] > 30000 and slo["p95_ok"] is False


# --------------------------------------------------------------------------- #
# 3) cold namespace per-repeat isolation + warm restore
# --------------------------------------------------------------------------- #
def _bare_runner(cache_protocol, cache_dir, recorder):
    """A CaseRunner with ONLY the cache attributes wired (bypasses the heavy __init__
    app-graph imports); set_cache_path is stubbed via the resolved-fn cache."""
    runner = rb.CaseRunner.__new__(rb.CaseRunner)
    runner.cache_protocol = cache_protocol
    runner._cache_dir = Path(cache_dir)
    runner._set_cache_path_fn = recorder
    return runner


def test_cold_cache_fresh_namespace_per_repeat(tmp_path):
    calls = []
    runner = _bare_runner({"mode": "cold"}, tmp_path / "cold_cache",
                          lambda p: calls.append(Path(p)))
    for repeat in (1, 2, 3):
        runner._prepare_cache(f"CR1#r{repeat}#cfg")
    # a distinct, never-reused path per repeat (the isolation mechanism)
    assert len(calls) == 3
    assert len(set(calls)) == 3
    assert all(p.parent == tmp_path / "cold_cache" for p in calls)


def test_warm_cache_restores_verified_copy_per_repeat(tmp_path):
    src = tmp_path / "listing_cache.sqlite3"
    _make_cache_db(src, rows=4)
    snap = tmp_path / "snapshots" / "warm.sqlite3"
    meta = cache_snapshot.make_snapshot(src, snap)

    calls = []
    runner = _bare_runner({"mode": "warm", "snapshot_path": str(snap)},
                          tmp_path / "warm_cache", lambda p: calls.append(Path(p)))
    for repeat in (1, 2):
        runner._prepare_cache(f"H2#r{repeat}#cfg")

    assert len(calls) == 2 and len(set(calls)) == 2
    for p in calls:
        assert p.exists()
        assert cache_snapshot.sha256_of(p) == meta["sha256"]  # verified restore


def test_prepare_cache_none_is_noop(tmp_path):
    runner = _bare_runner({"mode": "none"}, tmp_path / "x",
                          lambda p: pytest.fail("set_cache_path must not be called"))
    runner._prepare_cache("A1#r1#cfg")  # no raise, no call


def test_resolve_set_cache_path_hard_fails_when_missing(monkeypatch):
    """If the contract API is absent, a requested protocol must raise, never silently
    fall back to the shared default cache."""
    import sys
    import types
    fake = types.ModuleType("core.scraping.on_demand")  # no set_cache_path attribute
    monkeypatch.setitem(sys.modules, "core.scraping.on_demand", fake)
    runner = rb.CaseRunner.__new__(rb.CaseRunner)
    runner._set_cache_path_fn = None
    with pytest.raises(RuntimeError, match="set_cache_path is unavailable"):
        runner._resolve_set_cache_path()


# --------------------------------------------------------------------------- #
# 4) timeout_claimed_no_listings matcher + cold-resilience block
# --------------------------------------------------------------------------- #
def test_timeout_claimed_no_listings_positive_zh_and_en():
    zh = _mk_run(tools_timed_out=["search_properties"],
                 final_answer="很抱歉，没有找到房源。")
    en = _mk_run(partial_tool_result=True,
                 final_answer="Unfortunately, no properties found in Camden.")
    assert rb.timeout_claimed_no_listings(zh) is True
    assert rb.timeout_claimed_no_listings(en) is True


def test_timeout_claimed_no_listings_honest_phrasing_negative():
    # Honest partial-result phrasing must NOT count as a violation.
    zh_honest = _mk_run(tools_timed_out=["search_properties"],
                        final_answer="搜索超时，先给出部分结果，这是我找到的部分房源：1) ...")
    en_honest = _mk_run(partial_tool_result=True,
                        final_answer="The search timed out; here is what I have so far.")
    assert rb.timeout_claimed_no_listings(zh_honest) is False
    assert rb.timeout_claimed_no_listings(en_honest) is False


def test_timeout_claimed_no_listings_requires_a_timeout():
    # A no-listings claim WITHOUT any timeout/partial is not this violation.
    clean = _mk_run(tools_timed_out=[], partial_tool_result=False,
                    final_answer="no properties found")
    assert rb.timeout_claimed_no_listings(clean) is False


def test_cold_resilience_block_gate():
    good = _mk_run("CR1", passed=True, turn_latency_ms=5000.0)
    # over-SLO run fails the gate
    slow = _mk_run("CR2", passed=True, turn_latency_ms=45000.0)
    block = rb.cold_resilience_block([good, slow])
    assert block["applicable"] is True
    assert block["gate_passed"] is False
    kinds = {v["kind"] for v in block["violations"]}
    assert "over_slo_run" in kinds
    assert any(o["case_id"] == "CR2" for o in block["over_slo_runs"])

    clean = rb.cold_resilience_block([good])
    assert clean["gate_passed"] is True and clean["per_case"] == {"CR1": "1/1"}


def test_cold_resilience_block_not_applicable_without_cr_cases():
    block = rb.cold_resilience_block([_mk_run("A1", category="A_retrieval", passed=True,
                                              turn_latency_ms=1000.0)])
    assert block["applicable"] is False and block["gate_passed"] is True


# --------------------------------------------------------------------------- #
# 5) cache_stats scan
# --------------------------------------------------------------------------- #
def test_scan_cache_and_partial_aggregates():
    artifacts = [
        {"tool": "search_properties", "cache_stats": {"hits": 3, "misses": 1}},
        {"tool": "search_properties", "data": {"cache_stats": {"hits": 2, "misses": 4},
                                               "partial": True}},
    ]
    evidence = [{"tool": "x", "data": {"cache_stats": {"hits": 1, "misses": 0}}}]
    hits, misses, partial = rb._scan_cache_and_partial(artifacts, evidence)
    assert (hits, misses, partial) == (6, 5, True)

    hits, misses, partial = rb._scan_cache_and_partial([], [])
    assert (hits, misses, partial) == (0, 0, False)


# --------------------------------------------------------------------------- #
# 6) collector budget-timeout / soft-wrap event shape
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _eval_off(monkeypatch):
    monkeypatch.delenv("RENTCOMPASS_EVAL", raising=False)
    monkeypatch.delenv("RENTCOMPASS_EVAL_LOG", raising=False)
    collector._refresh_env_flag()
    collector.reset_sink()
    yield
    collector._refresh_env_flag()
    collector.reset_sink()


def _read_events(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_record_tool_budget_timeout_event_shape(tmp_path):
    log = tmp_path / "events.jsonl"
    with collector.capture_run("R1", "C1", "cfg", log_path=str(log)):
        collector.record_tool_budget_timeout(
            tool="search_properties", phase="batch", budget_s=20.0,
            elapsed_ms=21500.0, outcome="abandoned")
    events = _read_events(log)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "tool_budget_timeout"
    assert ev["tool"] == "search_properties" and ev["phase"] == "batch"
    assert ev["budget_s"] == 20.0 and ev["elapsed_ms"] == 21500.0
    assert ev["outcome"] == "abandoned"
    # run-context tags present
    assert ev["run_id"] == "R1" and ev["case_id"] == "C1" and ev["config"] == "cfg"


def test_record_turn_soft_wrap_event_shape(tmp_path):
    log = tmp_path / "events.jsonl"
    with collector.capture_run("R2", "C2", "cfg", log_path=str(log)):
        collector.record_turn_soft_wrap(elapsed_ms=28000.0, llm_calls=4, tool_batches=3)
    events = _read_events(log)
    assert len(events) == 1 and events[0]["type"] == "turn_soft_wrap"
    assert events[0]["elapsed_ms"] == 28000.0
    assert events[0]["llm_calls"] == 4 and events[0]["tool_batches"] == 3


def test_budget_timeout_and_soft_wrap_noop_when_inactive(tmp_path):
    log = tmp_path / "events.jsonl"
    # No capture_run, env flag off -> no-op, no file written.
    collector.record_tool_budget_timeout(tool="t", phase="p", budget_s=1.0,
                                         elapsed_ms=2.0, outcome="killed")
    collector.record_turn_soft_wrap(elapsed_ms=1.0, llm_calls=0, tool_batches=0)
    assert not log.exists()


# --------------------------------------------------------------------------- #
# 8) integrity check + provenance + pinned warm-up protocol (post-final4 wiring fixes)
# --------------------------------------------------------------------------- #
def test_integrity_check_passes_valid_and_rejects_corrupt(tmp_path):
    good = tmp_path / "good.sqlite3"
    _make_cache_db(good, rows=2)
    cache_snapshot.integrity_check(good)  # must not raise

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"SQLite format 3" + bytes([0]) + bytes([255]) * 4096)
    with pytest.raises((ValueError, sqlite3.Error)):
        cache_snapshot.integrity_check(corrupt)


def test_make_snapshot_rejects_corrupt_source(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"SQLite format 3" + bytes([0]) + bytes([255]) * 4096)
    with pytest.raises((ValueError, sqlite3.Error)):
        cache_snapshot.make_snapshot(corrupt, tmp_path / "snap.sqlite3")


def test_make_snapshot_records_provenance(tmp_path):
    src = tmp_path / "cache.sqlite3"
    _make_cache_db(src, rows=1)
    out = tmp_path / "snap.sqlite3"
    prov = {"git_commit": "abc1234", "git_dirty": False,
            "warmup_commands": ["cmd1"], "budget_env": {"FC_X": "1"}}
    meta = cache_snapshot.make_snapshot(src, out, provenance=prov)
    assert meta["provenance"] == prov
    _, meta_side = cache_snapshot.sidecar_paths(out)
    on_disk = json.loads(meta_side.read_text(encoding="utf-8"))
    assert on_disk["provenance"]["git_commit"] == "abc1234"


def test_cache_protocol_pinned_mode(tmp_path):
    import argparse
    args = argparse.Namespace(cache_snapshot=None, cold_cache=False,
                              cache_path=str(tmp_path / "warmup" / "shared.sqlite3"),
                              cache_ttl_hours="8760")
    block = rb._build_cache_protocol(args)
    assert block["mode"] == "pinned"
    assert block["pinned_path"].endswith("shared.sqlite3")
    assert block["restored_per_repeat"] is False
    assert Path(block["pinned_path"]).parent.exists()


def test_prepare_cache_pinned_uses_shared_path(tmp_path):
    calls = []
    runner = rb.CaseRunner.__new__(rb.CaseRunner)
    runner.cache_protocol = {"mode": "pinned",
                             "pinned_path": str(tmp_path / "shared.sqlite3")}
    runner._set_cache_path_fn = lambda p: calls.append(Path(p))
    runner._cache_dir = tmp_path / "unused"
    runner._prepare_cache("CR1#r1#cfg")
    runner._prepare_cache("CR1#r2#cfg")
    # Same shared path every run - never a per-run namespace.
    assert calls == [tmp_path / "shared.sqlite3", tmp_path / "shared.sqlite3"]
