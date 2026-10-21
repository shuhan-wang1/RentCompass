"""R4-09 follow-up: the two DeepSeek acceptance (acc-r10) failures, on the recorded texts.

``tests/data/r5_r10_corpus.json`` holds the agent's checkpointed draft and the critic output
the format pass validated, with the state keys the validator reads, exported from the frozen
r10 inst-D checkpoints:

* conversation ebea7b9f… (request bb5cdb25…): "Which is the better commute to Canary Wharf,
  Stratford or Bow, and is either under 25 minutes?" calculate_commute: Stratford 20 min
  (walk 4, Jubilee 9, walk 7), Bow 29 min (walk 14, DLR 9, walk 6); the user's cap is 25.
* request 385542cc…: a recall answer listing the three saved facts.
"""
import json
import pathlib

from core.candidate_validation import validate_commute_response

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r10_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _case(name):
    return CORPUS[name]["text"], CORPUS[name]["state"]


def test_measured_figure_beside_a_satisfied_bound_is_kept():
    text, state = _case("r10_bb5cdb25_critic")
    out = validate_commute_response(text, state)
    assert "Yes — Stratford at 20 minutes is comfortably under 25 minutes." in out
    assert "Bow at 29 minutes is not; it sits above the 25-minute threshold" in out


def test_side_by_side_measured_routes_and_legs_are_kept():
    text, state = _case("r10_bb5cdb25_critic")
    out = validate_commute_response(text, state)
    assert "Stratford is the clearly better commute: 20 minutes versus 29 minutes" in out
    assert ("Both routes are direct with no interchanges, but Stratford's advantage comes from a "
            "much shorter walk to the station (4 min vs 14 min)") in out


def test_the_weekly_saving_follows_the_measured_difference():
    """Owner rule (r12): a weekly total derived from a measured difference quoted on the line,
    by the ordinary multipliers (x2 a day, x5 or the user's office days a week), is evidenced:
    9 min each way x 2 x 5 = "roughly 90 minutes". Arithmetic that does not follow is still
    removed, with the Note."""
    text, state = _case("r10_bb5cdb25_critic")
    out = validate_commute_response(text, state)
    assert "Over a five-day week, Stratford saves roughly 90 minutes of commuting compared with Bow." in out
    assert NOTE_EN not in out
    wrong = validate_commute_response(text.replace("roughly 90 minutes", "roughly 150 minutes"), state)
    assert "150 minutes" not in wrong
    assert wrong.count(NOTE_EN) == 1


def test_the_agent_draft_is_kept_whole():
    """The draft's cap margins ("about 5 minutes to spare", "roughly 4 minutes over"), the leg
    comparison and "identical at 9 minutes on both routes" are all measured."""
    text, state = _case("r10_bb5cdb25_draft")
    assert validate_commute_response(text, state) == text


def test_false_bounds_and_wrong_comparisons_are_still_removed():
    text, state = _case("r10_bb5cdb25_critic")
    for true, false in (
            ("Stratford at 20 minutes is comfortably under 25 minutes",
             "Stratford at 20 minutes is comfortably under 15 minutes"),
            ("Stratford at 20 minutes is comfortably under 25 minutes",
             "Stratford at 22 minutes is comfortably under 25 minutes"),
            ("20 minutes versus 29 minutes", "20 minutes versus 31 minutes"),
            ("(4 min vs 14 min)", "(14 min vs 4 min)"),     # Stratford's walk is the 4
            ("(4 min vs 14 min)", "(4 min vs 12 min)")):
        assert true in text
        out = validate_commute_response(text.replace(true, false), state)
        assert false not in out, false
        assert out.count(NOTE_EN) == 1, false


def test_recall_caveat_after_a_commute_topic_is_not_a_commute_claim():
    """r10 385542cc: ", so some of the above may have come from that as well" followed
    "extracts durable details you mention (budget, areas, commute, needs)", was removed, and
    the Note was appended although no commute claim went."""
    text, state = _case("r10_385542cc_draft")
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out
    assert "Your maximum commute time is 35 minutes" in out


def test_the_above_as_a_reference_vs_above_as_a_verdict():
    text, state = _case("r10_385542cc_draft")
    claim = "Your commute from Hackney is above 50 minutes."
    out = validate_commute_response(f"{text}\n\n{claim}", state)
    assert claim not in out and out.count(NOTE_EN) == 1
