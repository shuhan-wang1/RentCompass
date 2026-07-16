"""Focused tests for the deterministic post-retrieval rental ranker."""

import os
import sys


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

from core.ranking import rank_and_diversify


def _listing(address, *, price=1000, commute=20, semantic=0.7, description="bright flat"):
    return {
        "Address": address,
        "price": price,
        "travel_time": commute,
        "similarity_score": semantic,
        "Description": description,
        "Room_Type_Category": "Studio",
    }


def test_ranker_rewards_budget_headroom_continuously():
    ranked = rank_and_diversify([
        _listing("Affordable House, Camden", price=900, commute=20, semantic=0.7),
        _listing("Cap House, Camden", price=1200, commute=20, semantic=0.7),
    ], max_budget=1200, max_commute=30)

    assert ranked[0]["Address"] == "Affordable House, Camden"
    assert ranked[0]["score_breakdown"]["price"] > ranked[1]["score_breakdown"]["price"]


def test_missing_commute_is_not_treated_as_a_perfect_commute():
    ranked = rank_and_diversify([
        _listing("Known commute, Camden", commute=15),
        _listing("Unknown commute, Camden", commute=None),
    ], max_budget=1200, max_commute=30)

    unknown = next(row for row in ranked if row["Address"] == "Unknown commute, Camden")
    assert "commute" not in unknown["score_breakdown"]
    assert unknown["score_breakdown"]["freshness"] > 0


def test_mmr_promotes_a_similarly_scored_distinct_listing():
    ranked = rank_and_diversify([
        _listing("Alpha House Camden", semantic=0.80, description="quiet studio near station"),
        _listing("Alpha House Camden", semantic=0.79, description="quiet studio near station"),
        _listing("Beta Court Islington", semantic=0.79, description="riverside studio near market"),
    ], max_budget=1200, max_commute=30, diversity_lambda=0.70)

    assert ranked[0]["Address"] == "Alpha House Camden"
    assert ranked[1]["Address"] == "Beta Court Islington"
