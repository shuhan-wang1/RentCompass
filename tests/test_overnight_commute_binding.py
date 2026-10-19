"""The commute validator must keep MEASURED minutes it can attribute (2026-10-19 B-02, C-08).

B-02: on 9 of 16 turns with a measured TfL duration the answer lost its minutes and kept a
dangling sentence ("Northern line from King's Cross St. Pancras to Moorgate —"), plus a footnote
saying the claims "could not be verified". The tool HAD measured them: ``_line_binding`` matched
place labels by raw substring, and TfL's labels ("King's Cross St. Pancras Underground Station",
"King's Cross Station, London") never occur verbatim in prose ("King's Cross St. Pancras"); leg
durations were never evidence; "to arrive by 09:00" was read as a destination.

C-08: when TfL returned no journey, ``calculate_commute`` returned an ESTIMATE with an explicit
range and the instruction to quote it as one. The validator deleted the range and left
"Estimated public transport time:" and "the lower end of that range" pointing at nothing.

The fixtures are the tool results the testers actually got (inst-B s5-arrive-1, inst-C
fc-commute), trimmed to the fields the validator reads. Fail-closed cases sit beside every
keep case: a figure the tool did not measure must still go.
"""
from __future__ import annotations

import pytest

from core.candidate_validation import validate_commute_response

_KX_CW = {
    "success": True, "query_type": "journey", "coverage": "london",
    "from": {"input": "King's Cross Station, London",
             "resolved_station": "King's Cross St. Pancras Underground Station"},
    "to": {"input": "Canary Wharf, London E14 5AB",
           "resolved_station": "Canary Wharf Underground Station"},
    "duration_minutes": 23, "changes_count": 1,
    "route_legs": [
        {"mode": "tube", "lines": ["Northern"], "duration_minutes": 6,
         "summary": "Northern line to Moorgate",
         "from": "King's Cross St. Pancras Underground Station", "to": "Moorgate Underground Station"},
        {"mode": "walking", "lines": [], "duration_minutes": 5,
         "summary": "Walk to Liverpool Street Station",
         "from": "Moorgate Underground Station", "to": "Liverpool Street Station"},
        {"mode": "elizabeth-line", "lines": ["Elizabeth line"], "duration_minutes": 7,
         "summary": "Elizabeth line to Canary Wharf Rail Station",
         "from": "Liverpool Street Station", "to": "Canary Wharf"},
        {"mode": "walking", "lines": [], "duration_minutes": 5,
         "summary": "Walk to Canary Wharf Underground Station",
         "from": "Canary Wharf", "to": "Canary Wharf Underground Station"},
    ],
}

_CAMDEN_KX_ESTIMATE = {
    "success": True, "from_address": "Camden, London", "to_address": "King's Cross, London",
    "mode": "transit", "route_legs": [], "route_source": "estimate",
    "basis": "straight_line_estimate", "duration_minutes": None,
    "estimated_duration_minutes": 19, "estimate_low_minutes": 16, "estimate_high_minutes": 24,
}

_NOTE = "could not be verified"


def _state(tool, raw, message="How long is it from King's Cross to Canary Wharf?"):
    return {"tool_artifacts": [{"tool": tool, "raw_data": raw, "success": True,
                                "params_digest": "d"}],
            "extracted_context": {"current_message": message, "reply_language": "en"},
            "user_query": message}


@pytest.mark.parametrize("answer", [
    "The fastest route takes 23 minutes: Northern line from King's Cross St. Pancras to "
    "Moorgate (6 min), walk to Liverpool Street (5 min), Elizabeth line to Canary Wharf "
    "(7 min), then walk (5 min).",
    "Journey time: 23 minutes.\n1. Northern line to Moorgate (6 min)\n"
    "2. Walk to Liverpool Street (5 min)",
    "Fastest route: King's Cross St. Pancras to Canary Wharf takes 23 minutes with 1 change.",
    "Total journey time is 23 minutes. Leave King's Cross at 08:35 to arrive by 09:00.",
    "It takes about 23 minutes to get from King’s Cross to Canary Wharf.",
])
def test_a_measured_journey_keeps_its_minutes_and_gets_no_footnote(answer):
    assert validate_commute_response(answer, _state("get_transport_info", _KX_CW)) == answer


@pytest.mark.parametrize("answer,gone", [
    ("It takes 45 minutes from King's Cross to Canary Wharf.", "45 minutes"),
    ("The journey takes 6 minutes.", "6 minutes"),                # a leg is not the total
    ("The journey via the Northern line takes 6 minutes.", "6 minutes"),
    ("Northern line to Moorgate (9 min)", "9 min"),               # not that leg's figure
    ("From King's Cross it is 23 minutes to Stratford.", "to Stratford"),  # wrong destination
])
def test_an_unmeasured_figure_still_goes(answer, gone):
    out = validate_commute_response(answer, _state("get_transport_info", _KX_CW))
    assert gone not in out


def test_an_estimate_may_be_quoted_as_an_estimate():
    state = _state("calculate_commute", _CAMDEN_KX_ESTIMATE,
                   "How long does the commute from Camden to King's Cross take?")
    for answer in (
        "**Estimated public transport time: 16–24 minutes** (estimated from the straight-line "
        "distance, not measured)",
        "It's roughly 16-24 minutes, so the real figure is likely at the lower end of that range.",
    ):
        assert validate_commute_response(answer, state) == answer


@pytest.mark.parametrize("answer", [
    "The commute from Camden to King's Cross takes 19 minutes.",      # stated as measured
    "Estimated 16-24 minutes, comfortably within your 30 minute limit.",  # a verdict
    "It's roughly 35 minutes.",                                       # outside the range
])
def test_an_estimate_never_becomes_a_measurement_or_a_verdict(answer):
    state = _state("calculate_commute", _CAMDEN_KX_ESTIMATE,
                   "How long does the commute from Camden to King's Cross take?")
    out = validate_commute_response(answer, state)
    assert out != answer


def test_a_fit_verdict_against_the_users_limit_is_not_a_mere_reference():
    """`_honest_commute_reference` let "the commute is well inside your 30 minute limit"
    through as if it only named the limit."""
    state = _state("get_transport_info", {"success": True}, "commute under 30 minutes")
    state["accumulated_search_criteria"] = {"max_travel_time": 30}
    out = validate_commute_response(
        "Oak Row: the commute is well inside your 30 minute limit.", state)
    assert "well inside" not in out
