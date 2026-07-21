"""Tests for the area value-ranking capability (workstream B, design §2.5b):

  * core.area_stats.aggregate  — per-area rent aggregation over the OnTheMarket
    listing SQLite cache (min/max/median/sample/freshness/budget-match, weekly
    normalisation, zero-listing honesty);
  * core.tools.compare_or_rank_areas — the explainable value/commute composite
    (weights from priorities, priority-ordering effects, missing-destination
    handling, bilingual strings, input validation);
  * core.recommend_areas.generate_candidate_areas — the shared candidate core.

All network is stubbed (commute + classify_place + geocode + candidate
generation), and the listing cache is a temp SQLite seeded in-process, so the
whole suite runs fully offline.

Run:
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 conda run --no-capture-output -n uk_rent \
      python -m pytest tests/test_compare_areas.py -q
"""

import asyncio
import json
import os
import sqlite3
import sys

# --- Pin the real source roots ahead of tests/ (mirrors test_on_demand_listings).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import pytest

from core import area_stats
import core.recommend_areas as recommend_areas
import core.tools.compare_or_rank_areas as cra
from core.tools.compare_or_rank_areas import compare_or_rank_areas_impl

NOW = 1_800_000_000.0  # fixed "now" for deterministic freshness
DAY = 86400.0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _row(url, price, address="1 Test St, London"):
    return {"URL": url, "Price": price, "Address": address}


def _seed_cache(path, entries):
    """entries: {cache_key: (rows_list, fetched_epoch)}."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS listings ("
        "key TEXT PRIMARY KEY, rows TEXT NOT NULL, fetched REAL NOT NULL)"
    )
    for key, (rows, fetched) in entries.items():
        conn.execute(
            "INSERT OR REPLACE INTO listings(key, rows, fetched) VALUES (?, ?, ?)",
            (key, json.dumps(rows), float(fetched)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    p = tmp_path / "listing_cache.sqlite3"
    monkeypatch.setenv("SEARCH_LISTING_CACHE_PATH", str(p))
    return p


def _fake_candidates(*cands):
    """Return an async generate_candidate_areas stub yielding fixed candidates.
    Each cand is (name, slug, commute_minutes)."""
    items = [
        {"name": n, "slug": s, "commute_minutes": c,
         "city": "london", "centroid": [51.5, -0.1], "reason": "", "source": "web+validated"}
        for (n, s, c) in cands
    ]

    async def _gen(seed, **kwargs):
        return list(items)

    return _gen


def _run(coro):
    # asyncio.run (not get_event_loop().run_until_complete): the legacy pattern breaks
    # when another test module has already created and closed its own loop.
    return asyncio.run(coro)


# ==========================================================================
# area_stats.aggregate — rent math
# ==========================================================================
def test_aggregate_min_max_median_sample_freshness(cache_path):
    rows = [_row(f"u{i}", f"£{p} pcm") for i, p in enumerate([1000, 1200, 1400, 1600, 1800])]
    _seed_cache(cache_path, {"otm|camden|b0-2|p800-2000": (rows, NOW - 2 * DAY)})

    st = area_stats.aggregate(["camden"], now=NOW)["camden"]
    assert st["min"] == 1000.0
    assert st["max"] == 1800.0
    assert st["median"] == 1400.0
    assert st["sample_size"] == 5
    assert st["freshness_days"] == 2


def test_weekly_price_normalised_to_monthly(cache_path):
    # "£300 pw" -> 300 * 4.33 = 1299/month; a plain pcm row stays monthly.
    rows = [_row("w1", "£300 pw"), _row("m1", "£1300 pcm")]
    _seed_cache(cache_path, {"otm|leeds|b0-2|p100-5000": (rows, NOW - DAY)})

    st = area_stats.aggregate(["leeds"], now=NOW)["leeds"]
    assert st["min"] == 1299.0            # weekly converted, not read as £300/month
    assert st["max"] == 1300.0
    assert st["sample_size"] == 2


def test_budget_match_rate_month_and_week(cache_path):
    rows = [_row(f"u{i}", f"£{p} pcm") for i, p in enumerate([1000, 1200, 1400, 1600, 1800])]
    _seed_cache(cache_path, {"otm|brixton|b0-2|p800-2000": (rows, NOW - DAY)})

    # Monthly budget £1300 -> 2 of 5 under budget.
    st = area_stats.aggregate(["brixton"], budget=(1300, "month"), now=NOW)["brixton"]
    assert st["budget_match_rate"] == 0.4
    # Weekly budget £300 -> £1299/month -> still 2 of 5.
    st_w = area_stats.aggregate(["brixton"], budget=(300, "week"), now=NOW)["brixton"]
    assert st_w["budget_match_rate"] == 0.4


def test_zero_listings_is_no_data_not_invented(cache_path):
    _seed_cache(cache_path, {"otm|camden|b0-2|p100-5000": ([_row("u1", "£1000 pcm")], NOW - DAY)})
    stats = area_stats.aggregate(["camden", "narnia"], now=NOW)
    assert stats["camden"]["sample_size"] == 1
    empty = stats["narnia"]
    assert empty["sample_size"] == 0
    assert empty["min"] is None and empty["median"] is None
    assert empty["budget_match_rate"] is None


def test_dedupe_same_url_newest_fetched_wins(cache_path):
    # Same listing URL under two price-band keys; newer key's price is authoritative.
    _seed_cache(cache_path, {
        "otm|hackney|b0-2|p100-1500": ([_row("same", "£1000 pcm")], NOW - 10 * DAY),
        "otm|hackney|b0-2|p1500-3000": ([_row("same", "£2000 pcm")], NOW - DAY),
    })
    st = area_stats.aggregate(["hackney"], now=NOW)["hackney"]
    assert st["sample_size"] == 1          # counted once
    assert st["min"] == 2000.0             # newest copy wins
    assert st["freshness_days"] == 1


def test_slug_prefix_does_not_leak_across_areas(cache_path):
    # 'leeds' must not pick up 'leedsville' rows (pipe-delimited key guards this).
    _seed_cache(cache_path, {
        "otm|leeds|b0-2|p100-5000": ([_row("a", "£1000 pcm")], NOW - DAY),
        "otm|leedsville|b0-2|p100-5000": ([_row("b", "£9000 pcm")], NOW - DAY),
    })
    st = area_stats.aggregate(["leeds"], now=NOW)["leeds"]
    assert st["sample_size"] == 1 and st["max"] == 1000.0


def test_missing_cache_file_is_all_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_LISTING_CACHE_PATH", str(tmp_path / "nope.sqlite3"))
    stats = area_stats.aggregate(["camden"], now=NOW)
    assert stats["camden"]["sample_size"] == 0


# ==========================================================================
# compare_or_rank_areas — input validation
# ==========================================================================
def test_input_validation_requires_city_or_destination():
    res = _run(compare_or_rank_areas_impl(reply_language="en"))
    assert res["success"] is False
    assert res["status"] == "need_input"
    assert res["missing_fields"] == ["city_or_destination"]
    assert "which city" in res["question"].lower()


# ==========================================================================
# compare_or_rank_areas — scoring
# ==========================================================================
def _seed_two_areas(cache_path, a_price=1000, b_price=2000):
    """Area A cheap, area B expensive; 5 fresh listings each (no low-sample shrink)."""
    a_rows = [_row(f"a{i}", f"£{a_price + 50 * i} pcm") for i in range(5)]
    b_rows = [_row(f"b{i}", f"£{b_price + 50 * i} pcm") for i in range(5)]
    _seed_cache(cache_path, {
        "otm|areaa|b0-2|p100-5000": (a_rows, NOW - DAY),
        "otm|areab|b0-2|p100-5000": (b_rows, NOW - DAY),
    })


def test_value_priority_vs_commute_priority_reorder(cache_path, monkeypatch):
    _seed_two_areas(cache_path)
    # A: cheap but far (55 min); B: pricey but close (5 min).
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 55), ("Area B", "areab", 5)))

    val = _run(compare_or_rank_areas_impl(
        destination="UCL", max_commute_minutes=60,
        priorities=["value", "commute"], reply_language="en"))
    com = _run(compare_or_rank_areas_impl(
        destination="UCL", max_commute_minutes=60,
        priorities=["commute", "value"], reply_language="en"))

    assert val["status"] == "ok"
    assert val["areas"][0]["slug"] == "areaa"   # value-first -> cheap A wins
    assert com["areas"][0]["slug"] == "areab"   # commute-first -> close B wins


def test_weights_sum_to_one_and_reflect_priority_order(cache_path, monkeypatch):
    _seed_two_areas(cache_path)
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 30), ("Area B", "areab", 20)))

    res = _run(compare_or_rank_areas_impl(
        destination="UCL", priorities=["value", "commute"], reply_language="en"))
    w = res["weights"]
    assert set(w) == {"value", "commute"}
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["value"] > w["commute"]           # leading priority heaviest

    res2 = _run(compare_or_rank_areas_impl(
        destination="UCL", priorities=["commute", "value"], reply_language="en"))
    w2 = res2["weights"]
    assert abs(sum(w2.values()) - 1.0) < 1e-6
    assert w2["commute"] > w2["value"]


def test_missing_destination_nulls_commute_and_excludes_it(cache_path, monkeypatch):
    _seed_two_areas(cache_path)
    # No destination -> candidates carry commute_minutes=None.
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", None), ("Area B", "areab", None)))

    res = _run(compare_or_rank_areas_impl(
        city="London", priorities=["value", "commute"], reply_language="en"))
    assert res["status"] == "ok"
    assert "commute" not in res["weights"]      # excluded, not faked
    assert set(res["weights"]) == {"value"}
    for a in res["areas"]:
        assert a["commute_minutes"] is None
        assert "commute" not in a["score"]["components"]
    assert any("commute is excluded" in n.lower() for n in res["explanation_notes"])


def test_safety_priority_excluded_with_note(cache_path, monkeypatch):
    _seed_two_areas(cache_path)
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 20), ("Area B", "areab", 25)))
    res = _run(compare_or_rank_areas_impl(
        destination="UCL", priorities=["safety", "value"], reply_language="en"))
    assert "safety" not in res["weights"]
    assert any("safety" in n.lower() for n in res["explanation_notes"])


def test_low_sample_flagged_and_no_data_area(cache_path, monkeypatch):
    # areaa: 5 listings (confident); thin: 2 listings (low_sample); ghost: none (no_data).
    a_rows = [_row(f"a{i}", f"£{1000 + 50 * i} pcm") for i in range(5)]
    t_rows = [_row("t0", "£900 pcm"), _row("t1", "£950 pcm")]
    _seed_cache(cache_path, {
        "otm|areaa|b0-2|p100-5000": (a_rows, NOW - DAY),
        "otm|thin|b0-2|p100-5000": (t_rows, NOW - DAY),
    })
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 20),
                                         ("Thin", "thin", 22),
                                         ("Ghost", "ghost", 25)))
    res = _run(compare_or_rank_areas_impl(
        destination="UCL", priorities=["value"], reply_language="en"))
    by_slug = {a["slug"]: a for a in res["areas"]}
    assert by_slug["areaa"]["rent"]["low_sample"] is False
    assert by_slug["thin"]["rent"]["low_sample"] is True
    assert by_slug["ghost"]["rent"] is None          # no invented number
    assert by_slug["ghost"]["no_data"] is True
    assert res["status"] == "ok"                     # some data exists overall


def test_all_areas_no_cache_is_no_data(cache_path, monkeypatch):
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 20)))
    res = _run(compare_or_rank_areas_impl(
        destination="UCL", reply_language="en"))
    assert res["status"] == "no_data"
    assert res["areas"][0]["rent"] is None
    assert res["areas"][0]["no_data"] is True


def test_no_candidates_is_no_data(cache_path, monkeypatch):
    monkeypatch.setattr(cra, "generate_candidate_areas", _fake_candidates())  # empty
    res = _run(compare_or_rank_areas_impl(destination="UCL", reply_language="en"))
    assert res["status"] == "no_data"
    assert res["areas"] == []


# ==========================================================================
# compare_or_rank_areas — localisation
# ==========================================================================
def test_reply_language_en_vs_zh(cache_path, monkeypatch):
    _seed_two_areas(cache_path)
    monkeypatch.setattr(cra, "generate_candidate_areas",
                        _fake_candidates(("Area A", "areaa", 20), ("Area B", "areab", 25)))

    en = _run(compare_or_rank_areas_impl(destination="UCL", reply_language="en"))
    zh = _run(compare_or_rank_areas_impl(destination="UCL", reply_language="zh"))
    assert any("Composite score" in n for n in en["explanation_notes"])
    assert any("综合分" in n for n in zh["explanation_notes"])
    # sources are localised too
    assert any("OnTheMarket listing cache" in s for a in en["areas"] for s in a["sources"])
    assert any("缓存" in s for a in zh["areas"] for s in a["sources"])


def test_input_validation_localised_zh():
    res = _run(compare_or_rank_areas_impl(reply_language="zh"))
    assert res["success"] is False
    assert "比较哪个城市" in res["question"]


# ==========================================================================
# recommend_areas.generate_candidate_areas — shared core (stubbed, offline)
# ==========================================================================
def test_generate_candidate_areas_validates_named_list(monkeypatch):
    monkeypatch.setattr(recommend_areas, "classify_place",
                        lambda name: {"slug": name.strip().lower().replace(" ", "-"),
                                      "city": "london", "kind": "area", "address": None})
    monkeypatch.setattr(recommend_areas, "resolve_location",
                        lambda name: (name.strip().lower().replace(" ", "-"), "london"))
    monkeypatch.setattr(recommend_areas, "is_destination", lambda place: False)
    monkeypatch.setattr(recommend_areas, "geocode_address",
                        lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(recommend_areas, "commute_minutes", lambda *a, **k: 20)

    out = _run(recommend_areas.generate_candidate_areas(
        "UCL", city="london", dest_coords={"lat": 51.52, "lng": -0.13}, london=True,
        candidate_names=["Camden", "Islington"], max_commute_time=60))

    slugs = sorted(i["slug"] for i in out)
    assert slugs == ["camden", "islington"]
    assert all(i["commute_minutes"] == 20 for i in out)


def test_generate_candidate_areas_no_commute_mode_nulls_commute(monkeypatch):
    monkeypatch.setattr(recommend_areas, "classify_place",
                        lambda name: {"slug": name.strip().lower(), "city": "london",
                                      "kind": "area", "address": None})
    monkeypatch.setattr(recommend_areas, "resolve_location",
                        lambda name: (name.strip().lower(), "london"))
    monkeypatch.setattr(recommend_areas, "is_destination", lambda place: False)
    monkeypatch.setattr(recommend_areas, "geocode_address", lambda addr: {"lat": 51.5, "lng": -0.1})
    # commute must never be consulted in no-commute mode.
    monkeypatch.setattr(recommend_areas, "commute_minutes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("commute called")))

    out = _run(recommend_areas.generate_candidate_areas(
        "London", city="london", candidate_names=["camden"], no_commute_mode=True))
    assert len(out) == 1
    assert out[0]["commute_minutes"] is None
