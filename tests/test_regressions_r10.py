"""Regression tests (G-xx ids in each test name)."""
from __future__ import annotations

import json

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


# G-01 ── a weekly rent is weekly however it is written
@pytest.mark.parametrize("text,expected", [
    ("Find me a 1-bed in Hackney, £450pw", (450, "week")),
    ("£600pw max in Camden", (600, "week")),
    ("£450 pcw", (450, "week")),
    ("450 pcw", (450, "week")),
    ("max £300 p.w.", (300, "week")),
    ("max £300 p.w. Looking in Camden.", (300, "week")),
    ("£300 p.c.w. in Hackney", (300, "week")),
    ("£350pw all bills", (350, "week")),
    ("£120 per wk", (120, "week")),
    ("£120 a wk", (120, "week")),
    ("£120 weekly", (120, "week")),
    ("450 weekly", (450, "week")),
    ("my weekly budget is £300", (300, "week")),
    ("£450 p/w", (450, "week")),
    ("£400 per week", (400, "week")),
    ("每周£350", (350, "week")),
    ("350镑/周", (350, "week")),
])
def test_g_01_a_weekly_figure_reads_weekly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("£1800 p.c.m.", (1800, "month")),
    ("£1,800pcm", (1800, "month")),
    ("£1500/month", (1500, "month")),
    ("1500 pm", (1500, "month")),
    ("under 1800 pcm", (1800, "month")),
    ("budget £1,800 pcm, gym once a week", (1800, "month")),
    ("budget £1500 and I go to the gym weekly", (1500, "month")),
    ("budget £1500, gym twice a week", (1500, "month")),
    ("£2000 a month, three days a week in the office", (2000, "month")),
    ("预算每月1500，我每周去两次健身房", (1500, "month")),
])
def test_g_01_a_monthly_figure_stays_monthly(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


def test_g_01_a_weekly_correction_moves_the_ceiling():
    after = _state_node({"area": "Hackney", "areas": ["Hackney"], "max_budget": 1500, "budget_period": "month"},
                        "Actually I can stretch to £480pw.")
    assert after["max_budget"] == 2080


@pytest.mark.parametrize("text,expected", [
    ("Actually I can stretch to £2,000.", (2000, "month")),
    ("I can stretch to £480pw", (480, "week")),
    # A stretch beside a ceiling, or with a condition, stays a note.
    ("My budget is £1,900, but I can stretch to £2,100.", (1900, "month")),
    ("I can stretch to £2,000 if needed", (None, None)),
    ("I could stretch to £2,000", (None, None)),
])
def test_g_01_a_plain_can_stretch_is_the_new_ceiling(text, expected):
    from core.tools.search_properties import _extract_budget
    assert _extract_budget(text) == expected


@pytest.mark.parametrize("text", ["raise it by £50pw", "raise it by £50 p.w.", "raise it by £50 per wk"])
def test_g_01_a_weekly_delta_is_converted(text):
    from core.search_readiness import detect_relative_budget_delta
    assert detect_relative_budget_delta(text) == pytest.approx(216.67, abs=0.01)


# G-02 ── a withdrawal with a replacement in one message
_STRATFORD = {"area": "Stratford", "areas": ["Stratford"], "bedrooms": 1, "max_budget": 2000,
              "no_commute": True, "room_type": "whole", "budget_period": "month"}


@pytest.mark.parametrize("text", [
    "Actually I changed my mind, forget Stratford, use Poplar",
    "Forget about Stratford and search Poplar",
    "Never mind Stratford, Poplar please",
    "Never mind Stratford, how about Poplar?",
    "Scrap Stratford, use Poplar instead",
    "Forget Stratford. Use Poplar.",
    "Use Poplar, forget Stratford",
    "Ignore Stratford, use Poplar",
    "Disregard Stratford, search Poplar.",
    "忘掉Stratford，用Poplar",
    "不要Stratford了，搜Poplar",
])
def test_g_02_the_replacement_takes_over(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert (after["area"], after["areas"]) == ("Poplar", ["Poplar"])
    assert after["max_budget"] == 2000


@pytest.mark.parametrize("text", [
    "Remove Stratford", "Never mind Stratford", "Scrap Stratford",
    "Remove Stratford, Canary Wharf is too far",
    "Remove Stratford and Hackney",
])
def test_g_02_a_removal_with_no_replacement_leaves_no_area(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert (after["area"], after["areas"]) == (None, [])


def test_g_02_one_of_two_areas_is_replaced():
    from core.tools.search_properties import area_list_edit
    assert area_list_edit("Forget Stratford, use Poplar", ["Stratford", "Hackney"]) == ["Hackney", "Poplar"]
    assert area_list_edit("Forget Stratford, use Poplar", ["Poplar"]) == ["Poplar"]     # idempotent


@pytest.mark.parametrize("text", ["Search Poplar", "Try Poplar", "Poplar please"])
def test_g_02_a_place_without_a_removal_is_no_list_edit(text):
    from core.tools.search_properties import area_list_edit
    assert area_list_edit(text, ["Stratford"]) is None


def test_g_02_a_budget_named_to_be_replaced_is_no_reference_to_it():
    from core.constraint_updates import budget_reference
    assert budget_reference("Forget my old budget, use £2,300 instead") is None
    assert budget_reference("Go back to my old budget") == "previous"
    assert budget_reference("Use the original budget") == "first"


def test_g_02_the_new_budget_holds_through_the_state_node():
    """The erasure pops the budget's history first: "previous" then had no target and undid the
    turn, so view_state held no budget while the search and the reply used £2,300."""
    before = {"area": "Bow", "areas": ["Bow"], "bedrooms": 1, "max_budget": 1700, "budget_period": "month",
              "constraint_history": {"area": ["Bow"], "bedrooms": [1]},
              "_last_change": {"before": {"area": None, "areas": None, "bedrooms": None, "max_budget": None}}}
    after = _state_node(before, "Forget my old budget, use £2,300 instead")
    assert after["max_budget"] == 2300
    assert after["constraint_sources"]["max_budget"]["source"] == "user_message"


def test_g_02_an_erased_budget_stays_out_of_the_records():
    before = {"area": "Bow", "areas": ["Bow"], "max_budget": 1700, "budget_period": "month",
              "_erased_fields": ["budget"], "constraint_history": {"area": ["Bow"]},
              "_last_change": {"before": {"area": None, "areas": None, "max_budget": None}}}
    after = _state_node(before, "Forget my old budget, use £2,300 instead")
    assert after["constraint_history"]["max_budget"] == [2300]
    assert after["_last_change"]["before"]["max_budget"] is None
    # A later change keeps its own history as usual.
    later = _state_node(after, "Actually make it £2,500")
    assert later["constraint_history"]["max_budget"] == [2300, 2500]
    assert later["_last_change"]["before"]["max_budget"] == 2300


def test_g_02_a_figure_beside_a_previous_budget_wins():
    from core.langgraph_agent import undo_last_change
    before = {"max_budget": 1700, "_last_change": {"before": {"max_budget": 1500}}}
    after = {"max_budget": 1900}
    assert undo_last_change(before, after, "Back to my previous budget, £1,900")["max_budget"] == 1900
    assert undo_last_change(before, dict(before), "Back to my previous budget")["max_budget"] == 1500


# G-03 ── a change verb moves the search, it does not filter the cards
_POPLAR_IN_BOW = [
    {"address": "Aegean Court, Poplar, E3", "price": "£1,800 pcm", "area": "bow", "bedrooms": 1, "url": "https://x/1"},
    {"address": "Bow Road, London E3", "price": "£1,700 pcm", "area": "bow", "bedrooms": 1, "url": "https://x/2"},
]


@pytest.mark.parametrize("text", [
    "Change the area to Poplar", "Switch to Poplar", "Move the search to Poplar", "Go back to Poplar",
    "Make it Poplar", "换到Poplar", "改成Poplar",
])
def test_g_03_a_change_of_area_is_no_subset(text):
    from core.refine_results import parse_refinement
    assert parse_refinement(text, _POPLAR_IN_BOW) is None


@pytest.mark.parametrize("text", ["Only the ones in Poplar", "Just the ones in Poplar"])
def test_g_03_a_real_subset_still_filters(text):
    from core.refine_results import parse_refinement
    spec = parse_refinement(text, _POPLAR_IN_BOW)
    assert spec and spec["filters"][0]["kind"] == "area"


# G-04 ── a rent bound is the budget however it is worded
@pytest.mark.parametrize("row,rest", [
    ("User asked: Find me a 1-bed in Hackney under £1,850, no commute",
     "User asked: Find me a 1-bed in Hackney, no commute"),
    ("User asked: Find a flat, max £1,850 a month", "User asked: Find a flat"),
    ("User asked: cheap studio up to £1.5k", "User asked: cheap studio"),
    ("User asked: 1-bed, 1500 max", "User asked: 1-bed"),
    ("User asked: 在Hackney找一居室，1850镑以内", "User asked: 在Hackney找一居室"),
    ("User asked: 帮我找房，不超过两千镑", "User asked: 帮我找房"),
    ("The user seems focused on 1-beds under £1,850 in Hackney.", "The user seems focused on 1-beds in Hackney."),
])
def test_g_04_a_rent_bound_is_found_and_only_it_goes(row, rest):
    from rag.agent_memory import _states_field_value, _without_field_clauses
    assert _states_field_value(row, "budget", "episodic")
    assert _states_field_value(row, "budget", "reflection")
    assert _without_field_clauses(row, "budget") == rest


def test_g_04_a_bare_bound_answers_its_question():
    from rag.agent_memory import _states_field_value, _without_field_clauses
    assert _states_field_value("User asked: under £1,850 please", "budget", "episodic")
    assert _without_field_clauses("User asked: under £1,850 please", "budget") == ""


@pytest.mark.parametrize("row", [
    "User asked: Is there a travelcard under £150?",
    "User asked: What is the fare, under £200?",
    "User asked: flat with a deposit under £2,000",
    "User asked: The flat is listed at £1,850",
    "User asked: my friend wants a flat under £2,000",
    "User asked: find me a 1-bed within 800 metres of the station",
    "User asked: find a 2-bed up to 30 minutes from Bank",
    "User asked: houses under 500m from the park",
    "User asked: what do Camden rents average? £1800",
])
def test_g_04_other_figures_are_no_budget(row):
    from rag.agent_memory import _states_field_value
    assert not _states_field_value(row, "budget", "episodic")


def test_g_04_forget_scrubs_the_search_log(tmp_path, monkeypatch):
    from rag.agent_memory import AgentMemory
    monkeypatch.setenv("AGENT_MEMORY_DB_PATH", str(tmp_path / "memory"))
    memory = AgentMemory()
    uid = "g04-user"
    memory.add("User asked: Find me a 1-bed in Hackney under £1,850, no commute", "episodic", uid, uid, role="user")
    memory.add("User asked: Is there a travelcard under £150?", "episodic", uid, uid, role="user")
    memory.add("The user keeps looking at 1-beds under £1,850.", "reflection", uid, uid)
    report = memory.forget_fact(uid, ("budget",))
    assert report["complete"]
    documents = memory.col.get(where={"user_id": uid}).get("documents")
    assert sorted(documents) == ["The user keeps looking at 1-beds.",
                                 "User asked: Find me a 1-bed in Hackney, no commute",
                                 "User asked: Is there a travelcard under £150?"]


_ERASED = {"fields": ("budget",), "complete": True, "deleted": 1, "rewritten": 0}


@pytest.mark.parametrize("body,kept", [
    ("Done. I've deleted your saved budget from long-term memory, so it won't be recalled in any "
     "future conversation.\n\nI still have two other things on file.", "I still have two other things on file."),
    ("Done. Your saved budget figures have been deleted from long-term memory, in every place they "
     "were stored. They will not be recalled in any future conversation.\n\nAnything else?", "Anything else?"),
    ("I've deleted your old budget and set £2,600 as the new one.", "I've deleted your old budget and set £2,600 as the new one."),
])
def test_g_04_the_reply_says_the_deletion_once(body, kept):
    from core.memory_gate import MEMORY_ERASED_ACK_EN, acknowledge_memory_erasure
    text = acknowledge_memory_erasure(body, _ERASED, "en")
    assert text.startswith(MEMORY_ERASED_ACK_EN.format(fields="budget"))
    assert text.endswith(kept)
    assert "long-term memory" not in text and "future conversation" not in text


def test_g_04_the_zh_reply_says_the_deletion_once():
    from core.memory_gate import acknowledge_memory_erasure
    body = ("已经处理好了：你保存过的所有预算数据，我已经从长期记忆中全部删除，不会再被调取，之后的对话里也不会再用到。\n\n"
            "目前仍保留的内容有两条。")
    text = acknowledge_memory_erasure(body, _ERASED, "zh")
    assert text.count("删除") == 1 and text.endswith("目前仍保留的内容有两条。")


# G-05 ── a bounded sample says it is one
def _held_sample(rows=(), sample_size=30, cheapest=2900, exhaustive=False):
    return {"rows": list(rows), "meta": {
        "requested_city": "london", "source": "hit", "source_status": "cached", "stale": False,
        "retrieval_complete": True, "count": len(rows), "sample_size": sample_size,
        "sample_cheapest": cheapest,
        "coverage": {"scope": "bounded_area_sample", "exhaustive": exhaustive}}}


def _search(monkeypatch, result, **kwargs):
    import asyncio
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl
    monkeypatch.setattr(on_demand, "classify_place", lambda name: {
        "kind": "area", "slug": str(name).lower(), "city": "london", "address": None, "source": "landmark"})
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: result)
    return asyncio.run(search_properties_impl(**kwargs))


def test_g_05_an_empty_band_says_the_sample_held_rows(monkeypatch):
    out = _search(monkeypatch, _held_sample(), area="Islington", areas=["Islington"], bedrooms=2,
                  max_budget=2500, no_commute=True, room_type="whole", confirmed=True,
                  current_message="Find a 2-bed flat in Islington under £2500 per month, no commute")
    assert out["status"] == "no_results"
    assert out["retrieval_outcome"] == "no_matching_candidates"
    assert out["area_status"] == {"Islington": "results"}
    assert out["coverage"] == {"scope": "bounded_area_sample", "exhaustive": False, "rows_held": 30}
    assert "No usable source candidates" not in out["message"]
    assert "I hold 30 listings for this area in a bounded sample" in out["message"]
    assert "£2,900/month" in out["message"]


def test_g_05_nothing_held_stays_nothing_retrieved(monkeypatch):
    out = _search(monkeypatch, _held_sample(sample_size=0, cheapest=None), area="Islington",
                  areas=["Islington"], bedrooms=2, max_budget=2500, no_commute=True, room_type="whole",
                  confirmed=True, current_message="Find a 2-bed flat in Islington under £2500 per month, no commute")
    assert out["retrieval_outcome"] == "no_source_candidates"
    assert out["area_status"] == {"Islington": "empty"}
    assert "No usable source candidates" in out["message"]


def test_g_05_the_empty_sample_note_is_bilingual():
    from core.tools.search_properties import _empty_sample_note
    en = _empty_sample_note(30, 2900, 2, False)
    assert "2 fit the price and size but lie outside the search radius" in en
    assert "does not establish that the market is empty" in en
    zh = _empty_sample_note(30, 2900, 2, True)
    assert "30 套" in zh and "2 套" in zh and "£2,900/月" in zh


def test_g_05_sample_facts_count_the_whole_sample():
    from core.scraping.on_demand import _sample_facts
    rows = [{"Address": "a", "URL": "u", "Price": "£3,100 pcm", "Room_Type_Category": "2 bed Flat"},
            {"Address": "b", "URL": "v", "Price": "£2,900 pcm", "Room_Type_Category": "2 bed Flat"},
            {"Address": "c", "URL": "w", "Price": "£900 pcm", "Room_Type_Category": "Studio"}]
    assert _sample_facts(rows, 2, 2) == {"sample_size": 3, "sample_cheapest": 2900}
    assert _sample_facts([], 2, 2) == {"sample_size": 0, "sample_cheapest": None}


def test_g_05_found_says_it_is_a_sample():
    from core.tools.search_properties import _sample_note
    assert _sample_note(12, ["Camden"], False) == (
        "These come from a bounded sample of 12 listings held for Camden, not every listing on the market.")
    assert "Camden、Hackney" in _sample_note(30, ["Camden", "Hackney"], True)


def test_g_05_a_found_result_carries_the_sample(monkeypatch):
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    row = {"Address": "1 Camden Road, London NW1", "Price": "£1500/month", "Price_Numeric": 1500,
           "URL": "https://www.onthemarket.com/details/1/", "Room_Type_Category": "1 bed flat",
           "geo_location": "51.5390,-0.1426", "Description": "A flat"}
    out = _search(monkeypatch, _held_sample([row], sample_size=12, cheapest=1500), area="Camden",
                  areas=["Camden"], bedrooms=1, max_budget=2000, no_commute=True, confirmed=True,
                  reply_language="en", current_message="Show me every listing in Camden")
    assert out["status"] == "found", out
    assert out["coverage"]["rows_held"] == 12
    assert "bounded sample of 12 listings held for Camden" in out["summary"]


# G-06 ── a reset takes the cleared values out of what the model reads
_OLD_TURNS = [
    {"user": "Find me a 1-bed in Hackney under £2,200", "assistant": "Here are 1-bed flats under £2,200."},
    {"user": "What about a studio?", "assistant": "Studios run from £1,400."},
]


def test_g_06_the_history_starts_at_the_reset():
    from core.constraint_updates import history_after_reset
    assert history_after_reset(_OLD_TURNS, "Can we start from scratch with Hackney?") == []
    assert history_after_reset(_OLD_TURNS, "Find a flat nearby") == _OLD_TURNS
    reset = {"user": "Can we start from scratch with Hackney?", "assistant": "Sure."}
    later = {"user": "Find a flat nearby", "assistant": "Here is one."}
    assert history_after_reset(_OLD_TURNS + [reset, later], "And a garden?") == [reset, later]


def test_g_06_a_question_about_resetting_keeps_the_history():
    from core.constraint_updates import history_after_reset
    assert history_after_reset(_OLD_TURNS, "How do I reset my filters?") == _OLD_TURNS


def test_g_06_a_rehydrated_history_starts_at_the_reset(monkeypatch):
    import importlib
    appmod = importlib.import_module("app")
    uid, cid = "u_g06", "c_g06"
    monkeypatch.setattr(appmod.conversation_store, "latest_snapshot", lambda *a: None)
    monkeypatch.setattr(appmod.conversation_store, "get_messages", lambda *a: [])
    monkeypatch.setattr(appmod.conversation_store, "rehydrate_history", lambda *a: [
        *_OLD_TURNS, {"user": "Start over", "assistant": "Sure, where next?"}])
    appmod._session_store.clear(uid, cid)
    session = appmod._get_session(uid, cid)
    assert [turn["user"] for turn in session.history] == ["Start over"]
    appmod._session_store.clear(uid, cid)


def test_g_06_the_memory_block_loses_only_what_this_conversation_derived():
    from rag.agent_memory import SOURCE_AUTO, without_rows_of_conversation
    rows = [
        {"id": 1, "mtype": "episodic", "text": "User asked: 1-bed under £2,200", "from_conversation": "c1"},
        {"id": 2, "mtype": "semantic", "source": SOURCE_AUTO, "text": "budget under £2,200", "from_conversation": "c1"},
        {"id": 3, "mtype": "reflection", "text": "The user may want a 1-bed", "from_conversation": "c1"},
        {"id": 4, "mtype": "semantic", "source": "user_remember", "text": "I have a cat", "from_conversation": "c1"},
        {"id": 5, "mtype": "semantic", "source": SOURCE_AUTO, "text": "Prefers Hackney", "from_conversation": "c2"},
        {"id": 6, "mtype": "reflection", "text": "An old inference"},
    ]
    assert [row["id"] for row in without_rows_of_conversation(rows, "c1")] == [4, 5, 6]


def _turn_up_to_the_graph(monkeypatch, tmp_path, message, *, session_history, summary=""):
    """Run the handler up to the graph and return what it hands the model."""
    import asyncio
    import importlib
    appmod = importlib.import_module("app")
    seen = {}

    class _Stop(Exception):
        pass

    def _capture(**kwargs):
        seen.update(kwargs)
        raise _Stop()

    uid, cid = "u_g06_turn", "c_g06_turn"
    monkeypatch.setattr(appmod, "create_initial_state", _capture)
    monkeypatch.setattr(appmod, "_ensure_agent_runtime", lambda: object())
    monkeypatch.setattr(appmod.conversation_store, "latest_snapshot", lambda *a: None)
    monkeypatch.setattr(appmod.conversation_store, "get_messages", lambda *a: [])
    appmod._session_store.clear(uid, cid)
    session = appmod._session_store.get(uid, cid)
    session.rehydrated = True
    session.history = list(session_history)
    session.persistent_state.setdefault("extracted_context", {})["rolling_summary"] = summary
    with pytest.raises(_Stop):
        asyncio.run(appmod.handle_with_react_agent(message, {}, False, user_id=uid, conversation_id=cid))
    after = list(session.history), dict(session.persistent_state.get("extracted_context") or {})
    appmod._session_store.clear(uid, cid)
    return seen, after


def test_g_06_a_reset_turn_hands_the_model_no_old_turn_and_no_summary(monkeypatch, tmp_path):
    seen, (history, context) = _turn_up_to_the_graph(
        monkeypatch, tmp_path, "Can we start from scratch with Hackney?", session_history=_OLD_TURNS,
        summary="Hard criteria: 1-bed under £2,200")
    assert seen["extracted_context"]["history"] == []
    assert "2,200" not in seen["user_query"]
    assert history == [] and not context.get("rolling_summary")


def test_g_06_any_other_turn_keeps_its_history(monkeypatch, tmp_path):
    seen, (history, context) = _turn_up_to_the_graph(
        monkeypatch, tmp_path, "What about Camden?", session_history=_OLD_TURNS, summary="Hard criteria: x")
    assert seen["extracted_context"]["history"] == _OLD_TURNS
    assert history == _OLD_TURNS and context["rolling_summary"] == "Hard criteria: x"


# G-12 ── a new reflection records its turn, and an edit of that turn takes it back
def test_g_12_a_reflection_goes_with_its_turn(tmp_path, monkeypatch):
    import rag.agent_memory as am
    monkeypatch.setenv("AGENT_MEMORY_DB_PATH", str(tmp_path / "memory"))
    memory = am.AgentMemory()
    uid = "g12-user"
    for index in range(5):
        memory.add(f"User asked: note {index}", "episodic", "s", uid, role="user", importance=9)
    monkeypatch.setattr(am, "call_ollama", lambda *a, **k: json.dumps(
        {"insights": ["The user may be weighing a £1,500 maximum against a garden."]}))
    memory.maybe_reflect("s", uid, conversation_id="c1", turn_id="t1")
    rows = memory.col.get(where={"$and": [{"user_id": uid}, {"mtype": "reflection"}]})
    assert len(rows["ids"]) == 1 and rows["metadatas"][0]["origin_turn_id"] == "t1"
    memory.col.add(documents=["The user may like an old thing."], ids=["old_reflection"],
                   metadatas=[{"mtype": "reflection", "user_id": uid, "hash": "x"}])
    assert memory.retire_turn_rows(uid, "c2", ["t1"]) == 0      # another conversation's turn
    assert memory.retire_turn_rows(uid, "c1", ["t1"]) == 1
    rows = memory.col.get(where={"$and": [{"user_id": uid}, {"mtype": "reflection"}]})
    assert rows["ids"] == ["old_reflection"]        # a row with no provenance stays


def test_g_12_an_edit_during_the_reflection_writes_nothing(tmp_path, monkeypatch):
    import rag.agent_memory as am
    monkeypatch.setenv("AGENT_MEMORY_DB_PATH", str(tmp_path / "memory"))
    memory = am.AgentMemory()
    uid = "g12-late"
    for index in range(5):
        memory.add(f"User asked: note {index}", "episodic", "s", uid, role="user", importance=9)

    def _edit_lands_now(*a, **k):
        memory.retire_turn_rows(uid, "c1", ["t1"])
        return json.dumps({"insights": ["The user may want a garden."]})
    monkeypatch.setattr(am, "call_ollama", _edit_lands_now)
    memory.maybe_reflect("s", uid, conversation_id="c1", turn_id="t1")
    assert not memory.col.get(where={"$and": [{"user_id": uid}, {"mtype": "reflection"}]})["ids"]


# G-07 ── the validator keeps what is true and logs the sentence around what it removes
def _commute_state(*routes, search=None, criteria=None, current="x", language="en", earlier=()):
    artifacts = [{"tool": "calculate_commute", "success": True, "raw_data": dict(route)} for route in routes]
    if search:
        artifacts.append({"tool": "search_properties", "success": True, "raw_data": search})
    state = {"tool_artifacts": artifacts,
             "extracted_context": {"current_message": current, "reply_language": language,
                                   "earlier_routes": list(earlier)},
             "accumulated_search_criteria": criteria or {}}
    return state


def _route(origin, destination, minutes, **extra):
    return {"success": True, "from_address": origin, "to_address": destination, "mode": "transit",
            "duration_minutes": minutes, "route_source": "TfL Journey Planner",
            "route_summary": f"Tube to {destination} ({minutes} min)", **extra}


def _search_row(address, destination, minutes):
    return {"address": address, "price": "£1800/month", "travel_time": f"{minutes} min to {destination}",
            "travel_time_source": "routing", "url": f"https://www.onthemarket.com/details/{minutes}/"}


_KING_SQUARE = "King Square, London, EC1V"
_THIRTY_TO_KX = {"commute_destination": "King's Cross", "max_travel_time": 30, "area": "Islington"}


def _validated(draft, state):
    from core.candidate_validation import validate_commute_response
    return validate_commute_response(draft, state)


def test_g_07_a_listing_and_its_own_route_are_one_measurement():
    state = _commute_state(_route(_KING_SQUARE, "King's Cross", 32),
                           search={"status": "found", "recommendations": [_search_row(_KING_SQUARE, "King's Cross", 32)]},
                           criteria=_THIRTY_TO_KX)
    draft = (f"The closest is {_KING_SQUARE} at £1,800, but the journey to King's Cross is 32 minutes, "
             "which is 2 minutes over your limit.")
    assert _validated(draft, state) == draft


def test_g_07_within_budget_is_no_commute_verdict():
    state = _commute_state(_route(_KING_SQUARE, "King's Cross", 32),
                           search={"status": "found", "recommendations": [_search_row(_KING_SQUARE, "King's Cross", 32)]},
                           criteria=_THIRTY_TO_KX)
    draft = f"{_KING_SQUARE} is within budget but the commute is 32 minutes."
    assert _validated(draft, state) == draft


def test_g_07_a_wrong_figure_for_that_listing_still_goes():
    state = _commute_state(_route(_KING_SQUARE, "King's Cross", 32),
                           search={"status": "found", "recommendations": [_search_row(_KING_SQUARE, "King's Cross", 32)]},
                           criteria=_THIRTY_TO_KX)
    assert "25 minutes" not in _validated(f"{_KING_SQUARE}: the journey to King's Cross is 25 minutes.", state)


@pytest.mark.parametrize("draft", [
    "It does not confirm that a particular station or journey falls within Zones 1-4.",
    "This route is outside the area our journey tool covers (it only has TfL data).",
    "The journey planner only covers Greater London, and this route is outside that area.",
    "Newport is outside the area the TfL Journey Planner covers.",
    "I can still check commute times within London.",
])
def test_g_07_what_the_tool_covers_is_no_journey(draft):
    assert _validated(draft, _commute_state()) == draft


def test_g_07_a_verdict_on_a_commute_still_counts():
    from core.candidate_validation import _asserts_commute
    assert _asserts_commute("the commute is within your limit")
    assert _asserts_commute("this journey is outside your limit")


@pytest.mark.parametrize("draft,kept", [
    ("I searched for a 1-bed, including a commute of 30 minutes or less to King's Cross.", True),
    ("(1-bed whole flat, under £1,900 a month, commute to King's Cross under 30 minutes)", True),
    ("（Islington、1 房整套、月租 £2,200 以内、通勤 30 分钟内）", True),
    ("None are within 30 minutes of King's Cross.", True),
    ("Nothing came back within 30 minutes of King's Cross.", True),
    # Another destination, or another figure, is not the user's limit.
    ("(1-bed whole flat, commute to Bank under 30 minutes)", False),
    ("None are within 20 minutes of King's Cross.", False),
])
def test_g_07_the_users_own_limit_said_back(draft, kept):
    state = _commute_state(criteria=_THIRTY_TO_KX)
    state["accumulated_search_criteria"]["no_commute"] = False
    assert (_validated(draft, state) == draft) is kept


def test_g_07_nothing_inside_the_limit_has_to_hold():
    state = _commute_state(_route(_KING_SQUARE, "King's Cross", 28), criteria=_THIRTY_TO_KX)
    assert "None are within" not in _validated("None are within 30 minutes of King's Cross.", state)


def test_g_07_a_wish_for_something_nearby_is_no_commute():
    state = _commute_state(current="What do you remember about me?")
    draft = "1. You prefer a quiet street.\n2. You would like a gym within 10 minutes.\n3. You have a cat."
    assert _validated(draft, state) == draft
    # In an answer about a commute the rule does not read a journey as an amenity.
    assert "gym within 10 minutes" not in _validated("The commute is a gym within 10 minutes.", _commute_state())


def test_g_07_the_tenant_of_a_destination_can_be_left_out():
    row = _search_row("Queensbridge Road, London E2", "Google King's Cross", 29)
    state = _commute_state(search={"status": "found", "recommendations": [row]},
                           criteria={"commute_destination": "Google King's Cross", "area": "Hackney"})
    draft = "| Queensbridge Road, London E2 | £2,100 | 29 min to King's Cross |"
    assert _validated(draft, state) == draft
    assert "to Cross" not in _validated("Queensbridge Road, London E2: 29 min to Cross", state)


def test_g_07_an_estimate_said_as_an_estimate_under_a_line_that_names_nothing():
    row = {"address": "Highgate West Hill, London, N6", "price": "£1950/month",
           "travel_time": "~39 min to Bank (estimated, unverified)", "travel_time_source": "estimate",
           "url": "https://www.onthemarket.com/details/39/"}
    state = _commute_state(_route("Camden", "Bank", 29), criteria={"commute_destination": "Bank", "area": "Camden"},
                           search={"status": "found", "recommendations": [row]})
    draft = "Commute to Bank is an estimated ~39 minutes (derived from straight-line distance, not a verified journey plan)."
    assert "~39 minutes" in _validated(draft, state)
    # Said as a measurement it goes, and it never licenses another listing.
    assert "39 minutes" not in _validated("The commute to Bank takes 39 minutes.", state)


def test_g_07_a_route_of_an_earlier_turn_stays_quotable():
    from core.candidate_validation import earlier_routes
    first = [{"tool": "calculate_commute", "success": True, "raw_data": _route("Jacob House, London, E8", "Bank", 40)}]
    kept = earlier_routes(None, first)
    state = _commute_state(earlier=kept, criteria={"commute_destination": "Bank"})
    draft = "Jacob House, London, E8: 40 min to Bank."
    assert _validated(draft, state) == draft
    assert "40 min" not in _validated("Jacob House, London, E8: 40 min to Bank.", _commute_state())


def test_g_07_this_turn_measures_over_an_earlier_route():
    from core.candidate_validation import earlier_routes
    kept = earlier_routes(None, [{"tool": "calculate_commute", "success": True,
                                  "raw_data": _route("Jacob House, London, E8", "Bank", 40)}])
    state = _commute_state(_route("Jacob House, London, E8", "Bank", 31), earlier=kept)
    assert "40 min" not in _validated("Jacob House, London, E8: 40 min to Bank.", state)
    assert _validated("Jacob House, London, E8: 31 min to Bank.", state).startswith("Jacob House, London, E8: 31 min")


def test_g_07_earlier_routes_keep_the_newest_and_no_failure():
    from core.candidate_validation import earlier_routes
    calls = [{"tool": "calculate_commute", "success": True, "raw_data": _route(f"Place {n}", "Bank", 20 + n)}
             for n in range(12)]
    calls.append({"tool": "calculate_commute", "success": False, "raw_data": {"success": False}})
    calls.append({"tool": "search_properties", "success": True, "raw_data": {}})
    kept = earlier_routes(None, calls)
    assert len(kept) == 8 and kept[-1]["raw_data"]["from_address"] == "Place 11"


def test_g_07_a_removed_item_leaves_the_list_counting_straight():
    from core.candidate_validation import _renumbered_list
    assert _renumbered_list("1. a\n2. b\n4. c\n\n5. d\n- x\n7. e") == "1. a\n2. b\n3. c\n\n4. d\n- x\n7. e"
    assert _renumbered_list("Top 3:\n3. a\n4. b") == "Top 3:\n3. a\n4. b"


def test_g_07_the_log_carries_the_draft_sentence(caplog):
    import logging
    from core.candidate_validation import _log_removed_commute_text
    draft = "It is a nice flat. Commute to Bank takes 25 minutes, sadly. Rent is fine."
    with caplog.at_level(logging.INFO):
        _log_removed_commute_text(["Commute to Bank takes 25 minutes"], draft)
    message = next(r.getMessage() for r in caplog.records if "claims_removed" in r.getMessage())
    assert "sentences=" in message and "Commute to Bank takes 25 minutes, sadly." in message


# G-08 ── an explicit reference to "my budget" applies it
_ONE_SAVED = [{"id": "a", "mtype": "semantic", "text": "The user's budget is £1,800 a month.", "created_at": "2026-10-30"},
              {"id": "b", "mtype": "semantic", "text": "The user prefers Hackney.", "created_at": "2026-10-30"},
              {"id": "c", "mtype": "reflection", "text": "The user's budget may be £2,300.", "created_at": "2026-10-30"}]
_TWO_SAVED = _ONE_SAVED + [{"id": "d", "mtype": "semantic", "text": "The user's budget is £2,500 a month.",
                            "created_at": "2026-10-31"}]


@pytest.mark.parametrize("text", [
    "Find 1-bed flats in Bow, use my default budget. No commute.",
    "Find me a 1-bed under my budget",
    "Search a 1-bed in Islington for me, applying my budget",
    "Search Bow and stick to my budget",
    "Find a flat within my budget",
    "Search 1-bed in Angel with my default budget, no commute needed",
    "Use the budget you have saved for me and search 1-bed in Hackney",
])
def test_g_08_the_one_saved_budget_is_applied(text):
    from core.constraint_updates import references_saved_criteria, restore_saved_criteria
    assert references_saved_criteria(text)
    restored = restore_saved_criteria({}, _ONE_SAVED, text)
    assert restored["max_budget"] == 1800
    assert restored["constraint_sources"]["max_budget"]["source"] == "saved_preference"
    assert "commute_destination" not in restored


def test_g_08_the_conversations_budget_is_my_budget():
    from core.constraint_updates import restore_saved_criteria
    assert restore_saved_criteria({"max_budget": 2100}, _ONE_SAVED, "Find me a 1-bed under my budget")["max_budget"] == 2100


def test_g_08_two_saved_budgets_set_nothing():
    from core.constraint_updates import restore_saved_criteria
    assert restore_saved_criteria({}, _TWO_SAVED, "Find me a 1-bed under my budget") == {}


@pytest.mark.parametrize("text", [
    "What is my budget?", "Raise my budget to £2,000", "Change the budget to £1,800", "my budget is £2,000",
    "Is it under my budget?", "Forget my budget", "Find a flat within my budget of £2,000",
])
def test_g_08_no_reference(text):
    from core.constraint_updates import references_saved_criteria
    assert not references_saved_criteria(text)


def test_g_08_a_saved_workplace_comes_with_it_when_named():
    from core.constraint_updates import restore_saved_criteria
    rows = _ONE_SAVED + [{"id": "e", "mtype": "semantic", "text": "My office is at King's Cross.", "created_at": "2026-10-30"}]
    restored = restore_saved_criteria({}, rows, "Use the budget you have saved for me and search Hackney, my workplace is saved too")
    assert restored["max_budget"] == 1800 and "King" in restored["commute_destination"]


@pytest.mark.parametrize("text", ["find a 1-bed flat in Angel", "a 1-bed in Bow", "search Camden", "looking for a studio"])
def test_g_08_a_request_is_no_destination(text):
    from core.constraint_updates import is_destination_text
    assert not is_destination_text(text)


@pytest.mark.parametrize("text", ["King's Cross", "Bank", "Flat Iron Square", "The Shard", "Canary Wharf, London E14 5AB"])
def test_g_08_a_place_is_a_destination(text):
    from core.constraint_updates import is_destination_text
    assert is_destination_text(text)


def test_g_08_the_rest_of_the_sentence_is_not_stored():
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply({"area": "Bow", "areas": ["Bow"]}, "Use my saved budget and commute to find a 1-bed flat in Angel")
    assert not after.get("commute_destination") and after["area"] == "Angel"


# G-09 ── an area named as an area flips nothing
@pytest.mark.parametrize("text", ["Change the area to Canary Wharf", "Switch to Canary Wharf", "Change the search area to Canary Wharf"])
def test_g_09_an_area_change_keeps_no_commute(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert after["area"] == "Canary Wharf" and after["no_commute"] is True
    assert not after.get("commute_destination")


@pytest.mark.parametrize("text", ["I work at Canary Wharf now", "My office is in Canary Wharf"])
def test_g_09_a_workplace_still_counts(text):
    from core.langgraph_agent import _apply_explicit_criteria_updates as apply
    after = apply(dict(_STRATFORD), text)
    assert after["no_commute"] is False and "Canary Wharf" in after["commute_destination"]


def test_g_09_a_bare_switch_is_a_workplace_only_for_someone_with_a_commute():
    from core.constraint_updates import moves_area_only
    assert moves_area_only("Switch to Canary Wharf", True)
    assert not moves_area_only("Switch to Canary Wharf", False)
    assert moves_area_only("Change the area to Canary Wharf", False)
