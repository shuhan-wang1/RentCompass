"""R4-09: precision of the answer post-processors, on real drafts and states (round 5).

The commute redactor policy is unchanged: an unsupported commute claim is removed. These
cases are true claims that were removed, and fragments a post-processor left behind, each
taken from a recorded turn. ``tests/data/r5_output_corpus.json`` holds the exact text that
reached the post-processor (the agent's draft, or the critic's output that the format pass
validated again) plus the state keys it reads, exported from the frozen checkpoints of
rounds r06/r07/r08 and of the 7483f77 stress test.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.listing_identity import bind_listing_links
from uk_rent_agent.agent.critic import enforce_no_evidence_numeric_contract

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_output_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _case(name):
    case = CORPUS[name]
    return case["text"], case.get("state")


def _without_shown_listings(state):
    context = {k: v for k, v in (state.get("extracted_context") or {}).items()
               if k not in ("view_results", "last_results_full")}
    return {**state, "extracted_context": context}


# ── (a) a qualitative bound the tool's figure satisfies is evidenced ──────────────────

def test_under_half_an_hour_for_a_29_minute_area_is_kept():
    """r06 099518b9 (acceptance NEW-5): compare_or_rank_areas gives Stratford 29 minutes."""
    text, state = _case("r06_099518b9_draft")
    assert "a commute that's still under half an hour" in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_a_bound_the_figure_does_not_satisfy_is_still_removed():
    text, state = _case("r06_099518b9_draft")
    for false_bound in ("still under 20 minutes", "still under a quarter of an hour",
                        "well over an hour"):
        draft = text.replace("still under half an hour", false_bound)
        out = validate_commute_response(draft, state)
        assert false_bound not in out, false_bound
        assert out.count(NOTE_EN) == 1


def test_a_walking_time_is_not_the_measured_bound():
    text, state = _case("r06_099518b9_draft")
    draft = text.replace("for a commute that's still under half an hour",
                         "for a commute, and the station is under 30 minutes' walk")
    out = validate_commute_response(draft, state)
    assert "under 30 minutes' walk" not in out


def test_a_bound_on_an_unattributed_clause_must_hold_for_every_measured_route():
    """r06 27d8b56d: "comfortably under 20 minutes" for routes measured at 11 and 15."""
    text, state = _case("r06_27d8b56d_critic")
    assert "comfortably under 20 minutes" in text
    assert validate_commute_response(text, state) == text
    draft = text.replace("comfortably under 20 minutes", "comfortably under 12 minutes")
    assert "under 12 minutes" not in validate_commute_response(draft, state)


# ── (c) the Note only when something visible and unsupported went ────────────────────

@pytest.mark.parametrize("name", ["r06_099518b9_draft", "r06_099518b9_critic",
                                  "r06_ecbf2c1f_critic", "r06_27d8b56d_critic",
                                  "r07_97b3d456_critic", "r07_97b3d456_draft",
                                  "r07_38299e93_draft", "stress_80e0de95_draft",
                                  "stress_562f3254_draft"])
def test_recorded_answers_with_only_evidenced_claims_get_no_note(name):
    """Each of these shipped with "Note: some commute claims ... removed" on ce29db4/7483f77;
    every claim the post-processor removed is backed by this turn's (or the shown listings')
    tool output, so nothing is removed and no Note is added."""
    text, state = _case(name)
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_difference_of_two_measured_areas_is_kept():
    text, state = _case("r06_099518b9_critic")
    assert "faster commute in this data, by 8 minutes (29 minus 21)" in validate_commute_response(text, state)


def test_priority_condition_and_positional_above_are_not_commute_verdicts():
    text, state = _case("r06_099518b9_critic")
    out = validate_commute_response(text, state)
    assert ("If your own priority is a shorter journey rather than lower rent, Hackney is the "
            "stronger pick on the numbers above; if rent matters more, Stratford leads.") in out
    assert ". if rent matters more" not in out


def test_rent_clause_and_possessive_area_name_survive():
    """r06 ecbf2c1f: a money comparison beside "the commute is excellent", and "Islington's
    15-minute journey" (Islington measured at 15)."""
    text, state = _case("r06_ecbf2c1f_critic")
    out = validate_commute_response(text, state)
    assert "but the median of £2,400 pcm is £200 pcm above your ceiling" in out
    assert "Islington's 15-minute journey is compelling" in out


def test_generic_commute_offer_in_a_clarification_is_not_a_claim():
    """Stress B-02 80e0de95: a clarification turn with no commute tool got the Note."""
    text, state = _case("stress_80e0de95_draft")
    assert "within a certain commute time" in validate_commute_response(text, state)


def test_walking_distance_offer_on_a_weather_turn_is_not_a_claim():
    """Stress f-weather-unavail 562f3254: "what's within walking distance of an address",
    after a commute clause in a capability offer, was withdrawn with the Note."""
    text, state = _case("stress_562f3254_draft")
    assert "what's within walking distance of an address" in validate_commute_response(text, state)


def test_tilde_estimates_are_quoted_as_estimates():
    """r08 ff6c228f: "~28 min" is the listing's own estimate, as search_properties renders it."""
    text, state = _case("r08_ff6c228f_draft")
    out = validate_commute_response(text, state)
    assert "Queensgate House, 1 Bow Central, Hereford Road, Bow E3 — £1,950/month, ~28 min" in out
    assert "Highgate West Hill, London N6 (Camden) — £2,000/month, ~38 min" in out
    # the invented "could differ by 10-15 minutes" is still withdrawn, with the Note
    assert "10-15 minutes" not in out and out.count(NOTE_EN) == 1


# ── (b) fragments ───────────────────────────────────────────────────────────────────

def test_shown_listing_routed_time_is_evidence():
    """r07 97b3d456: "Show details of the cheapest one" quoted Kingsland Road's routed
    35 minutes from the search the user is looking at (view_results, source routing)."""
    text, state = _case("r07_97b3d456_critic")
    assert "- Commute: 35 min to Canary Wharf, London E14 5AB." in validate_commute_response(text, state)


def test_withdrawn_claim_takes_the_rest_of_its_destination_with_it():
    """Without that evidence the claim goes, and "Commute: London E14 5AB." is not left."""
    text, state = _case("r07_97b3d456_critic")
    out = validate_commute_response(text, _without_shown_listings(state))
    assert "35 min" not in out
    assert "London E14 5AB." not in out
    assert "Commute:" not in out


def test_shown_rows_for_another_destination_are_not_evidence():
    text, state = _case("r07_97b3d456_critic")
    moved = dict(state, accumulated_search_criteria=dict(
        state["accumulated_search_criteria"], commute_destination="Bank, London EC2",
        destination="Bank, London EC2"))
    assert "35 min" not in validate_commute_response(text, moved)


def test_dangling_so_clause_goes_with_its_sentence():
    """Stress C-08 6b514555: "In practice, so the real figure is likely at the lower end of
    that range." was left after an invented 20-25 minute walk was withdrawn."""
    text, state = _case("stress_6b514555_draft")
    out = validate_commute_response(text, state)
    assert "20-25 minute walk" not in out
    assert "In practice, so" not in out
    assert "so the real figure is likely at the lower end of that range" not in out


def test_failed_lookups_keep_the_shown_listings_and_never_an_empty_lead_in():
    """r07 38299e93: every nearby lookup failed, and the no-evidence money contract removed
    the four listings (prices from the search the user is looking at), leaving "The listings
    I checked were:" with nothing after it."""
    text, state = _case("r07_38299e93_critic_input")
    context = state["extracted_context"]
    evidence = [a.get("raw_data") for a in state["tool_artifacts"] if a.get("success", True) is not False]
    kwargs = dict(retrieved_evidence=evidence, user_message=context["current_message"],
                  reply_language="en", failed_lookups=["search nearby pois"])
    kept = enforce_no_evidence_numeric_contract(text, shown_listings=context["view_results"], **kwargs)
    assert kept == text
    stripped = enforce_no_evidence_numeric_contract(text, **kwargs)
    assert "£2,995/month" not in stripped
    assert "The listings I checked were:" not in stripped


def test_price_range_is_not_rewritten_to_one_listings_rent():
    """r07 7d807c65: "Prices range from £2400 to £2650" became "£2400 to £2400" because the
    line named one listing in a later sentence."""
    case = CORPUS["r07_7d807c65_graph_output"]
    out = bind_listing_links(case["text"], case["entities"])
    assert "Prices range from £2400 to £2650 per month." in out
    assert "Prices range from £2400 to £2400" not in out


def test_rent_correction_still_applies_to_the_sentence_naming_the_listing():
    entities = [{"url": "https://www.onthemarket.com/details/20259996/",
                 "address": "Parkway, London, NW1", "price": "£2650/month"}]
    out = bind_listing_links("Prices range from £2400 to £2650 per month. "
                             "Parkway, London, NW1 is £2,999/month.", entities)
    assert out == "Prices range from £2400 to £2650 per month. Parkway, London, NW1 is £2650/month."


# ── a restated user limit is a constraint, not a measured journey ─────────────────────

def _with_saved_fact(name):
    """The recorded state, with the saved semantic row in the memory block as
    ``AgentMemory.format_for_prompt`` renders it ("[FACT] …")."""
    case = CORPUS[name]
    state = case["state"]
    block = f"What I remember about this user:\n[FACT] {case['saved_fact']}\n{state.get('memory_context') or ''}"
    return case["text"], dict(state, memory_context=block)


def test_recall_keeps_the_users_saved_commute_limit():
    """r07 inst-A turn 26 (A-F10): "What is my budget, my pet name, my max commute …" lost
    "**Max commute:** 35 minutes" and got the Note; the user saved "my maximum commute is 35
    minutes" in an earlier conversation."""
    text, state = _with_saved_fact("r07A_55581058_draft")
    assert "- **Max commute:** 35 minutes. You cycle to work" in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out
    assert validate_commute_response("Your maximum commute is 35 minutes.", state) == \
        "Your maximum commute is 35 minutes."


def test_an_inference_alone_does_not_make_a_limit():
    """The recorded block carried the 35 minutes only in an [INFERENCE] line: the assistant's
    own inference is never user-stated evidence (R2-06), so the restatement still goes."""
    case = CORPUS["r07A_55581058_draft"]
    assert "[INFERENCE]" in case["state"]["memory_context"]
    assert "35 minutes" not in validate_commute_response(case["text"], case["state"])


def test_a_saved_limit_never_licenses_a_journey_time():
    text, state = _with_saved_fact("r07A_55581058_draft")
    for invented in ("Kingsland Road takes 35 minutes to Canary Wharf.",
                     "- Commute from Hackney: 35 minutes.",
                     "- **Max commute:** 40 minutes."):
        assert "minutes" not in validate_commute_response(invented, state), invented


def test_a_cap_figure_is_not_a_measured_route_time():
    """Pre-existing: with Stratford measured at 29 and a 35-minute cap, "Stratford: 35
    minutes" was kept because 35 was a cap. The limit, stated as a limit, still stands."""
    text, state = _case("r06_099518b9_draft")
    state = dict(state, accumulated_search_criteria=dict(
        state.get("accumulated_search_criteria") or {}, max_travel_time=35))
    assert "35 minutes" not in validate_commute_response("- Stratford: 35 minutes to Canary Wharf.", state)
    for kept in ("- Stratford: 29 minutes to Canary Wharf.", "Stratford is within your 35-minute limit."):
        assert validate_commute_response(kept, state) == kept
