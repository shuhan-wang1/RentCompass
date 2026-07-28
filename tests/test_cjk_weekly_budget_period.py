"""`_extract_budget` reads the AMOUNT in two languages and the PERIOD in one.

Production, 2026-07-28. A Chinese-speaking user asked for a place near UCL at
"预算每周£350" — three hundred and fifty pounds PER WEEK. The turn returned no listings
at all, and the log says exactly why:

    🏠 [SEARCH TOOL] ... max_budget: 1517            <- correct: the model converted /week
       🔄 当前消息更新预算: £1517 → £350/month        <- the re-extraction overrode it
    🌐 [SEARCH] 抓取实时房源: areas=['London'], £100-402/month

`_extract_budget` matched the number 350 through its CJK amount patterns and then labelled
the period **'month'**, because the period test is English-only:

    period = 'week' if re.search(r'\\b(?:pw|/\\s*w(?:k|eek)?|per\\s+week|a\\s+week)\\b', t) else 'month'

There is no 每周 / 每星期 / 周租 in it, and a missing marker silently defaults to monthly.
So the weekly→monthly conversion further down never fired, a correct £1517 was replaced by
£350, and the scrape band became £100–402/month — London has no flats at £402/month.

Two properties make this worse than a missing feature:

  * the amount patterns ARE bilingual (以内/以下/左右/块/镑/元/英镑, 预算/月租/租金/房租), so
    the CJK vocabulary was considered — only the period line was never extended;
  * the re-extraction is DESIGNED to win ("本轮覆盖累积"), so a mislabelled period does not
    fail safe: it overwrites a value that was already right.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.tools.search_properties import _extract_budget


# ══════════════════════════════════════════════════════════════════════════
# The production message, and the CJK weekly vocabulary around it
# ══════════════════════════════════════════════════════════════════════════

def test_the_exact_production_message():
    assert _extract_budget("9月去UCL读书，伦敦找房，预算每周£350") == (350, "week")


@pytest.mark.parametrize("text", [
    "预算每周£350",
    "预算每週£350",          # traditional
    "每周350镑",
    "每星期350镑",
    "每個星期350英镑",
    "周租350左右",
    "週租350左右",
    "按周算350镑",
    "350镑/周",
    "350镑 / 週",
])
def test_cjk_weekly_markers_are_read_as_weekly(text):
    amount, period = _extract_budget(text)
    assert amount == 350, text
    assert period == "week", f"{text!r} was read as {period}, so £350/week becomes £350/month"


# ══════════════════════════════════════════════════════════════════════════
# The monthly side must not move — that is the whole existing corpus
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,amount", [
    ("预算每月£1500", 1500),
    ("月租1500镑", 1500),
    ("预算1500以内", 1500),
    ("房租1500左右", 1500),
    ("my budget is £1800", 1800),
    ("1800 pcm", 1800),
    ("budget of 2000", 2000),
    ("under 1200", 1200),
])
def test_monthly_readings_are_unchanged(text, amount):
    assert _extract_budget(text) == (amount, "month"), text


@pytest.mark.parametrize("text", [
    "350 pw",
    "£350 per week",
    "£350 a week",
    "350/week",
    "350 /wk",
])
def test_english_weekly_still_works(text):
    assert _extract_budget(text) == (350, "week"), text


# ══════════════════════════════════════════════════════════════════════════
# False positives: "周" is a common character and must not hijack the period
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "预算1500，想住在周边安静的地方",     # 周边 = surroundings
    "预算1500，周末看房",                 # 周末 = weekend
    "预算1500，上周看的那个房子还在吗",     # 上周 = last week
    "预算1500，希望一周内能看房",          # a viewing timeframe, not a rent period
    "预算每月1500，我每周去两次健身房",     # a real weekly marker, but not the RENT period
])
def test_incidental_uses_of_zhou_do_not_make_it_weekly(text):
    amount, period = _extract_budget(text)
    assert period == "month", (
        f"{text!r} was read as weekly; a £1500/month budget would be inflated to £6495"
    )


def test_no_budget_is_still_no_budget():
    assert _extract_budget("每周去两次超市") == (None, None)
    assert _extract_budget("") == (None, None)
    assert _extract_budget(None) == (None, None)


# ══════════════════════════════════════════════════════════════════════════
# The consequence the user actually felt
# ══════════════════════════════════════════════════════════════════════════

def test_a_weekly_budget_survives_into_a_usable_monthly_band():
    """End of the chain: with the period right, £350/week must become a band that can
    actually match London stock, not £100-402/month."""
    from uk_rent_agent.domain import constants as C

    amount, period = _extract_budget("预算每周£350")
    assert period == "week"
    monthly = int(amount * C.WEEKS_PER_MONTH)
    assert 1400 <= monthly <= 1600, monthly
    band_max = int(monthly * C.BUDGET_SOFT_MULTIPLIER)
    assert band_max > 1000, (
        f"scrape band still collapses to £{band_max}/month — the production symptom"
    )
