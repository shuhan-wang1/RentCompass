"""A request for DSS / housing-benefit-accepting listings is answered honestly (r6-C2).

No listing source records whether a landlord accepts housing benefit or Universal Credit, so
round 5 maps benefit status to no requirement (NEW-E) and memory never stores it (R4-08).
Re-verified on 6cb064d with the real fc nodes: for "... where the landlord accepts DSS /
housing benefit" the state parser records nothing about it, a model preference "landlord
accepts housing benefit" is dropped, but a model feature "DSS accepted" travels to the search
as a requirement no listing can evidence and is echoed into the stored criteria, and no
deterministic text tells the user the search cannot check it: whether they hear it depends
on the model's prose.

Owner decision: no new filter, but say plainly that the search cannot filter for it and
suggest confirming with the agent or landlord, without echoing or storing benefit status.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage

import app as appmod
from core import preference_grounding
from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.preference_grounding import (
    apply_preference_policy, benefit_acceptance_feature, benefit_acceptance_requested,
    render_unsupported_requirement_notice,
)
from tests.test_agent_contract_guards import _state, _tc
from tests.test_qa_null_injection import _ValidatingProvider

DSS_REQUEST = "Find me a 1-bed in Hackney under £1,500 where the landlord accepts DSS / housing benefit."
# What a model plausibly sends for it: the requirement as a feature and as a preference.
MODEL_ARGS = {
    "user_query": "1-bed flat in Hackney under £1,500, landlord accepts DSS / housing benefit",
    "current_message": DSS_REQUEST, "area": "Hackney", "areas": ["Hackney"], "bedrooms": 1,
    "max_budget": 1500, "property_features": ["DSS accepted"],
    "accumulated_preferences": ["landlord accepts housing benefit"],
    "reply_language": "en",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")
    monkeypatch.setattr(preference_grounding, "saved_fact_texts", lambda user_id: [])


@pytest.mark.parametrize("message", [
    DSS_REQUEST,
    "Only show listings that accept Universal Credit please, 1-bed in Camden under £1,600",
    "Are any of these DSS friendly?",
    "Which of them take housing benefit?",
    "I need a landlord who is open to tenants on Universal Credit",
    "Find a studio, LHA accepted, in Croydon",
    "有接受住房补贴的房子吗",
])
def test_a_request_for_benefit_accepting_listings_is_recognised(message):
    assert benefit_acceptance_requested(message)


@pytest.mark.parametrize("message", [
    "I'm on Universal Credit and need a 1-bed in Camden",     # a statement, not a request
    "What are the benefits of living in Camden?",
    "I'm considering Camden and I'm on Universal Credit",
    "It doesn't need to accept DSS",
    "The landlord accepted my offer",
    "I work at UCL",
])
def test_other_mentions_are_not_a_request(message):
    assert not benefit_acceptance_requested(message)


def test_the_notice_names_the_limitation_and_never_the_user():
    en = render_unsupported_requirement_notice(DSS_REQUEST)
    assert "can't filter" in en and "housing benefit" in en and "DSS" in en
    assert "letting agent or landlord" in en
    for echo in ("you're on", "you are on", "your benefit", "you receive", "you claim"):
        assert echo not in en.lower()
    zh = render_unsupported_requirement_notice("有接受住房补贴的房子吗", language="zh")
    assert "无法" in zh and "中介或房东确认" in zh
    assert render_unsupported_requirement_notice("1-bed in Hackney under £1,500") == ""


def test_the_search_carries_no_benefit_requirement_and_nothing_is_stored():
    state = _state(DSS_REQUEST)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates({}, DSS_REQUEST)
    criteria_text = repr({k: v for k, v in state["accumulated_search_criteria"].items()
                          if k != "constraint_sources"})
    assert "DSS" not in criteria_text and "benefit" not in criteria_text.lower()
    provider = _ValidatingProvider(["search_properties"])
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", dict(MODEL_ARGS), "c0")])]
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    assert provider.rejected == []
    (_name, params), = provider.calls
    assert not params.get("property_features") and "accumulated_preferences" not in params
    flat = repr({k: v for k, v in params.items() if k not in ("current_message", "user_query")})
    assert "DSS" not in flat and "benefit" not in flat.lower()


def test_policy_and_tool_boundary_drop_benefit_features():
    assert [benefit_acceptance_feature(f) for f in
            ("DSS accepted", "housing benefit accepted", "Universal Credit OK", "lift",
             "step-free", "cats allowed", "furnished")] == [True, True, True, False, False, False, False]
    params, changed = apply_preference_policy({"property_features": ["DSS accepted", "lift"]})
    assert changed and params["property_features"] == ["lift"]


def test_the_tool_echo_holds_no_benefit_feature(monkeypatch):
    from tests.test_r5_soft_signal_grounding import _Coordinator
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl, set_rag_coordinator
    import core.maps_service as maps

    row = {"Address": "Mare Street, London, E8", "Price": "£1450 pcm",
           "URL": "https://www.onthemarket.com/details/1450/", "geo_location": "51.54,-0.05",
           "Geo_Location": "51.54,-0.05", "Room_Type_Category": "1 bed Flat",
           "Description": "Bright flat near transport.", "Images": []}
    meta = {"slug": "hackney", "requested_location": "Hackney", "requested_city": "london",
            "source": "scraped", "stale": False, "count": 1, "elapsed_s": 0.01, "message": ""}
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": [dict(row)], "meta": meta})
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.54, "lng": -0.05})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")
    set_rag_coordinator(_Coordinator())
    try:
        result = asyncio.run(search_properties_impl(
            area="Hackney", max_budget=1500, bedrooms=1, room_type="whole", confirmed=True,
            current_message=DSS_REQUEST, property_features=["DSS accepted"], reply_language="en"))
    finally:
        set_rag_coordinator(None)
    for block in ("search_criteria", "known_criteria"):
        criteria = result.get(block) or {}
        assert "DSS accepted" not in (criteria.get("property_features") or []), block


def _install_graph(monkeypatch, final_response, captured):
    """Stub only the GRAPH, so the real handle_with_react_agent post-processing runs."""
    class _FakeGraph:
        async def ainvoke(self, *a, **kw):
            return {"final_response": final_response, "response_type": "answer", "tool_data": {}}

        async def aget_state(self, *a, **kw):
            return None

    def _spy_write_back(user_id, conversation_id, user_message, assistant_text, *a, **kw):
        captured["persisted"] = assistant_text

    graph = _FakeGraph()
    setattr(graph, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", graph)
    monkeypatch.setattr(appmod, "_write_back_turn", _spy_write_back)
    monkeypatch.setattr(appmod, "_queue_background_job", lambda job: True)
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.mark.parametrize("arch", ["fc_loop", "legacy"])
def test_the_answer_says_plainly_that_the_search_cannot_filter_for_it(monkeypatch, arch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", arch)
    captured = {}
    # A model answer that silently skips the requirement, as nothing obliged it otherwise.
    _install_graph(monkeypatch, "Here are three 1-bed flats in Hackney under £1,500.", captured)
    appmod.app.config.update(TESTING=True)
    response = appmod.app.test_client().post(
        "/api/alex", json={"message": DSS_REQUEST}, headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert response.status_code == 200, response.get_data(as_text=True)
    message = response.get_json()["message"]
    assert message.startswith("Here are three 1-bed flats in Hackney under £1,500.")
    assert render_unsupported_requirement_notice(DSS_REQUEST) in message
    assert render_unsupported_requirement_notice(DSS_REQUEST) in captured["persisted"]


def test_no_notice_without_the_request(monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    captured = {}
    _install_graph(monkeypatch, "Here are three 1-bed flats in Hackney under £1,500.", captured)
    appmod.app.config.update(TESTING=True)
    response = appmod.app.test_client().post(
        "/api/alex", json={"message": "I'm on Universal Credit; find a 1-bed in Hackney under £1,500"},
        headers={"X-User-Id": "u" + uuid.uuid4().hex[:16]})
    assert response.get_json()["message"] == "Here are three 1-bed flats in Hackney under £1,500."
