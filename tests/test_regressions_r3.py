"""Regression tests (R3-xx / K-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_HACKNEY_2BED = {"area": "Hackney", "areas": ["Hackney"], "bedrooms": 2, "max_budget": 2800,
                 "room_type": "whole"}


# R3-01 ── a combined "area + budget" change moves both ─────────────────────────────
@pytest.mark.parametrize("message,area,budget", [
    ("Change area to Clapham and budget to £3,200", "Clapham", 3200),
    ("Set budget to £2,400 and area to Camden", "Camden", 2400),
    ("Budget £2,600 and area Camden", "Camden", 2600),
    ("Switch to Clapham instead, budget £3,200", "Clapham", 3200),
])
def test_r3_01_area_and_budget_in_one_message_both_apply(message, area, budget):
    acc = _state_node(_HACKNEY_2BED, message)
    assert acc["area"] == area and acc["areas"] == [area]
    assert acc["max_budget"] == budget


def test_r3_01_a_route_from_this_area_is_not_a_switch():
    from core.tools.search_properties import _extract_area
    assert _extract_area("Is the commute from this area to Camden long?") is None
    assert _extract_area("What is the area like?") is None


# R3-02 ── a close misspelling after a switch cue, and a bare "show <Area>" ─────────
@pytest.mark.parametrize("message,area", [
    ("actually make that Eustan, and budget 1400", "Euston"),
    ("actually make that Bethnal Grean, budget 1800", "Bethnal Green"),
    ("How about Camdn instead?", "Camden"),
    ("how about Peckam", "Peckham"),
    ("ok show Islington", "Islington"),
    ("show Hackney", "Hackney"),
])
def test_r3_02_a_follow_up_switch_names_the_new_area(message, area):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) == area
    acc = _state_node({"area": "King's Cross", "areas": ["King's Cross"], "max_budget": 1400}, message)
    assert acc["area"] == area


@pytest.mark.parametrize("message", [
    "make it cheaper", "what about parking?", "how about later", "show me more",
    "show me the first one", "make it bigger please", "try again", "show the cheapest",
    "what about bills?", "make that two bedrooms", "what about the price instead?",
])
def test_r3_02_ordinary_words_after_a_cue_are_not_places(message):
    from core.tools.search_properties import _extract_area
    assert _extract_area(message) is None


# R3-03 / K-03 ── a workplace move replaces the destination; frequency is not a place ─
_UNI = "University College London, Gower Street, London WC1E 6BT"


@pytest.mark.parametrize("message,destination", [
    ("I actually work in Shoreditch now, not UCL. Search again.", "Shoreditch"),
    ("I actually work in Shoreditch now, not UCL. Find a 1 bed in Hackney under £2300", "Shoreditch"),
    ("My office moved to Waterloo. Update the search.", "Waterloo"),
    ("My office has moved to Waterloo", "Waterloo"),
    ("My office is now in Waterloo", "Waterloo"),
    ("My office is moving to Waterloo next month", "Waterloo"),
])
def test_r3_03_a_workplace_correction_replaces_the_destination(message, destination):
    acc = _state_node({"area": "Hackney", "areas": ["Hackney"], "commute_destination": _UNI,
                       "destination": _UNI}, message)
    assert acc["commute_destination"] == destination


@pytest.mark.parametrize("message,destination", [
    ("I want a studio in Hackney under £1,400, I commute to Liverpool Street 5 days a week",
     "Liverpool Street"),
    ("I work in Bank every day", "Bank"),
    ("I commute to Old Street three times a week", "Old Street"),
])
def test_k_03_a_frequency_phrase_is_not_part_of_the_destination(message, destination):
    acc = _state_node({}, message)
    assert acc["commute_destination"] == destination


def test_k_03_the_commute_tool_drops_a_frequency_phrase(monkeypatch):
    import core.maps_service as maps
    from core.tools.calculate_commute import calculate_commute_impl
    seen = []

    def fake(origin, destination, mode):
        seen.append(destination)
        return None
    monkeypatch.setattr(maps, "calculate_travel_details", fake)
    calculate_commute_impl("1 Sample Road, Hackney E8", "Liverpool Street 5 days a week")
    assert seen == ["Liverpool Street"]


# R3-04 ── a retraction with a new value in the same sentence is a replacement ──────
@pytest.mark.parametrize("message,budget", [
    ("Actually ignore my saved rent maximum, I can pay up to £3,500 this time", 3500),
    ("Ignore the budget I mentioned before, now it's £3,500 max", 3500),
    ("Disregard my old budget, it's £3,000 now", 3000),
    ("Ignore the £2,000 budget - £3,200 max now.", 3200),
])
def test_r3_04_ignore_the_old_budget_then_a_new_one_sets_the_new_one(message, budget):
    for start in ({"area": "Hackney", "max_budget": 2500}, {"area": "Hackney"}):
        acc = _state_node(start, message)
        assert acc["max_budget"] == budget


@pytest.mark.parametrize("message", [
    "Ignore my friend's £1,800 flat, it was just an example.",
    "Ignore the Watford experiment at £900, it was only a test",
])
def test_r3_04_a_withdrawn_reference_still_changes_nothing(message):
    acc = _state_node({"area": "Hackney", "max_budget": 2500}, message)
    assert acc["max_budget"] == 2500


def test_r3_04_the_search_grounds_the_replacement_budget():
    from core.tools.search_properties import ground_hard_constraints
    message = "Ignore the budget I mentioned before, now it's £3,500 max"
    acc = _state_node({"area": "Hackney", "areas": ["Hackney"], "bedrooms": 1}, message)
    params, dropped = ground_hard_constraints({"area": "Hackney", "max_budget": 3500}, acc, message)
    assert params["max_budget"] == 3500 and "max_budget" not in dropped


# R3-05 ── "never mind studio" lifts the room type ──────────────────────────────────
@pytest.mark.parametrize("message,room_type,bedrooms", [
    ("Never mind studio, 2 beds", "whole", 2),
    ("Forget the studio, 2 beds", "whole", 2),
    ("I no longer need a studio, a 1-bed is fine", "whole", 1),
    ("never mind studio", None, None),
    ("Scrap the studio idea", None, None),
])
def test_r3_05_a_withdrawn_studio_is_not_kept(message, room_type, bedrooms):
    acc = _state_node({"area": "Hackney", "areas": ["Hackney"], "max_budget": 2500,
                       "room_type": "studio"}, message)
    assert acc.get("room_type") == room_type
    assert acc.get("bedrooms") == bedrooms


def test_r3_05_dont_forget_the_studio_keeps_it():
    acc = _state_node({"area": "Hackney", "room_type": "studio"}, "Don't forget I want a studio")
    assert acc["room_type"] == "studio"


# R3-06 ── a question in its own sentence does not hide the budget before it ────────
@pytest.mark.parametrize("message", [
    "Make it £2,800. Also what do you remember about me?",
    "Make it £2,800. What do you remember?",
])
def test_r3_06_a_budget_before_a_separate_question_applies(message):
    acc = _state_node({"area": "Hackney", "max_budget": 2500}, message)
    assert acc["max_budget"] == 2800


@pytest.mark.parametrize("message", ["Is £2,800 too much?", "What is £2,800 per week in monthly terms?"])
def test_r3_06_a_question_about_a_figure_is_still_not_a_budget(message):
    acc = _state_node({"area": "Hackney", "max_budget": 2500}, message)
    assert acc["max_budget"] == 2500


def test_r3_06_a_decimal_amount_is_not_split_as_a_sentence():
    from core.tools.search_properties import _extract_budget
    assert _extract_budget("Find a flat for £1,750.50 a month") == (1750.5, "month")


# R3-10 ── a one-off distance question about a listing is not a criterion ───────────
@pytest.mark.parametrize("message", [
    "Out of curiosity how far is the first one from UCL?",
    "How far is it from UCL?",
    "How long is the commute from the second one to King's Cross?",
    "换成中文回答：第一套离国王十字远吗？",
    "第一套到国王十字要多久？",
])
def test_r3_10_a_distance_question_keeps_area_and_no_commute(message):
    acc = _state_node({"area": "Hackney", "areas": ["Hackney"], "max_budget": 2300,
                       "no_commute": True}, message)
    assert acc["area"] == "Hackney" and acc["areas"] == ["Hackney"]
    assert acc.get("no_commute") is True and not acc.get("commute_destination")


def test_r3_10_a_stated_workplace_still_sets_the_destination():
    acc = _state_node({"area": "Hackney", "no_commute": True},
                      "I study at UCL, how far is the first one from there?")
    assert acc["commute_destination"] == _UNI and acc["no_commute"] is False
    acc = _state_node({"area": "Hackney"}, "Find a flat in Hackney, how long is the commute to UCL?")
    assert acc["commute_destination"] == _UNI


def test_r3_10_a_zh_distance_question_names_no_area():
    from core.tools.search_properties import _extract_area
    assert _extract_area("第一套离国王十字远吗？") is None
    assert _extract_area("我想在国王十字找房") == "King's Cross"


# R3-07 / K-01 ── the verified header never covers a requirement nobody checked ──────
@pytest.mark.parametrize("message", [
    "Pet-friendly furnished 1 bed with a garden in Hackney under £2500",
    "Furnished pet-friendly 2 bed with a garden in Islington under £3300",
])
def test_r3_07_adjective_requirements_are_required_features(message):
    acc = _state_node({}, message)
    assert set(acc["property_features"]) == {"pets allowed", "furnished"}


@pytest.mark.parametrize("message", [
    "Ideally a furnished flat in Camden", "An unfurnished 1 bed in Camden", "Is the first one furnished?",
])
def test_r3_07_soft_or_question_wording_adds_no_requirement(message):
    assert not _state_node({}, message).get("property_features")


_ROW = {"address": "1 Sample Road, Hackney E8", "area": "Hackney", "price": 2000, "bedrooms": 1,
        "room_type": "flat"}


@pytest.mark.parametrize("language,header,note", [
    ("en", "Meets the checked conditions (verified):", "Not checked for any listing"),
    ("zh", "满足已核实的条件：", "以下要求未逐条核实"),
])
def test_r3_07_unchecked_requirements_are_listed_under_a_narrower_header(language, header, note):
    from core.candidate_validation import render_candidate_status, validate_candidates
    criteria = {"max_budget": 2500, "areas": ["Hackney"],
                "soft_preferences": ["garden", "no basement or ground-floor flats", "不要一楼",
                                     "Would like gym", "my budget could stretch to £2,600"]}
    text = render_candidate_status(validate_candidates([dict(_ROW)], criteria), language=language)
    assert header in text and note in text
    assert "Meets all declared conditions" not in text and "满足全部已声明条件" not in text
    for item in ("garden", "no basement or ground-floor flats", "不要一楼", "gym"):
        assert item in text
    assert "stretch" not in text


def test_r3_07_the_header_is_unchanged_when_everything_was_checked():
    from core.candidate_validation import render_candidate_status, validate_candidates
    text = render_candidate_status(validate_candidates([dict(_ROW)], {"max_budget": 2500,
                                                                      "areas": ["Hackney"]}))
    assert text.startswith("Meets all declared conditions (verified):")
    assert "Not checked" not in text

