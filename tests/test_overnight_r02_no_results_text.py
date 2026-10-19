"""No-results text polish (R2-03, on R1-13's new template).

Live: a studio search said "… that match: up to £1,500/month, studio, Studio." (bedrooms=0 and
room_type='studio' both listed) and suggested loosening "the budget or the commute limit" on a
search with no commute filter at all.
"""
from __future__ import annotations

import pytest

from core.tools.search_properties import _applied_filters_text, _no_results_message

_STUDIO = {"area": "Hackney", "max_budget": 1500, "bedrooms": 0, "room_type": "studio",
           "commute_destination": None, "no_commute": True, "max_travel_time": None,
           "property_features": []}


@pytest.mark.parametrize("zh", [False, True])
def test_r2_03_a_studio_is_named_once(zh):
    text = _applied_filters_text(_STUDIO, zh)
    assert text.lower().count("studio") == 1, text


def test_r2_03_no_commute_hint_without_a_commute_filter():
    en = _no_results_message("Hackney", False, criteria=_STUDIO)
    assert "commute" not in en.lower()
    assert "for example the budget)" in en
    zh = _no_results_message("Hackney", True, criteria=_STUDIO)
    assert "通勤" not in zh and "例如预算" in zh


def test_r2_03_commute_hint_when_a_commute_filter_was_applied():
    criteria = dict(_STUDIO, commute_destination="UCL", no_commute=False, max_travel_time=30)
    en = _no_results_message("Hackney", False, criteria=criteria)
    assert "within 30 min of UCL" in en and "the budget or the commute limit" in en
    zh = _no_results_message("Hackney", True, criteria=criteria)
    assert "预算或通勤时间" in zh


def test_r2_03_no_filters_means_no_loosening_hint_about_them():
    en = _no_results_message("Hackney", False, criteria={})
    assert "budget" not in en and "commute" not in en and "these" not in en
    features_only = _no_results_message("Hackney", False,
                                        criteria={"property_features": ["pets allowed"]})
    assert "pets allowed" in features_only and "commute" not in features_only
    assert "budget" not in features_only
