"""acc-r18 (round 6b, output): an "each way" difference on a "Per office day" line, the closing
search offer, and the answer when the model never produced a usable search request.

``tests/data/r6_r18_corpus.json`` is exported from the frozen r18 inst-D checkpoints:

* r18_eb65e918 "I go into the office 3 days a week. Compare Stratford and Bow …" — area ranking
  Stratford 18, Bow 29 (difference 11); office days 3. The draft (``raw_draft``) was graded
  grounded by the critic, yet the redactor removed "11 minutes each way" (leaving "- Per office
  day: so 22 minutes of travel per day") and the figure-free offer to search listings.
* r18_c14fe21d "I've changed jobs, I work at Bank now. Find me a 1-bed within 30 minutes of work
  under £2,000." — the model emitted unparsable JSON for search_properties twice; nothing ran;
  ``shipped`` is what the user saw.
"""
import json
import pathlib
import re

import pytest

from core.agent_loop import _artifact_grounded_fallback_answer
from core.candidate_validation import _SO_TOTAL_AFTER_DROP, _asserts_commute, validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r6_r18_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
LINE = "- Per office day: 11 minutes each way, so 22 minutes of travel per day"
OFFER = ("If you'd like, I can search actual listings in Stratford or Bow within a budget and check the "
         "door-to-door commute for specific flats.")


def _case(name):
    case = CORPUS[name]
    state = case["state"]
    return case, state, _collect_grounding_evidence(state, ""), case["message"]


def test_the_recorded_week_and_month_answer_ships_whole_and_grounded():
    case, state, evidence, message = _case("r18_eb65e918")
    text = case["raw_draft"]
    assert LINE in text and OFFER in text
    assert validate_commute_response(text, state) == text
    assert not unsupported_external_numbers(text, evidence, message)
    assert evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


@pytest.mark.parametrize("line,gone,kept", [
    # a wrong difference takes its own total with it, and the stub label goes too
    ("- Per office day: 13 minutes each way, so 26 minutes of travel per day", ["13 minutes", "26 minutes", "Per office day"], []),
    # a wrong difference never leaves "so 22 minutes" behind
    ("- Per office day: 13 minutes each way, so 22 minutes of travel per day", ["13 minutes", "so 22 minutes", "Per office day"], []),
    # a wrong total goes; the true difference stays
    ("- Per office day: 11 minutes each way, so 24 minutes of travel per day", ["24 minutes"], ["- Per office day: 11 minutes each way"]),
])
def test_a_wrong_difference_or_total_is_removed_without_a_remnant(line, gone, kept):
    case, state, evidence, message = _case("r18_eb65e918")
    text = case["raw_draft"].replace(LINE, line)
    out = validate_commute_response(text, state)
    for piece in gone:
        assert piece not in out, piece
    for piece in kept:
        assert piece in out, piece
    assert out.count(NOTE_EN) == 1
    assert "- Per week (3 office days): about 66 minutes" in out      # the rest of the block stays
    assert unsupported_external_numbers(text, evidence, message)


def test_no_so_remnant_ships_when_its_base_clause_goes():
    """The base clause goes for a reason of its own (a verdict with no limit to check it
    against); "so 22 minutes of travel per day" goes with it instead of shipping as
    "- Per office day: so 22 minutes of travel per day"."""
    case, state, _, _ = _case("r18_eb65e918")
    text = case["raw_draft"].replace(LINE, "- Per office day: 11 minutes each way over your limit, "
                                           "so 22 minutes of travel per day")
    out = validate_commute_response(text, state)
    assert "so 22 minutes" not in out and "Per office day" not in out
    assert OFFER in out and out.count(NOTE_EN) == 1


def test_a_so_clause_with_a_figure_leans_on_the_clause_before_it():
    assert _SO_TOTAL_AFTER_DROP.match(" so 22 minutes of travel per day")
    assert not _SO_TOTAL_AFTER_DROP.match(" so Stratford is the better pick")


def test_a_search_offer_within_a_budget_is_not_a_commute_verdict():
    assert not _asserts_commute(" I can search actual listings in Stratford or Bow within a budget and "
                                "check the door-to-door commute for specific flats")
    assert _asserts_commute("Stratford is within your 25-minute limit")


# ── the model never produced a usable search request ──────────────────────────────────

def test_an_unparsable_search_request_gets_a_plain_answer():
    case = CORPUS["r18_c14fe21d"]
    assert "its request was malformed" in case["shipped"]          # what the user saw
    out = _artifact_grounded_fallback_answer(case["state"], reason="tool_call_error",
                                             failed_tools=["search_properties"])
    assert out.startswith("I couldn't run the property search this time.")
    assert "Please try again, or rephrase your request." in out
    assert "malformed" not in out and "lookup" not in out


def test_the_chinese_answer_is_plain_too():
    state = json.loads(json.dumps(CORPUS["r18_c14fe21d"]["state"]))
    state["extracted_context"]["reply_language"] = "zh"
    out = _artifact_grounded_fallback_answer(state, reason="tool_call_error",
                                             failed_tools=["search_properties"])
    assert out.startswith("这次没能运行房源搜索。") and "查询参数无效" not in out
    assert "请再试一次" in out


def test_another_failed_lookup_keeps_its_own_wording():
    state = {"user_query": "What's the weather in Leeds?", "tool_artifacts": [],
             "extracted_context": {"current_message": "What's the weather in Leeds?", "reply_language": "en"}}
    out = _artifact_grounded_fallback_answer(state, reason="tool_call_error", failed_tools=["get_weather"])
    assert out.startswith("I couldn't run one of the lookups this time.")
    assert "The lookup failed this turn, so I could not check what you asked." in out
    assert "property search" not in out


# ── r6b kept-wrong gaps: a route's own time, a bike figure, an orphaned route time ─────

R17 = json.loads((pathlib.Path(__file__).parent / "data" / "r6_r17_corpus.json").read_text(encoding="utf-8"))
R17_LINE = "- Per day: 8 minutes each way, so 16 minutes a day"     # r17 8c35fd5b: Stratford 19, Bow 27


def _r17(line, state=None):
    case = R17["r17_8c35fd5b"]
    state = state or case["state"]
    text = case["raw_draft"].replace(R17_LINE, line)
    message = case["message"]
    return (text, validate_commute_response(text, state),
            unsupported_external_numbers(text, _collect_grounding_evidence(state, ""), message))


@pytest.mark.parametrize("name,old,new,kept", [
    # (a) "<route> is N minutes" states the route's OWN time
    ("r17", R17_LINE, "- Per day: Stratford is 19 minutes each way", True),
    ("r17", R17_LINE, "- Per day: Stratford is 8 minutes each way, so 16 minutes a day", False),
    ("r18", "Per office day: 11 minutes each way", "Per office day: Stratford is 18 minutes each way", True),
    ("r18", "Per office day: 11 minutes each way", "Per office day: Stratford is 11 minutes each way", False),
])
def test_a_routes_own_time_must_be_its_measured_time(name, old, new, kept):
    case = R17["r17_8c35fd5b"] if name == "r17" else CORPUS["r18_eb65e918"]
    state, message = case["state"], case["message"]
    text = case["raw_draft"].replace(old, new)
    out = validate_commute_response(text, state)
    assert (new.split(":", 1)[1].split(",")[0].strip() in out) is kept
    if not kept:
        assert out.count(NOTE_EN) == 1
        assert unsupported_external_numbers(text, _collect_grounding_evidence(state, ""), message)


def _with_bike_route(minutes=22):
    """The r17 state plus a SYNTHETIC measured bike route Stratford -> Liverpool Street (the
    recorded turns measured public transport only)."""
    import copy
    state = copy.deepcopy(R17["r17_8c35fd5b"]["state"])
    stratford = next(a for a in state["tool_artifacts"] if a["tool"] == "calculate_commute"
                     and "Stratford" in a["raw_data"]["from_address"])
    bike = copy.deepcopy(stratford)
    bike["raw_data"].update({"mode": "bicycling", "duration_minutes": minutes, "route_legs": [],
                             "route_summary": f"Cycle to Liverpool Street ({minutes} min)"})
    state["tool_artifacts"].append(bike)
    return state


def test_b_a_bike_figure_needs_a_bike_measurement():
    """Rule: a cycling/driving figure is a different journey from the transit routes (the
    redactor's standing rule for bounds, ``_DURATION_BOUND``); only a route measured in that
    mode evidences it. With no such measurement nothing does, so it is removed, not kept."""
    _, out, flagged = _r17("- Per day: 8 minutes each way by bike, so 16 minutes a day")
    assert "by bike" not in out and "Per day" not in out and flagged


@pytest.mark.parametrize("sentence,kept", [
    ("Stratford is 22 minutes by bike.", True),
    ("By bike, Stratford takes 22 minutes.", True),
    ("Stratford is 25 minutes by bike.", False),
    ("Stratford is 19 minutes by bike.", False),     # the TRANSIT figure, called a bike time
    ("Bow is 22 minutes by bike.", False),           # Stratford's bike route, borrowed
])
def test_b_with_a_bike_measurement_only_its_own_figure_is_kept(sentence, kept):
    state = _with_bike_route()
    case = R17["r17_8c35fd5b"]
    assert validate_commute_response(case["raw_draft"], state) == case["raw_draft"]   # transit lines unaffected
    text = case["raw_draft"].replace("Two caveats worth knowing:", f"{sentence}\n\nTwo caveats worth knowing:")
    out = validate_commute_response(text, state)
    assert (sentence in out) is kept
    assert out.count(NOTE_EN) == (0 if kept else 1)


@pytest.mark.parametrize("old,new", [
    (R17_LINE, "- Per day: 19 minutes each way, so 38 minutes a day"),     # r17: Stratford's own 19
    (R17_LINE, "- Per day: 19 minutes each way"),
    ("- Per office day: 11 minutes each way, so 22 minutes of travel per day",
     "- Per office day: 18 minutes each way, so 36 minutes of travel per day"),   # r18: Stratford 18
])
def test_c_a_routes_time_is_not_left_alone_under_a_savings_block(old, new):
    """Under "**Time saved by the faster one (Stratford):**" a bullet's figure is a saving; a
    route's own time there is wrong, and once its wrong total went it stayed alone on the line."""
    case = R17["r17_8c35fd5b"] if old == R17_LINE else CORPUS["r18_eb65e918"]
    text = case["raw_draft"].replace(old, new)
    out = validate_commute_response(text, case["state"])
    assert new.split(":", 1)[0] + ":" not in out           # the whole bullet goes
    assert out.count(NOTE_EN) == 1


def test_c_the_true_savings_bullets_still_ship():
    for case in (R17["r17_8c35fd5b"], CORPUS["r18_eb65e918"]):
        assert validate_commute_response(case["raw_draft"], case["state"]) == case["raw_draft"]


# ── the join after a withdrawn clause keeps its blank (r18 live 5b, rid 1787af1e) ──────

TIMES_SQUARE = ("- **Times Square, London** — 1-bed apartment, £2,250/month, about 18 minutes to Bank by public "
                "transport (TfL routing). That is £250 over your ceiling, so it is not within budget — I'm mentioning "
                "it only so you know what the market looks like at this size and commute.")


def test_the_recorded_listing_line_keeps_its_budget_sentence_and_spacing():
    """Shipped: "… £2,250/month.I'm mentioning it only …": the unverified journey clause went,
    the budget sentence after it went as a "dependent verdict" (its "over your ceiling" is about
    money), and the " — " that carried the only blank went with it."""
    case = CORPUS["r18_1787af1e"]
    assert TIMES_SQUARE in case["raw_draft"]
    out = validate_commute_response(case["raw_draft"], case["state"])
    assert ("- **Times Square, London** — 1-bed apartment, £2,250/month. That is £250 over your ceiling, so it "
            "is not within budget — I'm mentioning it only so you know what the market looks like at this size "
            "and commute.") in out
    assert not re.search(r"[a-z0-9)]\.[A-Z]", out.replace("St.", "St"))


@pytest.mark.parametrize("middle", ["That journey is 40 minutes — ", "That is 40 minutes to Bank. "])
def test_a_withdrawn_middle_sentence_leaves_exactly_one_space(middle):
    case = CORPUS["r18_1787af1e"]
    text = case["raw_draft"].replace("That is £250 over your ceiling, so it is not within budget — ", middle)
    out = validate_commute_response(text, case["state"])
    assert "40 minutes" not in out
    assert "£2,250/month. I'm mentioning it only so you know" in out
