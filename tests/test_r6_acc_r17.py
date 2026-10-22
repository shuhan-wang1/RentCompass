"""acc-r17 (round 6, output): a route ranked AND measured, leg figures of the named route, a
combined TfL sentence.

``tests/data/r6_r17_corpus.json`` is exported from the frozen r17 inst-D checkpoints (the
model's draft as ``raw_draft``, the final state; ``critic_text`` is the critic's regeneration):

* r17_8c35fd5b "I go into the office 3 days a week. Compare Stratford and Bow …" — area ranking
  Bow 27; calculate_commute Stratford 19 (walk 4, Central 9, walk 6) and Bow 27 (walk 13 to Bow
  Road, District 1, Central 6, walk 6); office days 3.
* r17_392cdf42 "My commute limit is 25 minutes …" — the same two journeys, cap 25.
* r17_bf086e84 Holloway 22, Camden 32, Kentish Town 21 (control: shipped unchanged).
* r17_4f27cfd2 "Find me a 1-bed in Islington under £2,200 …" — listings with estimates (King
  Square ~20) and an Islington → King's Cross TfL journey of 14 (walk 1, bus 476 10, walk 3).

Every precision case has a wrong figure of the same shape that is still removed, and the critic
agrees with the redactor on both.
"""
import json
import pathlib

import pytest

from core.candidate_validation import (_guard_text_parts, _response_commute_bindings,
                                       validate_commute_response)
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r17_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _case(name):
    case = CORPUS[name]
    state = case["state"]
    return case, state, _collect_grounding_evidence(state, ""), case["message"]


def _grounded(text, evidence, message):
    return evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


def _mutate(name, old, new, field="raw_draft"):
    case, state, evidence, message = _case(name)
    assert old in case[field], old
    text = case[field].replace(old, new)
    return text, validate_commute_response(text, state), unsupported_external_numbers(text, evidence, message)


# ── the four recorded drafts ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,field", [("r17_bf086e84", "raw_draft"), ("r17_4f27cfd2", "raw_draft")])
def test_the_recorded_answer_ships_whole_and_grounded(name, field):
    case, state, evidence, message = _case(name)
    text = case[field]
    assert validate_commute_response(text, state) == text
    assert not unsupported_external_numbers(text, evidence, message)
    assert _grounded(text, evidence, message)


def test_the_limit_answer_keeps_the_walk_and_drops_only_the_speculation():
    """392cdf42: "Bow's problem is mostly the 13-minute walk to Bow Road" quotes Bow's own walk
    leg; the speculation about an unmeasured flat ("could well come in under 25 minutes") still
    goes, with the condition and the advice leaning on it, and no fragment of them is left."""
    case, state, evidence, message = _case("r17_392cdf42")
    out = validate_commute_response(case["raw_draft"], state)
    assert "it's both faster and change-free. Bow's problem is mostly the 13-minute walk to Bow Road." in out
    assert "could well come in under 25 minutes" not in out
    assert "if you were looking" not in out.lower() and "worth checking individual addresses" not in out
    assert "- 27 minutes total, 2 minutes over your limit" in out
    assert out.count(NOTE_EN) == 1
    assert not unsupported_external_numbers(case["raw_draft"], evidence, message)


# ── O2: a route the area ranking ranked and calculate_commute measured is one route ───

def test_the_ranked_and_measured_route_is_one_binding():
    """The trigger was never the leg sum (13 + 1 + 6 + 6 = 26 against 27: leg sums are not
    checked at all). compare_or_rank_areas' "Bow" (27) and calculate_commute's "Bow, London"
    (27) were two equally specific "Bow" bindings, so every Bow line was ambiguous."""
    _, state, _, _ = _case("r17_8c35fd5b")
    bindings, _ = _response_commute_bindings(state)
    bow = [b for b in bindings if "Bow" in b["labels"]]
    assert len(bow) == 1 and bow[0]["minutes"] == 27.0 and len(bow[0]["legs"]) == 4


def test_a_ranked_row_with_another_time_stays_a_competing_measurement():
    _, state, _, _ = _case("r17_8c35fd5b")
    state = json.loads(json.dumps(state))
    state["tool_artifacts"][0]["raw_data"]["areas"][0]["commute_minutes"] = 25
    bindings, _ = _response_commute_bindings(state)
    assert sorted(b["minutes"] for b in bindings if "Bow" in b["labels"]) == [25.0, 27.0]


def test_the_route_block_and_its_legs_are_kept():
    case, state, _, _ = _case("r17_8c35fd5b")
    out = validate_commute_response(case["raw_draft"], state)
    assert ("**Bow — 27 minutes each way**\n- Walk 13 min to Bow Road, District line 1 min to Mile End, "
            "Central line 6 min to Liverpool Street, 6 min walk\n- One change") in out


def test_a_wrong_route_total_keeps_the_route_name_so_no_bullet_is_orphaned():
    text, out, flagged = _mutate("r17_8c35fd5b", "**Bow — 27 minutes each way**", "**Bow — 25 minutes each way**")
    assert "25 minutes" not in out
    assert "**Bow**\n- Walk 13 min to Bow Road" in out          # the legs stay under their route
    assert out.count(NOTE_EN) == 1 and flagged


def test_a_wrong_leg_figure_is_removed():
    text, out, flagged = _mutate("r17_8c35fd5b", "Walk 13 min to Bow Road, District",
                                 "Walk 15 min to Bow Road, District")
    assert "15 min" not in out and "District line 1 min to Mile End" in out
    assert flagged


# ── O3: leg figures of the named route are evidence ───────────────────────────────────

WALK = ("Bow's problem is mostly the 13-minute walk to Bow Road — if you were looking at a specific flat "
        "nearer Bow Road or Mile End station, that journey could well come in under 25 minutes, so it's "
        "worth checking individual addresses rather than ruling out the whole area.")


@pytest.mark.parametrize("sentence,kept", [
    ("Bow's problem is mostly the 13-minute walk to Bow Road.", True),
    ("The gap is mostly the 13-minute walk to Bow Road versus 4 minutes at Stratford.", True),
    ("Bow's problem is mostly the 15-minute walk to Bow Road.", False),   # no such leg
    ("Bow's problem is mostly the 6-minute walk to Bow Road.", False),    # Bow's OTHER walk
    ("The gap is mostly the 13-minute walk to Bow Road versus 6 minutes at Stratford.", False),
    ("The gap is mostly the 13-minute walk to Bow Road versus 5 minutes at Stratford.", False),
])
def test_a_walk_leg_is_evidence_only_as_the_named_routes_own_leg(sentence, kept):
    text, out, flagged = _mutate("r17_392cdf42", WALK, sentence)
    assert (sentence in out) is kept
    if kept:
        assert NOTE_EN not in out and not flagged
    elif "5 minutes at" in sentence:
        assert flagged          # a compared figure the turn never measured
    # The critic checks figures against the evidence, not against legs; "a 15-minute walk" in
    # plain prose is a walking estimate it deliberately leaves alone (test_critic_grounding).


def test_a_withdrawn_walk_leaves_no_plus_remnant():
    """8c35fd5b: with the walk figure wrong, "The extra change at Mile End." shipped alone."""
    text, out, flagged = _mutate("r17_8c35fd5b", "the 13-minute walk to Bow Road versus",
                                 "the 15-minute walk to Bow Road versus")
    assert "extra change at Mile End" not in out
    assert "So Stratford is the quicker option, and the saving is modest — under an hour a week." in out
    assert flagged


def test_the_figure_above_is_a_reference_not_a_verdict():
    """"The 19-minute Stratford figure above comes from the separate journey calculation": "above"
    points at the answer, and "from the separate journey calculation" names no origin."""
    case, state, _, _ = _case("r17_8c35fd5b")
    sentence = "The 19-minute Stratford figure above comes from the separate journey calculation."
    assert sentence in validate_commute_response(case["raw_draft"], state)
    text, out, _ = _mutate("r17_8c35fd5b", "The 19-minute Stratford figure above",
                           "The 21-minute Stratford figure above")
    assert "21-minute" not in out and out.count(NOTE_EN) == 1


# ── O4: the combined TfL sentence ─────────────────────────────────────────────────────

TFL = "a real route of about 14 minutes to King's Cross — a short walk, the 476 bus, then a 3-minute walk, with no changes."


@pytest.mark.parametrize("old,new", [
    ("about 14 minutes to King's Cross", "about 18 minutes to King's Cross"),
    ("then a 3-minute walk", "then a 5-minute walk"),
    ("then a 3-minute walk", "then a 10-minute walk"),          # the bus leg's figure, called a walk
])
def test_a_wrong_total_or_leg_takes_the_tfl_sentence_with_it(old, new):
    case, state, evidence, message = _case("r17_4f27cfd2")
    assert TFL in case["raw_draft"]
    text = case["raw_draft"].replace(old, new)
    out = validate_commute_response(text, state)
    assert new not in out
    assert "the 476 bus." not in out                            # no truncated route left
    assert out.count(NOTE_EN) == 1
    # The critic only sees "18 minutes" (a listing estimate) and "5-/10-minute walk" (a walking
    # estimate, or the bus leg's 10) as figures the evidence holds; the redactor binds them.


def test_the_flat_estimate_after_the_area_route_is_the_flats_own():
    """"That's a measured journey from the Islington area rather than from the King Square flat
    specifically, so treat the flat's own ~20 minutes as an estimate": "the flat" is King Square,
    whose search estimate is 20; a different figure still goes."""
    case, state, _, _ = _case("r17_4f27cfd2")
    assert "so treat the flat's own ~20 minutes as an estimate" in validate_commute_response(case["raw_draft"], state)
    text, out, flagged = _mutate("r17_4f27cfd2", "the flat's own ~20 minutes", "the flat's own ~25 minutes")
    assert "~25 minutes" not in out and flagged

