"""Offline unit tests for the Phase-2.1 eval fidelity + profiling additions
(evaluation/run_benchmark.py + evaluation/results_package.py).

Covers, with NO graph build / model / network:

* accumulated_search_criteria reconstructed from a zh+en conversation_history via the
  PRODUCTION extractors (sticky budget/area/room_type — H2 fidelity);
* last_results reconstructed WITH per-listing price + available_from, and the
  single-vs-multi listing property_address rule (H7 focus vs H8 result set — H8 fidelity);
* the per-span latency profile: repeated agent/execute_tools spans preserved IN ORDER
  with correct per-node aggregates, cross-case node-kind aggregate, top-N slowest block;
* the reproducible results package: per_case.csv columns/values + manifest.json fields,
  with the git commit STUBBED (no git dependency).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from evaluation import run_benchmark as rb
from evaluation import results_package as rp


# --------------------------------------------------------------------------- #
# 1) accumulated_search_criteria from history (production extractors)
# --------------------------------------------------------------------------- #
def test_accumulated_budget_from_zh_history():
    hist = [
        {"role": "user", "content": "帮我在 Islington 找房，月预算不超过1500镑。"},
        {"role": "assistant", "content": "好的，已按 Islington、月租≤£1500 搜索。"},
    ]
    acc = rb.accumulated_criteria_from_history(hist)
    assert acc is not None
    assert acc["max_budget"] == 1500
    assert acc["budget_period"] == "month"
    # Default template keys survive so downstream readers never KeyError.
    assert "property_features" in acc and "soft_preferences" in acc


def test_accumulated_budget_from_en_history_and_room_type():
    hist = [
        {"role": "user", "content": "Looking for a studio in Manchester, budget £2000 a month."},
    ]
    acc = rb.accumulated_criteria_from_history(hist)
    assert acc is not None
    assert acc["max_budget"] == 2000
    assert acc["room_type"] == "studio"
    # _extract_area returns the canonical English place name.
    assert acc.get("area") == "Manchester"


def test_accumulated_weekly_budget_keeps_amount_and_period():
    # Faithful to production: the amount + period are stored SEPARATELY and both flow to
    # the search tool (which applies the period) — the harness must not silently normalise.
    hist = [{"role": "user", "content": "budget is £400 pw please"}]
    acc = rb.accumulated_criteria_from_history(hist)
    assert acc is not None
    assert acc["max_budget"] == 400
    assert acc["budget_period"] == "week"


def test_accumulated_none_when_no_sticky_criteria():
    hist = [{"role": "user", "content": "你好"}]
    assert rb.accumulated_criteria_from_history(hist) is None
    assert rb.accumulated_criteria_from_history([]) is None


# --------------------------------------------------------------------------- #
# 2) last_results WITH prices + single/multi property_address rule
# --------------------------------------------------------------------------- #
def test_last_results_multi_listing_carries_prices_no_focus():
    # H8-shaped: a RESULT SET of 3 priced listings in one assistant turn.
    hist = [
        {"role": "user", "content": "帮我在 Camden 找 studio。"},
        {"role": "assistant", "content": (
            "为你找到 3 套：1) Camden Lock Studio, NW1 8AF, £1450/月；"
            "2) Kentish Town Studio, NW5 2AB, £1290/月；"
            "3) Camden High St Studio, NW1 7JN, £1600/月。")},
    ]
    ctx = rb.referent_context_from_history(hist)
    lr = ctx["last_results"]
    assert len(lr) == 3
    # Each listing carries its price string AND the derived monthly figure.
    prices = [r.get("price") for r in lr]
    assert any("1290" in (p or "") for p in prices)
    cheapest = min(lr, key=lambda r: r["monthly_price"])
    assert cheapest["monthly_price"] == 1290
    assert cheapest["name"] == "Kentish Town Studio"
    assert "NW5 2AB" in cheapest["address"]
    # A multi-listing RESULT SET must NOT pin a single focused property (else H8 would
    # route to property-focus instead of the comparative-followup path).
    assert "property_address" not in ctx


def test_last_results_single_listing_sets_property_address():
    # H7-shaped: a single FOCUSED listing ("你正在查看：…").
    hist = [
        {"role": "assistant",
         "content": "你正在查看：Scape Bloomsbury, WC1H 0AQ, £1500/月，studio。"},
    ]
    ctx = rb.referent_context_from_history(hist)
    assert len(ctx["last_results"]) == 1
    rec = ctx["last_results"][0]
    assert rec["name"] == "Scape Bloomsbury"
    assert "WC1H 0AQ" in rec["address"]
    assert rec["monthly_price"] == 1500
    # Single listing => focused property is exposed.
    assert ctx["property_address"] == rec["address"]


def test_last_results_available_from_when_present():
    hist = [{"role": "assistant",
             "content": "Riverside Studio, E1 6AN, £1400/month, available from 1 September."}]
    ctx = rb.referent_context_from_history(hist)
    rec = ctx["last_results"][0]
    assert rec.get("available_from", "").startswith("1 September")


def test_referent_empty_for_non_listing_turn():
    # A discussion turn (H6) names no priced/addressed listing -> no referent state.
    hist = [{"role": "assistant",
             "content": "两个都离 UCL 很近。你更看重安静还是热闹？"}]
    assert rb.referent_context_from_history(hist) == {}


# --------------------------------------------------------------------------- #
# 2b) ec["history"] SessionStore-shape reconstruction (H6)
# --------------------------------------------------------------------------- #
def test_history_snapshot_pairs_user_assistant():
    hist = [
        {"role": "user", "content": "帮我看看 Shoreditch 和 Hackney。"},
        {"role": "assistant", "content": "两个区都不错，Shoreditch 更热闹。"},
        {"role": "user", "content": "那个区域安全吗？"},
        {"role": "assistant", "content": "Hackney 的治安…"},
    ]
    snap = rb.history_snapshot_from_history(hist)
    # SessionStore shape: [{"user":..,"assistant":..}] — one entry per exchange.
    assert snap == [
        {"user": "帮我看看 Shoreditch 和 Hackney。", "assistant": "两个区都不错，Shoreditch 更热闹。"},
        {"user": "那个区域安全吗？", "assistant": "Hackney 的治安…"},
    ]


def test_history_snapshot_trailing_user_and_leading_assistant():
    # A leading assistant pairs with empty user; a trailing user keeps empty assistant.
    hist = [
        {"role": "assistant", "content": "你好，我是 Alex。"},
        {"role": "user", "content": "预算 £1500。"},
    ]
    snap = rb.history_snapshot_from_history(hist)
    assert snap == [
        {"user": "", "assistant": "你好，我是 Alex。"},
        {"user": "预算 £1500。", "assistant": ""},
    ]
    assert rb.history_snapshot_from_history([]) == []


def test_history_snapshot_truncates_assistant_to_500():
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "x" * 900}]
    snap = rb.history_snapshot_from_history(hist)
    assert len(snap[0]["assistant"]) == 500  # mirrors app.py's [:500]


# --------------------------------------------------------------------------- #
# 2c) three-way tool-call split: executed / denied / timed_out / requested (H13)
# --------------------------------------------------------------------------- #
def test_classify_fc_denied_and_timed_out_split():
    # Artifacts as Q1 emits them: a denied write, a timed-out call, a normal executed tool.
    artifacts = [
        {"turn": 0, "tool": "search_properties", "success": True, "params_digest": "d1"},
        {"turn": 0, "tool": "remember", "success": False, "denied": True,
         "params_digest": "d2", "error": "write blocked: not authorized"},
        {"turn": 1, "tool": "check_safety", "success": False, "timed_out": True,
         "params_digest": "d3", "error": "check_safety timed out after 8s"},
    ]
    ex, den, to, req, detail = rb._classify_tool_calls(
        arch="fc_loop", artifacts=artifacts, tool_events=[], evidence=[])
    assert ex == ["search_properties"]           # executed = NOT denied and NOT timed_out
    assert den == ["remember"]
    assert to == ["check_safety"]
    assert req == ["search_properties", "remember", "check_safety"]
    assert detail and detail[0]["tool"] == "remember" and detail[0]["digest"] == "d2"


def test_classify_fc_denied_via_error_without_flag():
    # Robust to a deny surfaced only through the error text (no explicit denied flag).
    artifacts = [{"turn": 0, "tool": "remember", "success": False,
                  "error": "write blocked: confirmation is required"}]
    ex, den, to, req, _ = rb._classify_tool_calls(
        arch="fc_loop", artifacts=artifacts, tool_events=[], evidence=[])
    assert ex == [] and den == ["remember"] and req == ["remember"]


def test_classify_legacy_from_events_and_evidence():
    tool_events = [
        {"tool": "search_properties", "timeout": False, "args_hash": "a1"},
        {"tool": "get_weather", "timeout": True, "args_hash": "a2"},
        {"tool": "remember", "timeout": False, "args_hash": "a3"},
    ]
    evidence = [
        {"tool": "remember", "success": False, "error": "PermissionError: write not authorized"},
    ]
    ex, den, to, req, detail = rb._classify_tool_calls(
        arch="legacy", artifacts=[], tool_events=tool_events, evidence=evidence)
    assert ex == ["search_properties"]
    assert to == ["get_weather"]
    assert den == ["remember"]
    assert req == ["search_properties", "get_weather", "remember"]
    assert detail[0]["tool"] == "remember"


def test_extract_tool_trace_skips_denied_and_timed_out():
    from evaluation.metrics import graders
    artifacts = [
        {"turn": 0, "tool": "search_properties", "params_digest": "d1"},
        {"turn": 0, "tool": "remember", "denied": True, "params_digest": "d2"},
        {"turn": 1, "tool": "check_safety", "timed_out": True, "params_digest": "d3"},
    ]
    # The executed route trace excludes the denied write and the timed-out call.
    assert graders.extract_tool_trace(artifacts) == [["search_properties"]]


def test_security_audit_aggregates_denied_writes():
    r1 = _run("H13", 10.0, [])
    r1.tools_denied = ["remember"]
    r1.denied_tool_detail = [{"tool": "remember", "digest": "d2", "error": "write blocked"}]
    r2 = _run("H2", 10.0, [])   # no denials
    audit = rb._security_audit([r1, r2])
    assert audit["cases_with_denied_writes"] == 1
    assert audit["denied_by_tool"] == {"remember": 1}
    assert audit["denied_cases"][0]["case_id"] == "H13"
    assert audit["denied_cases"][0]["detail"][0]["digest"] == "d2"


# --------------------------------------------------------------------------- #
# 3) per-span latency profile
# --------------------------------------------------------------------------- #
def _span_event(node, ms, ts):
    return {"type": "node_span", "node": node, "latency_ms": ms, "ts_monotonic": ts}


def test_build_node_spans_preserves_repeated_spans_in_order():
    # The fc loop hits agent/execute_tools twice each; events arrive interleaved with a
    # non-span event and out of ts order to prove sorting + preservation.
    events = [
        _span_event("agent", 10.0, 1.0),
        _span_event("execute_tools", 20.0, 2.0),
        {"type": "llm_call", "ts_monotonic": 2.5},
        _span_event("agent", 12.0, 3.0),
        _span_event("execute_tools", 22.0, 4.0),
        _span_event("reflect", 5.0, 5.0),
    ]
    spans = rb.build_node_spans(events)
    assert [s["node"] for s in spans] == [
        "agent", "execute_tools", "agent", "execute_tools", "reflect"]
    assert [s["seq"] for s in spans] == [0, 1, 2, 3, 4]
    # Aggregate: agent appears twice (10+12, max 12), execute_tools twice (20+22, max 22).
    agg = rb._aggregate_spans(spans)
    assert agg["agent"] == {"sum_ms": 22.0, "max_ms": 12.0, "count": 2}
    assert agg["execute_tools"] == {"sum_ms": 42.0, "max_ms": 22.0, "count": 2}
    assert agg["reflect"]["count"] == 1


def test_build_node_spans_out_of_order_ts_sorted():
    events = [_span_event("b", 2.0, 9.0), _span_event("a", 1.0, 1.0)]
    spans = rb.build_node_spans(events)
    assert [s["node"] for s in spans] == ["a", "b"]
    assert [s["seq"] for s in spans] == [0, 1]


def _run(case_id, latency, spans, **kw):
    rr = rb.RunResult(case_id=case_id, category=kw.get("category", "H"),
                      config="routed_models", mode="offline",
                      run_id=f"{case_id}#r1", repeat=1)
    rr.turn_latency_ms = latency
    rr.node_spans = spans
    rr.llm_calls = kw.get("llm_calls", len([s for s in spans if s["node"] == "agent"]))
    rr.tool_batches = kw.get("tool_batches", 0)
    rr.passed = kw.get("passed", True)
    rr.route_matched = kw.get("route_matched", True)
    rr.hard_gate = kw.get("hard_gate", True)
    rr.cost_usd = kw.get("cost_usd", 0.0)
    rr.verdict = kw.get("verdict", {"constraints": [], "forbidden_tool_violations": []})
    return rr


def test_aggregate_node_kinds_across_cases():
    r1 = _run("A", 100.0, [{"node": "agent", "ms": 10.0, "seq": 0},
                           {"node": "agent", "ms": 20.0, "seq": 1}])
    r2 = _run("B", 50.0, [{"node": "agent", "ms": 5.0, "seq": 0},
                          {"node": "reflect", "ms": 7.0, "seq": 1}])
    agg = rb.aggregate_node_kinds([r1, r2])
    assert agg["agent"]["count"] == 3
    assert agg["agent"]["sum_ms"] == 35.0
    assert agg["agent"]["max_ms"] == 20.0
    assert agg["reflect"]["count"] == 1


def test_top_slowest_cases_shape_and_order():
    runs = [_run(f"C{i}", float(i * 10), [{"node": "agent", "ms": float(i), "seq": 0}])
            for i in range(1, 15)]
    top = rb.top_slowest_cases(runs, k=10)
    assert len(top) == 10
    # Descending by latency: slowest first.
    assert top[0]["case_id"] == "C14"
    assert top[0]["latency_ms"] == 140.0
    assert top[-1]["case_id"] == "C5"
    # Each block carries the full span timeline.
    for block in top:
        assert set(block) >= {"run_id", "case_id", "latency_ms", "llm_calls",
                              "tool_batches", "spans"}
        assert isinstance(block["spans"], list)


# --------------------------------------------------------------------------- #
# 4) reproducible results package (per_case.csv + manifest.json, git stubbed)
# --------------------------------------------------------------------------- #
def test_per_case_csv_columns_and_values(tmp_path):
    good = _run("H2", 42.0, [{"node": "agent", "ms": 10.0, "seq": 0}],
                tool_batches=1, cost_usd=0.0,
                verdict={"constraints": [{"type": "all_results_satisfy", "passed": True}],
                         "forbidden_tool_violations": []})
    good.tools_executed = ["search_properties"]
    good.tools_requested = ["search_properties"]
    bad = _run("H8", 99.0, [], passed=False, route_matched=False, tool_batches=0,
               verdict={"constraints": [{"type": "must_mention_value", "passed": False}],
                        "forbidden_tool_violations": ["search_properties"]})
    path = rp.write_per_case(tmp_path, [good, bad], arch="fc_loop")
    rows = list(__import__("csv").reader(path.open(encoding="utf-8")))
    assert rows[0] == rp.PER_CASE_COLUMNS
    assert rows[0] == ["case_id", "category", "arch", "repeat", "passed", "route_matched",
                       "hard_gate", "llm_calls", "tool_batches", "tools_executed",
                       "tools_denied", "tools_requested", "latency_ms",
                       "cost_usd", "cache_hit_rate", "budget_timeout_tools", "soft_wrapped",
                       "failed_constraints", "violation_kinds"]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["H2"][2] == "fc_loop"
    assert by_id["H2"][3] == "1"                     # repeat
    assert by_id["H2"][4] == "True"                  # passed
    assert by_id["H2"][9] == "search_properties"    # tools_executed
    assert by_id["H2"][10] == ""                     # tools_denied
    assert by_id["H2"][11] == "search_properties"   # tools_requested
    assert by_id["H2"][17] == ""  # no failed constraints
    # A failing case lists its failed constraint + the forbidden-tool use.
    assert "must_mention_value" in by_id["H8"][17]
    assert "forbidden:search_properties" in by_id["H8"][17]


def test_per_case_csv_denied_write_split(tmp_path):
    # H13-shaped: the model REQUESTED remember but the gate DENIED it — executed excludes
    # it, requested keeps it, denied surfaces it (auditable, not silently dropped).
    rr = _run("H13", 30.0, [{"node": "agent", "ms": 5.0, "seq": 0}], tool_batches=1)
    rr.tools_executed = ["search_properties"]
    rr.tools_denied = ["remember"]
    rr.tools_requested = ["search_properties", "remember"]
    rr.violation_kinds = []
    path = rp.write_per_case(tmp_path, [rr], arch="fc_loop")
    rows = list(__import__("csv").reader(path.open(encoding="utf-8")))
    row = rows[1]
    assert row[9] == "search_properties"          # executed: remember NOT here
    assert row[10] == "remember"                   # denied
    assert "remember" in row[11]                   # requested keeps it
    assert row[18] == ""                           # no zero-tolerance violation fired


def test_manifest_fields_with_stubbed_commit(tmp_path):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "H2"}\n', encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "node_span"}\n', encoding="utf-8")
    manifest = rp.write_manifest(
        tmp_path, argv=["python", "-m", "evaluation.run_benchmark", "--arch", "fc_loop"],
        arch="fc_loop", config="routed_models", timestamp="2026-07-19T00:00:00",
        case_file=case_file, events_log=events, mode="offline",
        git_commit=lambda: "deadbeef", git_dirty=lambda: False)
    assert manifest["git_commit"] == "deadbeef"
    # git_dirty (clean/dirty tree) is recorded, stubbable (no git dependency).
    assert manifest["git_dirty"] is False
    assert manifest["arch"] == "fc_loop"
    assert manifest["config"] == "routed_models"
    assert manifest["command"].endswith("--arch fc_loop")
    assert "AGENT_ARCH" in manifest["env"] and "DEEPSEEK_STRICT" in manifest["env"]
    # Digests are computed for both the case file and the event log.
    assert manifest["case_file"]["sha256"] == rp.sha256_of(case_file)
    assert manifest["events_log"]["sha256"] == rp.sha256_of(events)
    # It was actually written to disk.
    written = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert written["git_commit"] == "deadbeef"
    assert written["git_dirty"] is False


def test_manifest_records_gz_and_both_hashes(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "tool_call"}\n{"type": "llm_call"}\n', encoding="utf-8")
    gz = rp.write_events_gz(tmp_path, events)
    assert gz is not None and gz.exists() and gz.name == "events.jsonl.gz"
    # The gz round-trips back to the raw bytes (it is the real event stream).
    import gzip as _gz
    assert _gz.decompress(gz.read_bytes()) == events.read_bytes()
    manifest = rp.build_manifest(
        argv=["python", "x"], arch="fc_loop", config="routed_models", timestamp="t",
        case_file=events, events_log=events, git_commit="c", git_dirty=True, events_gz=gz)
    el = manifest["events_log"]
    # Raw AND gz SHA256 are both recorded; the gz is now shipped (committed True).
    assert el["sha256"] == rp.sha256_of(events)
    assert el["sha256_gz"] == rp.sha256_of(gz)
    assert el["sha256"] != el["sha256_gz"]
    assert el["committed"] is True
    assert el["gz_path"] == str(gz)
    assert manifest["git_dirty"] is True


def test_write_events_gz_missing_raw_returns_none(tmp_path):
    assert rp.write_events_gz(tmp_path, tmp_path / "nope.jsonl") is None


def test_sha256_of_missing_file_is_none(tmp_path):
    assert rp.sha256_of(tmp_path / "nope.jsonl") is None


def test_write_results_package_emits_all(tmp_path):
    run = _run("H2", 42.0, [{"node": "agent", "ms": 10.0, "seq": 0}])
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text("{}\n", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text("{}\n", encoding="utf-8")
    manifest = rp.write_results_package(
        tmp_path, [run], argv=["python", "x"], arch="legacy", config="routed_models",
        timestamp="t", case_file=case_file, events_log=events,
        git_commit=lambda: "cafebabe", git_dirty=lambda: False)
    assert (tmp_path / "per_case.csv").exists()
    assert (tmp_path / "manifest.json").exists()
    # The gzipped event stream travels WITH the package for verification.
    assert (tmp_path / "events.jsonl.gz").exists()
    assert manifest["events_log"]["committed"] is True
    assert manifest["events_log"]["sha256_gz"] == rp.sha256_of(tmp_path / "events.jsonl.gz")


# --------------------------------------------------------------------------- #
# 5) R3: SLO gate block + budget-breach detection from node_spans (env-honoring)
# --------------------------------------------------------------------------- #
def test_slo_block_math_and_limits():
    # Ten latencies 1000..10000ms: p50 ~= 5500ms (<= 6000), p95 ~= 9550ms (<= 30000).
    runs = [_run(f"S{i}", float(i * 1000), []) for i in range(1, 11)]
    slo = rb.slo_block(runs)
    assert slo["p50_limit"] == 6000 and slo["p95_limit"] == 30000
    assert slo["p50_ms"] <= 6000 and slo["p50_ok"] is True
    assert slo["p95_ms"] <= 30000 and slo["p95_ok"] is True
    assert slo["legacy_relative"] is None          # diagnostic only, unset by default


def test_slo_block_p95_breach_flags_not_ok():
    # A heavy tail (top decile ~45s) pushes p95 over 30000ms while p50 stays within limit.
    # 18 fast + 2 slow of 20: p95 index = 19*0.95 = 18.05 -> lands in the slow tail.
    runs = [_run(f"S{i}", 2000.0, []) for i in range(1, 19)]
    runs += [_run("Sslow1", 45000.0, []), _run("Sslow2", 45000.0, [])]
    slo = rb.slo_block(runs)
    assert slo["p50_ms"] <= 6000 and slo["p50_ok"] is True
    assert slo["p95_ms"] > 30000 and slo["p95_ok"] is False


def test_slo_block_p50_breach_flags_not_ok():
    runs = [_run(f"S{i}", 9000.0, []) for i in range(1, 6)]
    slo = rb.slo_block(runs)
    assert slo["p50_ms"] > 6000 and slo["p50_ok"] is False


def test_slo_block_legacy_relative_passthrough():
    runs = [_run("S1", 3000.0, [])]
    slo = rb.slo_block(runs, legacy_relative=0.61)
    assert slo["legacy_relative"] == 0.61          # kept as a diagnostic line


def test_budget_breach_default_limit(monkeypatch):
    # Default FC_BATCH_TOOL_BUDGET_S=20 + 2s grace = 22000ms. A 25000ms execute_tools span
    # breaches; an 18000ms one does not.
    monkeypatch.delenv("FC_BATCH_TOOL_BUDGET_S", raising=False)
    breach = _run("B", 25000.0,
                  [{"node": "execute_tools", "ms": 25000.0, "seq": 0}])
    clean = _run("C", 18000.0,
                 [{"node": "execute_tools", "ms": 18000.0, "seq": 0}])
    v = rb.zero_tolerance_violations([breach, clean])
    assert [e["kind"] for e in v] == ["budget_breach"]
    assert v[0]["case_id"] == "B" and "seq=0" in v[0]["detail"]


def test_budget_breach_honors_env_override(monkeypatch):
    # Raise the budget to 30s -> limit 32000ms; the same 25000ms span no longer breaches.
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "30")
    run = _run("B", 25000.0, [{"node": "execute_tools", "ms": 25000.0, "seq": 0}])
    assert rb.zero_tolerance_violations([run]) == []
    # Lower it to 5s -> limit 7000ms; a 9000ms span now breaches.
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "5")
    run2 = _run("B2", 9000.0, [{"node": "execute_tools", "ms": 9000.0, "seq": 1}])
    v = rb.zero_tolerance_violations([run2])
    assert [e["kind"] for e in v] == ["budget_breach"]


def test_budget_breach_only_execute_tools_spans(monkeypatch):
    # A slow AGENT span (model latency) is NOT a tool-budget breach — only execute_tools
    # spans carry the batch budget.
    monkeypatch.delenv("FC_BATCH_TOOL_BUDGET_S", raising=False)
    run = _run("A", 40000.0, [{"node": "agent", "ms": 40000.0, "seq": 0},
                              {"node": "execute_tools", "ms": 1000.0, "seq": 1}])
    assert rb.zero_tolerance_violations([run]) == []


def test_violation_kinds_stamped_deduped_and_sorted():
    # Two distinct kinds on one run surface as a sorted, de-duplicated kind list (the
    # per_case.csv column source).
    run = _run("X", 25000.0, [{"node": "execute_tools", "ms": 25000.0, "seq": 0}],
               verdict={"constraints": [
                   {"type": "no_fabricated_number", "passed": False, "detail": "d"}],
                   "forbidden_tool_violations": []})
    run.forbidden_executed = ["web_search"]
    import os as _os
    _os.environ.pop("FC_BATCH_TOOL_BUDGET_S", None)
    v = rb.zero_tolerance_violations([run])
    kinds = sorted({e["kind"] for e in v})
    assert kinds == ["budget_breach", "forbidden_tool_executed", "no_evidence_numbers"]
