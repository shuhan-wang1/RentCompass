"""
Unit tests for the ZERO-workflow-prior search path: budget and commute are OPTIONAL,
only a search AREA is required (and even that can be derived from a commute
destination). These tests never touch the network — the on-demand scraper and the
RAG coordinator are both stubbed.

Covers the three reported defects' fixes:
  * area only -> proceeds (no clarification), no travel_time rows, no budget_status
  * area+budget, no commute -> budget filter applied, no commute filter
  * commute_destination only ("UCL") -> search area derived, commute annotation present
  * no_commute=True with a commute time set -> commute logic fully disabled
  * nothing at all -> need_clarification, missing_fields==['area'], full known_criteria
  * _extract_no_commute EN+ZH; classify_place university/area/substring/unknown
"""

import asyncio
import os
import sys

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core`/`rag` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "local_data_demo")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
from core.tools import search_properties as sp
from core.tools.search_properties import (
    _extract_no_commute,
    search_properties_impl,
    set_rag_coordinator,
)


# --------------------------------------------------------------------------
# Stubs: on-demand scraper + RAG coordinator (no network, no ML model)
# --------------------------------------------------------------------------
def _row(addr, price, geo="51.52,-0.13", rt="1 bed Flat"):
    return {
        "Address": addr,
        "URL": "https://www.onthemarket.com/details/x/",
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
    """Returns whatever rows were indexed (the city-correct live rows), so the tool
    exercises its full ranking/commute/price path without the real RAG stack."""

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
    """Fresh fake RAG coordinator + patched maps helpers for every test."""
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# --------------------------------------------------------------------------
# search_properties_impl: optional-criteria behaviour
# --------------------------------------------------------------------------
def test_area_only_proceeds_without_clarification(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("1 Camden High St, London", 1200),
                                    _row("2 Camden Rd, London", 1450)])
    res = _run(area="Camden")
    assert res["status"] == "found"
    assert res["recommendations"], "expected listings"
    for row in res["recommendations"]:
        # No commute target -> travel_time field omitted entirely (no "0 min to None").
        assert "travel_time" not in row
        # No budget -> budget_status blank.
        assert row["budget_status"] == ""
    sc = res["search_criteria"]
    assert sc["area"] == "Camden"
    assert sc["commute_destination"] is None
    assert sc["no_commute"] is False
    # legacy keys still present for old consumers
    assert "destination" in sc and "max_travel_time" in sc
    assert "current listing" in res["summary"] and "Camden" in res["summary"]


def test_area_plus_budget_no_commute_applies_budget_filter(stub_env, monkeypatch):
    _install_listings(monkeypatch, [
        _row("A, London", 1200),   # in budget
        _row("B, London", 1450),   # in budget
        _row("C, London", 3000),   # far over 1.15x -> excluded
    ])
    res = _run(area="Camden", max_budget=1500)
    assert res["status"] == "found"
    prices = [r["price"] for r in res["recommendations"]]
    assert "£3000/month" not in prices  # over-budget row filtered out
    for row in res["recommendations"]:
        assert "travel_time" not in row          # no commute filter/annotation
        assert row["budget_status"] != ""        # budget status populated
    assert res["search_criteria"]["max_budget"] == 1500


def test_commute_destination_only_derives_area_and_annotates(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("Flat near UCL, London WC1", 1300)])
    res = _run(commute_destination="UCL")
    assert res["status"] == "found"
    assert res["recommendations"]
    row = res["recommendations"][0]
    # Commute annotation present (no filter, since no max_commute_time limit).
    assert "travel_time" in row and "UCL" in row["travel_time"]
    sc = res["search_criteria"]
    assert sc["commute_destination"] == "UCL"
    # area derived from the university slug
    assert sc["area"] == "bloomsbury"


def test_no_commute_true_disables_commute_filter(stub_env, monkeypatch):
    # Even with a commute destination AND a max_commute_time, no_commute wins.
    called = {"maps": 0}
    import core.maps_service as maps

    def _boom(*a, **k):
        called["maps"] += 1
        return 999
    monkeypatch.setattr(maps, "calculate_travel_time", _boom)
    _install_listings(monkeypatch, [_row("A, London", 1200), _row("B, London", 1400)])
    res = _run(area="Camden", commute_destination="UCL", no_commute=True, max_commute_time=30)
    assert res["status"] == "found"
    assert len(res["recommendations"]) == 2      # nothing filtered by commute
    for row in res["recommendations"]:
        assert "travel_time" not in row
    assert res["search_criteria"]["no_commute"] is True
    assert res["search_criteria"]["commute_destination"] is None
    assert called["maps"] == 0                    # commute computation skipped entirely


def test_nothing_at_all_asks_for_area_only(stub_env, monkeypatch):
    # No area, no query -> the ONLY clarification the tool ever emits.
    monkeypatch.setattr(on_demand, "get_listings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not scrape")))
    res = _run()
    assert res["success"] is False
    assert res["status"] == "need_clarification"
    assert res["missing_fields"] == ["area"]
    kc = res["known_criteria"]
    for key in ("area", "commute_destination", "max_budget", "max_travel_time",
                "no_commute", "bedrooms", "budget_period", "property_features",
                "soft_preferences"):
        assert key in kc, f"missing canonical key {key}"
    assert kc["area"] is None
    # Question asks WHERE TO LIVE, never "where do you commute to".
    assert "live in" in res["question"].lower()
    assert "commute to" not in res["question"].lower()
    # legacy extracted_so_far shape retained
    assert set(["destination", "max_budget", "max_travel_time",
                "property_features", "soft_preferences"]).issubset(res["extracted_so_far"])


def test_chinese_query_gets_chinese_clarification(stub_env):
    res = _run(current_message="帮我查找房子")  # CJK, but no resolvable area
    assert res["status"] == "need_clarification"
    assert res["missing_fields"] == ["area"]
    assert "住" in res["question"]  # Chinese question


def test_no_budget_no_results_message_is_chinese_when_cjk(stub_env, monkeypatch):
    _install_listings(monkeypatch, [], meta={"source": "none", "count": 0})
    res = _run(area="Camden", current_message="在Camden找个房子")
    assert res["status"] == "no_results"
    assert "右侧" in res["message"] or "搜索表单" in res["message"]


# --------------------------------------------------------------------------
# _extract_no_commute (EN + ZH)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "I don't commute, I just live there",
    "work from home mostly",
    "fully remote wfh",
    "我不通勤我单纯住着",
    "不需要通勤，就是住",
    "在家办公",
])
def test_extract_no_commute_positive(text):
    assert _extract_no_commute(text) is True


@pytest.mark.parametrize("text", [
    "I commute to UCL every day",
    "within 30 minutes of King's College",
    "找一个离学校近的房子",
    "",
])
def test_extract_no_commute_negative(text):
    assert _extract_no_commute(text) is False


# --------------------------------------------------------------------------
# classify_place
# --------------------------------------------------------------------------
def test_classify_place_university():
    assert on_demand.classify_place("ucl")["kind"] == "university"
    assert on_demand.classify_place("King's College London")["kind"] == "university"
    assert on_demand.classify_place("Imperial College")["kind"] == "university"


def test_classify_place_area():
    c = on_demand.classify_place("camden")
    assert c == {"kind": "area", "slug": "camden", "city": "london"}
    assert on_demand.classify_place("Manchester")["kind"] == "area"


def test_classify_place_glued_typo_substring_fallback():
    # "axocamden" resolves to camden (area) via the last-resort substring fallback.
    c = on_demand.classify_place("axocamden")
    assert c["kind"] == "area" and c["slug"] == "camden"


def test_classify_place_unknown():
    c = on_demand.classify_place("zzqqxx")
    assert c["kind"] == "unknown" and c["slug"] == "zzqqxx" and c["city"] is None
