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
    bad = _run("H8", 99.0, [], passed=False, route_matched=False, tool_batches=0,
               verdict={"constraints": [{"type": "must_mention_value", "passed": False}],
                        "forbidden_tool_violations": ["search_properties"]})
    path = rp.write_per_case(tmp_path, [good, bad], arch="fc_loop")
    rows = list(__import__("csv").reader(path.open(encoding="utf-8")))
    assert rows[0] == rp.PER_CASE_COLUMNS
    assert rows[0] == ["case_id", "category", "arch", "passed", "route_matched",
                       "hard_gate", "llm_calls", "tool_batches", "latency_ms",
                       "cost_usd", "failed_constraints"]
    by_id = {r[0]: r for r in rows[1:]}
    assert by_id["H2"][2] == "fc_loop"
    assert by_id["H2"][3] == "True"
    assert by_id["H2"][10] == ""  # no failed constraints
    # A failing case lists its failed constraint + the forbidden-tool use.
    assert "must_mention_value" in by_id["H8"][10]
    assert "forbidden:search_properties" in by_id["H8"][10]


def test_manifest_fields_with_stubbed_commit(tmp_path):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "H2"}\n', encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "node_span"}\n', encoding="utf-8")
    manifest = rp.write_manifest(
        tmp_path, argv=["python", "-m", "evaluation.run_benchmark", "--arch", "fc_loop"],
        arch="fc_loop", config="routed_models", timestamp="2026-07-19T00:00:00",
        case_file=case_file, events_log=events, mode="offline",
        git_commit=lambda: "deadbeef")
    assert manifest["git_commit"] == "deadbeef"
    assert manifest["arch"] == "fc_loop"
    assert manifest["config"] == "routed_models"
    assert manifest["command"].endswith("--arch fc_loop")
    assert "AGENT_ARCH" in manifest["env"] and "DEEPSEEK_STRICT" in manifest["env"]
    # Digests are computed for both the case file and the (out-of-git) event log.
    assert manifest["case_file"]["sha256"] == rp.sha256_of(case_file)
    assert manifest["events_log"]["sha256"] == rp.sha256_of(events)
    assert manifest["events_log"]["committed"] is False
    # It was actually written to disk.
    written = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert written["git_commit"] == "deadbeef"


def test_sha256_of_missing_file_is_none(tmp_path):
    assert rp.sha256_of(tmp_path / "nope.jsonl") is None


def test_write_results_package_emits_both(tmp_path):
    run = _run("H2", 42.0, [{"node": "agent", "ms": 10.0, "seq": 0}])
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text("{}\n", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text("{}\n", encoding="utf-8")
    rp.write_results_package(
        tmp_path, [run], argv=["python", "x"], arch="legacy", config="routed_models",
        timestamp="t", case_file=case_file, events_log=events,
        git_commit=lambda: "cafebabe")
    assert (tmp_path / "per_case.csv").exists()
    assert (tmp_path / "manifest.json").exists()
