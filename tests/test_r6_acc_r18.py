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
