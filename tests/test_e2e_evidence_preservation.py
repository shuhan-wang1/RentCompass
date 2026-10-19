"""Audit regressions for independent tool evidence and entity-scoped journey claims.

Reproductions: e2e_tools_2026_10_19 professional R3/R10/R14 and family R6 returned
successful journey/fare/detail/weather data but shipped only the fixed commute warning.
Fixtures below are deliberately synthetic; no provider or network access is involved.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "app"):
    if str(_path) in sys.path:
        sys.path.remove(str(_path))
    sys.path.insert(0, str(_path))

import core.candidate_validation as cv


def artifact(tool, raw, **flags):
    return {"tool": tool, "success": True, "raw_data": raw, **flags}


def route(origin="Croxley View, Watford, WD18", target="Gower Street, London WC1E 6BT", minutes=32):
    return artifact("calculate_commute", {"from_address": origin, "to_address": target,
                    "duration_minutes": minutes, "basis": "tfl_journey_plan"})


def state(*artifacts, address="Croxley View, Watford, WD18", message="Check the weather and commute"):
    return {"tool_artifacts": list(artifacts), "extracted_context": {"current_message": message},
            "accumulated_search_criteria": {"max_travel_time": 50},
            "candidate_validation": {"constraints": {"max_commute_minutes": 50}, "statuses": [
                {"candidate": {"address": address}, "status": "unknown", "evidence_status": "missing"}]}}


def weather():
    return artifact("get_weather", {"location": "Watford", "current_temp": 14.2,
                                    "condition": "Light Rain", "rainfall_chance": None})


def test_regressions_pin_the_actual_source_module():
    assert Path(cv.__file__).resolve() == _ROOT / "app/core/candidate_validation.py"


def test_family_r6_keeps_weather_returned_route_and_wheelchair_uncertainty():
    """'Check Watford weather over the next three days ... meet our 50-minute limit
    for a wheelchair user ... or only a fastest-route estimate?'"""
    st = state(weather(), route())
    text = ("Watford current weather: 14.2°C, Light Rain. No three-day forecast was returned.\n"
            "Croxley View, Watford, WD18 → Gower Street, London WC1E 6BT: 32 minutes.\n"
            "I cannot confirm the 50-minute commute requirement for a wheelchair user.\n"
            "Step-free access and extra time with a child remain unknown.")
    out = cv.validate_commute_response(text, st)
    assert "14.2°C" in out and "Light Rain" in out
    assert "32 minutes" in out and "No three-day forecast" in out
    assert "cannot confirm the 50-minute" in out
    assert "Step-free access" in out and "remain unknown" in out


def test_unverified_commute_cannot_delete_independent_weather_or_cost():
    text = ("Current weather is 14.2°C; the unmeasured commute takes 19 minutes.\n"
            "The rent is £1,100/month. The one-way fare is £2.80.\n"
            "I cannot verify the 50-minute commute requirement.")
    out = cv.validate_commute_response(text, state(weather()))
    assert "19 minutes" not in out
    assert all(fact in out for fact in ("14.2°C", "£1,100/month", "£2.80", "cannot verify"))


def test_independent_expenses_are_not_misclassified_as_commute_verdicts():
    text = "The monthly commute cost is within your £150 budget; journey time is unverified."
    assert cv.validate_commute_response(text, state()) == text


def test_professional_r3_station_journey_survives_an_unverified_listing_ledger():
    """'Check actual public-transport journeys from Watford Junction to King's Cross ...'"""
    st = state(route("Watford Junction, Watford", "King's Cross St Pancras, London"),
               address="North Approach, Watford, WD25")
    text = ("Watford Junction → King's Cross St Pancras: 32 minutes.\n"
            "Returned one-way fare: £2.80. No cheaper alternative was returned.")
    assert cv.validate_commute_response(text, st) == text


def test_professional_r14_area_route_cannot_license_a_property_duration():
    """The UI asked about North Approach; calculate_commute only used 'Watford'."""
    details = artifact("get_property_details", {"found": True, "property": {
        "address": "North Approach, Watford, WD25", "price": "£1,100 pcm", "room_type": "1 bed Flat"}})
    st = state(details, route("Watford", "King's Cross St Pancras"),
               address="North Approach, Watford, WD25")
    text = ("North Approach, Watford, WD25: £1,100 pcm, 1 bed Flat, 32 minutes to King's Cross St Pancras.\n"
            "Watford → King's Cross St Pancras: 32 minutes. This is an area-level journey.")
    out = cv.validate_commute_response(text, st)
    assert "£1,100 pcm" in out and "1 bed Flat" in out
    assert out.count("32 minutes") == 1
    assert "area-level journey" in out


def test_route_evidence_is_bound_to_destination_too():
    st = state(route("Croxley View, Watford, WD18", "Gower Street"))
    out = cv.validate_commute_response(
        "Croxley View, Watford, WD18, £1600/month, 32 minutes to Canary Wharf.", st)
    assert "32 minutes" not in out
    assert "£1600/month" in out


def test_a_long_destination_does_not_outweigh_a_more_specific_property_origin():
    target = "An Unusually Long Campus Destination Name"
    st = state(route("Watford", target), address="Flat A, Watford")
    text = f"Flat A, Watford, £1100/month, 32 minutes to {target}."
    out = cv.validate_commute_response(text, st)
    assert "32 minutes" not in out
    assert "£1100/month" in out


def test_coordinated_entities_do_not_borrow_each_others_measurements():
    st = state(route("Alpha House", "Campus", 32), route("Beta House", "Campus", 48), address="Alpha House")
    text = "Alpha House takes 32 minutes to Campus and Beta House takes 32 minutes to Campus."
    out = cv.validate_commute_response(text, st)
    assert "Alpha House takes 32 minutes" in out
    assert "Beta House takes 32 minutes" not in out


def test_caps_are_not_licensed_as_a_measured_trip_duration():
    out = cv.validate_commute_response("It takes 50 minutes.", state())
    assert "50 minutes" not in out


@pytest.mark.parametrize("bad", [True, math.nan, math.inf, -1])
def test_non_finite_boolean_or_negative_route_durations_cannot_license_claims(bad):
    st = state(route(minutes=bad))
    out = cv.validate_commute_response("Croxley View takes 32 minutes.", st)
    assert "32 minutes" not in out


def test_explicit_estimate_basis_never_becomes_measured_evidence():
    a = route(); a["raw_data"]["basis"] = "straight_line_estimate"
    out = cv.validate_commute_response("Croxley View takes 32 minutes.", state(a))
    assert "32 minutes" not in out


@pytest.mark.parametrize("flag", ["timed_out", "denied", "abandoned", "outcome_unknown"])
def test_incomplete_artifacts_do_not_reappear_in_fallback(flag):
    a = weather(); a[flag] = True
    assert cv.render_structured_evidence_fallback(state(a)) == ""


def test_provider_error_fallback_preserves_current_weather_without_inventing_forecast():
    out = cv.render_structured_evidence_fallback(state(weather()))
    assert "14.2°C" in out and "Light Rain" in out
    assert "no multi-day forecast" in out and "rain probability was not returned" in out


def test_professional_r10_tariffs_and_nested_cost_failure_are_both_reported():
    """'Retry fare/travelcard/line status ... if a tool fails report its actual error.'"""
    arts = [
        artifact("get_transport_info", {"query_type": "travelcard", "zones": "Zone 1-6",
                 "monthly_gbp": 313.4, "weekly_gbp": 81.6, "daily_cap_gbp": 16.3,
                 "effective_date": "2026-03-01"}),
        artifact("get_transport_info", {"query_type": "fare", "fare_available": True, "fare_gbp": 2.8}),
        artifact("get_transport_info", {"query_type": "line_status", "lines": [
            {"name": "Northern", "status": "Good Service"}]}),
        artifact("calculate_commute_cost", {"from_address": "North Approach", "to_address": "King's Cross",
                 "commute": {"duration_minutes": 32, "basis": "tfl_journey_plan"},
                 "transport_cost": {"error": "Unable to determine zones for one or both addresses."}}),
    ]
    out = cv.render_structured_evidence_fallback(state(*arts))
    for fact in ("£313.40", "monthly", "£81.60", "weekly", "£16.30", "£2.80", "one-way",
                 "Northern", "Good Service", "Unable to determine zones", "2026-03-01"):
        assert fact in out
    assert "not established" in out and "not a return fare or a monthly total" in out
    assert "£1,950" not in out, "an incomplete total must not be fabricated"


def test_a_previous_whole_answer_replacement_can_recover_independent_evidence():
    out = cv.validate_commute_response(
        "The commute condition could not be verified for the listing this round.", state(weather()))
    assert "14.2°C" in out


def test_fallback_keeps_cache_and_contact_agent_limits():
    a = artifact("get_property_details", {"found": True, "possibly_outdated": True, "property": {
        "address": "North Approach", "price": "£1,100 pcm", "room_type": "1 bed Flat",
        "available_from": "Contact agent"}})
    out = cv.render_structured_evidence_fallback(state(a))
    assert "£1,100 pcm" in out and "Contact agent" in out and "cached" in out
    assert "Confirm current availability" in out


def test_safety_fallback_uses_covered_months_not_the_legacy_six_month_field_name():
    a = artifact("check_safety", {"address": "Watford", "crime_data": {
        "total_crimes_6m": 135, "months_covered": 3, "data_months": ["2026-06", "2026-07", "2026-08"]},
        "safety_level": "Very Safe", "safety_analysis": "6-month total; monthly average: 22; below London average"})
    out = cv.render_structured_evidence_fallback(state(a))
    assert "135 incidents across 3 covered months" in out
    assert "Very Safe" not in out and "below London average" not in out
    assert "personal safety" in out


def test_chinese_candidate_reasons_are_localized_without_changing_machine_codes():
    validation = cv.validate_candidates([{"address": "Other House", "price": "£2000/month", "bedrooms": 2,
                                         "area": "London", "alternative": True}],
                                        {"max_budget": 1800, "bedrooms": 1, "area": "Watford"})
    out = cv.render_candidate_status(validation, language="zh")
    assert "月租 £2000 超过预算 £1800" in out and "卧室数量为 2" in out
    assert "备选" in out and "指定范围" in out
    assert "monthly price" not in out and "alternative is" not in out
    assert "monthly price 2000 exceeds budget 1800" in validation["excluded"][0]["reasons"]


def test_unknown_features_are_not_hidden_by_a_commute_unknown_reason():
    validation = {"unknown": [{"candidate": {"address": "Croxley View"}, "unknown_reasons": [
        "required features are not verified: step-free, lift", "commute evidence is missing"]}]}
    out = cv.render_candidate_status(validation, language="zh")
    assert "无台阶通行" in out and "电梯" in out and "通勤证据" in out


def test_candidate_summary_keeps_cache_warning_even_for_eligible_results():
    validation = cv.validate_candidates([{"address": "Cached flat", "price": "£1100/month",
                                          "source": "OnTheMarket (cached)", "possibly_outdated": True}], {})
    assert "缓存" in cv.render_candidate_status(validation, language="zh")
    assert "cached" in cv.render_candidate_status(validation, language="en")


def safety_reference():
    return artifact("check_safety", {"address": "Watford", "reference_point": {
        "query": "Watford", "precision": "area", "lat": 51.65, "lon": -0.4,
        "coordinate_source": "nominatim", "is_specific_address": False}})


def test_safety_reference_cannot_borrow_the_poi_listing_coordinates():
    poi = artifact("search_nearby_pois", {"reference_point": {
        "precision": "listing_coordinates", "lat": 51.646597, "lon": -0.423485},
        "completed_types": ["supermarket"], "pois": {"supermarket": [{"name": "Test shop"}]}})
    st = state(safety_reference(), poi, weather())
    text = ("The safety check used the flat's exact coordinates.\n"
            "The nearby supermarket is Test shop. Current weather is 14.2°C.")
    out = cv.validate_evidence_scope(text, st)
    assert "safety check used the flat's exact coordinates" not in out
    assert "Test shop" in out and "14.2°C" in out
    assert "own reference is Watford (precision: area)" in out


def test_explicit_safety_coordinates_must_match_its_own_reference_point():
    out = cv.validate_evidence_scope(
        "The safety search used coordinates 51.646597, -0.423485.\nThe weather is rainy.",
        state(safety_reference()))
    assert "51.646597" not in out and "weather is rainy" in out
    supported = "The safety search used coordinates 51.65, -0.4."
    assert cv.validate_evidence_scope(supported, state(safety_reference())) == supported


def test_unverified_supplied_coordinate_identity_is_not_promoted_to_property_identity():
    a = safety_reference();a["raw_data"]["reference_point"].update(
        {"precision": "supplied_coordinates", "coordinate_source": "caller_supplied",
         "is_specific_address": True, "coordinate_identity_verified": False})
    out = cv.validate_evidence_scope("The police check used this property's exact coordinates.", state(a))
    assert "used this property's exact coordinates" not in out
    assert "precision: supplied_coordinates" in out


def test_an_unrelated_caveat_cannot_save_a_false_safety_scope_claim():
    text = "The safety check used the flat's exact coordinates, but I cannot confirm school admission."
    out = cv.validate_evidence_scope(text, state(safety_reference()))
    assert "used the flat's exact coordinates" not in out


def test_cached_fresh_details_cannot_be_described_as_live_checked():
    details = artifact("get_property_details", {"found": True, "from_cache": True,
        "cache_status": "fresh", "live_verified": False, "property": {
            "address": "North Approach", "price": "£1,100 pcm", "available_from": "Contact agent"}})
    text = "I checked the live price and current availability today.\nThe weather is rainy."
    out = cv.validate_evidence_scope(text, state(details))
    assert "checked the live price" not in out
    assert "£1,100 pcm" in out and "cached" in out and "weather is rainy" in out


def test_furniture_and_cache_limits_survive_provenance_validation():
    details = artifact("get_property_details", {"from_cache": True, "live_verified": False})
    text = "The cached details are not live verified. Furniture remains unknown."
    assert cv.validate_evidence_scope(text, state(details)) == text


@pytest.mark.parametrize("category", ["school", "gp"])
def test_unqueried_poi_type_is_not_an_empty_result(category):
    raw = {"completed_types": ["supermarket"], "executed_types": ["supermarket"],
           "empty_types": [], "category_status": {category: {"status": "not_requested"}},
           "pois": {"supermarket": [{"name": "Test shop"}]}}
    label = "schools" if category == "school" else "GPs"
    out = cv.validate_evidence_scope(f"No {label} were found nearby.\nTest shop was returned.",
                                     state(artifact("search_nearby_pois", raw)))
    assert f"No {label} were found" not in out
    assert "no completed lookup" in out and "Test shop" in out


def test_executed_but_failed_school_query_does_not_support_absence():
    raw = {"executed_types": ["school"], "completed_types": [], "failed_types": ["school"],
           "category_status": {"school": {"status": "failed"}}, "pois": {}}
    out = cv.validate_evidence_scope("No schools were found nearby.", state(artifact("search_nearby_pois", raw)))
    assert "No schools were found" not in out


def test_completed_empty_school_result_supports_a_scoped_negative():
    raw = {"completed_types": ["school"], "empty_types": ["school"],
           "category_status": {"school": {"status": "empty"}}, "pois": {"school": []}}
    text = "No schools were found within the searched 800-metre radius."
    assert cv.validate_evidence_scope(text, state(artifact("search_nearby_pois", raw))) == text


def test_school_admission_unknown_is_not_a_false_poi_absence_claim():
    text = "No school place can be confirmed. GP registration remains unknown."
    assert cv.validate_evidence_scope(text, state()) == text


def test_attendance_and_payg_upper_bound_are_not_rendered_as_actual_fixed_month_cost():
    cost = {"office_days_per_week": 2, "commuting_days_per_month": 8.667,
            "payg_weekly_estimate": 32.6, "payg_monthly_estimate": 141.27,
            "monthly_cost": 141.27, "payg_estimate_kind": "daily_cap_upper_bound",
            "coverage_verified": False, "coverage_note": "Origin zone could not be verified."}
    out = cv.render_structured_evidence_fallback(state(artifact("calculate_commute_cost", {
        "transport_cost": cost})))
    assert "2 days/week" in out and "8.667 commuting days/month" in out
    assert "£32.60/week" in out and "£141.27/month" in out
    assert "daily-cap upper bound" in out and "not verified actual spending" in out
    assert "coverage remains unverified" in out
    assert "22" not in out


def test_scope_correction_does_not_erase_an_already_honest_scope_note():
    st = state(safety_reference())
    text = "The safety check used the flat's exact coordinates."
    corrected = cv.validate_evidence_scope(text, st)
    assert cv.validate_evidence_scope(corrected, st) == corrected


def test_unverified_verdict_after_a_commute_heading_is_removed_without_minutes():
    text = "Commute: this meets your requirement.\nWeather: 14.2°C."
    out = cv.validate_commute_response(text, state())
    assert "meets your requirement" not in out
    assert "14.2°C" in out


def test_candidate_commute_measurement_must_match_both_origin_and_destination():
    rows = [{"address": "Alpha House", "price": "£1100/month"}]
    criteria = {"commute_destination": "Campus", "max_travel_time": 50}
    evidence = [{"success": True, "from_address": "Alpha House", "duration_minutes": 32,
                 "raw_data": {"from_address": "Alpha House", "to_address": "Other Campus",
                              "duration_minutes": 32, "basis": "tfl_journey_plan"}}]
    validation = cv.validate_candidates(rows, criteria, evidence)
    assert validation["unknown"] and not validation["eligible"]
    assert "verified_commute_minutes" not in validation["statuses"][0]["candidate"]


def test_failed_revalidation_clears_a_previous_plans_commute_measurement():
    rows = [{"address": "Alpha House", "verified_commute_minutes": 17}]
    validation = cv.validate_candidates(rows, {"commute_destination": "Campus", "max_travel_time": 50})
    assert "verified_commute_minutes" not in validation["statuses"][0]["candidate"]
    assert rows[0]["verified_commute_minutes"] == 17, "caller-owned rows must not be mutated"


def test_a_measured_duration_survives_a_separate_budget_exclusion():
    evidence = [{"success": True, "from_address": "Alpha House", "duration_minutes": 32,
                 "raw_data": {"from_address": "Alpha House", "to_address": "Campus", "duration_minutes": 32}}]
    validation = cv.validate_candidates([{"address": "Alpha House", "price": "£1900/month"}],
        {"max_budget": 1800, "commute_destination": "Campus", "max_travel_time": 50}, evidence)
    assert validation["excluded"] and not validation["eligible"]
    text = "Alpha House takes 32 minutes to Campus. Its rent exceeds the £1800 budget."
    assert cv.validate_commute_response(text, {"candidate_validation": validation}) == text
