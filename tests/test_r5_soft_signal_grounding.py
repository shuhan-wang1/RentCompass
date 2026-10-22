"""Soft preferences and flags a search carries (round 5: R4-12 and r08 NEW-E).

R4-12 — r06 dadc016210614aae9f99b81112649cd0. In a new conversation the user said only
"What do you remember about me?" and "Find me something suitable then, budget £2,300, 1 bed".
The memory block held six [INFERENCE] rows (one: "pet-friendliness (cat) and area safety are
deal-breakers") and no saved fact about safety; the only safety mention anywhere was a
question, "Is the area safe?", asked once in ANOTHER conversation. The model's search call
carried ``accumulated_preferences ["pet friendly (cat)", "safe area"]`` and
``care_about_safety: true``. The cat is a saved fact ("I have a cat and work at Canary
Wharf"); the safety concern was an inference only, so it must not travel.

NEW-E — r08 45f4be4675b9451d8ae280d3c15acb27. "I use a wheelchair, I'm on Universal Credit
and my son Tom goes to Oakfield Primary. Find a step-free 1-bed in Camden under £1,800."
The search carried ``accumulated_preferences ["wheelchair user - step-free access needed",
"Universal Credit tenant", "child attending Oakfield Primary"]``, echoed in
``search_criteria.soft_preferences`` and the browser's ``view_state.criteria``. A sensitive
detail becomes the housing requirement it names (step-free) or nothing.

The model arguments below are the checkpointed tool calls of those turns, byte for byte.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage

from core import preference_grounding
from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.preference_grounding import (
    ground_soft_signals, preference_is_traceable, split_sensitive_preferences,
    stored_soft_preferences,
)
from tests.test_qa_null_injection import _ValidatingProvider
from tests.test_agent_contract_guards import _state, _tc


# ── r06 dadc0162: the real turn ───────────────────────────────────────────────────────
R06_MESSAGE = "Find me something suitable then, budget £2,300, 1 bed"
R06_MODEL_ARGS = {
    "user_query": "1 bedroom whole flat in Hackney, budget £2,300 per month, commute to Canary Wharf",
    "area": "Hackney", "areas": ["Hackney"], "location": None, "commute_destination": "Canary Wharf",
    "no_commute": False, "bedrooms": 1, "bedrooms_minimum": False, "max_budget": 2300,
    "monthly_total_budget": None, "office_days_per_week": None, "max_changes": None,
    "max_commute_time": None, "care_about_safety": True, "limit": 10,
    "current_message": R06_MESSAGE, "property_features": ["1 bed", "whole flat", "pet friendly"],
    "accumulated_preferences": ["pet friendly (cat)", "safe area"], "budget_period": "month",
    "min_budget": None, "room_type": "whole", "move_in_date": None, "confirmed": True,
    "criteria_gate_shown": False, "reply_language": "en",
}
R06_MEMORY_CONTEXT = (
    "The assistant's own inferences (NOT said or saved by the user; never apply them as search "
    "filters or preferences, never present them as things the user told you or asked to save):\n"
    "[INFERENCE] User's budget is flexible but anchored low: they repeatedly revise between £1,800 "
    "and £2,500, suggesting £1,800–£2,200 is the real comfort zone with some stretch for the right place.\n"
    "[INFERENCE] Location (Hackney) and property type (1-bed) are firm priorities, while budget is negotiable.\n"
    "[INFERENCE] User wants a 1-bedroom flat in Hackney with a firm budget ceiling around £2,200, having "
    "tightened the initial £2,500 limit.\n"
    "[INFERENCE] Hackney is likely chosen for commute practicality to Canary Wharf combined with budget "
    "fit, so they need a balance of affordability, safety, and pet-friendly housing over pure location prestige.\n"
    "[INFERENCE] Beyond price and location, practical livability factors dominate: pet-friendliness (cat) "
    "and area safety are deal-breakers, not nice-to-haves.\n"
    "[INFERENCE] Location (Hackney) and unit type (1-bed) are fixed priorities, while price is the main "
    "negotiable constraint.")
# The user's semantic rows at 03:52:17 (r06 runtime/agent_memory, mtype=semantic).
R06_SAVED_FACTS = ["I have a cat and work at Canary Wharf", "budget £2,300", "in Hackney", "1-bed"]
R06_RECALL_ANSWER = (
    "Here's what I have on file for you:\n\n- You're looking for a 1-bedroom place\n- In Hackney\n"
    "- With a budget under £1,800 a month\n- You have a cat\n- You work at Canary Wharf\n\n"
    "A note on how this works: ...")


def _r06_state():
    state = _state(R06_MESSAGE)
    state["user_id"] = "2f5816d76d4443208a5dfc597d332ea7"
    state["extracted_context"]["history"] = [
        {"user": "What do you remember about me?", "assistant": R06_RECALL_ANSWER}]
    state["memory_context"] = R06_MEMORY_CONTEXT
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(
        {"destination": None, "max_budget": None, "max_travel_time": None,
         "property_features": [], "soft_preferences": [], "amenities_of_interest": []},
        R06_MESSAGE)
    return state


def _execute(state, args):
    provider = _ValidatingProvider(["search_properties"])
    nodes = build_fc_nodes(provider)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    asyncio.run(nodes["execute_tools"](state))
    assert provider.rejected == []
    (_name, params), = provider.calls
    return params


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")


def test_r4_12_a_reflection_only_preference_and_flag_never_reach_the_search(monkeypatch):
    looked_up = []
    monkeypatch.setattr(preference_grounding, "saved_fact_texts",
                        lambda user_id: looked_up.append(user_id) or list(R06_SAVED_FACTS))
    params = _execute(_r06_state(), dict(R06_MODEL_ARGS))

    # The cat is a saved fact; "safe area" and the safety flag came from a reflection only.
    assert params.get("accumulated_preferences") == ["pet friendly (cat)"]
    assert "care_about_safety" not in params
    assert looked_up == ["2f5816d76d4443208a5dfc597d332ea7"]
    # The hard slots of this turn are untouched.
    assert params["max_budget"] == 2300 and params["bedrooms"] == 1 and params["area"] == "Hackney"


def test_r4_12_with_no_saved_fact_the_cat_preference_is_not_traceable_either(monkeypatch):
    monkeypatch.setattr(preference_grounding, "saved_fact_texts", lambda user_id: [])
    params = _execute(_r06_state(), dict(R06_MODEL_ARGS))
    assert "accumulated_preferences" not in params
    assert "care_about_safety" not in params


def test_r4_12_what_the_user_said_in_this_conversation_still_travels(monkeypatch):
    monkeypatch.setattr(preference_grounding, "saved_fact_texts",
                        lambda user_id: pytest.fail("conversation text was enough"))
    state = _r06_state()
    state["extracted_context"]["history"] = [
        {"user": "Is the area safe? I'd also like somewhere quiet, near a park.", "assistant": "..."}]
    params = _execute(state, dict(R06_MODEL_ARGS, accumulated_preferences=[
        "quiet street", "safe area", "near green space"]))
    assert params["accumulated_preferences"] == ["quiet street", "safe area", "near green space"]
    assert params["care_about_safety"] is True


def test_r4_12_the_memory_blocks_saved_facts_count_but_its_inferences_do_not():
    state = {"extracted_context": {"current_message": R06_MESSAGE},
             "memory_context": "What I remember about this user:\n[FACT] Wants a quiet street\n"
                               "[PAST] User asked: Is the area safe?\n" + R06_MEMORY_CONTEXT}
    params, dropped = ground_soft_signals(
        {"accumulated_preferences": ["quiet street", "safe area"], "care_about_safety": True},
        state, saved_facts=lambda user_id: [])
    assert params == {"accumulated_preferences": ["quiet street"]}
    assert dropped == ["accumulated_preferences", "care_about_safety"]


@pytest.mark.parametrize("preference, evidence, traceable", [
    ("quiet area", ["我想住安静的地方"], True),          # concept across languages
    ("near Tube station", ["离地铁站近一点"], True),
    ("safe area", ["Is the area safe?"], True),
    ("safe area", ["Find a 1-bed in Hackney"], False),
    ("family-friendly area", ["find a 2 bed in Hackney"], False),
    ("close to Victoria Park", ["near the park"], False),  # a specific park the user never named
    ("1 bedroom", ["1 bedroom"], False),                   # restates a hard slot, carries nothing
])
def test_r4_12_traceability_is_by_content(preference, evidence, traceable):
    assert preference_is_traceable(preference, evidence) is traceable


# ── r08 45f4be46: sensitive details ──────────────────────────────────────────────────
R08_MESSAGE = ("I use a wheelchair, I'm on Universal Credit and my son Tom goes to Oakfield Primary. "
               "Find a step-free 1-bed in Camden under £1,800.")
R08_PREFERENCES = ["wheelchair user - step-free access needed", "Universal Credit tenant",
                   "child attending Oakfield Primary"]
R08_MODEL_ARGS = {
    "user_query": "step-free 1-bed flat in Camden under £1,800 per month", "area": "Camden",
    "areas": ["Camden"], "location": None, "commute_destination": None, "no_commute": None,
    "bedrooms": 1, "bedrooms_minimum": False, "max_budget": 1800, "monthly_total_budget": None,
    "office_days_per_week": None, "max_changes": None, "max_commute_time": None,
    "care_about_safety": None, "limit": 20, "current_message": R08_MESSAGE,
    "property_features": ["step-free"], "accumulated_preferences": list(R08_PREFERENCES),
    "budget_period": "month", "min_budget": None, "room_type": "whole", "move_in_date": None,
    "confirmed": True, "criteria_gate_shown": False, "reply_language": "en",
}


def test_new_e_the_search_call_carries_the_requirement_not_the_identity(monkeypatch):
    monkeypatch.setattr(preference_grounding, "saved_fact_texts", lambda user_id: [])
    state = _state(R08_MESSAGE)
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates(
        {"destination": None, "max_budget": None, "max_travel_time": None,
         "property_features": [], "soft_preferences": [], "amenities_of_interest": []}, R08_MESSAGE)
    params = _execute(state, dict(R08_MODEL_ARGS))
    assert "accumulated_preferences" not in params
    assert params["property_features"] == ["step-free"]
    flat = repr({k: v for k, v in params.items() if k != "current_message"})
    for detail in ("wheelchair", "Universal Credit", "Oakfield", "child"):
        assert detail not in flat


def test_new_e_a_wheelchair_with_no_stated_feature_still_becomes_step_free():
    assert split_sensitive_preferences(["wheelchair user"]) == ([], ["step-free"])
    assert split_sensitive_preferences(R08_PREFERENCES) == ([], ["step-free"])


@pytest.mark.parametrize("preference, kept", [
    ("Universal Credit tenant", []),                 # benefit status as a label
    ("on housing benefit", []),                      # no 'benefits accepted' filter exists
    ("child attending Oakfield Primary", []),        # a child's school by name
    ("near Oakfield Primary", []),
    ("Skilled Worker visa holder", []),
    ("quiet street, earns £30k a year", ["quiet street"]),   # the housing clause survives
    ("near a good primary school", ["near a good primary school"]),   # a housing preference
    ("mould-free", ["mould-free"]),
])
def test_new_e_identity_details_never_become_preferences(preference, kept):
    assert split_sensitive_preferences([preference])[0] == kept


class _Store:
    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows)

    def search(self, query, top_k=10):
        return list(self.rows)


class _Coordinator:
    """No ML model: the RAG coordinator returns the stubbed rows as they are."""

    def __init__(self):
        self.property_store = _Store()

    def enhanced_search(self, query, criteria):
        for row in self.property_store.rows:
            row.setdefault("similarity_score", 0.6)
        return list(self.property_store.rows), [], []


def test_new_e_the_search_tool_echo_is_clean(monkeypatch):
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl, set_rag_coordinator
    import core.maps_service as maps

    row = {"Address": "Kentish Town Road, London, NW5", "Price": "£1750 pcm",
           "URL": "https://www.onthemarket.com/details/1750/", "geo_location": "51.54,-0.14",
           "Geo_Location": "51.54,-0.14", "Room_Type_Category": "1 bed Flat",
           "Description": "Bright flat near transport.", "Images": []}
    meta = {"slug": "camden", "requested_location": "Camden", "requested_city": "london",
            "source": "scraped", "stale": False, "count": 1, "elapsed_s": 0.01, "message": ""}
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": [dict(row)], "meta": meta})
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.54, "lng": -0.14})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")
    set_rag_coordinator(_Coordinator())
    try:
        result = asyncio.run(search_properties_impl(
            area="Camden", max_budget=1800, bedrooms=1, room_type="whole", confirmed=True,
            current_message=R08_MESSAGE, accumulated_preferences=list(R08_PREFERENCES),
            property_features=[], reply_language="en"))
    finally:
        set_rag_coordinator(None)
    for block in ("search_criteria", "known_criteria"):
        criteria = result.get(block) or {}
        assert criteria.get("soft_preferences") == []
        assert "step-free" in criteria.get("property_features")


def test_new_e_stored_criteria_and_view_state_hold_no_sensitive_preference(monkeypatch):
    import app as appmod
    from uk_rent_agent.web.session_store import SessionStore
    from uk_rent_agent.web.view_state import view_state

    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    user, conversation = "u" + uuid.uuid4().hex[:16], "c" + uuid.uuid4().hex[:16]
    criteria = {"area": "Camden", "max_budget": 1800, "property_features": ["step-free"],
                "soft_preferences": R08_PREFERENCES + ["mould-free due to asthma", "near a park"]}
    appmod._write_back_turn(user, conversation, R08_MESSAGE, "Noted.", [],
                            accumulated_search_criteria=criteria)
    session = appmod._get_session(user, conversation)
    stored = session.persistent_state["accumulated_search_criteria"]
    assert stored["soft_preferences"] == ["mould-free", "near a park"]
    assert stored["property_features"] == ["step-free"]
    shown = view_state(session.persistent_state)["criteria"]
    assert shown["soft_preferences"] == ["mould-free", "near a park"]
    assert stored_soft_preferences(R08_PREFERENCES) == []


def test_r4_12_the_tools_own_extraction_cannot_launder_the_models_query(monkeypatch):
    """With no area, the tool asks an LLM to extract criteria from the MODEL's user_query,
    which restated the inference ("safe, pet-friendly"); its preferences must trace to this
    turn's own message."""
    from core import llm_interface
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl, set_rag_coordinator
    import core.maps_service as maps

    monkeypatch.setattr(llm_interface, "clarify_and_extract_criteria", lambda text: {
        "destination": "Hackney", "soft_preferences": "want safe area, pet friendly"})
    row = {"Address": "Mare Street, London, E8", "Price": "£1950 pcm",
           "URL": "https://www.onthemarket.com/details/19970243/", "geo_location": "51.54,-0.05",
           "Geo_Location": "51.54,-0.05", "Room_Type_Category": "1 bed Flat",
           "Description": "Flat.", "Images": []}
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
            user_query="safe, pet-friendly 1 bedroom flat, budget £2,300", max_budget=2300,
            bedrooms=1, confirmed=True, current_message=R06_MESSAGE, reply_language="en"))
    finally:
        set_rag_coordinator(None)
    assert (result.get("search_criteria") or {}).get("soft_preferences") == []
