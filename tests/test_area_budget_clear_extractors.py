"""
Unit tests for the conversational-state extractors added to the search tool:

- ``_extract_area``       — a NEW area/city the user is switching to (canonical
                            English name), incl. switch phrasings, bare place
                            names and Chinese city names; None for nonsense/non-UK.
- ``_extract_budget_clear`` — True only when the user asks to REMOVE/CLEAR the
                            budget limit (never on a normal budget statement).
- ``_strip_memory_block`` — removes the prepended long-term-memory block so hard
                            criteria are never extracted from a prior conversation
                            (cross-conversation bleed).

Importing search_properties is cheap: the sentence-transformers model loads lazily
on a real search, not at import time.
"""

import os
import sys

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "local_data_demo")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.tools.search_properties import (
    _extract_area,
    _extract_budget_clear,
    _strip_memory_block,
)


# ─────────────────────────── _extract_area ──────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # conversational switch phrasings -> canonical English
    ("make it Manchester", "Manchester"),
    ("switch to Bristol", "Bristol"),
    ("change it to Leeds", "Leeds"),
    ("actually London", "London"),
    ("move to Cardiff", "Cardiff"),
    ("look in Shoreditch", "Shoreditch"),
    ("how about Edinburgh", "Edinburgh"),
    ("let's try Camden", "Camden"),
    ("flats in Glasgow", "Glasgow"),
    ("places in Islington", "Islington"),
    ("can we search in Birmingham instead", "Birmingham"),
    ("I'd prefer Liverpool", "Liverpool"),
    # bare place names (message is essentially just a UK place)
    ("Manchester", "Manchester"),
    ("London", "London"),
    ("Glasgow", "Glasgow"),
    ("Shoreditch", "Shoreditch"),
    ("London please", "London"),
    ("Glasgow?", "Glasgow"),
    ("Notting Hill", "Notting Hill"),
    ("Milton Keynes", "Milton Keynes"),
])
def test_extract_area_english(text, expected):
    assert _extract_area(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("曼彻斯特", "Manchester"),
    ("曼城", "Manchester"),
    ("伦敦", "London"),
    ("利兹", "Leeds"),
    ("爱丁堡", "Edinburgh"),
    ("布里斯托", "Bristol"),
    ("布里斯托尔", "Bristol"),
    ("格拉斯哥", "Glasgow"),
    ("卡迪夫", "Cardiff"),
    ("伯明翰", "Birmingham"),
    # embedded in a sentence
    ("曼彻斯特的公寓", "Manchester"),
    ("我想搬到伦敦", "London"),
    ("爱丁堡怎么样", "Edinburgh"),
])
def test_extract_area_chinese(text, expected):
    assert _extract_area(text) == expected


@pytest.mark.parametrize("text", [
    "",
    None,
    "Mars",
    "move to Mars",
    "let's try Wakanda",
    "Moon",
    "火星",           # Mars
    "月球",           # Moon
    "瓦坎达",         # Wakanda
    "find me a flat",
    "under 1500",
    "I want a bath",          # 'bath' fixture must NOT map to the city Bath
    "does it have a bathroom",
    "what's my budget again",
])
def test_extract_area_negatives(text):
    assert _extract_area(text) is None


def test_extract_area_returns_none_for_nonsense_after_cue():
    # A switch cue followed by a non-UK/nonsense name must NOT invent an area.
    assert _extract_area("switch to Atlantis") is None
    assert _extract_area("make it Gotham") is None


# ──────────────────────── _extract_budget_clear ─────────────────────────────

@pytest.mark.parametrize("text", [
    "remove the budget",
    "no budget",
    "no budget limit",
    "no max budget",
    "forget the budget",
    "any price",
    "show all prices",
    "budget doesn't matter",
    "budget does not matter",
    "price doesn't matter",
    "price is not a concern",
    "money is no object",
    "don't care about the budget",
    "no price limit",
    "no spending limit",
    "without a budget",
    # Chinese
    "预算不限",
    "不限预算",
    "没有预算限制",
    "价格不限",
    "预算无所谓",
    "多少钱都行",
])
def test_extract_budget_clear_positive(text):
    assert _extract_budget_clear(text) is True


@pytest.mark.parametrize("text", [
    "",
    None,
    "budget is £1000",
    "my budget is now 1800",
    "under 1500",
    "up to 1200 a month",
    "budget of 2000",
    "预算1000",
    "月租1500",
    "max 900",
    "I have a budget",
    "what is the average budget in London",
])
def test_extract_budget_clear_negative(text):
    assert _extract_budget_clear(text) is False


# ───────────────────────── _strip_memory_block ──────────────────────────────

def test_strip_memory_block_removes_prepended_memory():
    injected = (
        "What I remember about this user: they searched Manchester with a "
        "£2000 budget for a 2-bed.\n\n"
        "Current user message: find me a flat"
    )
    assert _strip_memory_block(injected) == "find me a flat"


def test_strip_memory_block_passthrough_plain_query():
    # A plain current-turn message (no memory block) is returned unchanged.
    assert _strip_memory_block("find me a studio in Camden") == "find me a studio in Camden"


def test_strip_memory_block_handles_clarification_marker():
    q = "some context\nanswer to the clarification question: my budget is £1000"
    assert _strip_memory_block(q) == "my budget is £1000"


def test_bleed_hard_criteria_not_extracted_from_memory():
    # The core cross-conversation-bleed guarantee: a budget/area that appears ONLY
    # in the remembered memory block is NOT extractable from the stripped message.
    from core.tools.search_properties import _extract_budget, _extract_area as _ea
    injected = (
        "What I remember about this user: budget £2000, area Manchester, wants ensuite.\n\n"
        "Current user message: hi"
    )
    stripped = _strip_memory_block(injected)
    assert _extract_budget(stripped) == (None, None)
    assert _ea(stripped) is None
