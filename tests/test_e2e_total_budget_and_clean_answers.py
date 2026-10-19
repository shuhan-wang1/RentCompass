"""Freeze-2 regressions: independent total budgets and complete preference/route prose.

Reproduces the total-only £2200 budget leak, family_retest turn04's orphaned
headings/basis/which clause, and student_retest turn06's deleted saved 30-minute cap.
All evidence is synthetic and all tests are local, with no provider/network calls.
"""
from __future__ import annotations

import asyncio
import copy
import json
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


def row(price=1800, address="Alpha House"):
    return {"address": address, "price": f"£{price}/month", "url": f"https://listing.test/{address}"}


def plan(**changes):
    return {"monthly_total_budget": 2200, "office_days_per_week": 2,
            "commute_destination": "North Campus", **changes}


def cost(amount=150, **changes):
    result = {"tool": "calculate_commute_cost", "success": True, "raw_data": {
        "success": True, "from_address": "Alpha House", "to_address": "North Campus",
        "office_days_per_week": 2, "transport_cost": {
            "office_days_per_week": 2, "monthly_cost": amount, "coverage_verified": True,
            "monthly_cost_basis": "Verified monthly fare total"}}}
    result["raw_data"]["transport_cost"].update(changes)
    return result


def test_actual_module_is_source_pinned():
    assert Path(cv.__file__).resolve() == _ROOT / "app/core/candidate_validation.py"


@pytest.mark.parametrize("rent", [2250, 2300, 3000])
def test_total_only_budget_excludes_a_rent_that_already_exceeds_it(rent):
    criteria = plan();before = copy.deepcopy(criteria)
    result = cv.validate_candidates([row(rent)], criteria)
    assert result["excluded"] and not result["eligible"]
    assert "alone exceeds total budget 2200" in result["excluded"][0]["reasons"][0]
    assert result["constraints"]["max_budget_monthly"] is None
    assert criteria == before and "max_budget" not in criteria


@pytest.mark.parametrize("rent", [1800, 2200])
def test_rent_within_total_is_unknown_without_certified_monthly_transport(rent):
    result = cv.validate_candidates([row(rent)], plan())
    assert result["unknown"] and not result["eligible"]
    assert "monthly travel cost" in result["unknown"][0]["unknown_reasons"][0]


def test_verified_bound_monthly_cost_can_establish_total_eligibility():
    result = cv.validate_candidates([row()], plan(), transport_cost_evidence=[cost()])
    candidate = result["eligible"][0]["candidate"]
    assert candidate["verified_monthly_transport_cost"] == 150
    assert candidate["verified_monthly_total_cost"] == 1950
    assert result["eligible"][0]["transport_cost_evidence_status"] == "success"


def test_a_verified_total_can_exceed_budget_when_rent_alone_is_inside_it():
    result = cv.validate_candidates([row(2100)], plan(), transport_cost_evidence=[cost(150)])
    assert result["excluded"] and not result["eligible"]
    assert "monthly rent and travel 2250 exceeds total budget 2200" in result["excluded"][0]["reasons"]
    assert "合计 £2250" in cv.render_candidate_status(result, language="zh")


@pytest.mark.parametrize("mutation", ["origin", "destination", "key", "frequency", "nested_frequency",
                                     "coverage", "upper_bound", "zone_error", "failed", "abandoned"])
def test_unbound_or_estimated_transport_cannot_establish_total_eligibility(mutation):
    evidence = cost();raw = evidence["raw_data"];monthly = raw["transport_cost"]
    if mutation == "origin": raw["from_address"] = "Beta House"
    if mutation == "destination": raw["to_address"] = "Other Campus"
    if mutation == "key": evidence["candidate_key"] = "url:https://listing.test/other"
    if mutation == "frequency": raw["office_days_per_week"] = 5
    if mutation == "nested_frequency": monthly["office_days_per_week"] = 5
    if mutation == "coverage": monthly["coverage_verified"] = False
    if mutation == "upper_bound": monthly.update(monthly_cost_basis="PAYG daily caps", payg_estimate_kind="daily_cap_upper_bound")
    if mutation == "zone_error": monthly["error"] = "Unable to determine zones"
    if mutation == "failed": evidence["success"] = False
    if mutation == "abandoned": evidence["abandoned"] = True
    result = cv.validate_candidates([row()], plan(), transport_cost_evidence=[evidence])
    assert result["unknown"] and not result["eligible"], mutation
    assert "verified_monthly_total_cost" not in result["unknown"][0]["candidate"]


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -1, "£150"])
def test_monthly_cost_must_be_a_finite_nonnegative_structured_number(value):
    result = cv.validate_candidates([row()], plan(), transport_cost_evidence=[cost(value)])
    assert result["unknown"] and not result["eligible"]


def test_latest_attendance_cannot_reuse_an_older_frequency():
    result = cv.validate_candidates([row()], plan(office_days_per_week=3), transport_cost_evidence=[cost()])
    assert result["unknown"][0]["transport_cost_evidence_status"] == "stale_frequency"


def test_a_newer_matching_failure_cannot_resurrect_a_previous_success():
    failed = cost();failed["success"] = False
    result = cv.validate_candidates([row()], plan(), transport_cost_evidence=[cost(), failed])
    assert result["unknown"] and not result["eligible"]


@pytest.mark.parametrize("changes", [{"office_days_per_week": None}, {"commute_destination": None}])
def test_missing_attendance_or_destination_is_not_inferred_from_the_fare(changes):
    result = cv.validate_candidates([row()], plan(**changes), transport_cost_evidence=[cost()])
    assert result["unknown"] and not result["eligible"]


def test_exact_monthly_pass_can_be_used_when_unselected_payg_comparison_is_a_cap_bound():
    evidence = cost(313.4, monthly_cost_basis="Monthly Travelcard",
                    payg_estimate_kind="daily_cap_upper_bound")
    result = cv.validate_candidates([row()], plan(), transport_cost_evidence=[evidence])
    assert result["eligible"][0]["candidate"]["verified_monthly_total_cost"] == 2113.4


def test_collector_style_cost_evidence_retains_backwards_compatible_positional_route_argument():
    evidence = cost();evidence.update(candidate_key=cv.candidate_key(row()), from_address="Alpha House", to_address="North Campus")
    result = cv.validate_candidates([row()], plan(), [evidence])
    assert result["eligible"]


def test_payload_and_provider_revalidate_a_new_total_instead_of_reusing_old_green_status():
    original = cv.validate_search_payload({"recommendations": [row(2250)], "search_criteria": {}})
    original["search_criteria"] = plan()
    out, _ = asyncio.run(cv.validate_search_payload_with_provider(None, original))
    assert out["recommendations"] == []
    assert out["excluded_candidates"][0]["price"] == "£2250/month"


def test_payload_passes_bound_cost_evidence_without_turning_total_into_rent_cap():
    out = cv.validate_search_payload({"recommendations": [row()], "search_criteria": plan()},
                                     transport_cost_evidence=[cost()])
    assert out["recommendations"][0]["verified_monthly_total_cost"] == 1950
    assert out["candidate_validation"]["constraints"]["max_budget_monthly"] is None


@pytest.mark.parametrize("reason, chinese", [
    ("monthly rent 2250 alone exceeds total budget 2200", "仅月租 £2250 就已超过每月总预算 £2200"),
    ("monthly rent and travel 2250 exceeds total budget 2200", "经核实的月租和交通费合计 £2250，超过每月总预算 £2200"),
    ("monthly travel cost is not verified for this listing, destination and attendance",
     "尚未核实该房源、目的地及最新出勤频率对应的月交通费，不能确认符合总预算"),
])
def test_public_candidate_reason_formatter_keeps_machine_values_and_localizes_total_reasons(reason, chinese):
    assert cv.candidate_reason_text(reason) == reason
    assert cv.candidate_reason_text(reason, language="zh-CN") == chinese


def test_refresh_rebuilds_total_qualification_with_bilingual_display_and_stable_machine_reasons():
    original = row(2250)
    original["qualification"] = {"status": "eligible", "constraints": cv.hard_constraints({})}
    out = cv.revalidate_view_results([original], plan())[0]
    qualification = out["qualification"]
    assert qualification["status"] == "excluded"
    assert qualification["reasons"] == ["monthly rent 2250 alone exceeds total budget 2200"]
    assert qualification["unknown_reasons"] == []
    assert qualification["reason_messages"] == {
        "en": qualification["reasons"], "zh": ["仅月租 £2250 就已超过每月总预算 £2200"]}
    assert original["qualification"]["status"] == "eligible"
    assert "reason_messages" not in original["qualification"]


def test_refresh_adds_display_to_current_stored_qualification_without_losing_unknown_machine_reasons():
    constraints = cv.hard_constraints(plan())
    reason = "monthly travel cost is not verified for this listing, destination and attendance"
    original = row();original["qualification"] = {
        "status": "unknown", "constraints": constraints, "reasons": [],
        "unknown_reasons": [reason], "source": "structured_listing"}
    out = cv.revalidate_view_results([original], plan())[0]
    assert out["qualification"]["unknown_reasons"] == [reason]
    assert out["qualification"]["reason_messages"]["en"] == [reason]
    assert "尚未核实该房源" in out["qualification"]["reason_messages"]["zh"][0]
    assert "reason_messages" not in original["qualification"]


@pytest.mark.parametrize("same_plan", [True, False])
def test_refresh_clears_a_legacy_green_budget_label_when_the_rent_cap_is_unspecified(same_plan):
    reason = "monthly travel cost is not verified for this listing, destination and attendance"
    criteria = plan()
    original = row();original.update(budget_basis=None, budget_status_code="unspecified",
                                     budget_status="✅ 在预算内")
    original["qualification"] = {
        "constraints": cv.hard_constraints(criteria if same_plan else {}),
        "status": "unknown", "reasons": [], "unknown_reasons": [reason]}
    out = cv.revalidate_view_results([original], criteria)[0]
    assert out["budget_basis"] is None and out["budget_status_code"] == "unspecified"
    assert out["budget_status"] == ""
    assert out["qualification"]["reasons"] == []
    assert out["qualification"]["unknown_reasons"] == [reason]
    assert original["budget_status"] == "✅ 在预算内"


def measured_row():
    original = row(2450, "Barrowgate Road, London W4")
    original.update(alternative=True, area="London")
    criteria = {"area": "London", "max_budget": 2400, "max_travel_time": 30,
                "commute_destination": "Gower Street, London WC1E 6BT"}
    proof = {"candidate_key": cv.candidate_key(original), "success": True,
             "evidence_status": "success", "from_address": original["address"],
             "to_address": criteria["commute_destination"], "duration_minutes": 32,
             "raw_data": {"from_address": original["address"],
                          "to_address": criteria["commute_destination"],
                          "duration_minutes": 32, "basis": "tfl_journey_plan"}}
    result = cv.validate_candidates([original], criteria, [proof])
    status = result["statuses"][0];candidate = status["candidate"]
    candidate["qualification"] = {"constraints": result["constraints"], "status": status["status"],
                                   "reasons": status["reasons"], "unknown_reasons": status["unknown_reasons"]}
    return candidate, criteria, proof


def test_student_r11_refresh_keeps_an_exact_listing_route_when_only_area_metadata_differs():
    candidate, criteria, _ = measured_row();criteria.pop("area")
    refreshed = cv.revalidate_view_results([candidate], criteria)[0]
    assert refreshed["verified_commute_minutes"] == 32
    assert refreshed["candidate_status"] == "excluded"
    assert "measured commute 32 min exceeds limit 30 min" in refreshed["qualification"]["reasons"]
    assert "commute evidence is missing" not in refreshed["qualification"]["unknown_reasons"]
    assert refreshed["verified_commute_evidence"]["candidate_key"] == cv.candidate_key(refreshed)


def test_refresh_can_use_explicit_server_evidence_for_an_older_row_without_stored_proof():
    candidate, criteria, proof = measured_row();criteria.pop("area")
    candidate.pop("verified_commute_evidence")
    refreshed = cv.revalidate_view_results([candidate], criteria, commute_evidence=[proof])[0]
    assert refreshed["verified_commute_minutes"] == 32
    assert "measured commute 32 min exceeds limit 30 min" in refreshed["qualification"]["reasons"]


def test_bound_route_survives_a_real_snapshot_json_round_trip_and_a_read_only_next_turn():
    from core.context_assembler import build_turn_snapshot
    candidate, criteria, _ = measured_row();criteria.pop("area")
    rows = cv.revalidate_view_results([candidate], criteria)
    snapshot = build_turn_snapshot(turn_id="synthetic-read-only", persistent_state={
        "accumulated_search_criteria": criteria, "extracted_context": {"view_results": rows}})
    restored = json.loads(json.dumps(snapshot))
    next_rows = cv.revalidate_view_results(restored["view_results"], restored["accumulated_search_criteria"])
    assert next_rows[0]["verified_commute_minutes"] == 32
    assert next_rows[0]["verified_commute_evidence"] == rows[0]["verified_commute_evidence"]
    assert "measured commute 32 min exceeds limit 30 min" in next_rows[0]["qualification"]["reasons"]


def test_refresh_recomputes_a_new_limit_against_the_same_measured_route():
    candidate, criteria, _ = measured_row();criteria["max_travel_time"] = 40
    refreshed = cv.revalidate_view_results([candidate], criteria)[0]
    assert refreshed["verified_commute_minutes"] == 32
    assert not any("measured commute" in reason for reason in refreshed["qualification"]["reasons"])


@pytest.mark.parametrize("change", ["destination", "listing_key", "origin", "town_origin", "estimate", "no_proof"])
def test_refresh_never_promotes_bare_minutes_or_a_route_for_another_scope(change):
    candidate, criteria, _ = measured_row();criteria.pop("area")
    if change == "destination": criteria["commute_destination"] = "Other Campus"
    if change == "listing_key": candidate["url"] = "https://listing.test/other-flat"
    if change == "origin": candidate["address"] = "Other Flat, London W4"
    if change == "town_origin": candidate["verified_commute_evidence"]["raw_data"]["from_address"] = "London"
    if change == "estimate": candidate["verified_commute_evidence"]["raw_data"]["basis"] = "straight_line_estimate"
    if change == "no_proof": candidate.pop("verified_commute_evidence")
    refreshed = cv.revalidate_view_results([candidate], criteria)[0]
    assert "verified_commute_minutes" not in refreshed
    assert "verified_commute_evidence" not in refreshed
    assert "commute evidence is missing" in refreshed["qualification"]["unknown_reasons"]


def test_latest_explicit_failure_overrides_the_saved_success_for_the_same_listing_and_destination():
    candidate, criteria, proof = measured_row();criteria.pop("area")
    failed = {**proof, "success": False, "evidence_status": "failed", "raw_data": {}}
    refreshed = cv.revalidate_view_results([candidate], criteria, commute_evidence=[proof, failed])[0]
    assert "verified_commute_minutes" not in refreshed
    assert "commute tool failed" in refreshed["qualification"]["unknown_reasons"]


def test_global_or_other_listing_evidence_cannot_restore_a_bare_stored_duration():
    candidate, criteria, proof = measured_row();criteria.pop("area")
    candidate.pop("verified_commute_evidence")
    other = {**proof, "candidate_key": "url:https://listing.test/other-flat"}
    refreshed = cv.revalidate_view_results([candidate], criteria, commute_evidence=[other])[0]
    assert "verified_commute_minutes" not in refreshed
    assert "commute evidence is missing" in refreshed["qualification"]["unknown_reasons"]


def commute_state(current="Check the selected Croxley View journey and current weather"):
    return {"extracted_context": {"current_message": current},
            "accumulated_search_criteria": {"max_travel_time": 50},
            "tool_artifacts": [
                {"tool": "get_weather", "success": True, "raw_data": {
                    "location": "Watford", "current_temp": 14.2, "condition": "Light Rain"}},
                {"tool": "calculate_commute", "success": True, "raw_data": {
                    "from_address": "Croxley View", "to_address": "Gower Street",
                    "duration_minutes": 32, "basis": "tfl_journey_plan"}}]}


def test_family_retest_r4_orphans_are_removed_and_structured_journey_is_restored():
    text = ('**Weather (Watford, current)**\n- 14.2°C, light rain.\n'
            '- No next-day forecast was returned.\n\n'
            '**Journey (Croxley View to Gower Street)**\n'
            '- basis "tfl_journey_plan" (includes walking legs and interchanges).\n'
            '**Does this establish a reliable\nNo. What is established\n'
            '- which is under your 50-minute cap.\n\n'
            'What remains unverified\n- Step-free status and street-to-door access remain unverified.')
    out = cv.validate_commute_response(text, commute_state())
    assert "14.2°C" in out and "No next-day forecast" in out
    assert "**Does this establish a reliable\n" not in out
    assert "No. What is established" not in out
    assert "- which is under" not in out and '- basis "' not in out
    assert "Croxley View → Gower Street, 32 minutes" in out
    assert "Step-free status" in out and "remain unverified" in out


def test_family_retest_r10_keeps_complete_details_weather_and_journey_without_orphan_asides():
    st = commute_state()
    st["tool_artifacts"][-1]["raw_data"]["from_address"] = "St. Augustine's Road, Camden Square, London NW1"
    text = ("## Property details\n**St.\nAugustine's Road, Camden Square, NW1**\n"
            "Price: £2,400 pcm. Street-to-door access and pet permission are not evidenced.\n"
            "## Weather\nCurrent temperature: 14.2°C, Light Rain.\n"
            "### What the returned fastest itinerary does NOT verify for a wheelchair user and a child\n"
            "including walking legs and interchanges.\nIt does not tell you\n"
            "- Whether every station on the route is step-free.\n"
            "- Whether there is space for a wheelchair and a child's buggy.\n"
            "Returned journey: St. Augustine's Road, Camden Square, London NW1 → Gower Street, 32 minutes.")
    out = cv.validate_commute_response(text, st)
    assert "St.\nAugustine" not in out
    assert "**St. Augustine's Road, Camden Square, NW1**" in out
    assert "£2,400 pcm" in out and "14.2°C, Light Rain" in out
    assert "including walking legs and interchanges." not in out
    assert "It does not tell you\n" not in out
    assert "Whether every station on the route is step-free." in out
    assert "Whether there is space for a wheelchair" in out
    assert "Gower Street, 32 minutes" in out


def test_removing_an_unknown_journey_also_removes_its_dependent_walking_aside():
    st = commute_state();st["tool_artifacts"] = []
    text = ("St. Augustine's Road costs £2,400.50; this journey takes 17 minutes, "
            "including walking legs and interchanges. Weather: 14.2°C.")
    out = cv.validate_commute_response(text, st)
    assert "17 minutes" not in out and "including walking legs" not in out
    assert "St. Augustine's Road costs £2,400.50" in out and "Weather: 14.2°C." in out


@pytest.mark.parametrize("fact", [
    "St. Augustine's Road has two bedrooms.",
    "Dr. Patel's Court has two bedrooms.",
    "Mr. Brown's House has two bedrooms.",
    "Mrs. Smith's House has two bedrooms.",
    "The current temperature is 14.2°C and the rent is £2,400.50.",
    "Saved link: https://listing.test/path;view?draft=yes and confirmation remains pending.",
    "The agent said ... further checks are needed.",
])
def test_scope_correction_does_not_split_abbreviations_decimal_url_or_ellipsis_facts(fact):
    st = {"tool_artifacts": [{"tool": "get_property_details", "success": True,
          "raw_data": {"from_cache": True, "live_verified": False,
                       "property": {"address": "St. Augustine's Road", "price": "£2400 pcm"}}}]}
    text = fact + " The listing is currently available."
    out = cv.validate_evidence_scope(text, st)
    assert "The listing is currently available." not in out
    assert fact in out


def test_complete_rhetorical_question_about_a_user_cap_is_not_mutilated():
    text = ('**Does this establish a reliable, step-free trip within our 50-minute limit?**\n'
            'No. The wheelchair route has not been verified.\nWeather: 14.2°C.')
    out = cv.validate_commute_response(text, commute_state())
    assert "**Does this establish a reliable, step-free trip within our 50-minute limit?**" in out
    assert "Weather: 14.2°C" in out


def test_orphan_cleanup_does_not_delete_an_independent_noncommute_basis_heading():
    text = 'Basis: police records.\nWhat is established:\nThe current weather is rainy.'
    assert cv.validate_commute_response(text, {}) == text


def preference_state(language="zh"):
    message = ("这条消息只保存长期偏好，不用搜索房源：在伦敦整租2居室，整套月租£2400，"
               "坐公共交通到UCL Bloomsbury主校区30分钟以内。")
    return {"extracted_context": {"current_message": message, "reply_language": language},
            "accumulated_search_criteria": {"max_travel_time": 30},
            "memory_write_contract": {"requested": True, "success": True},
            "tool_artifacts": [{"tool": "remember", "success": True, "raw_data": {"stored": True}}]}


def test_student_retest_r6_saved_default_cap_survives_inside_the_full_quote():
    text = ('我已经记住了。\n\n已确认这次只保存长期偏好，不搜索房源。\n\n'
            '「长期默认租房偏好：伦敦整租2居室，整套月租上限£2400；'
            '坐公共交通到UCL Bloomsbury主校区（Gower Street, WC1E 6BT，30分钟以内）；'
            '目前尚未决定居住区域，UCL只是上课地点。」')
    out = cv.validate_commute_response(text, preference_state())
    assert out == text
    assert "30分钟以内" in out and "结论缺少" not in out


def test_saved_cap_can_be_grounded_in_user_text_before_criteria_refresh():
    st = preference_state();st["accumulated_search_criteria"] = {}
    text = "已保存默认要求：公共交通到UCL 30分钟以内。这是通勤上限，并非已核实路线。"
    assert cv.validate_commute_response(text, st) == text


def test_repeating_the_users_ceiling_is_not_a_measured_duration_statement():
    text = "已保存默认要求：通勤上限是30分钟；这不是实测路线。"
    assert cv.validate_commute_response(text, preference_state()) == text


def test_saving_a_cap_does_not_license_a_property_measurement_of_the_same_number():
    st = preference_state("en")
    out = cv.validate_commute_response("This flat is 30 minutes from UCL. The rent is £2400/month.", st)
    assert "This flat is 30 minutes" not in out
    assert "£2400/month" in out


def test_an_unmeasured_noncap_number_is_not_saved_as_a_preference_reference():
    out = cv.validate_commute_response("长期偏好：实际通勤需要17分钟。月租2400。", preference_state())
    assert "17分钟" not in out
    assert "月租2400" in out


def test_family_r12_weather_only_answer_keeps_the_complete_unchanged_requirements_recap():
    st = commute_state("Only check Islington weather; do not calculate a commute or change housing conditions.")
    st["tool_artifacts"] = [st["tool_artifacts"][0]]
    text = ("Current weather in Islington: 14.2°C, Light Rain.\n\n"
            "As requested, I ran no commute calculation, the area stays Camden, the ceiling stays £3,000 per month, "
            "the whole-home and at least two-bedroom requirements stand, and the step-free street-to-door access, "
            "internal wheelchair space, permission for our cat and the 50-minute UCL commute limit remain unchanged. "
            "Islington was used only as a weather location, not as a search area.")
    assert cv.validate_commute_response(text, st) == text


def test_a_requirements_recap_does_not_allow_an_unevidenced_property_measurement():
    st = commute_state("Only check weather; requirements remain unchanged.")
    st["tool_artifacts"] = [st["tool_artifacts"][0]]
    text = "The area stays Camden and requirements remain unchanged. This flat takes 50 minutes to UCL. Weather: 14.2°C."
    out = cv.validate_commute_response(text, st)
    assert "This flat takes 50 minutes" not in out
    assert "Weather: 14.2°C" in out


@pytest.mark.parametrize("language, expected", [("en", "without room-type or bedroom filtering"),
                                                ("zh", "未按房型或卧室数筛选")])
def test_area_comparison_scope_is_always_visible_when_explicitly_reported_by_that_tool(language, expected):
    st = {"extracted_context": {"reply_language": language}, "tool_artifacts": [{
        "tool": "compare_or_rank_areas", "success": True, "raw_data": {"evidence_scope": {
            "rent_sample": "mixed_cached_listings", "room_type_filtered": False,
            "bedrooms_filtered": False, "budget_match_basis": "rent_only",
            "freshness_basis": "cache_fetch_time", "live_verified": False,
            "commute_scope": "area_reference_point"}}}]}
    out = cv.validate_evidence_scope("Camden: median rent £2000.", st)
    assert expected in out and "£2000" in out
    assert cv.validate_evidence_scope(out, st) == out


def test_unrelated_or_failed_tools_cannot_add_an_area_comparison_scope_note():
    for artifact in [{"tool": "get_weather", "success": True, "raw_data": {}},
                     {"tool": "compare_or_rank_areas", "success": False, "raw_data": {}}]:
        assert cv.validate_evidence_scope("Current weather: 14.2°C.", {"tool_artifacts": [artifact]}) == "Current weather: 14.2°C."


def test_area_comparison_scope_is_not_removed_by_an_independent_cached_details_guard():
    st = {"tool_artifacts": [
        {"tool": "get_property_details", "success": True, "raw_data": {"from_cache": True}},
        {"tool": "compare_or_rank_areas", "success": True, "raw_data": {"evidence_scope": {
            "rent_sample": "mixed_cached_listings", "room_type_filtered": False,
            "bedrooms_filtered": False, "budget_match_basis": "rent_only",
            "freshness_basis": "cache_fetch_time", "live_verified": False,
            "commute_scope": "area_reference_point"}}}]}
    out = cv.validate_evidence_scope("Camden comparison. The listing is currently available.", st)
    assert "without room-type or bedroom filtering" in out
    assert "availability is not verified live" in out
    assert "The listing is currently available." not in out
