"""R3-07: a failed details lookup on an ordinal follow-up is reported as that lookup failing.

r05 rid 7dc47a4c (conv f66254df): "Does the first one allow pets, and what's the deposit?"
produced two malformed get_property_details calls; the fallback then said "I do not yet have
listing results ready to show" although seven listings were on screen. The malformed calls
are now logged by parse-error class, never by content.
"""
from __future__ import annotations

import json

from core.agent_loop import _artifact_grounded_fallback_answer, _invalid_call_diagnostics

REGISTRY = [{"index": 1, "address": "Powerscroft Road, Hackney, London, E5", "price": "£2250/month"},
            {"index": 2, "address": "Kingsland Road, London, E8", "price": "£2200/month"}]


def _state(message, lang="en", registry=REGISTRY):
    return {"user_query": message, "tool_artifacts": [],
            "extracted_context": {"current_message": message, "reply_language": lang,
                                  "recommended_registry": registry}}


def test_ordinal_follow_up_does_not_claim_there_are_no_listings():
    out = _artifact_grounded_fallback_answer(
        _state("Does the first one allow pets, and what's the deposit?"),
        reason="tool_call_error", failed_tools=["get_property_details"])
    assert "I do not yet have listing results" not in out
    assert "details lookup for that listing failed" in out
    assert "shown earlier are still available" in out


def test_chinese_ordinal_follow_up():
    out = _artifact_grounded_fallback_answer(
        _state("第一套可以养宠物吗？", lang="zh"), reason="tool_call_error",
        failed_tools=["get_property_details"])
    assert "目前还没有可以直接展示的房源结果" not in out
    assert "没能查到你问的那套房源的详情" in out


def test_tool_call_error_without_prior_listings_says_the_lookup_failed():
    out = _artifact_grounded_fallback_answer(
        _state("What's the weather in Leeds?", registry=[]), reason="tool_call_error",
        failed_tools=["get_weather"])
    assert "I do not yet have listing results" not in out
    assert "The lookup failed this turn" in out


def test_listing_turn_without_results_keeps_its_wording():
    out = _artifact_grounded_fallback_answer(_state("Find me a flat", registry=[]),
                                             reason="time_budget")
    assert "I do not yet have listing results ready to show." in out


def test_invalid_call_diagnostics_carry_the_error_class_not_the_content():
    secret = '{"property_url": "https://www.onthemarket.com/details/1615360/", "question": "pets my-dog-rex'
    try:
        json.loads(secret)
    except json.JSONDecodeError as exc:
        error = (f"Function get_property_details arguments:\n\n{secret}\n\nare not valid JSON. "
                 f"Received JSONDecodeError {exc}")
    diag = _invalid_call_diagnostics([{"name": "get_property_details", "args": secret, "error": error}])
    assert diag.startswith("get_property_details:Unterminated string:len=")
    assert ":obj=1:closed=0" in diag
    assert "onthemarket" not in diag and "rex" not in diag

