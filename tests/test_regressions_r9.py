"""Regression tests (Y-xx, E-xx ids in each test name)."""
from __future__ import annotations

import pytest

from core.listing_identity import bind_listing_links


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2500, "room_type": "whole"}


# Y-01 ── only a rent assertion takes the listing's rent
_CAMDEN_ROAD = [{"address": "Camden Road, Islington, London, N7", "price": "£1800/month",
                 "url": "https://www.onthemarket.com/details/12345678/"}]


@pytest.mark.parametrize("text", [
    "For a student, the cheapest option from Camden Road (N7) to Bank is a Zone 1-2 Monthly Travelcard at £119.90 a month.",
    "From Camden Road, Islington, N7 the Monthly Travelcard costs £119.90 per month.",
    "Camden Road, Islington, N7: the monthly transport cost is £119.90/month.",
    "Camden Road, Islington, N7: council tax is about £130/month.",
    "Camden Road: broadband adds £30 a month.",
    "From Camden Road to Bank the total comes to £1,920/month.",
    "Camden Road is £150 a month over.",
    "Camden Road 到 Bank 的月票是 £119.90/月。",
    "Camden Road：市政税约 £130/月。",
    "The Gallery at £1350/month is cheaper than Camden Road.",
])
def test_y_01_a_figure_that_is_no_rent_stays(text):
    assert bind_listing_links(text, _CAMDEN_ROAD) == text


@pytest.mark.parametrize("text,fixed", [
    ("Camden Road, Islington, N7 is £1750/month.", "Camden Road, Islington, N7 is £1800/month."),
    ("- Camden Road, Islington, London, N7 (£1750/month)", "- Camden Road, Islington, London, N7 (£1800/month)"),
    ("1. Camden Road, London, N7: 1 bed flat, £1750/month.", "1. Camden Road, London, N7: 1 bed flat, £1800/month."),
    ("Camden Road, N7 (Islington) at £1750/month.", "Camden Road, N7 (Islington) at £1800/month."),
    ("The Camden Road flat (the first one, £1750/month)", "The Camden Road flat (the first one, £1800/month)"),
    ("Camden Road is the cheapest at £1750/month.", "Camden Road is the cheapest at £1800/month."),
    ("[Camden Road](https://www.onthemarket.com/details/12345678/) — £1750/month.",
     "[Camden Road](https://www.onthemarket.com/details/12345678/) — £1800/month."),
    ("Camden Road: the rent is £1750/month.", "Camden Road: the rent is £1800/month."),
    ("Camden Road：月租£1750/月。", "Camden Road：月租£1800/月。"),
    ("Camden Road N7（£1750/月）", "Camden Road N7（£1800/月）"),
    ("Camden Road is listed at £1750 per month plus bills.", "Camden Road is listed at £1800 per month plus bills."),
    ("Camden Road is fine. The flat is £1750/month.", "Camden Road is fine. The flat is £1800/month."),
    ("Camden Road has the lowest rent (£1750/month).", "Camden Road has the lowest rent (£1800/month)."),
])
def test_y_01_a_wrong_rent_is_still_corrected(text, fixed):
    assert bind_listing_links(text, _CAMDEN_ROAD) == fixed


@pytest.mark.parametrize("text", [
    "Camden Road: your maximum rent is £2000.",
    "Camden Road: five weeks' rent of £2,076.",
    "Camden Road: the annual rent of £21,600 is due up front.",
    "Camden Road: the rent is £150 over your limit.",
    "Camden Road: the rent is £415 per week.",
])
def test_y_01_the_stated_rent_form_means_this_listings_monthly_rent(text):
    assert bind_listing_links(text, _CAMDEN_ROAD) == text


def test_y_01_a_rent_after_a_fare_is_corrected_in_the_same_form():
    out = bind_listing_links("Camden Road: Travelcard £119.90 a month; the rent is £1750.", _CAMDEN_ROAD)
    assert out == "Camden Road: Travelcard £119.90 a month; the rent is £1800."
