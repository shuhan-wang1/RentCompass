"""acc-r21 (round 6e): one place measured twice, move-in totals, the cached-record caveat.

``tests/data/r6_r21_corpus.json`` is exported from the frozen r21 inst-D checkpoints:

* r21_8f4f0a00 "I go into the office 3 days a week. Compare Stratford and Bow for a commute to
  Liverpool Street …" — calculate_commute Stratford 20 / Bow 27 and compare_or_rank_areas Bow
  28. The draft and the regeneration were correct (from 20 and 27). The critic flagged both;
  the redactor stripped the Bow header, the 13-minute walk and the per-period lines.
* r21_63fb6f8a "What would a 6-week deposit and the first month's rent come to for a £1,950/month
  flat …? Find me one under £2,000 too." — "£2,700 + £1,950 = £4,650" flagged as an unsupported
  price, and the shipped turn was the listing ledger alone, without the deposit answer.
* r21_0136f2f6 "Tell me more about Jacob House" — "First, this is a cached record, not a live
  check — … have not been verified as current …" went, leaving "Two things … Second, …".
"""
import json
import pathlib

import pytest

from core.agent_loop import build_fc_nodes
from core.candidate_validation import (
    _clean_commute_fragments, _response_commute_bindings, validate_commute_response,
    validate_evidence_scope,
)
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import (
    evaluate_grounding, unsupported_external_numbers, unsupported_reply_prices,
)

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r21_corpus.json").read_text(encoding="utf-8"))
LEDGER_HEAD = "Meets all declared conditions (verified):"


def _ctx(key):
    case = CORPUS[key]
    state = json.loads(json.dumps(case["state"]))
    return case, state, _collect_grounding_evidence(state, ""), case["message"]


def _grounded(text, evidence, message):
    return evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


# ── r21 8f4f0a00: one place, two tool times ─────────────────────────────────────

SAVING = ("So Stratford is the faster option: it saves you **7 minutes each way, 14 minutes a day, "
          "42 minutes a week and roughly 3 hours a month** compared with Bow.")


@pytest.mark.parametrize("field", ["raw_draft", "critic_text"])
def test_the_correct_comparison_and_its_regeneration_are_kept_and_grounded(field):
    case, state, evidence, message = _ctx("r21_8f4f0a00")
    text = case[field]
    assert validate_commute_response(text, state) == text
    assert not unsupported_external_numbers(text, evidence, message)
    assert _grounded(text, evidence, message)


def test_the_route_binds_bow_and_the_ranking_time_is_its_alternative():
    _, state, _, _ = _ctx("r21_8f4f0a00")
    bindings, _ = _response_commute_bindings(state)
    bow = [b for b in bindings if any("Bow" in label for label in b["labels"])]
    assert len(bow) == 1 and bow[0]["minutes"] == 27.0 and bow[0]["place_alternatives"] == [28.0]


@pytest.mark.parametrize("header,ok", [
    ("**Bow — 28 minutes each way**", True),    # the ranking's own time for Bow
    ("**Bow — 25 minutes each way**", False),   # neither measurement
])
def test_a_bow_time_holds_only_if_one_measurement_gives_it(header, ok):
    case, state, evidence, message = _ctx("r21_8f4f0a00")
    text = case["raw_draft"].replace("**Bow — 27 minutes each way**", header)
    assert (header in validate_commute_response(text, state)) is ok
    assert unsupported_external_numbers(text, evidence, message) is (not ok)


@pytest.mark.parametrize("sentence,ok", [
    # consistent with the ranking's 28 throughout
    ("So Stratford saves you 8 minutes each way, 16 minutes a day and 48 minutes a week compared with Bow.", True),
    # mixed: 7 a way (27) but 48 a week (28) in one chain
    ("So Stratford saves you 7 minutes each way, 14 minutes a day and 48 minutes a week compared with Bow.", False),
    ("Bow is 27 minutes each way, so 28 × 2 × 3 = 168 minutes a week on the road.", False),
    # matches no measurement
    ("So Stratford saves you 5 minutes each way compared with Bow.", False),
    ("So Stratford saves you 7 minutes each way and roughly 4 hours a month compared with Bow.", False),
])
def test_one_chain_must_follow_one_measurement(sentence, ok):
    case, state, evidence, message = _ctx("r21_8f4f0a00")
    assert SAVING in case["raw_draft"]
    text = case["raw_draft"].replace(SAVING, sentence)
    assert (sentence in validate_commute_response(text, state)) is ok
    assert unsupported_external_numbers(text, evidence, message) is (not ok)


ROWS = ("| Per day (return) | 40 min | 54 min |", "| Per week (3 days) | 2 h 0 min | 2 h 42 min |",
        "| Per month (approx. 13 days) | 8 h 40 min | 11 h 42 min |")


@pytest.mark.parametrize("replacements,ok", [
    # the whole Bow column on the ranking's 28
    ({ROWS[0]: "| Per day (return) | 40 min | 56 min |", ROWS[1]: "| Per week (3 days) | 2 h 0 min | 2 h 48 min |",
      ROWS[2]: "| Per month (approx. 13 days) | 8 h 40 min | 12 h 8 min |"}, True),
    # one row on 28 in a column on 27: a table is one chain
    ({ROWS[1]: "| Per week (3 days) | 2 h 0 min | 2 h 48 min |"}, False),
    # neither measurement (25)
    ({ROWS[0]: "| Per day (return) | 40 min | 50 min |"}, False),
])
def test_a_period_table_follows_one_measurement(replacements, ok):
    case, state, evidence, message = _ctx("r21_8f4f0a00")
    text = case["raw_draft"]
    for old, new in replacements.items():
        assert old in text
        text = text.replace(old, new)
    out = validate_commute_response(text, state)
    assert all(row in out for row in replacements.values()) is ok
    assert unsupported_external_numbers(text, evidence, message) is (not ok)


def test_a_possessive_names_its_route_and_a_wrong_walk_still_goes():
    case, state, _, _ = _ctx("r21_8f4f0a00")
    right = "versus Stratford's 4-minute walk"
    assert right in validate_commute_response(case["raw_draft"], state)
    text = case["raw_draft"].replace(right, "versus Stratford's 9-minute walk")
    assert "9-minute" not in validate_commute_response(text, state)


# ── r21 63fb6f8a: the move-in total ────────────────────────────────────────────

def test_the_deposit_answer_is_kept_and_its_total_is_evidenced():
    case, state, evidence, message = _ctx("r21_63fb6f8a")
    draft = case["raw_draft"]
    assert validate_commute_response(draft, state) == draft
    assert "That is before any holding deposit" in validate_commute_response(draft, state)
    assert unsupported_reply_prices(draft, evidence) == []
    assert _grounded(draft, evidence, message)


@pytest.mark.parametrize("wrong,value", [
    ("**Total: £4,800**", 4800.0),            # a wrong total of the same shape
    ("**Total: £3,575**", 3575.0),            # Jacob House's deposit + the flat share's rent
])
def test_a_wrong_move_in_total_is_still_flagged(wrong, value):
    case, _, evidence, _ = _ctx("r21_63fb6f8a")
    text = case["raw_draft"].replace("**Total: £4,650**", wrong)
    assert wrong in text
    assert unsupported_reply_prices(text, evidence) == [value]


def test_a_multi_sentence_paragraph_is_not_an_orphan_clause_but_a_leftover_clause_is():
    case, _, _, _ = _ctx("r21_63fb6f8a")
    assert _clean_commute_fragments(case["raw_draft"]) == (case["raw_draft"], False)
    assert _clean_commute_fragments("Bow fits.\nwhich is well under your 30-minute limit.") == ("Bow fits.", True)
    assert _clean_commute_fragments("Bow fits.\nand which keeps you under 30 mins") == ("Bow fits.", True)


class _NoTools:
    def list_specs(self):
        return []


def _format(state, answer, message=None):
    state = dict(state, final_response=answer)
    if message is not None:
        state["extracted_context"] = dict(state.get("extracted_context") or {}, current_message=message)
    return build_fc_nodes(_NoTools())["format_output_fc"](state)["final_response"]


def test_the_deposit_answer_leads_and_the_listing_ledger_follows():
    case, state, _, _ = _ctx("r21_63fb6f8a")
    out = _format(state, case["raw_draft"])
    assert out.startswith("**Upfront cost on a £1,950/month flat**")
    assert "**Total: £4,650**" in out
    assert out.index("**Total: £4,650**") < out.index(LEDGER_HEAD)


def test_a_plain_search_turn_still_ships_the_ledger_alone():
    case, state, _, _ = _ctx("r21_63fb6f8a")
    out = _format(state, case["raw_draft"], message="Find me a flat in Hackney under £2,000.")
    assert out.startswith(LEDGER_HEAD) and "£4,650" not in out


# ── r21 0136f2f6: the cached-record caveat and its enumeration ─────────────────

CAVEAT = ("First, this is a cached record, not a live check — the price and availability have not "
          "been verified as current, so confirm both with the agent before relying on them.")


def test_the_cached_record_caveat_is_a_caveat():
    case, state, _, _ = _ctx("r21_0136f2f6")
    assert CAVEAT in case["raw_draft"]
    assert validate_evidence_scope(case["raw_draft"], state) == case["raw_draft"]


def test_a_withdrawn_item_takes_its_count_and_the_others_ordinals():
    case, state, _, _ = _ctx("r21_0136f2f6")
    text = case["raw_draft"].replace(
        CAVEAT, "First, the price and availability were checked live today, so it is ready to book.")
    out = validate_evidence_scope(text, state)
    assert "checked live today" not in out
    assert "Two things to keep in mind" not in out and "Second," not in out
    assert "It is still over your budget, so it remains an alternative rather than a match." in out


SOURCE = "- Source: OnTheMarket, cached snapshot taken 21 July 2026"


@pytest.mark.parametrize("sentence,kept", [
    # caveats in the wordings the recorded drafts use (r07, r10)
    ("I haven't checked it against the live listing, so the price may be out of date.", True),
    ("Both entries were not refreshed live, so the recorded prices and availability are unverified.", True),
    ("This is a cached snapshot, not a live check.", True),
    # a live claim next to a caveat about something else is still a live claim
    ("The price is live and current, and the furnishing is unverified.", False),
    ("The price is live; I haven't checked the photos.", False),
])
def test_a_caveat_names_the_live_check_it_did_not_make(sentence, kept):
    case, state, _, _ = _ctx("r21_0136f2f6")
    text = case["raw_draft"].replace(SOURCE, f"- Source: OnTheMarket. {sentence}")
    assert (sentence in validate_evidence_scope(text, state)) is kept


def test_an_unhedged_live_claim_still_goes():
    case, state, _, _ = _ctx("r21_0136f2f6")
    text = case["raw_draft"].replace("- Source: OnTheMarket, cached snapshot taken 21 July 2026",
                                     "- Source: OnTheMarket. The price is live and verified as current today.")
    out = validate_evidence_scope(text, state)
    assert "live and verified as current today" not in out
    assert CAVEAT in out and "Second, it is still over your budget" in out
