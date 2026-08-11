import pytest

from core import loop_prompts
from core.search_readiness import (
    SEARCH_READINESS_SYSTEM_RULE,
    SEARCH_TOOL_DESCRIPTION,
    assess_search_readiness,
)
from core.tools.search_properties import search_properties_tool


_NEUTRAL_EN = [
    "find a place", "please search", "I need a flat", "show listings", "rent a room",
    "look for housing", "start a search", "find rentals", "I need housing", "search flats",
]
_NEUTRAL_ZH = [
    "帮我找房", "请搜索房源", "我需要公寓", "展示房源", "租一个房间",
    "寻找住房", "开始找房", "找出租房", "我需要住处", "搜索公寓",
]
_PROCEED_EN = [
    "continue", "go ahead", "search anyway", "just search", "search now",
    "proceed", "that's fine", "go on", "keep going", "it's fine",
]
_PROCEED_ZH = [
    "继续搜索", "继续搜", "继续找", "继续", "就这样吧",
    "直接搜索", "直接搜", "可以了", "都行", "先搜",
]


def _case(scenario, message):
    base = dict(
        resolved_area="Camden",
        max_budget=None,
        room_type=None,
        no_commute=False,
        commute_destination=None,
        criteria_gate_shown=False,
        confirmed=False,
        user_message=message,
        move_in_date=None,
    )
    expected = "ask_soft_once"
    if scenario == "missing_hard":
        base["resolved_area"] = None
        expected = "missing_hard"
    elif scenario == "complete":
        base.update(max_budget=1800, room_type="ensuite", commute_destination="UCL")
        expected = "ready"
    elif scenario == "gate_answered":
        base["criteria_gate_shown"] = True
        expected = "ready"
    elif scenario == "proceed":
        expected = "ready"
    elif scenario == "area_only":
        pass
    else:  # pragma: no cover - test-data authoring guard
        raise AssertionError(scenario)
    return scenario, base, expected


_CASES = []
for language_messages in (_NEUTRAL_EN, _NEUTRAL_ZH):
    for scenario in ("missing_hard", "area_only", "complete", "gate_answered"):
        _CASES.extend(_case(scenario, message) for message in language_messages)
for message in _PROCEED_EN + _PROCEED_ZH:
    _CASES.append(_case("proceed", message))


@pytest.mark.parametrize(
    "scenario,kwargs,expected", _CASES,
    ids=[f"{scenario}-{index}" for index, (scenario, _kw, _ex) in enumerate(_CASES)],
)
def test_readiness_matrix_100_bilingual_paraphrases(scenario, kwargs, expected):
    result = assess_search_readiness(**kwargs)
    assert result.status == expected
    assert result.should_search is (expected == "ready")
    if scenario == "missing_hard":
        assert result.missing_hard == ("area",)
    if scenario == "area_only":
        assert result.missing_soft == ("budget", "room_type", "commute")
    if scenario == "gate_answered":
        assert result.status == "ready", "a shown gate can never be repeated"


def test_prompt_and_tool_description_share_the_canonical_policy():
    assert loop_prompts.SEARCH_READINESS_RULE is SEARCH_READINESS_SYSTEM_RULE
    assert search_properties_tool.description == SEARCH_TOOL_DESCRIPTION
    directive = loop_prompts.build_system_directive("en")
    assert directive.count("SEARCH READINESS CONTRACT v1") == 1


def test_no_commute_satisfies_commute_recommendation():
    result = assess_search_readiness(
        resolved_area="Leeds",
        max_budget=1400,
        room_type="studio",
        no_commute=True,
        commute_destination=None,
        criteria_gate_shown=False,
        confirmed=False,
        user_message="I work from home",
    )
    assert result.status == "ready"
    assert "commute" not in result.missing_soft
