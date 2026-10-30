"""Regression tests (R4-xx / S-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_BOW_1BED = {"area": "Bow", "areas": ["Bow"], "bedrooms": 1, "max_budget": 1800, "room_type": "whole"}


# S-01 ── the figure before the field ───────────────────────────────────────────────
@pytest.mark.parametrize("message,budget", [
    ("改成1500预算", 1500),
    ("改为1500预算", 1500),
    ("我想改成1500预算，找Bow的一居室", 1500),
    ("1500的预算", 1500),
    ("换成1500的预算", 1500),
    ("换成1500镑的预算", 1500),
    ("预算改成1500", 1500),
    ("1500以内", 1500),
    ("make it a £1,500 budget", 1500),
    ("£1,500 budget instead", 1500),
    ("change to a 1,500 budget", 1500),
    ("1500 is my new budget", 1500),
    ("a budget of 1500 instead", 1500),
    ("1.5k budget", 1500),
    ("£1.5k budget instead", 1500),
    ("budget 2k", 2000),
    ("2,000 max now", 2000),
    ("1500 tops", 1500),
])
def test_s_01_a_figure_before_the_budget_noun_sets_the_budget(message, budget):
    acc = _state_node(_BOW_1BED, message)
    assert acc["max_budget"] == budget
    assert acc["area"] == "Bow" and acc["bedrooms"] == 1


@pytest.mark.parametrize("message", [
    "1500预算够吗？",
    "预算1500够吗？",
    "1500的预算够不够？",
    "Is a £1,500 budget enough?",
    "is £1,500 enough?",
    "show listing 1500",
    "within 1500 metres of the station",
    "1500 metres max from the station",
    "离车站1500米以内",
    "1500平方英尺以内",
    "I run 5k every morning",
    "我是2020年来的",
])
def test_s_01_questions_and_other_numbers_leave_the_budget_alone(message):
    assert _state_node(_BOW_1BED, message)["max_budget"] == 1800


@pytest.mark.parametrize("message,beds", [
    ("改成2个卧室", 2),
    ("两个卧室", 2),
    ("改成两室一厅", 2),
    ("两室", 2),
    ("三间卧室", 3),
    ("make it 2 bedrooms", 2),
    ("改成两居室", 2),
])
def test_s_01_a_count_before_the_bedroom_word_sets_bedrooms(message, beds):
    acc = _state_node(_BOW_1BED, message)
    assert acc["bedrooms"] == beds
    assert acc["max_budget"] == 1800


@pytest.mark.parametrize("message", ["302室的那套怎么样", "每人一个卧室", "我有两个室友"])
def test_s_01_room_numbers_and_per_person_rooms_are_not_bedroom_counts(message):
    assert _state_node(_BOW_1BED, message)["bedrooms"] == 1


def test_s_01_minutes_before_the_commute_word_still_set_the_limit():
    assert _state_node(_BOW_1BED, "改成30分钟通勤")["max_travel_time"] == 30


# R4-01 ── "forget my budget only" keeps the other facts of the same note ────────────
_COMBINED = ("User is allergic to cats, needs step-free access, and has a maximum budget of "
             "£1,800 per month.")


@pytest.fixture()
def r4_memory(tmp_path, monkeypatch):
    import rag.agent_memory as am
    monkeypatch.setattr(am, "call_ollama", lambda *a, **k: "5")
    return am, am.AgentMemory(db_path=str(tmp_path / "mem"))


def _docs(store, user):
    return list(store.col.get(where={"user_id": user}).get("documents") or [])


def test_r4_01_forgetting_the_budget_keeps_the_rest_of_a_combined_note(r4_memory):
    am, store = r4_memory
    user = "u_r401"
    store.add(_COMBINED, "semantic", "s1", user)
    store.add("User asked: Remember: I'm allergic to cats, I need step-free access, and my "
              "budget is max £1,800 a month.", "episodic", "s1", user)
    store.add("User's budget is £1,800 per month.", "semantic", "s1", user)
    report = store.forget_fact(user, ("budget",))
    docs = _docs(store, user)
    assert report["complete"] and report["rewritten"] == 2 and report["deleted"] == 1
    assert "User is allergic to cats, needs step-free access." in docs
    assert any("allergic to cats, I need step-free access." in d for d in docs)
    blob = "\n".join(docs)
    assert "1,800" not in blob and "budget" not in blob.lower()


def test_r4_01_a_zh_combined_note_loses_only_the_budget_clause():
    from rag.agent_memory import _without_field_clauses
    assert _without_field_clauses("用户对猫过敏，需要无障碍通道，每月预算最多1800英镑。", "budget") \
        == "用户对猫过敏，需要无障碍通道。"
    assert _without_field_clauses("User asked: Please remember my budget is £1400 a month.",
                                  "budget") == ""
    assert _without_field_clauses("The user's budget is £2,000. The user works at King's Cross.",
                                  "budget") == "The user works at King's Cross."


def test_r4_01_the_acknowledgement_says_the_other_details_were_kept():
    from core.memory_gate import memory_erasure_texts
    note, ack = memory_erasure_texts({"fields": ("budget",), "complete": True, "rewritten": 1})
    assert "still stored" in ack and "KEPT" in note
    _note, plain = memory_erasure_texts({"fields": ("budget",), "complete": True, "rewritten": 0})
    assert "still stored" not in plain


def test_r4_01_a_late_job_of_an_earlier_turn_does_not_write_the_value_back(r4_memory):
    import datetime
    am, store = r4_memory
    user = "u_r401b"
    started = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=5)).isoformat()
    store.add(_COMBINED, "semantic", "s1", user)
    store.forget_fact(user, ("budget",))
    # The remember turn's own memory job runs after the forget.
    assert store.remember_turn("Remember: I'm allergic to cats, I need step-free access, and my "
                               "budget is max £1,800 a month.", "Saved.", session_id="s1",
                               user_id=user, turn_started_at=started)
    blob = "\n".join(_docs(store, user))
    assert "1,800" not in blob and "step-free" in blob
    # A turn that starts after the erasure may state a new budget.
    later = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(seconds=1)).isoformat()
    assert store.erased_fields_since(user, later) == ()
    assert store.erased_fields_since(user, started) == ("budget",)


# R4-02 ── "anywhere in London" widens the area; it never re-filters the old results ──
_BOW_NO_COMMUTE = {"area": "Bow", "areas": ["Bow"], "bedrooms": 1, "max_budget": 2200,
                   "room_type": "whole", "no_commute": True}
_SHOWN_BOW = [{"area": "Bow", "address": "Tredegar Square, London, E3", "price": "£1650/month",
               "bedrooms": 1},
              {"area": "Bow", "address": "Yeo Street, London E3", "price": "£1700/month",
               "bedrooms": 1}]


@pytest.mark.parametrize("message", [
    "Retract the area: I don't care about the area any more, anywhere in London.",
    "anywhere in London",
    "伦敦任何地方都可以",
    "哪里都行",
    "Any area is fine",
])
def test_r4_02_anywhere_widens_a_london_search_to_london(message):
    from core.refine_results import parse_refinement
    acc = _state_node(_BOW_NO_COMMUTE, message)
    assert acc["area"] == "London" and acc["areas"] == ["London"]
    assert parse_refinement(message, _SHOWN_BOW) is None


@pytest.mark.parametrize("message", [
    "I don't care about the area any more", "Retract the area", "撤销区域要求",
])
def test_r4_02_withdrawing_the_area_clears_it(message):
    acc = _state_node(_BOW_NO_COMMUTE, message)
    assert not acc.get("area") and not acc.get("areas")


@pytest.mark.parametrize("message", [
    "Is there anywhere cheaper?", "anywhere near the station?", "Don't forget the area",
])
def test_r4_02_other_anywhere_phrases_keep_the_area(message):
    assert _state_node(_BOW_NO_COMMUTE, message)["area"] == "Bow"


def test_r4_02_london_covers_every_london_area_in_the_validator():
    from core.candidate_validation import validate_candidates
    rows = [{"title": "Tredegar Square", "address": "Tredegar Square, London, E3", "area": "Bow",
             "price": "£1650/month", "bedrooms": 1, "room_type": "whole",
             "url": "https://example.test/1"},
            {"title": "Mill Road", "address": "Mill Road, Cambridge", "area": "Cambridge",
             "price": "£1650/month", "bedrooms": 1, "room_type": "whole",
             "url": "https://example.test/2"}]
    out = validate_candidates(rows, {"area": "London", "areas": ["London"], "bedrooms": 1,
                                     "max_budget": 2200, "room_type": "whole", "no_commute": True})
    reasons = {s["candidate"]["url"]: s["reasons"] for s in out["statuses"]}
    assert not any("outside the requested areas" in r for r in reasons["https://example.test/1"])
    assert any("outside the requested areas" in r for r in reasons["https://example.test/2"])


# R4-02b ── a transport question naming a shown area is a question, not a filter ─────
_SHOWN_TWO_AREAS = [{"area": "Clapham", "address": "x, Clapham, London SW4", "price": "£2000/month"},
                    {"area": "Hackney", "address": "y, Hackney, London E8", "price": "£2100/month"}]


@pytest.mark.parametrize("message", [
    "What are the fares and zones for Clapham to Canary Wharf?",
    "Which zones are the Clapham ones in, and what's a travelcard?",
    "Clapham transport links?",
    "How is Hackney connected by tube?",
    "Is Clapham safe?",
    "Clapham到金丝雀码头的地铁要多久？",
])
def test_r4_02b_a_question_about_a_shown_area_reaches_the_model(message):
    from core.refine_results import parse_refinement
    assert parse_refinement(message, _SHOWN_TWO_AREAS) is None


@pytest.mark.parametrize("message", [
    "only the Clapham ones", "Which of these are in Clapham?", "just the Hackney ones please",
])
def test_r4_02b_an_explicit_narrowing_still_refines(message):
    from core.refine_results import parse_refinement
    spec = parse_refinement(message, _SHOWN_TWO_AREAS)
    assert spec and spec["filters"][0]["kind"] == "area"


# R4-04 ── "<field> to <value>" and list-form corrections ────────────────────────────
_CAMDEN_1BED = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
                "room_type": "whole"}


@pytest.mark.parametrize("message,area,beds,budget", [
    ("Set bedrooms to 3, area Shoreditch, budget £3,500.", "Shoreditch", 3, 3500),
    ("Set the budget to £2,000 and the bedrooms to 2.", "Camden", 2, 2000),
    ("把卧室数改成两间，预算改成2600", "Camden", 2, 2600),
    ("撤销通勤要求，在Hackney找", "Hackney", 1, 2200),
    ("bedrooms: 2", "Camden", 2, 2200),
    ("房间数改成3", "Camden", 3, 2200),
    ("卧室改成2个", "Camden", 2, 2200),
    ("Bedrooms now 2", "Camden", 2, 2200),
    ("Change the area to Shoreditch and make it 2 bedrooms", "Shoreditch", 2, 2200),
])
def test_r4_04_a_field_named_before_its_value_is_applied(message, area, beds, budget):
    acc = _state_node(_CAMDEN_1BED, message)
    assert (acc["area"], acc["bedrooms"], acc["max_budget"]) == (area, beds, budget)


@pytest.mark.parametrize("message", ["beds at 10pm", "the bedroom is 20 sqm", "卧室要朝南"])
def test_r4_04_other_bedroom_mentions_change_nothing(message):
    assert _state_node(_CAMDEN_1BED, message)["bedrooms"] == 1


def test_r4_04_a_minimum_after_the_slot_value_is_kept():
    acc = _state_node(_CAMDEN_1BED, "bedrooms to 2 minimum")
    assert acc["bedrooms"] == 2 and acc.get("bedrooms_minimum") is True


# R4-05 ── "Retract the commute (again)." withdraws the commute ──────────────────────
_CAMDEN_BANK = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
                "room_type": "whole", "commute_destination": "Bank", "destination": "Bank",
                "max_travel_time": 30, "no_commute": False}


@pytest.mark.parametrize("message", [
    "Retract the commute again.", "Retract the commute.", "Withdraw the commute requirement.",
    "Undo the commute", "撤销通勤要求", "取消通勤要求",
])
def test_r4_05_retracting_the_commute_clears_it(message):
    acc = _state_node(_CAMDEN_BANK, message)
    assert acc.get("commute_destination") is None and acc.get("max_travel_time") is None
    assert acc.get("no_commute") is True
    assert acc["area"] == "Camden"


def test_r4_05_lifting_only_the_limit_keeps_the_destination():
    acc = _state_node(_CAMDEN_BANK, "取消通勤时间限制")
    assert acc["commute_destination"] == "Bank" and acc.get("max_travel_time") is None


# R4-06 ── a new place replaces the stale area / destination ─────────────────────────
_GLASGOW = {"area": "Glasgow", "areas": ["Glasgow"], "bedrooms": 1, "max_budget": 2200,
            "room_type": "whole"}
_BRISTOL_UNI = {"area": "Bristol", "areas": ["Bristol"], "bedrooms": 1, "max_budget": 1800,
                "room_type": "whole", "commute_destination": "University of Bristol",
                "destination": "University of Bristol", "no_commute": False}


@pytest.mark.parametrize("message,postcode", [
    ("And near SW1A 1AA postcode?", "SW1A 1AA"),
    ("What about EC1A 4HQ?", "EC1A 4HQ"),
    ("Find a flat near e1 6an", "E1 6AN"),
])
def test_r4_06a_a_postcode_is_the_new_search_location(message, postcode):
    acc = _state_node(_GLASGOW, message)
    assert acc["area"] == postcode and acc["areas"] == [postcode]
    assert acc.get("commute_destination") in (None, "")


@pytest.mark.parametrize("message", [
    "How far is SW1A 1AA from the first one?", "I work at EC1A 4HQ",
])
def test_r4_06a_a_postcode_in_a_question_or_a_workplace_is_not_the_area(message):
    assert _state_node(_GLASGOW, message)["area"] == "Glasgow"


def test_r4_06a_a_postcode_is_never_read_as_a_headquarters():
    from core.scraping.on_demand import extract_destination_from_text
    assert extract_destination_from_text("What about EC1A 4HQ?") is None


@pytest.mark.parametrize("message", [
    "What are the fares and zones for Clapham to Canary Wharf?",
    "How do I get from Clapham to Canary Wharf?",
    "Which zones is Canary Wharf in?",
])
def test_r4_06b_a_fare_or_zone_question_sets_no_commute_destination(message):
    acc = _state_node({"area": "Hackney", "areas": ["Hackney", "Clapham"], "max_budget": 2500}, message)
    assert not acc.get("commute_destination")
    later = _state_node(acc, "Find me a 2-bed in Bayswater")
    assert later["area"] == "Bayswater" and not later.get("commute_destination")


def test_r4_06b_a_stated_commute_still_sets_the_destination():
    acc = _state_node({"area": "Hackney", "areas": ["Hackney"]},
                      "I work at Canary Wharf, what are the fares from Hackney?")
    assert "Canary Wharf" in (acc.get("commute_destination") or "")


@pytest.mark.parametrize("message,area", [
    ("查一下伦敦玛丽女王大学附近的房子，预算2000以内", "Mile End"),
    ("帮我找贝斯沃特的两居室", "Bayswater"),
])
def test_r4_06c_zh_place_names_replace_the_old_area(message, area):
    acc = _state_node(_BRISTOL_UNI, message)
    assert acc["area"] == area and acc["areas"] == [area]
    # The Bristol destination stays behind when the search moves to London.
    assert not acc.get("commute_destination")


# R4-07 ── a mixed "search + weather" turn switches the area ─────────────────────────
@pytest.mark.parametrize("message", [
    "Show me Angel flats under £2100 and tell me the weather there.",
    "Show me flats in Angel under £2100 and tell me the weather there.",
])
def test_r4_07_a_search_clause_beside_a_weather_question_switches_the_area(message):
    acc = _state_node({"area": "Camden", "areas": ["Camden"], "max_budget": 2100}, message)
    assert acc["area"] == "Angel" and acc["areas"] == ["Angel"]


@pytest.mark.parametrize("message", [
    "What's the weather in Angel? Keep my search unchanged", "Show me the weather in Angel",
    "show me the fare from Angel to Bank",
])
def test_r4_07_a_weather_or_fare_question_alone_keeps_the_area(message):
    acc = _state_node({"area": "Camden", "areas": ["Camden"], "max_budget": 2100}, message)
    assert acc["area"] == "Camden"


# R4-11 ── "I applied your saved £2,000 monthly maximum" with no budget in the search ─
@pytest.mark.parametrize("text,lang", [
    ("I applied your saved £2,000 monthly maximum when searching. Here are 5 flats.", "en"),
    ("Using your saved budget, here are the flats.", "en"),
    ("已按你保存的预算搜索，以下是结果。", "zh"),
])
def test_r4_11_an_applied_saved_budget_claim_without_a_budget_gets_the_note(text, lang):
    from core.agent_loop import disclose_unapplied_budget_claim
    out = disclose_unapplied_budget_claim(text, True, lang)
    assert out != text and ("without a budget filter" in out or "没有使用预算筛选" in out)
    assert disclose_unapplied_budget_claim(text, False, lang) == text


def test_r4_11_other_wording_is_untouched():
    from core.agent_loop import disclose_unapplied_budget_claim
    text = "Here are five flats in Camden. Tell me a budget to narrow them."
    assert disclose_unapplied_budget_claim(text, True, "en") == text


# R4-03 ── 「用我记住的预算」 is not a save request; a bare fragment is never stored ───────
@pytest.mark.parametrize("message", [
    "帮我在Camden找一个一居室，用我记住的预算。", "用你记住的预算帮我搜", "帮我记住的预算是多少？",
    "Find a 1-bed using the budget you remembered",
])
def test_r4_03_a_relative_clause_about_saved_memory_is_no_save_cue(message):
    from core.memory_gate import explicit_remember_clause, user_authorizes_memory
    assert not user_authorizes_memory(message)
    assert explicit_remember_clause(message) == ""


@pytest.mark.parametrize("message,fact", [
    ("请记住：我的预算每月最多1600英镑。", "我的预算每月最多1600英镑"),
    ("记住我有一只猫", "我有一只猫"),
])
def test_r4_03_a_real_zh_save_request_still_saves(message, fact):
    from core.memory_gate import explicit_remember_clause
    assert explicit_remember_clause(message) == fact


def test_r4_03_a_bare_fragment_is_rejected_at_the_write_path():
    from core.memory_gate import memory_write_rejection
    assert memory_write_rejection("的预算") is not None
    assert memory_write_rejection("了的预算") is not None
    assert memory_write_rejection("素食") is None
    assert memory_write_rejection("我的预算每月最多1600英镑") is None


# R4-08 ── a clarification question is asked, never reported as a failed search ──────
_CLARIFY_Q = ("Before I search, could you confirm a couple of things: what room type you'd like "
              "(studio / en-suite / shared)?")


def test_r4_08_a_failure_claim_is_replaced_by_the_clarification_question():
    from core.agent_loop import answer_with_clarification
    text = ("The listing search did not complete, so I can't show flats yet. Please try again "
            "later. In Angel it is 14 C with light rain.")
    out = answer_with_clarification(text, _CLARIFY_Q)
    assert _CLARIFY_Q in out and "try again" not in out and "did not complete" not in out
    assert "14 C with light rain" in out


def test_r4_08_zh_failure_wording_is_replaced_too():
    from core.agent_loop import answer_with_clarification
    out = answer_with_clarification("房源查询没有完成，这是失败的查询，请稍后重试。", "需要考虑通勤吗？")
    assert out == "需要考虑通勤吗？"


def test_r4_08_an_answer_that_already_asks_the_question_is_unchanged():
    from core.agent_loop import answer_with_clarification
    text = f"Sure. {_CLARIFY_Q}"
    assert answer_with_clarification(text, _CLARIFY_Q) == text


class _NoTools:
    def list_specs(self):
        return []

    def get(self, name):
        return None


def test_r4_08_the_fc_answer_carries_the_question_of_a_clarifying_search():
    from core.agent_loop import build_fc_nodes
    raw = {"success": False, "status": "need_clarification", "clarification_kind": "soft_criteria",
           "question": _CLARIFY_Q, "missing_fields": ["room_type"]}
    state = {"tool_artifacts": [{"tool": "search_properties", "raw_data": raw, "success": False}],
             "user_preferences": {}, "accumulated_search_criteria": {},
             "final_response": "The listing search failed this turn. Please try again later.",
             "response_type": "answer", "extracted_context": {}}
    out = build_fc_nodes(_NoTools())["format_output_fc"](state)
    assert _CLARIFY_Q in out["final_response"]
    assert "try again later" not in out["final_response"]


# R4-09 ── a secret code word is a credential in every memory row ────────────────────
@pytest.mark.parametrize("text,secret", [
    ("Please remember I'm allergic to cats. My secret code word is PINEAPPLE-77.", "PINEAPPLE-77"),
    ("my code word: banana", "banana"),
    ("My memorable word is Rosebud", "Rosebud"),
    ("The answer to my security question is Fluffy", "Fluffy"),
    ("我的暗号是天王盖地虎", "天王盖地虎"),
])
def test_r4_09_a_code_word_is_redacted(text, secret):
    from rag.agent_memory import redact_secrets
    redacted, found = redact_secrets(text)
    assert secret not in redacted and "credentials" in found


@pytest.mark.parametrize("text", ["I want a word with the landlord", "a secret garden would be nice"])
def test_r4_09_ordinary_words_are_kept(text):
    from rag.agent_memory import redact_secrets
    assert redact_secrets(text) == (text, [])


def test_r4_09_the_episodic_log_keeps_no_code_word(r4_memory):
    am, store = r4_memory
    user = "u_r409"
    assert store.remember_turn("Please remember I'm allergic to cats and I always want a quiet "
                               "street. My secret code word is PINEAPPLE-77.", "Saved.",
                               session_id="s1", user_id=user)
    assert "PINEAPPLE" not in "\n".join(_docs(store, user))


# S-03 ── "Do I have any allergies on file?" lists every saved fact ──────────────────
@pytest.mark.parametrize("message", [
    "Do I have any allergies on file?", "What do you have on me?", "Do you have my budget?",
    "What's on file for me?", "我有没有过敏的记录？",
])
def test_s_03_an_on_file_question_is_a_recall_question(message):
    import app as appmod
    assert appmod._asks_what_is_remembered(message)


@pytest.mark.parametrize("message", [
    "Do you have flats in my area?", "Do you have any 2-bed flats?",
    "What's on file for the listing?", "Is the flat on file with the council?",
])
def test_s_03_other_questions_are_not_recall_questions(message):
    import app as appmod
    assert not appmod._asks_what_is_remembered(message)


# R4-13 ── a named station resolves by its own name, not by a nearby geocode ─────────
def test_r4_13_a_station_name_with_a_suffix_resolves_to_that_station(monkeypatch):
    from core.tools import get_transport_info as gti
    monkeypatch.setattr(gti, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_geocode", lambda text: {"lat": 51.530, "lng": -0.124, "postcode": "N1C 4AA"})

    def fake_tfl(path, params=None, **_k):
        if path.startswith("/StopPoint/Search/"):
            return 200, {"matches": [
                {"id": "940GZZLUCTN", "name": "Camden Town Underground Station", "lat": 51.539, "lon": -0.142},
                {"id": "940GZZLUKSX", "name": "King's Cross St. Pancras Underground Station"}]}
        return 200, {"stopPoints": [{"id": "940GZZLUKSX", "commonName": "King's Cross St. Pancras Underground Station",
                                     "stopType": "NaptanMetroStation", "distance": 100}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    resolved = gti._resolve_station("Camden Town Underground Station")
    assert resolved["token"] == "940GZZLUCTN" and resolved["name"] == "Camden Town Underground Station"


def test_r4_13_an_address_still_goes_to_the_geocoder(monkeypatch):
    from core.tools import get_transport_info as gti
    monkeypatch.setattr(gti, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(gti, "_geocode", lambda text: {"lat": 51.546, "lng": -0.075, "postcode": "E8 2AA"})
    searched = []

    def fake_tfl(path, params=None, **_k):
        searched.append(path)
        return 200, {"stopPoints": [{"id": "910GDALSKLD", "commonName": "Dalston Kingsland Rail Station",
                                     "stopType": "NaptanRailStation", "distance": 300}]}
    monkeypatch.setattr(gti, "_tfl_get", fake_tfl)
    assert gti._resolve_station("Kingsland Road, London E8")["name"] == "Dalston Kingsland Rail Station"
    assert not any(p.startswith("/StopPoint/Search/") for p in searched)


def test_r4_13_station_keys_ignore_suffixes_and_punctuation():
    from core.tools.get_transport_info import _station_key
    assert _station_key("King's Cross St. Pancras Underground Station") == _station_key("Kings Cross St Pancras")
    assert _station_key("Stratford (London) Rail Station") == "stratford"


# R4-12 ── an airport is routed to its station; a long final walk is flagged ─────────
def _commute(monkeypatch, legs, captured):
    import core.maps_service as ms
    from core.tools import calculate_commute as cc

    def fake_details(frm, to, mode):
        captured.append((frm, to))
        return {"duration_minutes": sum(l["duration_minutes"] for l in legs), "source": "tfl",
                "route_legs": legs, "route_summary": "x", "changes_count": 1}
    monkeypatch.setattr(ms, "calculate_travel_details", fake_details)
    return cc.calculate_commute_impl("South Kensington", "Heathrow", "transit")


def test_r4_12_heathrow_is_routed_to_the_terminal_station(monkeypatch):
    captured = []
    out = _commute(monkeypatch, [{"mode": "tube", "duration_minutes": 50, "from": "South Kensington",
                                  "to": "Heathrow Terminals 2 & 3"}], captured)
    assert captured[0][1].startswith("Heathrow Terminals 2 & 3")
    assert out["to_address"] == "Heathrow" and out["success"] is True
    assert "endpoint_check" not in out


def test_r4_12_a_long_final_walk_is_flagged(monkeypatch):
    out = _commute(monkeypatch, [{"mode": "tube", "duration_minutes": 60, "to": "Hatton Cross"},
                                 {"mode": "walking", "duration_minutes": 48, "from": "Hatton Cross",
                                  "to": "Airside Road Tunnel"}], [])
    assert out["endpoint_check"] == "final_walk_long" and "48-minute walk" in out["caveat"]


def test_r4_12_other_places_are_unchanged():
    from core.tools.calculate_commute import transport_hub_address
    assert transport_hub_address("Heathrow Airport, London") .startswith("Heathrow Terminals")
    assert transport_hub_address("Hatton Garden") == "Hatton Garden"
    assert transport_hub_address("Bank") == "Bank"
