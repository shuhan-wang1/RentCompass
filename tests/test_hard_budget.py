"""Product-correctness tests for the HARD budget contract (defect H2).

A hard ``max_budget`` is a strict ceiling on the MAIN recommendations: a listing
whose normalized monthly rent exceeds the budget must NEVER appear in
``recommendations`` and must NEVER be counted in the "found N" claim. The tool keeps
a soft-expansion behaviour (listings up to 15% over budget), but those may only surface
in a separate, clearly-labelled ``over_budget_alternatives`` list — the summary must not
claim they satisfy the budget.

The leak these tests pin: step 6 previously merged ``soft_violation`` straight into
``all_results`` -> ``recommendations`` and counted them in ``total_found``, so £1582 /
£1625 listings appeared inside a £1500 result set.

No network: the on-demand scraper, RAG coordinator, maps and detail-enrichment are all
stubbed / disabled, exactly like test_search_optional_criteria.
"""

import asyncio
import os
import re
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies of core/rag). ---
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
from core.tools.search_properties import search_properties_impl, set_rag_coordinator


# --------------------------------------------------------------------------
# Stubs (no network, no ML model) — mirrors test_search_optional_criteria.
# --------------------------------------------------------------------------
def _row(addr, price, geo="51.52,-0.13", rt="1 bed Flat"):
    return {
        "Address": addr,
        "URL": f"https://www.onthemarket.com/details/{price}/",
        "Price": f"£{price} pcm",
        "geo_location": geo,
        "Geo_Location": geo,
        "Room_Type_Category": rt,
        "Description": "Bright flat near transport. Bus 10 min.",
        "Images": [],
    }


class _FakeStore:
    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows)

    def search(self, query, top_k=10):
        return list(self.rows)


class _FakeCoordinator:
    def __init__(self):
        self.property_store = _FakeStore()

    def enhanced_search(self, query, criteria):
        rows = self.property_store.rows
        for r in rows:
            r.setdefault("similarity_score", 0.6)
        return list(rows), [], []


def _install_listings(monkeypatch, rows, meta=None):
    m = {"slug": "x", "requested_location": "x", "requested_city": "london",
         "source": "scraped", "stale": False, "count": len(rows),
         "elapsed_s": 0.01, "message": ""}
    if meta:
        m.update(meta)
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": list(rows), "meta": m})


@pytest.fixture
def stub_env(monkeypatch):
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    # Keep the budget-partition logic the sole variable under test.
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")   # no detail-page network
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")     # deterministic ordering
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    kwargs.setdefault("confirmed", True)  # bypass the soft-criteria gate
    return asyncio.run(search_properties_impl(**kwargs))


def _price_int(row):
    return int(re.sub(r"[^\d]", "", row["price"]))


def _prices(rows):
    return sorted(_price_int(r) for r in rows)


# --------------------------------------------------------------------------
# Core H2 partition: main strictly <= budget, over-budget only in alternatives.
# --------------------------------------------------------------------------
def test_hard_budget_never_leaks_over_budget_into_recommendations(stub_env, monkeypatch):
    _install_listings(monkeypatch, [
        _row("A, London", 1499),   # in budget
        _row("B, London", 1500),   # exactly at budget -> in budget
        _row("C, London", 1582),   # over -> alternative (<= 1725)
        _row("D, London", 1625),   # over -> alternative (<= 1725)
        _row("E, London", 3000),   # far over 1.15x -> excluded entirely
    ])
    res = _run(area="Camden", max_budget=1500, reply_language="en")
    assert res["status"] == "found"

    recs = res["recommendations"]
    alts = res["over_budget_alternatives"]

    # 1) EVERY main recommendation is strictly within budget.
    assert _prices(recs) == [1499, 1500]
    for row in recs:
        assert _price_int(row) <= 1500
        assert row.get("match_type") != "soft_violation"
        assert "在预算内" in row["budget_status"]

    # 2) The over-budget listings live ONLY in the alternatives list, clearly labelled.
    assert _prices(alts) == [1582, 1625]
    assert 1582 not in _prices(recs) and 1625 not in _prices(recs)
    for row in alts:
        assert 1500 < _price_int(row) <= int(1500 * 1.15)
        assert row.get("alternative") is True
        assert row.get("match_type") == "soft_violation"
        assert "超预算" in row["budget_status"]

    # 3) £3000 (beyond the 15% soft band) is nowhere.
    assert 3000 not in _prices(recs) and 3000 not in _prices(alts)


def test_found_count_and_summary_exclude_over_budget(stub_env, monkeypatch):
    _install_listings(monkeypatch, [
        _row("A, London", 1400),
        _row("B, London", 1500),
        _row("C, London", 1600),   # over -> alternative
    ])
    res = _run(area="Camden", max_budget=1500, reply_language="en")

    # "found N" counts ONLY the in-budget recommendations.
    assert res["total_found"] == 2
    assert res["total_found"] == len(res["recommendations"])
    assert res["soft_count"] == 1
    assert res["perfect_count"] == 2

    summary = res["summary"]
    assert "I found 2 current listings in Camden" in summary
    # The over-budget option is mentioned as a SEPARATE alternative, never as in-budget.
    assert "alternative" in summary.lower()
    assert "not within your budget" in summary.lower()
    # The over-budget figure must not appear in the headline/price-range claim.
    assert "1600" not in summary


# --------------------------------------------------------------------------
# Weekly budget: conversion happens BEFORE the hard ceiling is applied.
# --------------------------------------------------------------------------
def test_weekly_budget_converts_then_enforces_hard_ceiling(stub_env, monkeypatch):
    # £350/week -> int(350 * 4.33) = £1515/month ceiling.
    _install_listings(monkeypatch, [
        _row("A, London", 1500),   # <= 1515 -> in budget
        _row("B, London", 1515),   # exactly at converted ceiling -> in budget
        _row("C, London", 1600),   # over -> alternative (<= 1742)
        _row("D, London", 1800),   # over 1.15x (>1742) -> excluded
    ])
    res = _run(area="Camden", max_budget=350, budget_period="week", reply_language="en")
    assert res["status"] == "found"
    assert res["search_criteria"]["max_budget"] == 1515      # converted to monthly

    assert _prices(res["recommendations"]) == [1500, 1515]
    assert _prices(res["over_budget_alternatives"]) == [1600]
    for row in res["recommendations"]:
        assert _price_int(row) <= 1515
    assert 1800 not in _prices(res["recommendations"])
    assert 1800 not in _prices(res["over_budget_alternatives"])


# --------------------------------------------------------------------------
# Bilingual labelling of the alternatives section.
# --------------------------------------------------------------------------
def test_over_budget_alternatives_label_en(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A", 1499), _row("B", 1582)])
    res = _run(area="Camden", max_budget=1500, reply_language="en")
    assert res["over_budget_alternatives"]
    assert "Over-budget alternatives" in res["over_budget_alternatives_label"]
    assert "not within your budget" in res["over_budget_alternatives_label"].lower()


def test_over_budget_alternatives_label_zh(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A", 1499), _row("B", 1582)])
    res = _run(area="Camden", max_budget=1500, reply_language="zh")
    assert res["over_budget_alternatives"]
    assert "超预算备选" in res["over_budget_alternatives_label"]
    # zh summary counts only the in-budget listing and flags the alternative separately.
    assert "找到 1 套" in res["summary"]
    assert "略超预算" in res["summary"] and "备选" in res["summary"]
    assert "1582" not in res["summary"]


# --------------------------------------------------------------------------
# No soft expansion -> alternatives empty, label blank, backward-compatible payload.
# --------------------------------------------------------------------------
def test_all_in_budget_no_alternatives(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A", 1400), _row("B", 1499), _row("C", 1500)])
    res = _run(area="Camden", max_budget=1500, reply_language="en")
    assert _prices(res["recommendations"]) == [1400, 1499, 1500]
    assert res["over_budget_alternatives"] == []
    assert res["over_budget_alternatives_label"] == ""
    assert res["total_found"] == 3
    # No "alternatives" clause when there are none.
    assert "alternative" not in res["summary"].lower()
