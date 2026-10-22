"""Temporal and discourse words closing the clause are not part of the commute destination (r6-C3).

Round-5 note (LEDGER, "low-priority items"): "I've changed jobs, I work at Bank now." stored
``commute_destination = 'Bank now'``, and every later commute lookup geocoded "Bank now".
The labelled-destination parser ("I work at / my office is / commute to <place>") cut the
place at clause punctuation and connectors, but not at a trailing "now", "these days",
"at the moment", "currently", "from Monday" ...
"""
from __future__ import annotations

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates, _strip_destination_time_words

CW = "Canary Wharf, London E14 5AB"


@pytest.mark.parametrize("message, destination", [
    ("I've changed jobs, I work at Bank now.", "Bank"),
    ("I work at Old Street at the moment.", "Old Street"),
    ("I work at Bank currently", "Bank"),
    ("My office is Bank now actually.", "Bank"),
    ("I work at Bank from Monday.", "Bank"),
    ("I work at Bank from next Monday", "Bank"),
    ("I work at Bank starting next week", "Bank"),
    ("I work at Bank from 1st March", "Bank"),
    ("I work at Bank right now", "Bank"),
    ("I work in Shoreditch, currently.", "Shoreditch"),
    ("I commute to Liverpool Street these days", "Liverpool Street"),
    ("My commute is to King's Cross now", "King's Cross"),
    ("I work near Old Street now, so under 30 minutes please", "Old Street"),
    # r18: the clause ends at a comma that starts a new lower-case clause, then the time words go.
    ("I work at Old Street right now, budget £2000", "Old Street"),
    ("I work at Bank currently, and my budget is £2,000", "Bank"),
    # The known-destination table still canonicalises a recognised place.
    ("I work at Canary Wharf these days", CW),
])
def test_trailing_time_words_are_not_part_of_the_place(message, destination):
    assert _apply_explicit_criteria_updates({}, message)["commute_destination"] == destination


@pytest.mark.parametrize("message, destination", [
    ("I work at Bank.", "Bank"),                  # the place literally ends the sentence
    ("I work at Bank", "Bank"),
    ("My office is Old Street", "Old Street"),
    ("I work in Nowhere Lane", "Nowhere Lane"),   # "now" inside a word is not cut
    ("I work at Nowell Road", "Nowell Road"),
    ("I work at Mayfair", "Mayfair"),             # "May" inside a word is not cut
    ("I work at Canary Wharf", CW),
])
def test_real_place_names_are_kept(message, destination):
    assert _apply_explicit_criteria_updates({}, message)["commute_destination"] == destination


def test_a_job_change_replaces_the_previous_destination():
    before = {"commute_destination": CW, "destination": CW}
    after = _apply_explicit_criteria_updates(before, "I've changed jobs, I work at Bank now.")
    assert after["commute_destination"] == after["destination"] == "Bank"
    assert after["constraint_sources"]["commute_destination"]["value"] == "Bank"


def test_strip_helper_never_empties_a_place():
    assert _strip_destination_time_words("Bank now actually") == "Bank"
    assert _strip_destination_time_words("Old Street") == "Old Street"
    assert _strip_destination_time_words("now") == "now"
