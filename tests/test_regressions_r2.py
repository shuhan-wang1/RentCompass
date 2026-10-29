"""Regression tests (R2-xx ids in each test name)."""
from __future__ import annotations

import pytest

import app as appmod
from tests.test_alex_request_contract import _headers, _user, isolated_app  # noqa: F401
from uk_rent_agent.web.conversation_store import CANCELLED_TURN_TEXTS


# R2-01 ── a same-id retry after a disconnect re-runs over HTTP ──────────────────────
def _answering_agent(calls):
    async def fake_agent(user_message, context, is_continuation, user_id, conversation_id,
                         request_id, ui_language="en", turn=None):
        calls.append(user_message)
        appmod._write_back_turn(user_id, conversation_id, user_message, "fresh answer", [],
                                turn_id=(turn or {}).get("id"), reply_language="en")
        return {"response_type": "chat", "message": "fresh answer"}
    return fake_agent


@pytest.mark.parametrize("variant", ["cancelled", "interrupted"])
def test_r2_01_retry_of_a_turn_that_delivered_nothing_runs_again(isolated_app, monkeypatch,  # noqa: F811
                                                                  variant):
    client, store = isolated_app
    calls = []
    monkeypatch.setattr(appmod, "handle_with_react_agent", _answering_agent(calls))
    user = _user()
    first = store.start_request_turn(user, None, "rid-disc-1", "weather near the park?")
    if variant == "cancelled":
        store.finalize_request_turn(user, first["id"], status="failed", response_type="error",
                                    assistant_content=CANCELLED_TURN_TEXTS[0])
    else:
        store.fail_turn(user, first["id"])
    headers = _headers(user, "rid-disc-1")
    retry = client.post("/api/alex", json={"message": "weather near the park?"}, headers=headers)
    assert retry.status_code == 200, retry.get_json()
    assert "X-Idempotent-Replay" not in retry.headers
    assert retry.get_json()["message"] == "fresh answer"
    assert retry.get_json()["conversation_id"] == first["conversation_id"]
    # The answered retry now replays without running the agent again.
    again = client.post("/api/alex", json={"message": "weather near the park?"}, headers=headers)
    assert again.status_code == 200 and again.headers["X-Idempotent-Replay"] == "1"
    assert again.get_json()["message"] == "fresh answer"
    # The same id with a different message is still refused.
    other = client.post("/api/alex", json={"message": "find a flat"}, headers=headers)
    assert other.status_code == 409 and other.get_json()["code"] == "idempotency_key_reused"
    assert calls == ["weather near the park?"]


def test_r2_01_a_delivered_error_still_replays_over_http(isolated_app, monkeypatch):  # noqa: F811
    client, store = isolated_app
    calls = []
    monkeypatch.setattr(appmod, "handle_with_react_agent", _answering_agent(calls))
    user = _user()
    first = store.start_request_turn(user, None, "rid-err-1", "find a flat")
    store.finalize_request_turn(user, first["id"], status="failed", response_type="error",
                                assistant_content="The search service is unavailable right now.")
    replay = client.post("/api/alex", json={"message": "find a flat"},
                         headers=_headers(user, "rid-err-1"))
    assert replay.status_code == 502 and replay.headers["X-Idempotent-Replay"] == "1"
    assert calls == []


# R2-02 ── a new area in a composite message replaces the previous city ─────────────
_CITY_ACC = {
    "area": "Leeds", "areas": ["Leeds"], "bedrooms": 2, "max_budget": 1400, "budget_period": "month",
    "constraint_sources": {
        "area": {"source": "user_message", "text": "Find 2 bed flats in Leeds under £1400", "value": "Leeds"},
        "areas": {"source": "user_message", "text": "Find 2 bed flats in Leeds under £1400",
                  "value": ["Leeds"]},
    },
}
_COMPOSITE = ("Find a studio in Islington under £1400 and tell me the nearest supermarkets "
              "and the commute to Canary Wharf")


def test_r2_02_a_commute_target_is_not_a_coordinated_area():
    from core.tools.search_properties import _extract_area, _extract_areas
    assert _extract_area(_COMPOSITE) == "Islington"
    assert _extract_area("Find a flat in Camden and the journey to Bank") == "Camden"
    # Real alternatives still belong to the list extractor.
    assert _extract_area("Find flats in Hackney or Islington") is None
    assert _extract_areas("Find flats in Hackney or Islington") == ["Hackney", "Islington"]


def test_r2_02_the_search_runs_in_the_new_area():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import ground_hard_constraints
    acc = _apply_explicit_criteria_updates(dict(_CITY_ACC), _COMPOSITE, apply_relative_budget=False)
    assert acc["area"] == "Islington" and acc["areas"] == ["Islington"]
    params, dropped = ground_hard_constraints(
        {"area": "Islington", "areas": ["Islington"], "room_type": "studio", "max_budget": 1400},
        dict(_CITY_ACC), _COMPOSITE)
    assert params["area"] == "Islington" and params["areas"] == ["Islington"]
    assert "area" not in dropped and "areas" not in dropped


# R2-03 ── "forget my budget" is erased before the reply, and the reply says so ──────
class _CapturingGraph:
    def __init__(self, reply):
        self.reply, self.inputs = reply, []

    async def ainvoke(self, graph_input, config=None):
        self.inputs.append(graph_input)
        return {"final_response": self.reply, "response_type": "answer", "tool_data": {}}


def test_r2_03_forget_my_budget_is_erased_before_the_reply(tmp_path, monkeypatch):
    import asyncio
    import importlib
    am_mod = importlib.import_module("rag.agent_memory")
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    user = "u-r203-" + _user()
    memory.add("The user's budget is £1,450 a month", "semantic", user_id=user, importance=5)
    memory.add("Prefers a quiet street", "semantic", user_id=user, importance=5)
    monkeypatch.setattr(am_mod, "get_agent_memory", lambda *a, **k: memory)
    graph = _CapturingGraph("I can't delete saved memories. I can only add to them or read them "
                            "back, so your £1,450 budget is still stored. Give me a new budget "
                            "any time.")
    setattr(graph, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", graph)
    payload = asyncio.run(appmod.handle_with_react_agent(
        "Forget my budget.", {}, False, user_id=user, conversation_id="c-r203",
        request_id="req-r203", ui_language="en"))
    docs = memory.col.get(where={"user_id": user})["documents"]
    # Erased while the turn ran, so a new conversation cannot recall it.
    assert not any("1,450" in doc for doc in docs) and any("quiet" in doc for doc in docs)
    message = payload["message"]
    assert "can't delete" not in message and "still stored" not in message
    assert message.startswith("Done: I've deleted the budget you saved")
    assert "Give me a new budget any time." in message
    # The model read the erasure as runtime data, not a stale [FACT] line.
    seen = graph.inputs[0].get("memory_context") or graph.inputs[0].get("user_query") or ""
    assert "MEMORY ERASURE THIS TURN" in seen and "1,450" not in seen


def test_r2_03_acknowledgement_is_fixed_and_honest_about_a_partial_erasure():
    from core import memory_gate
    done = {"fields": ("budget",), "complete": True}
    zh = memory_gate.acknowledge_memory_erasure("我无法删除已保存的记忆。好的。", done, "zh")
    assert zh.startswith("已从我的记忆中删除你保存的预算") and "无法删除" not in zh
    partial = memory_gate.acknowledge_memory_erasure("OK.", {"fields": ("budget",), "complete": False}, "en")
    assert partial.startswith("I tried to delete the budget you saved")
    assert memory_gate.acknowledge_memory_erasure("OK.", {"fields": (), "complete": True}, "en") == "OK."
    once = memory_gate.acknowledge_memory_erasure("OK.", done, "en")
    assert memory_gate.acknowledge_memory_erasure(once, done, "en") == once


# R2-04 ── the idempotency store never keeps a remembered text ───────────────────────
def test_r2_04_a_remember_result_in_the_idempotency_store_carries_no_saved_text(tmp_path, monkeypatch):
    import asyncio
    import importlib
    import sqlite3
    from core.tools import memory_tools
    from uk_rent_agent.tools.idempotency import IdempotencyStore
    am_mod = importlib.import_module("rag.agent_memory")
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    monkeypatch.setattr(memory_tools, "_mem", lambda: memory)
    store = IdempotencyStore(tmp_path / "idempotency.sqlite3")
    monkeypatch.setattr(memory_tools, "memory_write_rejection", lambda *a, **k: None, raising=False)
    user = "u-r204-" + _user()

    def run():
        return asyncio.run(memory_tools.remember_tool.execute(
            content="I have a cat named Biscuit", user_id=user, idempotency_key="k-r204",
            _idempotency_store=store))

    first = run()
    assert first.success and first.data["stored"] == "I have a cat named Biscuit"
    with sqlite3.connect(tmp_path / "idempotency.sqlite3") as db:
        rows = db.execute("SELECT result, COALESCE(error, '') FROM tool_invocations").fetchall()
    assert rows and not any("Biscuit" in result + error for result, error in rows)
    # A replay reads the text back from memory ...
    replay = run()
    assert replay.success and replay.outcome == "replayed"
    assert replay.data["stored"] == "I have a cat named Biscuit"
    # ... and after an erasure it says nothing is stored instead of quoting it.
    memory.col.delete(ids=[first.data["id"]])
    gone = run()
    assert gone.success is False and gone.data["stored"] is None
    assert "deleted" in gone.data["error"]


# R2-05 ── a comparison with another place is not an area change ─────────────────────
@pytest.mark.parametrize("message", [
    "和Stratford比起来哪个更安全？", "跟Stratford比哪个更安全", "和斯特拉特福相比呢？",
    "Stratford和这里哪个更便宜？", "Which is safer, this or Stratford?",
])
def test_r2_05_a_comparison_keeps_the_search_area(message):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import _extract_area, _extract_areas
    assert _extract_area(message) is None and _extract_areas(message) == []
    acc = _apply_explicit_criteria_updates({"area": "Islington", "areas": ["Islington"]}, message)
    assert acc["area"] == "Islington" and acc["areas"] == ["Islington"]


@pytest.mark.parametrize("message,area", [
    ("Stratford instead", "Stratford"),
    ("在Stratford找房", "Stratford"),
])
def test_r2_05_an_explicit_switch_still_switches(message, area):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) == area


# R2-06 ── a borough word inside a street name is never rewritten ────────────────────
@pytest.mark.parametrize("address", [
    "Flat 2, 419-437, Hackney Road E2", "Unit 5, Islington Park Street N1",
    "Camden High Street, NW1 7JE",
])
def test_r2_06_a_street_named_after_a_borough_keeps_its_name(address):
    from core.tools.search_properties import reconcile_area_label
    assert reconcile_area_label(address) == address


def test_r2_06_a_wrong_borough_claim_is_still_corrected():
    from core.tools.search_properties import reconcile_area_label
    assert reconcile_area_label("Rugby House 6 Great Ormond Street, Islington WC1N") == \
        "Rugby House 6 Great Ormond Street, Camden WC1N"


# R2-07 ── a widened ceiling runs a new search instead of re-sorting the shown set ───
_SHOWN = [{"address": "1 Sample Road, Clapham SW4", "area": "Clapham", "price": "£2,400/month"},
          {"address": "2 Sample Road, Clapham SW4", "area": "Clapham", "price": "£2,100/month"},
          {"address": "3 Sample Road, Clapham SW4", "area": "Clapham", "price": "£2,450/month"}]


@pytest.mark.parametrize("message", [
    "Go back to the original £3,000 budget and show me again, sorted cheapest first",
    "Make the budget £3,000 and sort cheapest first",
    "Up to £3000 please, sorted cheapest first",
    "预算改回3000镑，按价格从低到高排序",
])
def test_r2_07_a_wider_ceiling_is_not_a_refinement(message):
    from core.refine_results import plan_refinement
    assert plan_refinement(message, _SHOWN) is None


def test_r2_07_a_narrower_ceiling_or_a_plain_sort_still_refines():
    from core.refine_results import plan_refinement
    spec, kept = plan_refinement("Only ones under £2,300, cheapest first", _SHOWN)
    assert [r["price"] for r in kept] == ["£2,100/month"]
    assert plan_refinement("sort by price", _SHOWN) is not None


# R2-08 ── retraction phrasings and "forget that requirement" ────────────────────────
@pytest.mark.parametrize("message", [
    "Never mind about the garden.", "Never mind the garden.", "The garden doesn't matter any more.",
    "Garden doesn't matter.", "Drop the garden.", "Skip the garden.",
])
def test_r2_08_a_retracted_echoed_preference_is_removed(message):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    start = {"area": "Hackney", "soft_preferences": ["garden", "Would like gym"]}
    result = _apply_explicit_criteria_updates(dict(start), message)
    assert result["soft_preferences"] == ["Would like gym"]
    assert result["constraint_sources"]["soft_preferences"]["value"] == ["Would like gym"]


def test_r2_08_a_retracted_preference_cannot_be_echoed_back():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import ground_hard_constraints
    acc = _apply_explicit_criteria_updates({"area": "Hackney", "soft_preferences": ["garden"]},
                                           "Never mind about the garden.")
    params, _ = ground_hard_constraints({"area": "Hackney", "accumulated_preferences": ["garden"]},
                                        acc, "Search again.")
    assert "garden" not in (params.get("accumulated_preferences") or [])


@pytest.mark.parametrize("message", [
    "Forget that requirement.", "forget that requirement", "Never mind that.", "Actually, drop that one.",
    "That doesn't matter any more.", "算了",
])
def test_r2_08_forget_that_requirement_withdraws_what_the_last_message_added(message):
    from core.langgraph_agent import retract_previous_requirement
    start = {"area": "Hackney", "soft_preferences": ["Would like balcony", "Would like gym"],
             "property_features": ["lift"]}
    result = retract_previous_requirement(dict(start), message, "I'd also like to be near a gym.")
    assert result["soft_preferences"] == ["Would like balcony"]
    assert result["property_features"] == ["lift"]


def test_r2_08_a_bare_retraction_with_nothing_named_changes_nothing():
    from core.langgraph_agent import retract_previous_requirement
    start = {"area": "Hackney", "soft_preferences": ["Would like gym"]}
    assert retract_previous_requirement(start, "Forget that requirement.", "What is the weather?") is start
    assert retract_previous_requirement(start, "Forget the weather, find flats", "I want a gym.") is start


def test_r2_08_the_state_node_reads_the_previous_message():
    from core.langgraph_agent import _make_extract_preferences_node
    node = _make_extract_preferences_node()
    state = {"user_preferences": {}, "accumulated_search_criteria": {
                 "area": "Hackney", "soft_preferences": ["garden"]},
             "extracted_context": {"current_message": "Forget that requirement.",
                                   "history": [{"user": "I want a garden.", "assistant": "Noted."}]}}
    update = node(state)
    assert update["accumulated_search_criteria"]["soft_preferences"] == []


# R2-11 ── a zh street named after a city is not that city ───────────────────────────
@pytest.mark.parametrize("message", ["牛津街附近的地铁线路有哪些？", "牛津街附近有什么超市", "剑桥路附近的公寓"])
def test_r2_11_a_street_named_after_a_city_keeps_the_search_area(message):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) is None
    assert _apply_explicit_criteria_updates({"area": "Hackney", "areas": ["Hackney"]}, message)["area"] == "Hackney"


def test_r2_11_an_explicit_new_city_wins():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import _extract_area
    assert _extract_area("我想在牛津找房") == "Oxford"
    acc = _apply_explicit_criteria_updates({"area": "Hackney", "areas": ["Hackney"]},
                                           "我想在加的夫找房子，预算每月1000镑")
    assert acc["area"] == "Cardiff" and acc["max_budget"] == 1000


# R2-12 ── "back to the area I had before X" resolves from the conversation ──────────
_AREA_HISTORY = [{"user": "Find a 1-bedroom in Hackney under £2,000 a month", "assistant": "..."},
                 {"user": "I want a garden.", "assistant": "..."},
                 {"user": "How about Stratford instead?", "assistant": "..."}]


@pytest.mark.parametrize("message", [
    "Actually back to the area I had before Stratford.", "Go back to the previous area.",
    "Back to the earlier area please.", "Switch back to the original area.", "回到之前的区域",
    "回到Stratford之前的区域",
])
def test_r2_12_back_to_the_previous_area(message):
    from core.langgraph_agent import _make_extract_preferences_node
    node = _make_extract_preferences_node()
    state = {"user_preferences": {},
             "accumulated_search_criteria": {"area": "Stratford", "areas": ["Stratford"], "max_budget": 2000},
             "extracted_context": {"current_message": message, "history": list(_AREA_HISTORY)}}
    acc = node(state)["accumulated_search_criteria"]
    assert acc["area"] == "Hackney" and acc["areas"] == ["Hackney"] and acc["max_budget"] == 2000
    assert acc["constraint_sources"]["area"]["value"] == "Hackney"


def test_r2_12_without_an_earlier_area_nothing_changes():
    from core.langgraph_agent import resolve_previous_area
    acc = {"area": "Stratford", "areas": ["Stratford"]}
    assert resolve_previous_area(acc, "Go back to the previous area.",
                                 [{"user": "Find flats in Stratford"}]) is acc
    assert resolve_previous_area(acc, "Tell me about the area.", _AREA_HISTORY) is acc


# R2-10 ── a London commute destination does not follow a switch to another city ─────
_LONDON_COMMUTE = {
    "commute_destination": "Imperial College London, South Kensington, London SW7 2AZ",
    "destination": "Imperial College London, South Kensington, London SW7 2AZ",
    "constraint_sources": {"commute_destination": {
        "source": "user_message", "text": "commute from my flat to Imperial College London",
        "value": "Imperial College London, South Kensington, London SW7 2AZ"}},
}


def test_r2_10_a_new_city_search_drops_a_london_commute_destination():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import ground_hard_constraints
    message = "Find 1 bed in Manchester under £1500"
    acc = _apply_explicit_criteria_updates(dict(_LONDON_COMMUTE), message)
    assert acc["area"] == "Manchester" and not acc.get("commute_destination") and not acc.get("destination")
    params, _ = ground_hard_constraints(
        {"area": "Manchester", "commute_destination": _LONDON_COMMUTE["commute_destination"]},
        dict(_LONDON_COMMUTE), message)
    assert "commute_destination" not in params


@pytest.mark.parametrize("message,area", [
    ("Find 1 bed flats in Hackney under £1500", "Hackney"),
    ("Find flats in London under £1500", "London"),
])
def test_r2_10_a_london_search_keeps_the_destination(message, area):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    acc = _apply_explicit_criteria_updates(dict(_LONDON_COMMUTE), message)
    assert acc["area"] == area
    assert acc["commute_destination"] == _LONDON_COMMUTE["commute_destination"]


# R2-14 ── a destination name never keeps the sentence's full stop ───────────────────
def test_r2_14_the_destination_name_drops_the_trailing_full_stop():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.scraping.on_demand import extract_destination_from_text
    message = "Remember that I work as a nurse at St Thomas' Hospital."
    place = extract_destination_from_text(message)
    assert place["name"] == "St Thomas' Hospital" and not place["address"].endswith(".")
    assert _apply_explicit_criteria_updates({}, message)["commute_destination"] == "St Thomas' Hospital"
