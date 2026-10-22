"""Derived totals were rewritten to a wrong figure (R2-18).

"£2750 rent + £201.60 = £2951.60 per month" was flagged ``unsupported_prices:2951.6`` (a
sum of two tool figures is not a weekly/monthly/N-month derivation), the regeneration was told
to use only verbatim or derived figures, and the shipped answer read "£2750 rent + £201.60 =
£2750 per month" (3/5 runs).
"""
from __future__ import annotations

from uk_rent_agent.agent.critic import build_correction_instruction, unsupported_reply_prices

_EVIDENCE = {
    "recommendations": [{"price": 2750, "address": "Clapham South, SW12"},
                        {"price": 2950, "address": "Old Town, Clapham Common"}],
    "transport": [{"monthly_travelcard_gbp": 201.60, "daily_cap_gbp": 10.50},
                  {"monthly_travelcard_gbp": 171.70, "daily_cap_gbp": 8.90}],
}


def test_r2_18_sums_and_differences_of_tool_figures_are_supported():
    answer = ("- Clapham South: £2750 rent + £201.60 = £2951.60 per month.\n"
              "- Old Town: £2950 rent + £171.70 = £3121.70 per month.\n"
              "Clapham South is cheaper overall by £170.10 a month (£3121.70 - £2951.60), "
              "and £200 cheaper on rent. Pay-as-you-go at the daily cap would be £231.00.")
    assert unsupported_reply_prices(answer, _EVIDENCE) == []


def test_r2_18_a_total_without_operands_is_supported_only_when_exact():
    assert unsupported_reply_prices("Total: £2,951.60 a month.", _EVIDENCE) == []
    assert unsupported_reply_prices("Total: £2,345 a month.", _EVIDENCE) == [2345.0]


def test_r2_18_a_wrong_step_does_not_support_its_result():
    assert 3500.0 in unsupported_reply_prices(
        "Clapham South: £2750 + £201.60 = £3500 per month.", _EVIDENCE)


def test_r2_18_the_correction_never_asks_to_swap_figures():
    text = build_correction_instruction([3121.7])
    assert "sum/difference" in text and "never replace a figure" in text
