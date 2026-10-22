"""compare_or_rank_areas ranks honestly (2026-10-19 B-07).

Live: an area with NO rent data (Finsbury Park, 85.2 on commute alone) ranked above one scored on
value AND commute (Bloomsbury, 76.1); 8 requested areas came back as 5 with Euston, Hackney and
Stratford never mentioned; and the answer's "top recommendation" contradicted the tool's order.
The tool now ranks partial scores after full ones, names every requested area it could not
evaluate, numbers the ranking and states the order in its notes.
"""
from __future__ import annotations

import asyncio

from core import area_stats
from core.tools import compare_or_rank_areas as rank_mod


def _stats(median, sample=12):
    return {"min": median - 200, "max": median + 200, "median": median, "sample_size": sample,
            "freshness_days": 3, "budget_match_rate": 0.5}


def _run(monkeypatch, candidates, stats, **kwargs):
    async def fake_candidates(seed, **_):
        return candidates

    monkeypatch.setattr(rank_mod, "generate_candidate_areas", fake_candidates)
    monkeypatch.setattr(area_stats, "aggregate",
                        lambda slugs, budget=None: {s: stats.get(s, area_stats._empty())
                                                    for s in slugs})
    return asyncio.run(rank_mod.compare_or_rank_areas_impl(destination="UCL", **kwargs))


def test_an_area_without_rent_data_ranks_after_fully_scored_ones(monkeypatch):
    candidates = [{"name": "Finsbury Park", "slug": "finsbury-park", "commute_minutes": 8},
                  {"name": "Bloomsbury", "slug": "bloomsbury", "commute_minutes": 12},
                  {"name": "Camden Town", "slug": "camden-town", "commute_minutes": 15}]
    out = _run(monkeypatch, candidates,
               {"bloomsbury": _stats(1700), "camden-town": _stats(1900)},
               max_commute_minutes=30, budget_hint={"amount": 1800, "period": "month"})
    names = [a["name"] for a in out["areas"]]
    assert names[-1] == "Finsbury Park"
    assert out["areas"][-1]["score"]["partial"] is True
    assert [a["rank"] for a in out["areas"]] == [1, 2, 3]
    assert any("Ranked order" in note and "#1 " + names[0] in note
               for note in out["explanation_notes"])


def test_requested_areas_that_were_not_evaluated_are_named(monkeypatch):
    candidates = [{"name": "Bloomsbury", "slug": "bloomsbury", "commute_minutes": 12},
                  {"name": "Camden Town", "slug": "camden-town", "commute_minutes": 15}]
    out = _run(monkeypatch, candidates,
               {"bloomsbury": _stats(1700), "camden-town": _stats(1900)},
               max_commute_minutes=30,
               candidate_areas=["Bloomsbury", "Camden Town", "Euston", "Hackney", "Stratford"])
    assert out["not_evaluated"] == ["Euston", "Hackney", "Stratford"]
    assert any("NOT evaluated" in note and "Euston, Hackney, Stratford" in note
               for note in out["explanation_notes"])


def test_areas_cut_by_the_limit_are_mentioned(monkeypatch):
    candidates = [{"name": f"Area {i}", "slug": f"area-{i}", "commute_minutes": 10 + i}
                  for i in range(4)]
    out = _run(monkeypatch, candidates, {f"area-{i}": _stats(1500 + 50 * i) for i in range(4)},
               max_commute_minutes=30, limit=2)
    assert len(out["areas"]) == 2
    assert any("below the top 2" in note for note in out["explanation_notes"])
