"""Regression tests (Y-xx, E-xx ids in each test name)."""
from __future__ import annotations

import json

import pytest

from core.listing_identity import bind_listing_links


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2500, "room_type": "whole"}


# Y-01 ── only a rent assertion takes the listing's rent
_CAMDEN_ROAD = [{"address": "Camden Road, Islington, London, N7", "price": "£1800/month",
                 "url": "https://www.onthemarket.com/details/12345678/"}]


@pytest.mark.parametrize("text", [
    "For a student, the cheapest option from Camden Road (N7) to Bank is a Zone 1-2 Monthly Travelcard at £119.90 a month.",
    "From Camden Road, Islington, N7 the Monthly Travelcard costs £119.90 per month.",
    "Camden Road, Islington, N7: the monthly transport cost is £119.90/month.",
    "Camden Road, Islington, N7: council tax is about £130/month.",
    "Camden Road: broadband adds £30 a month.",
    "From Camden Road to Bank the total comes to £1,920/month.",
    "Camden Road is £150 a month over.",
    "Camden Road 到 Bank 的月票是 £119.90/月。",
    "Camden Road：市政税约 £130/月。",
    "The Gallery at £1350/month is cheaper than Camden Road.",
])
def test_y_01_a_figure_that_is_no_rent_stays(text):
    assert bind_listing_links(text, _CAMDEN_ROAD) == text


@pytest.mark.parametrize("text,fixed", [
    ("Camden Road, Islington, N7 is £1750/month.", "Camden Road, Islington, N7 is £1800/month."),
    ("- Camden Road, Islington, London, N7 (£1750/month)", "- Camden Road, Islington, London, N7 (£1800/month)"),
    ("1. Camden Road, London, N7: 1 bed flat, £1750/month.", "1. Camden Road, London, N7: 1 bed flat, £1800/month."),
    ("Camden Road, N7 (Islington) at £1750/month.", "Camden Road, N7 (Islington) at £1800/month."),
    ("The Camden Road flat (the first one, £1750/month)", "The Camden Road flat (the first one, £1800/month)"),
    ("Camden Road is the cheapest at £1750/month.", "Camden Road is the cheapest at £1800/month."),
    ("[Camden Road](https://www.onthemarket.com/details/12345678/) — £1750/month.",
     "[Camden Road](https://www.onthemarket.com/details/12345678/) — £1800/month."),
    ("Camden Road: the rent is £1750/month.", "Camden Road: the rent is £1800/month."),
    ("Camden Road：月租£1750/月。", "Camden Road：月租£1800/月。"),
    ("Camden Road N7（£1750/月）", "Camden Road N7（£1800/月）"),
    ("Camden Road is listed at £1750 per month plus bills.", "Camden Road is listed at £1800 per month plus bills."),
    ("Camden Road is fine. The flat is £1750/month.", "Camden Road is fine. The flat is £1800/month."),
    ("Camden Road has the lowest rent (£1750/month).", "Camden Road has the lowest rent (£1800/month)."),
])
def test_y_01_a_wrong_rent_is_still_corrected(text, fixed):
    assert bind_listing_links(text, _CAMDEN_ROAD) == fixed


@pytest.mark.parametrize("text", [
    "Camden Road: your maximum rent is £2000.",
    "Camden Road: five weeks' rent of £2,076.",
    "Camden Road: the annual rent of £21,600 is due up front.",
    "Camden Road: the rent is £150 over your limit.",
    "Camden Road: the rent is £415 per week.",
])
def test_y_01_the_stated_rent_form_means_this_listings_monthly_rent(text):
    assert bind_listing_links(text, _CAMDEN_ROAD) == text


def test_y_01_a_rent_after_a_fare_is_corrected_in_the_same_form():
    out = bind_listing_links("Camden Road: Travelcard £119.90 a month; the rent is £1750.", _CAMDEN_ROAD)
    assert out == "Camden Road: Travelcard £119.90 a month; the rent is £1800."


# Y-02 ── a comparison covers both places and stores neither
def _search_params(message: str, accumulated: dict, **model) -> dict:
    """What the executor sends to the tool after this turn's state node."""
    import asyncio

    from langchain_core.messages import AIMessage

    from core.agent_loop import build_fc_nodes
    from tests.test_agent_contract_guards import _state, _tc
    from tests.test_qa_null_injection import _ValidatingProvider, _strict_null_args
    state = _state(message)
    state["accumulated_search_criteria"] = _state_node(accumulated, message)
    args = _strict_null_args("search_properties", {"user_query": message, **model})
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("search_properties", args, "c0")])]
    provider = _ValidatingProvider(["search_properties"])
    asyncio.run(build_fc_nodes(provider)["execute_tools"](state))
    (_, params), = provider.calls
    return params


@pytest.mark.parametrize("message,areas", [
    ("Now compare with Hackney, and check the weather there.", ["Camden", "Hackney"]),
    ("Compare with Hackney", ["Camden", "Hackney"]),
    ("compare it to Islington", ["Camden", "Islington"]),
    ("Compare Stratford with Bow for a 1-bed in terms of price", ["Stratford", "Bow"]),
    ("Compare Stratford and Bow", ["Stratford", "Bow"]),
    ("Stratford vs Bow", ["Stratford", "Bow"]),
    ("Stratford vs. Bow for a 1-bed", ["Stratford", "Bow"]),
    ("How does Camden compare to Hackney?", ["Camden", "Hackney"]),
    ("compare the rents in Camden, Hackney and Islington", ["Camden", "Hackney", "Islington"]),
    ("和哈克尼比较一下", ["Camden", "Hackney"]),
    ("比较一下哈克尼和伊斯灵顿", ["Hackney", "Islington"]),
    ("比较一下Hackney和Camden哪个便宜", ["Hackney", "Camden"]),
    ("哈克尼和伊斯灵顿哪个便宜", ["Hackney", "Islington"]),
    ("Hackney和Camden比一比", ["Hackney", "Camden"]),
])
def test_y_02_a_comparison_searches_both_and_stores_neither(message, areas):
    from core.tools.search_properties import comparison_areas
    assert comparison_areas(message, ["Camden"]) == areas
    stored = _state_node(_CAMDEN, message)
    assert (stored["area"], stored["areas"]) == ("Camden", ["Camden"])
    params = _search_params(message, _CAMDEN, area="Camden", areas=["Camden"])
    assert (params["area"], params["areas"]) == (areas[0], areas)


def test_y_02_a_first_turn_comparison_searches_both():
    params = _search_params("帮我比较一下哈克尼和伊斯灵顿，预算2000，两居室，不需要通勤", {}, area="Islington", areas=["Islington"])
    assert params["areas"] == ["Hackney", "Islington"]
    assert params["max_budget"] == 2000 and params["bedrooms"] == 2 and params["no_commute"] is True


@pytest.mark.parametrize("message", [
    "How did Camden compare to this?", "Compare listings 1 and 2",
    "Compare the cheapest from Bethnal Green with the cheapest in Angel you showed earlier.",
    "Don't compare with Hackney", "show me Hackney listings too", "Which is cheaper, the first or the second?",
])
def test_y_02_no_pair_of_places_is_no_comparison(message):
    from core.tools.search_properties import comparison_areas
    assert len(comparison_areas(message, ["Camden"])) < 2


def test_y_02_adding_an_area_stays_an_edit_of_the_stored_list():
    assert _state_node(_CAMDEN, "Add Hackney as well")["areas"] == ["Camden", "Hackney"]


def test_y_02_the_compared_rows_are_not_outside_the_area():
    from core.candidate_validation import validate_candidates
    from core.criteria_contract import criteria_with_compared_areas
    rows = [{"address": "1 Mare Street, London E8", "price": "£1900/month", "area": "Hackney", "bedrooms": 1,
             "property_type": "Flat", "url": "https://www.onthemarket.com/details/1/"}]
    criteria = dict(_CAMDEN, max_budget=None, bedrooms=None, room_type=None)
    assert validate_candidates(rows, criteria, [])["excluded"]
    compared = criteria_with_compared_areas(criteria, "Compare with Hackney")
    assert not validate_candidates(rows, compared, [])["excluded"]
    assert criteria["areas"] == ["Camden"]
    assert criteria_with_compared_areas(criteria, "Find a 1-bed")["areas"] == ["Camden"]


@pytest.mark.parametrize("message", [
    "Now compare with Islington, and check the weather there.", "比较一下卡姆登和伊斯灵顿", "Compare Camden with Islington",
])
def test_y_02_the_tool_keeps_both_places(monkeypatch, message):
    from tests import test_multi_area_and_recommender as multi
    multi._all_areas_classifier(monkeypatch)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    areas = ["Camden", "Islington"]
    geo = {"Camden": multi._CAMDEN_GEO, "Islington": multi._ISLINGTON_GEO}
    rows = {area: [multi._row(f"1 {area} Road", 1500, area, geo=geo[area])] for area in areas}
    monkeypatch.setattr(multi.on_demand, "get_listings", multi._fake_get_listings(rows))
    result = multi._run(area="Camden", areas=areas, current_message=message, no_commute=True, confirmed=True,
                        max_budget=3000, bedrooms=1)
    assert {row.get("area") for row in result["recommendations"]} == set(areas)
    assert result["search_criteria"]["areas"] == areas


# Y-03 ── a question about shown results moves nothing
_HACKNEY = {"area": "Hackney", "areas": ["Hackney"], "bedrooms": 1, "max_budget": 1900, "room_type": "whole"}


@pytest.mark.parametrize("message", [
    "Which of the listings you showed me in Bethnal Green was the cheapest?",
    "Which of the listings you've shown me in Bethnal Green was the cheapest?",
    "What's the first result you showed me in Angel? Give price and street.",
    "What was the rent of the Bethnal Green flat?", "Tell me about the listings you showed in Bow",
    "你之前给我看的 Bethnal Green 的房源里哪个最便宜？",
])
def test_y_03_a_question_about_shown_results_moves_nothing(message):
    assert _state_node(_HACKNEY, message)["areas"] == ["Hackney"]


@pytest.mark.parametrize("message,area", [
    ("Show me Bethnal Green again", "Bethnal Green"),
    ("Show me Bethnal Green listings again", "Bethnal Green"),
    ("Go back to Bethnal Green. Which was the cheapest and how far is it from Holborn?", "Bethnal Green"),
    ("Which of the listings you showed me was the cheapest? Now search in Bethnal Green.", "Bethnal Green"),
])
def test_y_03_a_request_to_look_there_still_moves(message, area):
    assert _state_node(_HACKNEY, message)["areas"] == [area]


def test_y_03_a_need_stated_beside_the_question_stays():
    stored = _state_node({}, "I need to commute to King's Cross, how long from the first area you showed?")
    assert stored["destination"] == "King's Cross"


def test_y_03_a_budget_in_a_later_sentence_still_counts():
    stored = _state_node(_HACKNEY, "Which of the listings you showed me in Bow was cheapest? Make the budget £2,000.")
    assert stored["max_budget"] == 2000 and stored["areas"] == ["Hackney"]


# Y-04 ── a studio has no stale bedroom count, and a restated count survives a clear
_TWO_BED = {"area": "Camden", "areas": ["Camden"], "bedrooms": 2, "max_budget": 2500, "room_type": "whole"}


@pytest.mark.parametrize("message", [
    "Retract the 2 beds; I want a studio.", "Retract the 2 beds; I want a studio instead.", "Cancel the 2 bedrooms; a studio please",
    "Remove the 2 beds, I want a studio", "I want a studio", "Make it a studio", "不要两居室，要studio", "改成单间公寓",
    "A studio instead of 2 beds", "2 beds is too much, a studio please",
])
def test_y_04_a_studio_drops_the_bedroom_count(message):
    stored = _state_node(_TWO_BED, message)
    assert stored["room_type"] == "studio" and stored["bedrooms"] == 0 and not stored.get("bedrooms_minimum")


def test_y_04_the_search_never_gets_a_studio_with_a_count():
    params = _search_params("Retract the 2 beds; I want a studio.", _TWO_BED)
    assert params["room_type"] == "studio" and not params.get("bedrooms")


@pytest.mark.parametrize("message", ["Let's say max 1 bed rather than studio.", "1 bed instead of a studio", "Make it 1 bed"])
def test_y_04_a_count_replaces_a_standing_studio(message):
    stored = _state_node(dict(_TWO_BED, room_type="studio", bedrooms=0), message)
    assert stored["bedrooms"] == 1 and stored["room_type"] != "studio"


@pytest.mark.parametrize("message,bedrooms", [
    ("把我的预算调回 1800，另外取消两间卧室的要求，改成一居室。", 1), ("取消两间卧室的要求，改成一居室", 1),
    ("Remove the bedrooms requirement, make it 1 bed", 1), ("取消两间卧室的要求", None), ("Remove the bedrooms filter", None),
])
def test_y_04_a_count_stated_with_the_clear_stays(message, bedrooms):
    assert _state_node(_TWO_BED, message)["bedrooms"] == bedrooms


# Y-05 ── a follow-up near a university moves the area; a change of destination does not
_NO_COMMUTE = dict(_CAMDEN, no_commute=True)


@pytest.mark.parametrize("message,area", [
    ("What about near the University of Edinburgh?", "Edinburgh"), ("near the University of Manchester", "Manchester"),
    ("What about near UCL?", "Bloomsbury"), ("How about near UCL?", "Bloomsbury"), ("And near UCL?", "Bloomsbury"),
    ("what about somewhere near the University of Manchester", "Manchester"),
])
def test_y_05_a_follow_up_near_a_university_moves_the_area(message, area):
    stored = _state_node(_CAMDEN, message)
    assert (stored["area"], stored["areas"]) == (area, [area])


@pytest.mark.parametrize("message", ["What about near the University of Edinburgh?", "What about near UCL?"])
def test_y_05_a_stated_no_commute_stays(message):
    stored = _state_node(_NO_COMMUTE, message)
    assert stored["no_commute"] is True and not stored.get("commute_destination") and not stored.get("destination")
    params = _search_params(message, _NO_COMMUTE)
    assert params["no_commute"] is True and not params.get("commute_destination")


def test_y_05_a_stated_commute_replaces_no_commute():
    stored = _state_node(_NO_COMMUTE, "Find a flat near UCL, I work at Canary Wharf")
    assert stored["no_commute"] is False and "Canary Wharf" in stored["commute_destination"]


def test_y_05_a_destination_left_in_another_city_goes():
    standing = dict(_CAMDEN, commute_destination="Imperial College London, South Kensington, London SW7 2AZ",
                    destination="Imperial College London, South Kensington, London SW7 2AZ")
    stored = _state_node(standing, "What about near the University of Edinburgh?")
    assert stored["area"] == "Edinburgh" and "London" not in str(stored.get("commute_destination"))


@pytest.mark.parametrize("message", [
    "How far is the first one from UCL?", "Is the flat near UCL?", "I study at UCL", "What is near UCL?",
    "Are there supermarkets near UCL?", "What about the University of Edinburgh?",
])
def test_y_05_a_mention_of_a_university_moves_nothing(message):
    assert _state_node(_CAMDEN, message)["area"] == "Camden"


_ISLINGTON = {"area": "Islington", "areas": ["Islington"], "bedrooms": 1, "max_budget": 2200, "room_type": "whole",
              "commute_destination": "Bank", "destination": "Bank"}


@pytest.mark.parametrize("message,destination", [
    ("如果换成去 Canary Wharf 呢？", "Canary Wharf, London E14 5AB"),
    ("如果通勤改去 Canary Wharf 呢", "Canary Wharf, London E14 5AB"),
    ("换成去 Bank 上班", "Bank"),
])
def test_y_05_zh_a_new_destination_keeps_the_area(message, destination):
    stored = _state_node(_ISLINGTON, message)
    assert stored["areas"] == ["Islington"] and stored["commute_destination"] == destination
    params = _search_params(message, _ISLINGTON)
    assert params["area"] == "Islington" and params["commute_destination"] == destination


@pytest.mark.parametrize("message,area", [("换成 Hackney", "Hackney"), ("改成 Hackney 吧", "Hackney"), ("换到 Hackney 呢", "Hackney")])
def test_y_05_zh_an_area_switch_stays_one(message, area):
    assert _state_node(_ISLINGTON, message)["areas"] == [area]


# Y-06 ── a recall reads what the queued memory jobs will write, and the stated budget wins
@pytest.fixture
def memory(tmp_path, monkeypatch):
    from rag import agent_memory as am_mod
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: '{"facts": []}')
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem"))


def _auto_row(memory, text, *, conversation="c1", turn="t1"):
    memory._turn_local.conversation_id, memory._turn_local.turn_id = conversation, turn
    try:
        return memory.add(text, "semantic", "u1", "u1", importance=7, extra_meta=memory._auto_row_meta())
    finally:
        memory._turn_local.conversation_id = memory._turn_local.turn_id = None


def _saved_texts(memory):
    return sorted(record["text"] for record in memory.semantic_records(user_id="u1"))


_UP = {"budget": {"from": 1800, "to": 2200}}
_DOWN = {"budget": {"from": 2200, "to": 1800}}


def test_y_06_the_row_comes_back_to_a_figure_it_held_before(memory):
    _auto_row(memory, "The user's budget is £1,800 a month.")
    assert memory._supersede_slot_facts(_UP, "u1") == 1
    assert _saved_texts(memory) == ["The user's budget is £2,200 a month."]
    assert memory._supersede_slot_facts(_DOWN, "u1") == 1
    assert _saved_texts(memory) == ["The user's budget is £1,800 a month."]


@pytest.mark.parametrize("row,pending", [
    ("The user's budget is £2,200 a month.", [_DOWN]),
    ("The user's budget is £1,800 a month.", [_UP, _DOWN]),
    ("The user's budget is £1,800 a month.", [_DOWN]),
])
def test_y_06_a_recall_reads_what_the_queued_jobs_will_write(memory, row, pending):
    from rag.agent_memory import with_pending_changes
    _auto_row(memory, row)
    rows = with_pending_changes(memory.semantic_records(user_id="u1"), pending)
    assert [record["text"] for record in rows] == ["The user's budget is £1,800 a month."]
    assert _saved_texts(memory) == [row]         # the job still does the writing


def test_y_06_an_explicit_row_is_not_rewritten(memory):
    from rag.agent_memory import with_pending_changes
    memory.add("The user's budget is £2,200 a month.", "semantic", "u1", "u1", importance=7,
               extra_meta={"source": "explicit_remember"})
    rows = with_pending_changes(memory.semantic_records(user_id="u1"), [_DOWN])
    assert [record["text"] for record in rows] == ["The user's budget is £2,200 a month."]


def test_y_06_retrieve_says_where_a_row_came_from(memory):
    from rag.agent_memory import with_pending_changes
    _auto_row(memory, "The user's budget is £2,200 a month.")
    rows = with_pending_changes(memory.retrieve("budget", session_id="u1", user_id="u1"), [_DOWN])
    assert "The user's budget is £1,800 a month." in [record["text"] for record in rows]


def test_y_06_the_store_lists_only_jobs_not_yet_stored(tmp_path):
    from uk_rent_agent.web.conversation_store import ConversationStore
    store = ConversationStore(str(tmp_path / "c.sqlite3"))
    conn = store._conn
    def job(status, change, result=None, user="u1", kind="memory_turn"):
        conn.execute(
            """INSERT INTO background_jobs (user_id, conversation_id, turn_id, kind, dedupe_key, payload_json,
               result_json, status, available_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user, "c", "t", kind, f"k{conn.total_changes}", json.dumps({"criteria_changes": change}),
             result, status, "2026", "2026", "2026"))
    job("done", {"area": {"from": ["A"], "to": ["B"]}})
    job("pending", _UP)
    job("running", {"bedrooms": {"from": 1, "to": 2}}, result='{"stored": true}')
    job("running", _DOWN)
    job("pending", _UP, user="u2")
    job("pending", _UP, kind="rolling_summary")
    job("pending", {})
    conn.commit()
    assert store.pending_memory_changes("u1") == [_UP, _DOWN]


@pytest.mark.parametrize("rows,criteria,expected", [
    ([{"mtype": "semantic", "text": "The user's budget is £2,200 a month."}], {"max_budget": 1800}, True),
    ([{"mtype": "semantic", "text": "The user's budget is £1,800 a month."}], {"max_budget": 1800}, False),
    ([{"mtype": "semantic", "text": "The user's budget is £2,200 a month."}], {}, False),
    ([{"mtype": "semantic", "text": "The user has a cat."}], {"max_budget": 1800}, False),
    ([{"mtype": "reflection", "text": "The budget may be £2,200."}], {"max_budget": 1800}, False),
])
def test_y_06_the_stated_budget_beats_a_saved_figure_in_a_recall(rows, criteria, expected):
    from rag.agent_memory import stated_budget_note
    note = stated_budget_note(rows, criteria)
    assert ("£1,800" in note) is expected


# Y-07 ── every way of asking for the whole list reads every saved fact
@pytest.mark.parametrize("message", [
    "List every single thing you have saved about me, numbered.",
    "List every single thing you have saved about me, numbered, don't skip any.",
    "List every single thing I asked you to remember, numbered.",
    "列出你保存的关于我的所有信息", "把你记住的全部内容告诉我", "What's every fact you have on me?",
    "Show me all the things you've stored about me",
])
def test_y_07_the_whole_list_is_a_recall(message):
    from app import _asks_what_is_remembered
    assert _asks_what_is_remembered(message)


@pytest.mark.parametrize("message", ["List every flat you found", "Show every listing in Camden", "Tell me everything about Camden"])
def test_y_07_a_list_of_listings_is_no_recall(message):
    from app import _asks_what_is_remembered
    assert not _asks_what_is_remembered(message)


def test_y_07_inferences_never_crowd_out_saved_facts(memory):
    from app import _with_all_saved_facts
    for index in range(17):
        _auto_row(memory, f"The user saved fact number {index}.")
    guesses = [{"id": f"r{i}", "text": f"The user may like option {i}.", "mtype": "reflection"} for i in range(6)]
    rows, total = _with_all_saved_facts(memory, "u1", guesses)
    assert total == 17
    block = memory.format_for_prompt(rows, saved_total=total)
    assert "all 17 saved facts are listed" in block
    assert block.count("[FACT]") == 17 and block.rindex("[FACT]") < block.index("[INFERENCE]")
