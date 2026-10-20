"""The commute-claim redactor, round-2 acceptance (R2-01).

Live DeepSeek on 62e0895: the model's text was right every time and the shipped text was not.

* (a) "Fastest option: 23 minutes, 1 change" for TfL's measured 23 minutes shipped as "Fastest
  option: 1 change": a single-route line was attributed to the route only when its subject
  was "It"/"The journey"/a named leg.
* (b) "About 14 minutes in the tube" after get_transport_info measured 14 minutes beside a
  calculate_commute straight-line estimate for the same pair: the two results became two
  equally specific bindings, every line about the pair was "ambiguous", and the only figure
  the user asked for went.
* (c) With TfL down, calculate_commute's estimate "roughly 34 to 51 minutes" lost its range.
* (d) An area comparison lost its routed minutes and shipped a broken "**Comparing Camden".
* (e) "your earlier search was a studio in Hackney under £1,500 with no commute." was removed
  from an out-of-scope refusal, leaving "for example, Want me to run that again".
* (f) A weather answer ("For your commute, it will be about 12°C") got the commute footnote.

The payloads are the shapes the tools build (get_transport_info._journey_evidence +
the journey ``out`` dict; calculate_commute_impl's estimate branch fed by
commute_basis.describe_estimate; compare_or_rank_areas' per-area rows). A fail-closed case sits
beside every keep case: a figure no tool returned still goes.
"""
from __future__ import annotations

import pytest

from core.candidate_validation import validate_commute_response

NOTE = "Note: some commute claims could not be verified and have been removed from this answer."

# get_transport_info(query_type="journey", from_location="King's Cross, London",
#                    to_location="Canary Wharf, London") — TfL Journey Planner, live.
_LEGS_KX_CW = [
    {"mode": "tube", "lines": ["Northern"], "duration_minutes": 6,
     "summary": "Northern line to Moorgate",
     "from": "King's Cross St. Pancras Underground Station", "to": "Moorgate Underground Station"},
    {"mode": "walking", "lines": [], "duration_minutes": 5, "summary": "Walk to Liverpool Street",
     "from": "Moorgate Underground Station", "to": "Liverpool Street"},
    {"mode": "elizabeth-line", "lines": ["Elizabeth line"], "duration_minutes": 7,
     "summary": "Elizabeth line to Canary Wharf", "from": "Liverpool Street",
     "to": "Canary Wharf"},
    {"mode": "walking", "lines": [], "duration_minutes": 5, "summary": "Walk to Canary Wharf",
     "from": "Canary Wharf", "to": "Canary Wharf Underground Station"},
]
_SUMMARY_KX_CW = ("Tube Northern to Moorgate Underground Station (6 min) -> Walk to Liverpool "
                  "Street (5 min) -> Elizabeth-line Elizabeth line to Canary Wharf (7 min) -> "
                  "Walk to Canary Wharf Underground Station (5 min)")
_KX_CW_JOURNEY = {
    "success": True, "query_type": "journey", "coverage": "london",
    "from": {"input": "King's Cross, London",
             "resolved_station": "King's Cross St. Pancras Underground Station",
             "naptan": "940GZZLUKSX"},
    "to": {"input": "Canary Wharf, London", "resolved_station": "Canary Wharf Underground Station",
           "naptan": "940GZZLUCYF"},
    "duration_minutes": 23, "changes_count": 1, "route_summary": _SUMMARY_KX_CW,
    "route_legs": _LEGS_KX_CW, "selected_itinerary_id": "tfl:0123456789abcdef",
    "fastest_journey": {"itinerary_id": "tfl:0123456789abcdef", "duration_minutes": 23,
                        "changes_count": 1, "route_summary": _SUMMARY_KX_CW,
                        "route_legs": _LEGS_KX_CW,
                        "source": "TfL Unified API Journey Planner", "fare_available": False,
                        "fare_basis": None},
    "cheapest_journey": None, "source": "TfL Unified API Journey Planner",
    "from_cache": False, "cache_status": "miss",
    "journey_time": {"timezone": "Europe/London", "time_basis": "user_requested",
                     "time": "09:00", "time_is": "departure"},
    "stations_used": "King's Cross St. Pancras Underground Station -> Canary Wharf Underground Station",
    "fare_available": False, "fare_basis": None, "fare_itinerary_id": None,
    "fare_matches_itinerary": False,
}

# get_transport_info journey from Highgate West Hill to UCL (planner resolved the stations).
_HIGHGATE_UCL_JOURNEY = {
    "success": True, "query_type": "journey", "coverage": "london",
    "from": {"input": "Highgate West Hill, London N6",
             "resolved_station": "Highgate Underground Station", "naptan": "940GZZLUHGT"},
    "to": {"input": "UCL, London", "resolved_station": "Goodge Street Underground Station",
           "naptan": "940GZZLUGDG"},
    "duration_minutes": 14, "changes_count": 0,
    "route_summary": "Tube Northern to Goodge Street Underground Station (14 min)",
    "route_legs": [{"mode": "tube", "lines": ["Northern"], "duration_minutes": 14,
                    "summary": "Northern line to Goodge Street",
                    "from": "Highgate Underground Station",
                    "to": "Goodge Street Underground Station"}],
    "source": "TfL Unified API Journey Planner", "from_cache": False, "cache_status": "miss",
}


def _straight_line(from_address, to_address, *, estimate, low, high, km):
    """calculate_commute_impl's no-journey branch (commute_basis.describe_estimate)."""
    return {
        "success": True, "from_address": from_address, "to_address": to_address,
        "mode": "transit",
        "route_summary": "No route available: TfL returned no journey for this pair.",
        "route_legs": [], "changes_count": None, "route_source": "estimate",
        "basis": "straight_line_estimate",
        "basis_note": (f"Estimated from {km:.2f} km straight-line distance because TfL returned "
                       f"no journey for this pair ... Plausible range {low}-{high} minutes. "
                       "Not a measured journey time.") if estimate is not None else
                      "No commute time could be established.",
        "duration_minutes": None, "estimated_duration_minutes": estimate,
        "estimate_low_minutes": low, "estimate_high_minutes": high, "straight_line_km": km,
        "estimate_model": "calibrated_overhead_plus_power_pace_v1" if estimate is not None else None,
        "is_acceptable": None, "duration_category": None, "caveat": "Not a measured journey time.",
    }


_KX_CW_ESTIMATE = _straight_line("King's Cross, London", "Canary Wharf, London",
                                 estimate=41, low=34, high=51, km=7.8)

# compare_or_rank_areas(candidate_areas=[Camden, Hackney, Islington], destination=King's Cross)
_AREAS = {
    "success": True, "status": "ok",
    "areas": [
        {"name": name, "slug": name.lower(),
         "rent": {"min": 1500, "max": 2600, "median": median, "sample_size": 12,
                  "freshness_days": 3, "low_sample": False},
         "commute_minutes": minutes, "budget_match_rate": 0.5,
         "score": {"total": total, "components": {}, "weights": {"value": 0.5, "commute": 0.5},
                   "partial": False},
         "sources": ["OnTheMarket listing cache", "commute routing"], "no_data": False,
         "rank": rank}
        for rank, (name, median, minutes, total) in enumerate(
            [("Camden", 2100, 31, 67.2), ("Islington", 2150, 14, 62.5),
             ("Hackney", 2400, 41, 5.5)], start=1)],
    "explanation_notes": [], "priorities": ["value", "commute"],
    "weights": {"value": 0.5, "commute": 0.5},
    "evidence_scope": {"rent_sample": "mixed_cached_listings", "room_type_filtered": False,
                       "bedrooms_filtered": False, "budget_match_basis": "rent_only",
                       "freshness_basis": "cache_fetch_time", "live_verified": False,
                       "commute_scope": "area_reference_point"},
    "not_evaluated": [],
}


def _artifact(tool, raw, success=True):
    return {"turn": 1, "tool": tool, "raw_data": raw, "success": success,
            "params_digest": f"{tool}-digest", "error": None if success else "failed"}


def _state(message, *artifacts):
    return {"extracted_context": {"current_message": message, "reply_language": "en"},
            "user_query": message, "tool_artifacts": list(artifacts)}


# ── (a) one measured journey: no label needed on the line ──────────────────────────────
_KX_CW_ANSWER = (
    "**King's Cross → Canary Wharf, tomorrow 09:00**\n\n"
    "Fastest option: 23 minutes, 1 change\n"
    "1. Northern line from King's Cross St. Pancras to Moorgate (6 min)\n"
    "2. Walk to Liverpool Street (5 min)\n"
    "3. Elizabeth line from Liverpool Street to Canary Wharf (7 min)\n"
    "4. Walk to the exit (5 min)\n\n"
    "Source: TfL Journey Planner (live).")


def _kx_cw_state():
    return _state("Journey from King's Cross to Canary Wharf tomorrow at 9am",
                  _artifact("get_transport_info", _KX_CW_JOURNEY))


def test_r2_01a_measured_total_and_legs_survive_on_unlabelled_lines():
    assert validate_commute_response(_KX_CW_ANSWER, _kx_cw_state()) == _KX_CW_ANSWER


@pytest.mark.parametrize("answer", [
    "From King's Cross it takes 23 minutes.",
    "From King's Cross, it's about 23 minutes on the tube.",
    "About 23 minutes, with one change at Moorgate.",
])
def test_r2_01a_origin_and_subjectless_phrasings_keep_the_measured_total(answer):
    assert validate_commute_response(answer, _kx_cw_state()) == answer


@pytest.mark.parametrize("answer,gone", [
    ("Fastest option: 45 minutes, 1 change", "45 minutes"),
    ("From Stratford it takes 23 minutes.", "From Stratford"),
    ("It takes 23 minutes to Stratford.", "to Stratford"),
    ("The journey takes 7 minutes.", "7 minutes"),           # a leg is never the trip
])
def test_r2_01a_unmeasured_or_misplaced_figures_still_go(answer, gone):
    out = validate_commute_response(answer, _kx_cw_state())
    assert gone not in out


# ── (b) a measurement beside a straight-line estimate of the same pair ─────────────────
_HIGHGATE_ANSWER = (
    "About 14 minutes in the tube, Highgate to Goodge Street on the Northern line.\n\n"
    "The 14 minutes is the tube leg only. Add the walk from Highgate West Hill to Highgate "
    "station and from Goodge Street to UCL.")


@pytest.mark.parametrize("estimate", [
    dict(estimate=27, low=23, high=33, km=5.6),        # calculate_commute returned an estimate
    dict(estimate=None, low=None, high=None, km=5.6),  # ... or refused one
])
def test_r2_01b_measured_minutes_survive_beside_a_calculate_commute_estimate(estimate):
    state = _state("How long is the commute from Highgate West Hill, London N6 to UCL?",
                   _artifact("calculate_commute", _straight_line(
                       "Highgate West Hill, London N6", "UCL, London", **estimate)),
                   _artifact("get_transport_info", _HIGHGATE_UCL_JOURNEY))
    assert validate_commute_response(_HIGHGATE_ANSWER, state) == _HIGHGATE_ANSWER


def test_r2_01b_the_estimate_is_still_not_a_measurement():
    state = _state("How long is the commute from Highgate West Hill, London N6 to UCL?",
                   _artifact("calculate_commute", _straight_line(
                       "Highgate West Hill, London N6", "UCL, London",
                       estimate=27, low=23, high=33, km=5.6)),
                   _artifact("get_transport_info", _HIGHGATE_UCL_JOURNEY))
    out = validate_commute_response("The commute takes 27 minutes. Rent there is £1,600.", state)
    assert "27 minutes" not in out and "£1,600" in out and NOTE in out
    kept = "The straight-line estimate was roughly 23 to 33 minutes; TfL measured 14 minutes."
    assert validate_commute_response(kept, state) == kept


# ── (c) TfL down: the estimate's range, quoted as an estimate ──────────────────────────
_ESTIMATE_ANSWER = (
    "TfL's journey planner did not respond, so I can't give you a measured journey time. "
    "What I can give you is an estimate: roughly 34 to 51 minutes, derived from the "
    "straight-line distance of about 7.8 km, not a timetable figure, so treat it as a rough "
    "range rather than a journey time.")


def _estimate_state():
    return _state("How long is the commute from King's Cross to Canary Wharf by tube?",
                  _artifact("get_transport_info",
                            {"success": False, "coverage": "london",
                             "error": "TfL could not verify this journey for the requested time.",
                             "service_status": "unavailable", "retryable": True},
                            success=False),
                  _artifact("calculate_commute", _KX_CW_ESTIMATE))


def test_r2_01c_estimate_range_survives_when_quoted_as_an_estimate():
    assert validate_commute_response(_ESTIMATE_ANSWER, _estimate_state()) == _ESTIMATE_ANSWER
    other = "What I can give you is an estimate: 34–51 minutes from the straight-line distance."
    assert validate_commute_response(other, _estimate_state()) == other


@pytest.mark.parametrize("answer,gone", [
    ("The commute takes 41 minutes.", "41 minutes"),                       # stated as measured
    ("That is only an estimate. The journey takes 41 minutes.", "41 minutes"),  # other sentence
    ("Roughly 34 to 60 minutes.", "60 minutes"),                           # not its range
])
def test_r2_01c_an_estimate_never_becomes_a_measurement(answer, gone):
    assert gone not in validate_commute_response(answer, _estimate_state())


# ── (d) area comparison ──────────────────────────────────────────────────────────────
def _areas_state():
    return _state("Compare Camden, Hackney and Islington for a 1-bed under £2,200, commute to "
                  "King's Cross", _artifact("compare_or_rank_areas", _AREAS))


@pytest.mark.parametrize("answer", [
    "**Comparing Camden, Hackney and Islington — 1-bed under £2,200, commute to King's Cross**\n\n"
    "1. **Camden** (score 67.2): median rent £2,100, commute about 31 minutes.\n"
    "2. **Islington** (score 62.5): median rent £2,150, commute 14 minutes, the shortest.\n"
    "3. **Hackney** (score 5.5): median rent £2,400, commute 41 minutes.",
    "Camden (31 min) and Islington (14 min) are both closer than Hackney (41 min).",
    "Camden and Islington are 31 and 14 minutes away respectively.",
    "Camden, Islington and Hackney are 31, 14 and 41 minutes away.",
    "| Area | Score | Median rent | Commute |\n|---|---|---|---|\n"
    "| Camden | 67.2 | £2,100 | 31 min |\n| Islington | 62.5 | £2,150 | 14 min |\n"
    "| Hackney | 5.5 | £2,400 | 41 min |",
])
def test_r2_01d_routed_area_minutes_survive(answer):
    assert validate_commute_response(answer, _areas_state()) == answer


def test_r2_01d_a_withdrawn_heading_clause_leaves_balanced_markdown():
    answer = ("**Comparing Camden, Hackney and Islington — all within 30 minutes of King's Cross**\n\n"
              "Camden has a median rent of £2,100.")
    out = validate_commute_response(answer, _areas_state())
    assert out.startswith("**Comparing Camden, Hackney and Islington**\n")
    assert "30 minutes" not in out and NOTE in out


@pytest.mark.parametrize("answer", ["Hackney: 25 minutes.", "Camden is 41 minutes from King's Cross.",
                                    "Camden and Islington are both 31 minutes away."])
def test_r2_01d_a_figure_from_another_area_still_goes(answer):
    assert validate_commute_response(answer, _areas_state()) != answer


# ── (e) a sentence that says there is no commute ──────────────────────────────────────
_PARIS_ANSWER = (
    "I can only search UK rentals, so Paris is outside what I can do. If you want to continue "
    "here, for example, your earlier search was a studio in Hackney under £1,500 with no "
    "commute. Want me to run that again, adjust the budget or area?")


@pytest.mark.parametrize("answer", [
    _PARIS_ANSWER,
    "You didn't set a commute limit, so results are not filtered by travel time.",
    "Your search has no commute requirement, and the budget stays under £1,500.",
])
def test_r2_01e_a_commute_denial_is_not_a_claim(answer):
    assert validate_commute_response(answer, _state("Find me a flat in Paris")) == answer


def test_r2_01e_no_commute_over_a_figure_is_still_a_claim():
    out = validate_commute_response("None of these has a commute over 30 minutes.",
                                    _state("Find me a flat in Camden"))
    assert "30 minutes" not in out


@pytest.mark.parametrize("answer", [
    "Flat A (£1,500/month) is within your commute limit.",
    "Flat A at £1,500/month is within your commute limit.",
])
def test_r2_01e_a_price_beside_an_unevidenced_commute_verdict_does_not_excuse_it(answer):
    out = validate_commute_response(answer, _state("Find me a flat in Camden"))
    assert "within your commute limit" not in out


# ── (f) weather ───────────────────────────────────────────────────────────────────────
def test_r2_01f_weather_for_the_commute_is_not_a_commute_claim():
    answer = ("Tomorrow in London: around 12°C with light rain in the morning.\n"
              "For your commute, it will be about 12°C and wet, so take an umbrella.\n"
              "Your commute home: temperatures below average, take a coat.")
    state = _state("What's the weather like tomorrow for my commute?",
                   _artifact("get_weather", {"success": True, "location": "London",
                                             "weather_type": "forecast", "temperature": 12,
                                             "condition": "light rain"}))
    assert validate_commute_response(answer, state) == answer


# ── a sentence that loses its claim goes whole ────────────────────────────────────────
@pytest.mark.parametrize("answer,kept", [
    ("Flat A is £1,500/month. If you want to continue here, for example, the commute is 12 "
     "minutes. Want me to search again?", "Flat A is £1,500/month. Want me to search again?"),
    ("Flat A is £1,500/month. For your commute, it takes 40 minutes.", "Flat A is £1,500/month."),
])
def test_r2_01_a_stub_left_by_a_withdrawn_claim_goes_with_it(answer, kept):
    out = validate_commute_response(answer, _state("Show me flats near UCL"))
    assert out == f"{kept}\n\n{NOTE}"


def test_r2_01d_a_withdrawn_table_cell_keeps_its_column():
    answer = ("| Area | Score | Median rent | Commute |\n|---|---|---|---|\n"
              "| Camden | 67.2 | £2,100 | 31 min |\n| Hackney | 5.5 | £2,400 | 25 min |")
    out = validate_commute_response(answer, _areas_state())
    assert "| Camden | 67.2 | £2,100 | 31 min |" in out
    assert "| Hackney | 5.5 | £2,400 | — |" in out and "25 min" not in out


def test_r2_01_sentence_scope_and_route_fallback_stay_linear():
    """The sentence-wide estimate marker and the any-route fallback must not make one long
    sentence quadratic (the B-01 class)."""
    import time

    state = _state("How long is the commute from Highgate West Hill, London N6 to UCL?",
                   _artifact("calculate_commute", _straight_line(
                       "Highgate West Hill, London N6", "UCL, London",
                       estimate=27, low=23, high=33, km=5.6)),
                   _artifact("get_transport_info", _HIGHGATE_UCL_JOURNEY),
                   _artifact("compare_or_rank_areas", _AREAS))
    answer = "Roughly, " + ", ".join(["it is 99 minutes"] * 1_500) + "."
    started = time.perf_counter()
    out = validate_commute_response(answer, state)
    assert time.perf_counter() - started < 2.0
    assert "99 minutes" not in out


# ── the exact pre-validation model texts of the live acceptance (checkpoint DB) ─────────
_LIVE_A = (
    "Here is the journey for tomorrow, Tuesday 20 October 2026, departing King's Cross at 09:00 "
    "(live data from the official TfL Journey Planner):\n\nFastest option: 23 minutes, 1 change\n"
    "- Northern line from King's Cross St. Pancras to Moorgate (6 min)\n"
    "- Walk to Liverpool Street (5 min)\n"
    "- Elizabeth line from Liverpool Street to Canary Wharf (7 min)\n"
    "- Walk to Canary Wharf station (5 min)\n\n"
    "Fare: adult pay-as-you-go single on this station pair is £3.60 at peak times and £3.10 "
    "off-peak (contactless or Oyster, TfL Single Fare Finder).")
_LIVE_B = (
    "From Highgate West Hill, N6 to UCL, the journey is quick and direct.\n\n"
    "**Journey (TfL Journey Planner, live data)**\n"
    "- Highgate Underground Station to Goodge Street Underground Station on the Northern line\n"
    "- About 14 minutes in the tube, no changes\n"
    "- Adult pay-as-you-go single: £3.30 (contactless or Oyster)\n\n"
    "Two things to keep in mind:\n"
    "- The 14 minutes is the tube leg only. It does not include walking from Highgate West Hill "
    "down to Highgate station, or the short walk from Goodge Street to the UCL main campus on "
    "Gower Street, so allow extra time for those.")
_LIVE_C = (
    "I couldn't get a real journey plan for this one — the TfL planner returned no itinerary for "
    "King's Cross to Canary Wharf, so I can't give you a measured tube time.\n\n"
    "What I can give you is an estimate: roughly 34 to 51 minutes, derived from the straight-line "
    "distance of about 7.8 km and a model fitted to real TfL journeys. That is an estimate, not a "
    "timetable figure, so treat it as a rough range rather than a journey time.")
_LIVE_D = (
    "Here's how the three areas stack up for a 1-bed under £2,200 with a commute to King's Cross.\n\n"
    "**1. Camden — best overall**\n- Median rent: £2,100/month (range £850–£5,000, 95 listings "
    "sampled)\n- Commute to King's Cross: about 31 minutes\n\n"
    "**2. Islington — fastest commute, tightest budget**\n- Median rent: £2,400/month\n"
    "- Commute to King's Cross: about 14 minutes — by far the quickest\n\n"
    "**3. Hackney — weakest fit**\n- Median rent: £2,750/month\n"
    "- Commute to King's Cross: about 41 minutes")
_LIVE_E = (
    "I can only help with UK student housing, so I can't search for flats in Paris.\n\n"
    "If you'd like, I can keep looking in Stratford (or another UK area) — for example, your "
    "earlier search was 1-bed flats in Stratford under £2,000 pcm with no commute. Want me to run "
    "that again, or adjust the budget or area?")


@pytest.mark.parametrize("answer,state_factory", [
    (_LIVE_A, lambda: _kx_cw_state()),
    (_LIVE_B, lambda: _state(
        "How long is the commute from Highgate West Hill, London N6 to UCL?",
        _artifact("calculate_commute", {"success": False,
                                        "error": "无法计算通勤时间（地址解析失败）"}, success=False),
        _artifact("get_transport_info", dict(_HIGHGATE_UCL_JOURNEY, fare_gbp=3.3,
                                             fare_display="£3.30")))),
    (_LIVE_C, lambda: _estimate_state()),
    (_LIVE_D, lambda: _areas_state()),
    (_LIVE_E, lambda: _state("Find me a flat in Paris")),
], ids=["a-kx-cw", "b-highgate", "c-estimate", "d-areas", "e-paris"])
def test_r2_01_the_live_model_texts_ship_unchanged(answer, state_factory):
    assert validate_commute_response(answer, state_factory()) == answer


# ── R2-25: the inst-B garbling cases (exact model texts) ───────────────────────────────
def _leg(mode, lines, minutes, to, frm=""):
    return {"mode": mode, "lines": lines, "duration_minutes": minutes, "summary": "",
            "from": frm, "to": to}


_T5_KX = {
    "success": True, "query_type": "fare", "coverage": "london",
    "from": {"input": "Heathrow Terminal 5", "resolved_station": "Heathrow Terminal 5"},
    "to": {"input": "King's Cross St Pancras",
           "resolved_station": "King's Cross St. Pancras Underground Station"},
    "duration_minutes": 42, "changes_count": 2,
    "route_legs": [_leg("national-rail", ["Heathrow Express"], 20, "London Paddington"),
                   _leg("bus", ["27"], 2, "Edgware Road"),
                   _leg("tube", ["Hammersmith & City"], 8,
                        "King's Cross St. Pancras Underground Station")],
    "cheapest_journey": {
        "duration_minutes": 47, "changes_count": 1, "source": "TfL Unified API Journey Planner",
        "route_legs": [_leg("national-rail", ["Heathrow Express"], 20, "London Paddington"),
                       _leg("walking", [], 12, "Paddington (H&C Line) Underground Station"),
                       _leg("tube", ["Hammersmith & City"], 11,
                            "King's Cross St. Pancras Underground Station")]},
    "source": "TfL Unified API Journey Planner", "fare_gbp": 30.75,
}
_T5_ANSWER = (
    "I found two TfL journey fares from Heathrow Terminal 5 to King's Cross St Pancras.\n\n"
    "- **Fastest route, £30.75:** about 42 minutes with 2 changes. It takes the Heathrow Express "
    "to Paddington (20 min), the 27 bus to Edgware Road (2 min), then the Hammersmith & City line "
    "to King's Cross St Pancras (8 min).\n"
    "- **Cheapest route, £29.00:** about 47 minutes with 1 change. It takes the Heathrow Express "
    "to Paddington (20 min), a 12-minute walk to the Hammersmith & City platforms, then that line "
    "to King's Cross St Pancras (11 min).\n\n"
    "Fastest route (42 minutes, £30.75):\n"
    "- Heathrow Express to London Paddington: 20 minutes\n"
    "- Bus 27 to Edgware Road: 2 minutes\n"
    "- Hammersmith & City line to King's Cross St Pancras: 8 minutes")


def test_r2_25_the_cheaper_itinerary_and_its_legs_are_evidence():
    state = _state("What is the fare from Heathrow Terminal 5 to King's Cross off-peak?",
                   _artifact("get_transport_info", _T5_KX))
    assert validate_commute_response(_T5_ANSWER, state) == _T5_ANSWER
    out = validate_commute_response("The cheaper route takes about 52 minutes.", state)
    assert "52 minutes" not in out


def _measured(origin, minutes):
    return {"success": True, "from_address": origin, "to_address": "Liverpool Street Station, London",
            "mode": "transit", "duration_minutes": minutes, "basis": "tfl_journey_plan",
            "route_source": "TfL Journey Planner", "route_legs": []}


def test_r2_25_a_comparison_of_two_measured_routes_survives():
    state = _state("Compare the first two on commute to Liverpool Street and total monthly cost",
                   _artifact("calculate_commute", _measured("Powerscroft Road, Hackney, London, E5", 36)),
                   _artifact("calculate_commute", _measured("Kingsland Road, London, E8", 16)))
    answer = ("- Time cost: Kingsland Road saves about 20 minutes each way.\n"
              "Kingsland Road is about £50 a month cheaper in total and has a much shorter "
              "commute, so it comes out ahead on both counts.")
    assert validate_commute_response(answer, state) == answer
    wrong = validate_commute_response(
        "Powerscroft Road has a much shorter commute than Kingsland Road.", state)
    assert "much shorter commute" not in wrong
