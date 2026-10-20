"""r08 NEW-B: search_properties' own per-listing travel_time is commute evidence.

rid 85710b23: the draft's table quoted "28 min (routed)" / "14 min (routed)" and the
estimated "~18 min (estimated, unverified)" figures, each equal to that listing's
``recommendations[].travel_time``; the shipped table had "—" in every commute cell plus the
Note. Built from the real draft and state.
"""
from __future__ import annotations

import copy
import json
import pathlib

from core.candidate_validation import validate_commute_response

CASE = json.loads((pathlib.Path(__file__).parent / "data" /
                   "overnight_r04_search_travel_time.json").read_text(encoding="utf-8"))
NOTE = "Note: some commute claims could not be verified and have been removed from this answer."


def test_listing_travel_times_from_the_search_are_kept():
    out = validate_commute_response(CASE["draft"], CASE["state"])
    for cell in ("| Jacob House, E8 | Hackney | £1,950 | 28 min (routed) |",
                 "| Queensbridge Road, E2 | Hackney | £2,500 | 14 min (routed) |",
                 "| King Square, EC1V | Islington | £1,800 | ~18 min (estimated, unverified) |",
                 "| Arbutus Street, Haggerston, E8 | Hackney | £2,200 | ~22 min (estimated, unverified) |"):
        assert cell in out, cell
    assert "(28 min routed)" in out
    assert NOTE not in out


def test_one_listings_minutes_never_license_another():
    draft = "| 2 | Jacob House, E8 | Hackney | £1,950 | 14 min (routed) |"
    out = validate_commute_response(draft, CASE["state"])
    assert "14 min" not in out


def test_an_estimate_is_not_licensed_as_a_measured_figure():
    state = copy.deepcopy(CASE["state"])
    draft = "King Square, EC1V takes 18 minutes to Liverpool Street."
    assert "18 minutes" not in validate_commute_response(draft, state)
