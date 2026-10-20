"""The offline search_properties stub keeps the real tool's no-results contract.

Every ``status: no_results`` return in ``core.tools.search_properties`` carries the
``search_criteria`` it searched with and a ``message`` from ``_no_results_message`` that
names the area and each applied filter. On a listing request the product's no-results
path shows that ``message`` as the answer (R1-13). The old offline stub sent
``search_criteria: {}`` and only a ``summary``, so the offline FC smoke answered A1 ("Find
me a 2-bed flat in Camden under £1500") with "(offline stub) no listings". Its
``room_type_match`` text fallback then failed, and CI's Eval smoke went red.

These tests pin the stub to the real contract: it echoes only what the executor sent, it
builds the message with the production helper, and it invents no filter.
"""
from __future__ import annotations

from core.tools.search_properties import _no_results_message
from evaluation import run_benchmark as rb
from evaluation.metrics import graders

# The arguments fc_loop's executor sent for A1 offline (``_inject_search_params``).
A1_KWARGS = {
    "user_query": "Find me a 2-bed flat in Camden under £1500 a month. No commute to "
                  "worry about, so just go ahead and search.",
    "current_message": "Find me a 2-bed flat in Camden under £1500 a month. No commute "
                       "to worry about, so just go ahead and search.",
    "max_budget": 1500, "budget_period": "month", "bedrooms": 2, "bedrooms_minimum": False,
    "room_type": "whole", "area": "Camden", "areas": ["Camden"], "no_commute": True,
    "property_features": [], "idempotency_key": "0" * 64,
}


def test_a1_stub_echoes_the_executor_criteria_and_names_them():
    payload = rb._offline_canned_data("search_properties", A1_KWARGS)
    assert payload["status"] == "no_results" and payload["success"] is True
    assert payload["recommendations"] == [] and payload["partial"] is False
    crit = payload["search_criteria"]
    assert (crit["area"], crit["areas"], crit["max_budget"], crit["bedrooms"]) == (
        "Camden", ["Camden"], 1500, 2)
    assert crit["no_commute"] is True and crit["commute_destination"] is None
    # The message is the production builder's own output for these criteria.
    assert payload["message"] == _no_results_message("Camden", False, criteria=crit)
    for named in ("Camden", "£1,500", "2 bedrooms"):
        assert named in payload["message"]
    # So the grader's two readings of an empty search line up with the real tool.
    assert graders._search_result_is_empty(payload)
    assert graders._room_type_in_text("2-bed", payload["message"])


def test_stub_invents_no_filter_the_executor_did_not_send():
    payload = rb._offline_search_no_results(
        {"area": "Leeds", "current_message": "Show me flats in Leeds"})
    crit = payload["search_criteria"]
    assert crit["max_budget"] is None and crit["bedrooms"] is None
    assert crit["room_type"] is None and crit["max_travel_time"] is None
    assert "£" not in payload["message"] and "bedroom" not in payload["message"]
    assert "Leeds" in payload["message"]


def test_bedrooms_fall_back_to_the_production_extractor_like_the_real_tool():
    payload = rb._offline_search_no_results(
        {"area": "Leeds", "current_message": "Any 1-bed flats in Leeds?"})
    assert payload["search_criteria"]["bedrooms"] == 1
    assert "1 bedroom" in payload["message"]


def test_cjk_turn_gets_the_chinese_message():
    payload = rb._offline_search_no_results(
        {"area": "Manchester", "max_budget": 867, "room_type": "shared",
         "current_message": "曼彻斯特合租，预算每月867镑"})
    assert payload["message"].startswith("本次检索数据中，在 Manchester 附近")
    assert "£867" in payload["message"]


def test_odd_argument_shapes_never_crash_and_each_call_is_fresh():
    for kwargs in (None, {}, {"area": 7, "areas": "Camden", "bedrooms": True,
                              "property_features": "garden"}):
        payload = rb._offline_search_no_results(kwargs)
        assert payload["status"] == "no_results"
        assert payload["search_criteria"]["property_features"] == []
        assert "the requested area" in payload["message"]
    assert rb._offline_search_no_results({}) is not rb._offline_search_no_results({})


def test_other_tools_keep_their_fixed_canned_payload():
    assert rb._offline_canned_data("check_safety") is rb._CANNED_TOOL_DATA["check_safety"]
    assert rb._offline_canned_data("no_such_tool") == {"success": True, "stub": True}
    assert "search_properties" not in rb._CANNED_TOOL_DATA
