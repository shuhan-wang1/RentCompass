"""Event-loop non-blocking regression tests (Phase 2.2 blocking-tool sweep).

The confirmed root cause: a tool registered as ``async def`` that performs SYNCHRONOUS
network / sqlite / sleep work runs that work INLINE on the asyncio event loop, so neither
the fc-loop's per-tool ``asyncio.wait_for`` nor the batch-budget ``asyncio.wait`` timer can
fire (a batch of four ``calculate_commute`` calls serialized to ~52s despite a 20s budget).

The fix mirrors the ``search_nearby_pois`` precedent:
  * tools whose guts are entirely synchronous are registered as plain ``def`` — Tool.execute
    then offloads them to an executor thread (core/tool_system.py :279-284);
  * tools that genuinely stay ``async`` (they await sibling tools / async helpers) push every
    blocking call through ``asyncio.to_thread``.

Each test below drives ``tool.execute(...)`` with a monkeypatched ~0.4s BLOCKING network/IO
layer, concurrently with a 10ms heartbeat coroutine, and asserts the heartbeat keeps ticking.
If the impl blocked the loop, the heartbeat could not advance. NO live network is used.
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd
import pytest

_BLOCK_S = 0.4  # duration of the simulated blocking network / IO call
_MIN_TICKS = 5  # heartbeat ticks expected while a 0.4s blocking call is in flight (10ms each)


async def _heartbeat_run(make_execute_coro):
    """Run ``make_execute_coro()`` (a coroutine factory calling tool.execute) concurrently
    with a 10ms heartbeat; return (tick_count, tool_result). A responsive loop => many ticks."""
    ticks = {"n": 0}
    stop = {"v": False}

    async def heartbeat():
        while not stop["v"]:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    hb = asyncio.ensure_future(heartbeat())
    try:
        result = await make_execute_coro()
    finally:
        stop["v"] = True
        await hb
    return ticks["n"], result


def _assert_responsive(ticks, result):
    assert ticks >= _MIN_TICKS, f"event loop appeared blocked (only {ticks} heartbeat ticks)"
    assert result.success is True


# ─── calculate_commute (CONFIRMED guilty; converted async->sync) ────────────────
def test_calculate_commute_not_blocking(monkeypatch):
    import core.maps_service as ms
    import core.tools.calculate_commute as cc

    def blocking_details(frm, to, mode="transit"):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS TfL/geopy call — must run off the loop
        return {"duration_minutes": 25, "route_summary": "Victoria line",
                "route_legs": [], "source": "tfl"}

    monkeypatch.setattr(ms, "calculate_travel_details", blocking_details)
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: cc.calculate_commute_tool.execute(from_address="A St", to_address="B St")))
    _assert_responsive(ticks, result)
    assert result.data["duration_minutes"] == 25


# ─── calculate_commute_cost (converted async->sync) ─────────────────────────────
def test_calculate_commute_cost_not_blocking(monkeypatch):
    import core.maps_service as ms
    import core.tools.calculate_commute_cost as ccc

    def blocking_time(frm, to, mode="transit"):
        time.sleep(_BLOCK_S)
        return 30

    monkeypatch.setattr(ms, "calculate_travel_time", blocking_time)
    # mode="walking" -> uses_transit False -> no zone/geocode network beyond the timed call.
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: ccc.calculate_commute_cost_tool.execute(
            from_address="A St", to_address="B St", mode="walking")))
    _assert_responsive(ticks, result)
    assert result.data["commute"]["duration_minutes"] == 30


# ─── check_safety (converted async->sync) ───────────────────────────────────────
def test_check_safety_not_blocking(monkeypatch):
    import core.tools.check_safety as cs

    def blocking_crime(location):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS data.police.uk call
        return {"total_crimes_6m": 12, "category_breakdown": {"Burglary": 4},
                "crime_trend": "stable", "most_recent_month_count": 2}

    monkeypatch.setattr(cs, "get_crime_data_by_location", blocking_crime)
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: cs.check_safety_tool.execute(address="Stratford, London")))
    _assert_responsive(ticks, result)
    assert result.data["safety_score"] == 94  # 100 - 12//2


# ─── get_weather (converted async->sync) ────────────────────────────────────────
def test_get_weather_not_blocking(monkeypatch):
    import core.tools.get_weather as gw

    class _Resp:
        status_code = 200

        def json(self):
            return {"current": {"temperature_2m": 12, "weather_code": 0,
                                 "relative_humidity_2m": 70, "wind_speed_10m": 10,
                                 "apparent_temperature": 11, "precipitation": 0},
                    "hourly": {"uv_index": [1]}}

        def raise_for_status(self):
            return None

    def blocking_get(url, params=None, timeout=None):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS requests.get to Open-Meteo
        return _Resp()

    monkeypatch.setattr(gw.requests, "get", blocking_get)
    # Supplying lat/lon skips the geocode request, leaving one (timed) forecast call.
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: gw.get_weather_tool.execute(location="Bloomsbury", latitude=51.5, longitude=-0.1)))
    _assert_responsive(ticks, result)
    assert result.data["condition"] == "Clear"


# ─── get_property_details (converted async->sync) ───────────────────────────────
def test_get_property_details_not_blocking(monkeypatch):
    import core.tools.get_property_details as gpd

    def blocking_db():
        time.sleep(_BLOCK_S)  # SYNCHRONOUS sqlite read + pandas construction
        return pd.DataFrame([{
            "Address": "19-29 Woburn Place, London WC1H",
            "Price": "1500", "Room_Type_Category": "Studio",
            "Description": "A studio flat", "Detailed_Amenities": "",
            "Guest_Policy": "", "Payment_Rules": "", "Excluded_Features": "",
            "URL": "https://example.com/1", "Available From": "2026-09-01",
        }])

    monkeypatch.setattr(gpd, "load_property_database", blocking_db)
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: gpd.get_property_details_tool.execute(property_address="Woburn Place")))
    _assert_responsive(ticks, result)
    assert result.data["found"] is True


# ─── recall_memory (converted async->sync) ──────────────────────────────────────
class _FakeMem:
    def retrieve(self, query, session_id="default", user_id=None, n=6):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS ChromaDB sqlite query + embedding
        return [{"text": "Budget 1500 near KCL"}]

    def add(self, content, mtype, session_id="default", user_id=None):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS ChromaDB write + embedding
        return "mem-id-1"

    def format_for_prompt(self, mems):
        return "\n".join(m["text"] for m in mems)


def test_recall_memory_not_blocking(monkeypatch):
    import core.tools.memory_tools as mt
    monkeypatch.setattr(mt, "_mem", lambda: _FakeMem())
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: mt.recall_memory_tool.execute(query="budget", user_id="u1")))
    _assert_responsive(ticks, result)
    assert result.data["count"] == 1


# ─── remember (converted async->sync; write tool) ───────────────────────────────
def test_remember_not_blocking(monkeypatch):
    import core.tools.memory_tools as mt
    monkeypatch.setattr(mt, "_mem", lambda: _FakeMem())
    # side_effect="write" -> Tool.execute requires an idempotency_key (no store passed here,
    # so the write proceeds without the sqlite idempotency round-trip).
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: mt.remember_tool.execute(content="likes Camden", user_id="u1",
                                         idempotency_key="k1")))
    _assert_responsive(ticks, result)
    assert result.data["id"] == "mem-id-1"


# ─── web_search (stays async; blocking get_search_snippets pushed to to_thread) ─
def test_web_search_not_blocking(monkeypatch):
    import core.tools.web_search as ws

    def blocking_snippets(query, max_results=5):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS SearXNG HTTP
        return ("London average rent is around 1800 per month according to several "
                "letting sources. Zone 2 is cheaper than Zone 1. Source: example.com")

    monkeypatch.setattr(ws, "get_search_snippets", blocking_snippets)
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: ws.web_search_tool.execute(query="London rent")))
    _assert_responsive(ticks, result)


# ─── compare_or_rank_areas (stays async; blocking area_stats.aggregate to_thread) ─
def test_compare_or_rank_areas_not_blocking(monkeypatch):
    import core.area_stats as area_stats_mod
    import core.tools.compare_or_rank_areas as cra

    async def fake_candidates(*args, **kwargs):
        return [{"name": "Camden", "slug": "camden", "commute_minutes": 20}]

    def blocking_aggregate(slugs, budget=None, *, now=None):
        time.sleep(_BLOCK_S)  # SYNCHRONOUS sqlite read over the listing cache
        return {s: {"sample_size": 3, "median": 1500, "min": 1200, "max": 1800,
                    "freshness_days": 5, "budget_match_rate": None} for s in slugs}

    monkeypatch.setattr(cra, "generate_candidate_areas", fake_candidates)
    monkeypatch.setattr(area_stats_mod, "aggregate", blocking_aggregate)
    ticks, result = asyncio.run(_heartbeat_run(
        lambda: cra.compare_or_rank_areas_tool.execute(city="Manchester", reply_language="en")))
    _assert_responsive(ticks, result)
    assert result.data["status"] == "ok"
