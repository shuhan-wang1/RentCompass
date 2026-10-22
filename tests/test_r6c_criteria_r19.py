"""r19 acceptance follow-ups in the criteria layer (r6c).

1. rid e54b194503864f449a87ab8abe9fbfcd (conv 2b707b3e): after a Hackney search, "I also
   work in Shoreditch and I cycle to work." The model called search_properties with area
   "Hackney" and commute_destination "Shoreditch"; the tool re-read "work in Shoreditch"
   from current_message as a location cue, searched Shoreditch and returned
   search_criteria.area "Shoreditch" while view_state kept Hackney. An explicit or already
   established home area now wins over a place the message introduces as a workplace; with
   no home area at all the workplace still centres the search (test_destination_in_message).
2. DeepSeek's recurrent malformed tool-call arguments (get_property_details in r03/r05/r06/
   r18/r19, search_properties in r18 c14fe21d): diag "Expecting value", object-shaped and
   closed, never "Extra data". The raw text is not recorded anywhere for those calls (logs
   keep the error class only, R3-07; the nudge retry drops the message before any
   checkpoint). The one raw payload on record, r10's compare_or_rank_areas call, has an
   unquoted value. Unquoted, single-quoted and Python-literal values in a flat object of
   known parameters are now repaired; anything ambiguous stays malformed.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.output_parsers.openai_tools import make_invalid_tool_call, parse_tool_call

from core import agent_loop
from core.agent_loop import _invalid_call_diagnostics, build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tool_args_repair import repair_invalid_tool_calls, repair_tool_arguments
from tests.test_agent_contract_guards import Chat, _state, _tc
from tests.test_hard_budget import _install_listings, _row, _run, stub_env  # noqa: F401
from tests.test_overnight_null_injection import _ValidatingProvider

# ── 1. a workplace never replaces the home area ─────────────────────────────────────────
R19_FIRST = "Find me a 1-bed in Hackney under £2,000."
R19_WORK = "I also work in Shoreditch and I cycle to work."
# The recorded search_properties arguments of rid e54b1945 (tool-dispatches.jsonl).
R19_ARGS = {
    "user_query": "1-bed flat in Hackney under £2000, commute to Shoreditch by bicycle",
    "area": "Hackney", "areas": ["Hackney"], "commute_destination": "Shoreditch",
    "no_commute": False, "bedrooms": 1, "bedrooms_minimum": False, "max_budget": 2000,
    "limit": 10, "current_message": R19_WORK, "accumulated_preferences": ["cycles to work in Shoreditch"],
    "budget_period": "month", "room_type": "whole", "confirmed": True, "criteria_gate_shown": True,
    "reply_language": "en", "property_features": [],
}


def test_r19_tool_keeps_the_requested_home_area(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("Jacob House, London, E8", 1950)])
    res = _run(**R19_ARGS)
    assert res["search_criteria"]["area"] == "Hackney"
    assert res["search_criteria"]["areas"] == ["Hackney"]
    assert res["search_criteria"]["commute_destination"] == "Shoreditch"


def test_with_no_home_area_the_workplace_still_centres_the_search(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("Old Street, London, EC1V", 1950)])
    args = {k: v for k, v in R19_ARGS.items() if k not in ("area", "areas", "commute_destination")}
    assert _run(**args)["search_criteria"]["area"] == "Shoreditch"


def test_a_real_area_switch_still_replaces_the_area(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("Upper Street, London, N1", 1950)])
    args = dict(R19_ARGS, current_message="Actually, search in Islington instead.")
    assert _run(**args)["search_criteria"]["area"] == "Islington"


@pytest.mark.parametrize("model_area", ["Hackney", None])
def test_fc_harness_keeps_the_established_area(model_area):
    acc = _apply_explicit_criteria_updates({}, R19_FIRST)
    state = _state(R19_WORK)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(acc, R19_WORK)
    args = {k: v for k, v in R19_ARGS.items() if k not in ("area", "areas")}
    if model_area:
        args.update(area=model_area, areas=[model_area])
    provider = _ValidatingProvider(["search_properties"])
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_name, params), = provider.calls
    assert params["area"] == "Hackney" and params["areas"] == ["Hackney"]
    assert params["commute_destination"] == "Shoreditch"


# ── 2. malformed arguments: repaired when unambiguous, refused otherwise ─────────────────
GPD_KEYS = {"property_url", "property_name", "property_address", "question"}
# r10 inst-D checkpoint (rid 6e02119f): the one raw malformed payload on record.
R10_RAW = ('{"city": "London", "destination": Camden Road, Islington N7, "max_commute_minutes": '
           'null, "budget_hint": null, "priorities": ["value"], "candidate_areas": null, '
           '"limit": 8, "reply_language": "en"}')
R10_KEYS = {"city", "destination", "max_commute_minutes", "budget_hint", "priorities",
            "candidate_areas", "limit", "reply_language"}


def test_the_recorded_unquoted_value_is_repaired():
    assert repair_tool_arguments(R10_RAW, R10_KEYS) == {
        "city": "London", "destination": "Camden Road, Islington N7", "max_commute_minutes": None,
        "budget_hint": None, "priorities": ["value"], "candidate_areas": None, "limit": 8,
        "reply_language": "en"}


# The r19 failures' shape (diag "Expecting value", len 266-276, obj=1, closed=1) in the three
# repairable forms; the listing is r19's own first result (Jacob House, E8).
_URL = "https://www.onthemarket.com/details/17009875/"
@pytest.mark.parametrize("raw, expected_address", [
    ('{"property_url": "%s", "property_name": \'Jacob House\', "property_address": '
     '\'Jacob House, London, E8\', "question": "Tell me more about the first one."}' % _URL,
     "Jacob House, London, E8"),
    ('{"property_url": "%s", "property_name": null, "property_address": Jacob House, London, E8, '
     '"question": "Tell me more about the first one."}' % _URL, "Jacob House, London, E8"),
    ('{"property_url": "%s", "property_name": None, "property_address": None, '
     '"question": "Tell me more about the first one."}' % _URL, None),
    ('{"property_url": null, "property_name": null, "property_address": King\'s Cross Road, N1, '
     '"question": null}', "King's Cross Road, N1"),
])
def test_unquoted_single_quoted_and_python_literal_values_are_repaired(raw, expected_address):
    args = repair_tool_arguments(raw, GPD_KEYS)
    assert args is not None and args["property_address"] == expected_address


@pytest.mark.parametrize("raw", [
    '{"property_url": null, "property_address": Jacob "House", "question": null}',   # a quote inside
    '{"property_url": null, "colour": blue, "question": null}',                      # unknown key
    '{"property_url": null, "property_name": {"a": b}, "question": null}',           # nested
    '{"property_url": null, "question": null} and more',                             # trailing text
    '{"property_url": null, "question": null',                                       # truncated
    '{"property_url": 12x, "question": null}',                                       # broken number
    '```json\n{"property_url": null}\n```',                                          # fenced
])
def test_ambiguous_arguments_are_never_repaired(raw):
    assert repair_tool_arguments(raw, GPD_KEYS) is None


def _invalid(name, raw, cid="call_bad"):
    try:
        parse_tool_call({"id": cid, "function": {"name": name, "arguments": raw}})
    except Exception as exc:          # exactly what langchain-openai records
        return make_invalid_tool_call({"id": cid, "function": {"name": name, "arguments": raw}}, str(exc))
    raise AssertionError("the payload parsed")


def test_a_repaired_call_runs_instead_of_the_nudge():
    raw = ('{"property_url": "%s", "property_name": null, "property_address": Jacob House, '
           'London, E8, "question": "Tell me more about the first one."}' % _URL)
    broken = AIMessage(content="", invalid_tool_calls=[_invalid("get_property_details", raw)])
    provider = _ValidatingProvider(["get_property_details"])
    nodes = build_fc_nodes(provider, agent_llm=Chat(broken))
    command = asyncio.run(nodes["agent"](_state("Tell me more about the first one.")))
    assert command.goto == "execute_tools"
    ai = command.update["messages"][-1]
    assert ai.invalid_tool_calls == []
    (call,) = ai.tool_calls
    assert call["id"] == "call_bad" and call["args"]["property_address"] == "Jacob House, London, E8"


def test_an_unrepairable_call_still_gets_the_corrective_nudge():
    raw = '{"property_url": null, "property_address": Jacob "House", "question": null}'
    broken = AIMessage(content="", invalid_tool_calls=[_invalid("get_property_details", raw)])
    nodes = build_fc_nodes(_ValidatingProvider(["get_property_details"]), agent_llm=Chat(broken))
    command = asyncio.run(nodes["agent"](_state("Tell me more about the first one.")))
    assert command.goto == "agent"
    assert "not valid JSON" in command.update["messages"][-1].content


def test_diagnostics_name_the_failing_parameter_without_its_content():
    raw = ('{"property_url": "%s", "property_name": null, "property_address": Jacob House, '
           'London, E8, "question": "Tell me more"}' % _URL)
    specs = _ValidatingProvider(["get_property_details"]).list_specs()
    diag = _invalid_call_diagnostics([_invalid("get_property_details", raw)], specs)
    assert diag.startswith("get_property_details:Expecting value:")
    assert diag.endswith(":at_key=property_address:at=alpha")
    assert "Jacob" not in diag and "onthemarket" not in diag


def test_search_properties_malformed_arguments_use_the_same_repair():
    raw = ('{"user_query": "1-bed in Bank", "area": None, "commute_destination": Bank, '
           '"max_commute_time": 30, "max_budget": 2000, "current_message": "I\'ve changed jobs, '
           'I work at Bank now.", "reply_language": "en"}')
    specs = _ValidatingProvider(["search_properties"]).list_specs()
    repaired, remaining = repair_invalid_tool_calls([_invalid("search_properties", raw)], specs)
    assert remaining == [] and repaired[0]["args"]["commute_destination"] == "Bank"
    assert repaired[0]["args"]["area"] is None and repaired[0]["args"]["max_budget"] == 2000


def test_the_details_schema_shows_no_quoted_examples():
    from core.tools.get_property_details import get_property_details_tool
    for name, prop in get_property_details_tool.parameters["properties"].items():
        assert "'" not in prop["description"] and '"' not in prop["description"], name
