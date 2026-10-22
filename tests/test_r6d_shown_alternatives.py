"""Alternatives a search showed are held by the server too, apart from the matches (r6d).

r20 acceptance, rid 1419b01e (conv of live 2): "Find me a 1-bed in Hackney under £2,000."
returned one match (Jacob House, £1,950) and two over-budget alternatives (Arbutus Street
£2,200, 22-24 Powell Road £2,300) that the answer listed. view_results / last_results /
the recommended registry held only Jacob House, so "And the second one?" and "Tell me more
about Arbutus Street in Haggerston." found no record (the model declined: "only an excluded
alternative, no record") and £2,200 had no server-held source. The alternatives are now kept
as ``shown_alternatives``: marked as alternatives, never recommendations, never in the
panel's results, never criteria.
"""
from __future__ import annotations

import copy
import uuid

import app as appmod
from core.context_assembler import (
    build_turn_snapshot, render_shown_alternatives, shown_alternative_rows,
    snapshot_to_session_patch,
)
from core.langgraph_agent import _structured_search_tool_data
from core.loop_prompts import build_context_sections
from src.uk_rent_agent.agent.critic import enforce_no_evidence_numeric_contract
from uk_rent_agent.web.session_store import SessionStore
from uk_rent_agent.web.view_state import view_state

# The r20 search result of rid 4bf4433d (tool-dispatches.jsonl), trimmed to the row fields.
_ROW = {"area": "Hackney", "bedrooms": 1, "room_type": "whole", "source": "OnTheMarket (cached)"}
JACOB = dict(_ROW, address="Jacob House, London, E8", provider_address="Jacob House, London, E8",
             price="£1950/month", url="https://www.onthemarket.com/details/19970243/",
             property_type="1 bed Flat", match_type="perfect", budget_status="✅ Within budget",
             geo_location="51.554058, -0.06814",
             explanation="1 bedroom flat to rent in Jacob House, London, E8")
ARBUTUS = dict(_ROW, address="Arbutus Street, Haggerston, London, E8",
               provider_address="Arbutus Street, Haggerston, London, E8", price="£2200/month",
               url="https://www.onthemarket.com/details/19973895/", property_type="1 bed Flat",
               match_type="soft_violation", budget_status="⚠️ Over budget by £200",
               geo_location="51.539025, -0.075944", alternative=True,
               explanation="1 bedroom flat to rent in Arbutus Street, Haggerston, London, E8")
POWELL = dict(_ROW, address="22-24 Powell Road, London, London, London, E5",
              provider_address="22-24 Powell Road, London, London, London, E5", price="£2300/month",
              url="https://www.onthemarket.com/details/19731055/", property_type="1 bed Apartment",
              match_type="soft_violation", budget_status="⚠️ Over budget by £300",
              geo_location="51.556694, -0.056791", alternative=True,
              explanation="1 bedroom apartment to rent in 22-24 Powell Road, London, London, London, E5")
CRITERIA = {"area": "Hackney", "areas": ["Hackney"], "commute_destination": None, "no_commute": False,
            "bedrooms": 1, "bedrooms_minimum": False, "budget_period": "month", "room_type": "whole",
            "move_in_date": None, "destination": "Hackney", "max_budget": 2000.0,
            "max_travel_time": None, "property_features": [], "soft_preferences": []}
R20_PAYLOAD = {"success": True, "status": "found", "recommendations": [JACOB],
               "over_budget_alternatives": [ARBUTUS, POWELL], "search_criteria": CRITERIA}


def _r20_tool_data():
    _payload, tool_data = _structured_search_tool_data(copy.deepcopy(R20_PAYLOAD), {})
    return tool_data


def test_the_shown_alternatives_are_extracted_as_alternatives_only():
    tool_data = _r20_tool_data()
    assert [r["address"] for r in tool_data["recommendations"]] == [JACOB["address"]]
    rows = shown_alternative_rows(tool_data, tool_data["recommendations"])
    assert [(r["address"], r["price"]) for r in rows] == [
        (ARBUTUS["address"], "£2200/month"), (POWELL["address"], "£2300/month")]
    assert all(r["alternative"] is True and r["candidate_status"] == "excluded"
               and r["status_reason"] == "over_budget" for r in rows)
    assert rows[0]["url"] == ARBUTUS["url"]


def test_write_back_keeps_them_apart_from_matches_and_criteria(monkeypatch):
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    user, conv = "u" + uuid.uuid4().hex[:12], "c" + uuid.uuid4().hex[:12]
    tool_data = _r20_tool_data()
    recs = tool_data["recommendations"]
    appmod._write_back_turn(user, conv, "Find me a 1-bed in Hackney under £2,000.", "answer", recs,
                            accumulated_search_criteria=dict(CRITERIA),
                            shown_alternatives=shown_alternative_rows(tool_data, recs))
    session = appmod._get_session(user, conv)
    ec = session.persistent_state["extracted_context"]
    assert [r["address"] for r in ec["shown_alternatives"]] == [ARBUTUS["address"], POWELL["address"]]
    # Never matches: not in the results, the registry, the panel or the criteria.
    assert [r["address"] for r in session.last_results] == [JACOB["address"]]
    assert [r["address"] for r in ec["view_results"]] == [JACOB["address"]]
    assert [e["address"] for e in ec["recommended_registry"]] == [JACOB["address"]]
    assert session.persistent_state["accumulated_search_criteria"] == CRITERIA
    # The browser's panel still shows only the match; the snapshot keeps the alternatives
    # server-side so they survive a restart; a new search replaces them.
    snapshot = build_turn_snapshot(turn_id="t1", persistent_state=session.persistent_state)
    browser = view_state(snapshot)
    assert [r["address"] for r in browser["results"]] == [JACOB["address"]]
    assert "Arbutus" not in str(browser)
    assert snapshot_to_session_patch(snapshot)["shown_alternatives"] == ec["shown_alternatives"]
    # A turn that only re-narrates the shown set keeps them; a new search replaces them.
    appmod._write_back_turn(user, conv, "show me those again", "answer", recs)
    assert len(appmod._get_session(user, conv).persistent_state["extracted_context"]["shown_alternatives"]) == 2
    appmod._write_back_turn(user, conv, "Now Islington", "answer", [dict(JACOB, address="Upper St, N1")],
                            shown_alternatives=[])
    assert appmod._get_session(user, conv).persistent_state["extracted_context"]["shown_alternatives"] == []


def test_the_model_sees_their_urls_marked_as_not_matches():
    rows = shown_alternative_rows(_r20_tool_data(), [JACOB])
    block = build_context_sections(recommendations_index=[{"index": 1, **JACOB}], shown_alternatives=rows)
    assert "SHOWN ALTERNATIVES" in block and "not recommendations" in block
    line = next(l for l in block.splitlines() if l.startswith("Arbutus Street"))
    assert "price £2200/month" in line and "over budget" in line and ARBUTUS["url"] in line
    assert render_shown_alternatives([]) == ""


def test_the_alternatives_price_is_grounded_on_a_later_turn():
    answer = "Arbutus Street, Haggerston, London, E8 is listed at £2,200 a month."
    shown = [JACOB] + shown_alternative_rows(_r20_tool_data(), [JACOB])
    kept = enforce_no_evidence_numeric_contract(
        answer, retrieved_evidence=[], shown_listings=shown,
        user_message="Tell me more about Arbutus Street in Haggerston.")
    assert "£2,200" in kept
    removed = enforce_no_evidence_numeric_contract(
        answer, retrieved_evidence=[], shown_listings=[JACOB],
        user_message="Tell me more about Arbutus Street in Haggerston.")
    assert "£2,200" not in removed          # without the server-held row it had no source


def test_the_critic_node_grounds_the_alternatives_price(monkeypatch):
    """Through the real critic node: a details lookup that failed, no fresh evidence, and
    the answer quotes the alternative's price from the server-held rows."""
    import asyncio
    from core import llm_config
    import core.langgraph_agent as lga
    from uk_rent_agent.agent.state import create_initial_state

    answer = "Arbutus Street, Haggerston, London, E8 is listed at £2,200 a month."

    class _Same:
        async def ainvoke(self, _messages):
            return type("R", (), {"content": answer})()

    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: _Same())

    from core.context_assembler import render_listing_index

    def run(alternatives):
        state = create_initial_state("Tell me more about Arbutus Street in Haggerston.")
        registry = [{"index": 1, "address": JACOB["address"], "price": JACOB["price"],
                     "url": JACOB["url"], "area": "Hackney"}]
        # As app.py pre-renders it for the turn.
        state["extracted_context"].update(
            current_message="Tell me more about Arbutus Street in Haggerston.",
            view_results=[JACOB], recommended_registry=registry, shown_alternatives=alternatives,
            recommended_index=render_listing_index(registry, alternatives))
        state["tool_artifacts"] = [{"tool": "get_property_details", "turn": 0, "success": False,
                                    "raw_data": {"success": False, "found": False}}]
        state["final_response"] = answer
        update = asyncio.run(lga._make_critic_node()(state))
        return update.get("final_response", state["final_response"])   # unchanged when grounded

    assert "£2,200" in run(shown_alternative_rows(_r20_tool_data(), [JACOB]))
    assert "£2,200" not in run([])
