"""R3-06: compare_or_rank_areas medians are evidence, not unsupported prices.

r04 rid 00af8362: ``critic.verdict stage=initial grounded=False
issues=['unsupported_prices:2100,2400,2750']`` while the tool JSON held
``areas[].rent.median`` 2400.0 / 2100.0 / 2750.0 — a wasted regeneration plus the generic
"Please double-check the exact prices…" caveat.
"""
from __future__ import annotations

import json
import pathlib

from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_reply_prices

CASE = json.loads((pathlib.Path(__file__).parent / "data" /
                   "qa_r03_area_medians.json").read_text(encoding="utf-8"))


def test_area_rent_medians_support_the_reply():
    assert unsupported_reply_prices(CASE["draft"], [CASE["raw_data"]]) == []


def test_area_comparison_draft_is_grounded():
    verdict = evaluate_grounding(CASE["draft"], [CASE["raw_data"]], retrieval_expected=True)
    assert not any(str(issue).startswith("unsupported_prices") for issue in verdict.issues), verdict.issues


def test_a_figure_absent_from_the_rent_stats_is_still_unsupported():
    draft = "Islington's median rent is £2,400 and Camden's median rent is £2,987 per month."
    assert unsupported_reply_prices(draft, [CASE["raw_data"]]) == [2987.0]


def test_non_money_statistics_stay_out():
    evidence = {"commute": {"median": 1450}, "rent": {"sample_size": 1450}}
    assert unsupported_reply_prices("The rent is £1,450 per month.", [evidence]) == [1450.0]
