"""Area resolution and area-list edits (overnight round r01: R1-01, R1-02).

R1-01: "伦敦<neighbourhood>附近…" resolved to the bare city — 肖迪奇 / 国王十字 were not in
``_ZH_AREA_MAP`` and an English neighbourhood inside Chinese text ("伦敦Camden Town附近") was
never scanned — and the executor then OVERWROTE the model's area with "London", so listings
from Chigwell IG7 and Barking were shown under the "verified" header.

R1-02: "Add Islington as well." / "Drop Hackney, just Islington." / "Add Camden to the
search too." / "Remove Stratford." left the stored areas and the search arguments on the old
place while the reply claimed the change.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import (
    _extract_area, _extract_areas, area_list_edit, bare_city_downgrades,
)
from tests.test_agent_contract_guards import _state, _tc
from tests.test_overnight_null_injection import _ValidatingProvider, _strict_null_args


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")


def _dispatch(message, acc, model_args):
    state = _state(message)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(acc, message)
    provider = _ValidatingProvider(["search_properties"])
    nodes = build_fc_nodes(provider)
    args = _strict_null_args("search_properties", {"user_query": message, **model_args})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(nodes["execute_tools"](state))
    assert provider.rejected == []
    (_, params), = provider.calls
    return state["accumulated_search_criteria"], params


# ── R1-01 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message, expected", [
    ("伦敦肖迪奇附近的一居室，预算2200以内", "Shoreditch"),
    ("伦敦国王十字附近的一居室，月租2000以内", "King's Cross"),
    ("帮我在伦敦国王十字附近找一个两居室，预算2500以内", "King's Cross"),
    ("伦敦Camden Town附近两居室 3000以内", "Camden"),
    ("伦敦Shoreditch附近一居室", "Shoreditch"),
    ("伦敦哈克尼附近一居室", "Hackney"),
    ("在伦敦卡姆登租一居室", "Camden"),            # control: already right
    ("我想在伦敦找房", "London"),                  # a bare city stays the city
    ("曼彻斯特的公寓", "Manchester"),
])
def test_r1_01_chinese_neighbourhood_is_not_widened_to_the_city(message, expected):
    assert _extract_area(message) == expected
    acc = _apply_explicit_criteria_updates({}, message)
    assert acc.get("area") == expected


@pytest.mark.parametrize("message", [
    "我在金丝雀码头上班，想在伦敦找房",
    "我在Canary Wharf上班，想在伦敦找一居室",
    "通勤到金丝雀码头，在伦敦找房",
])
def test_r1_01_a_workplace_in_the_same_message_is_not_the_home_area(message):
    assert _extract_area(message) == "London"


def test_r1_01_chinese_area_list_with_trailing_predicate():
    assert _extract_areas("我想在哈克尼和伊斯灵顿找房") == ["Hackney", "Islington"]


def test_r1_01_university_landmark_does_not_invent_an_area():
    acc = _apply_explicit_criteria_updates(
        {}, "我想在伦敦大学学院UCL附近租房，预算每月2200镑以内，一居室")
    assert acc.get("area") is None
    assert "University College London" in str(acc.get("commute_destination"))


def test_r1_01_bare_city_never_replaces_a_finer_model_area():
    assert bare_city_downgrades("London", "Bloomsbury")
    assert bare_city_downgrades("London", "Shoreditch")
    assert not bare_city_downgrades("London", "London")
    assert not bare_city_downgrades("London", "Manchester")     # a switch, not a refinement
    assert not bare_city_downgrades("Manchester", "Didsbury")   # only London's sub-areas known
    assert not bare_city_downgrades("London", None)


def test_r1_01_executor_keeps_the_models_neighbourhood():
    _, params = _dispatch("伦敦UCL附近一居室", {}, {"area": "Bloomsbury"})
    assert params["area"] == "Bloomsbury"
    assert params["areas"] == ["Bloomsbury"]


def test_r1_01_executor_sends_the_neighbourhood_not_london():
    acc, params = _dispatch("伦敦肖迪奇附近的一居室，预算2200以内", {}, {"area": "London"})
    assert acc["area"] == "Shoreditch"
    assert params["area"] == "Shoreditch" and params["areas"] == ["Shoreditch"]


# ── R1-02 ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current, message, expected", [
    (["Hackney"], "Add Islington as well.", ["Hackney", "Islington"]),
    (["Hackney", "Islington"], "Drop Hackney, just Islington.", ["Islington"]),
    (["Hackney"], "Drop Hackney, just Islington.", ["Islington"]),
    (["Stratford"], "Add Camden to the search too.", ["Stratford", "Camden"]),
    (["Stratford", "Camden"], "Remove Stratford.", ["Camden"]),
    (["Stratford"], "Add Camden and remove Stratford.", ["Camden"]),
    (["Hackney"], "Also search in Islington.", ["Hackney", "Islington"]),
    (["Hackney"], "Only Islington please.", ["Islington"]),
    (["Hackney", "Islington"], "Remove Hackney from the search.", ["Islington"]),
    (["Hackney"], "Can you also include Dalston?", ["Hackney", "Dalston"]),
    (["Hackney"], "Add Angel as a second area", ["Hackney", "Angel"]),
    (["Hackney"], "把伊斯灵顿也加上", ["Hackney", "Islington"]),
    (["Hackney", "Islington"], "去掉哈克尼", ["Islington"]),
    (["Hackney", "Islington"], "不要哈克尼，只看伊斯灵顿", ["Islington"]),
    (["Stratford"], "Remove Stratford", []),
])
def test_r1_02_area_list_edits(current, message, expected):
    assert area_list_edit(message, current) == expected
    # Idempotent: the executor and the tool re-apply the same message to the result
    # (an emptied list has nothing left to edit, which callers read as "no edit").
    assert area_list_edit(message, expected) == (expected if expected else None)
    acc = _apply_explicit_criteria_updates(
        {"area": current[0], "areas": list(current), "max_budget": 2500}, message)
    assert acc.get("areas") == expected
    assert acc.get("area") == (expected[0] if expected else None)
    assert acc["max_budget"] == 2500


@pytest.mark.parametrize("message", [
    "Compare Stratford and Hackney for me.",
    "Is Islington safe?",
    "Does the rent include Hackney council tax?",
    "Add a park nearby as a preference.",
    "Remove the commute requirement.",
    "Don't add Islington.",
    "What if I add Islington?",
    "Drop the move-in date",
    "Search Islington instead.",          # a switch: the existing switch logic owns it
    "What's the weather like in Islington this week?",
])
def test_r1_02_non_edits_are_not_area_edits(message):
    assert area_list_edit(message, ["Hackney"]) is None


def test_r1_02_first_turn_negated_city_is_not_an_empty_list():
    assert area_list_edit("在Camden找，不要伦敦其他地方。整套一卧。", []) is None


def test_r1_02_switch_still_replaces():
    acc = _apply_explicit_criteria_updates({"area": "Hackney", "areas": ["Hackney"]},
                                           "Search Islington instead.")
    assert acc["areas"] == ["Islington"]


@pytest.mark.parametrize("acc, message, model_area, expected", [
    ({"area": "Hackney", "areas": ["Hackney"]}, "Add Islington as well.", "Islington",
     ["Hackney", "Islington"]),
    ({"area": "Hackney", "areas": ["Hackney", "Islington"]}, "Drop Hackney, just Islington.",
     "Hackney", ["Islington"]),
    ({"area": "Stratford", "areas": ["Stratford", "Camden"]}, "Remove Stratford.", "Stratford",
     ["Camden"]),
    ({"area": "Hackney", "areas": ["Hackney"]}, "Also search in Islington.", "Islington",
     ["Hackney", "Islington"]),
])
def test_r1_02_tool_arguments_follow_the_edited_state(acc, message, model_area, expected):
    state_acc, params = _dispatch(message, dict(acc, max_budget=2500), {"area": model_area})
    assert state_acc["areas"] == expected
    assert params["areas"] == expected
    assert params["area"] == expected[0]


def test_r1_02_tool_side_reapplication_is_stable():
    """The search tool re-reads this turn's text and would read an add as a switch on its
    own (``_extract_areas`` -> ["Islington"]); it applies the edit to the injected list
    instead, which is a no-op on the already-edited list."""
    from core.tools import search_properties as sp

    assert sp._extract_areas("Also search in Islington.") == ["Islington"]
    assert sp.area_list_edit("Also search in Islington.", ["Hackney", "Islington"]) == [
        "Hackney", "Islington"]
