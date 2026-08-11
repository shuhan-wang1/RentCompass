from __future__ import annotations

import asyncio

from uk_rent_agent.data.tfl_fares import (
    ADULT_FARE_SOURCE_URL,
    FARE_EFFECTIVE_DATE,
    STUDENT_FARE_SOURCE_URL,
    get_zonal_fare,
)


def test_official_2026_zone_1_2_adult_and_student_values():
    adult = get_zonal_fare(1, 2, "adult")
    student = get_zonal_fare(1, 2, "student")

    assert (adult["daily_cap"], adult["weekly"], adult["monthly"]) == (
        8.90, 44.70, 171.70,
    )
    assert (student["daily_cap"], student["weekly"], student["monthly"]) == (
        8.90, 31.20, 119.90,
    )
    assert adult["effective_date"] == student["effective_date"] == FARE_EFFECTIVE_DATE
    assert adult["source_url"] == ADULT_FARE_SOURCE_URL
    assert student["source_url"] == STUDENT_FARE_SOURCE_URL


def test_standard_adult_or_student_does_not_receive_railcard_offpeak_cap():
    for passenger in ("adult", "student"):
        fare = get_zonal_fare(1, 5, passenger)
        assert fare["daily_off_peak_cap"] is None
        assert "Railcard" in fare["daily_off_peak_cap_note"]


def test_all_transport_tools_import_the_same_fare_function():
    from core.tools import calculate_commute_cost
    from core.tools import check_transport_cost
    from core.tools import get_transport_info

    assert calculate_commute_cost.get_zonal_fare is get_zonal_fare
    assert check_transport_cost.get_zonal_fare is get_zonal_fare
    assert get_transport_info.get_zonal_fare is get_zonal_fare


def test_travelcard_tools_return_identical_values_and_provenance():
    from core.tools.check_transport_cost import check_transport_cost_impl
    from core.tools.get_transport_info import _do_travelcard

    checked = asyncio.run(
        check_transport_cost_impl(start_zone=1, end_zone=2, travel_type="student")
    )["data"]
    transport = _do_travelcard(2, "", "", "student")

    assert checked["prices"]["monthly_pass"] == transport["monthly_display"] == "£119.90"
    assert checked["prices"]["weekly_pass"] == transport["weekly_display"] == "£31.20"
    assert checked["source"] == transport["source"]
    assert checked["source_url"] == transport["source_url"]
    assert checked["effective_date"] == transport["effective_date"]
