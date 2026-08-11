"""Authoritative TfL zonal caps and Travelcard prices used by all tools.

The 2026 fare change took effect on 1 March 2026. TfL/Mayoral Decision
MD3464 froze daily caps and Travelcard prices at their 2025 level through
March 2027. Values below are transcribed from TfL's official 2026 adult and
18+ Student Oyster fare tables, not inferred by multiplying adult prices.
"""

from __future__ import annotations

from typing import Any


FARE_EDITION = "2026"
FARE_EFFECTIVE_DATE = "2026-03-01"
FARE_SOURCE = "Transport for London 2026 zonal caps and Travelcard prices"
ADULT_FARE_SOURCE_URL = "https://content.tfl.gov.uk/adult-fares.pdf"
STUDENT_FARE_SOURCE_URL = (
    "https://content.tfl.gov.uk/18-plus-student-oyster-photocard-fares.pdf"
)
FARE_DECISION_URL = (
    "https://www.london.gov.uk/md3464-march-2026-transport-london-fare-changes"
)


# Daily PAYG caps are the same for a standard adult and an 18+ Student Oyster.
# TfL's lower off-peak cap requires a separately eligible Railcard discount and
# must not be presented as the ordinary adult/student cap.
_DAILY_CAPS = {
    (1, 1): 8.90, (1, 2): 8.90, (1, 3): 10.50,
    (1, 4): 12.80, (1, 5): 15.30, (1, 6): 16.30,
    (2, 2): 8.90, (2, 3): 10.50, (2, 4): 12.80,
    (2, 5): 15.30, (2, 6): 16.30,
    (3, 3): 10.50, (3, 4): 12.80, (3, 5): 15.30,
    (3, 6): 16.30,
    (4, 4): 12.80, (4, 5): 15.30, (4, 6): 16.30,
    (5, 5): 15.30, (5, 6): 16.30,
    (6, 6): 16.30,
}

_ADULT_TRAVELCARDS = {
    (1, 1): (44.70, 171.70), (1, 2): (44.70, 171.70),
    (1, 3): (52.50, 201.60), (1, 4): (64.20, 246.60),
    (1, 5): (76.40, 293.40), (1, 6): (81.60, 313.40),
    (2, 2): (33.50, 128.70), (2, 3): (33.50, 128.70),
    (2, 4): (37.10, 142.50), (2, 5): (44.50, 170.90),
    (2, 6): (55.90, 214.70),
    (3, 3): (33.50, 128.70), (3, 4): (33.50, 128.70),
    (3, 5): (37.10, 142.50), (3, 6): (44.50, 170.90),
    (4, 4): (33.50, 128.70), (4, 5): (33.50, 128.70),
    (4, 6): (37.10, 142.50),
    (5, 5): (33.50, 128.70), (5, 6): (33.50, 128.70),
    (6, 6): (33.50, 128.70),
}

_STUDENT_TRAVELCARDS = {
    (1, 1): (31.20, 119.90), (1, 2): (31.20, 119.90),
    (1, 3): (36.70, 141.00), (1, 4): (44.90, 172.50),
    (1, 5): (53.40, 205.10), (1, 6): (57.10, 219.30),
    (2, 2): (23.40, 89.90), (2, 3): (23.40, 89.90),
    (2, 4): (25.90, 99.50), (2, 5): (31.10, 119.50),
    (2, 6): (39.10, 150.20),
    (3, 3): (23.40, 89.90), (3, 4): (23.40, 89.90),
    (3, 5): (25.90, 99.50), (3, 6): (31.10, 119.50),
    (4, 4): (23.40, 89.90), (4, 5): (23.40, 89.90),
    (4, 6): (25.90, 99.50),
    (5, 5): (23.40, 89.90), (5, 6): (23.40, 89.90),
    (6, 6): (23.40, 89.90),
}


def zone_key(start_zone: int, end_zone: int) -> str:
    """Return the stable key used in tool payloads, e.g. zone1-3."""
    start, end = sorted((int(start_zone), int(end_zone)))
    if start < 1 or end > 6:
        raise ValueError("TfL zonal fare table covers Zones 1-6")
    return f"zone{start}only" if start == end else f"zone{start}-{end}"


def get_zonal_fare(
    start_zone: int,
    end_zone: int,
    user_type: str = "adult",
) -> dict[str, Any]:
    """Return one fare record with source/effective-date metadata.

    Raises ValueError for unsupported zones or passenger types so tools can
    degrade honestly instead of silently substituting a Zone 1-N product.
    """
    start, end = sorted((int(start_zone), int(end_zone)))
    key = (start, end)
    passenger = "student" if "student" in (user_type or "").lower() else "adult"
    travelcards = (
        _STUDENT_TRAVELCARDS if passenger == "student"
        else _ADULT_TRAVELCARDS
    )
    if key not in _DAILY_CAPS or key not in travelcards:
        raise ValueError(f"No TfL fare data for Zones {start}-{end}")

    weekly, monthly = travelcards[key]
    source_url = (
        STUDENT_FARE_SOURCE_URL if passenger == "student"
        else ADULT_FARE_SOURCE_URL
    )
    return {
        "zone_key": zone_key(start, end),
        "start_zone": start,
        "end_zone": end,
        "user_type": passenger,
        "daily_cap": _DAILY_CAPS[key],
        "daily_peak_cap": _DAILY_CAPS[key],
        "daily_off_peak_cap": None,
        "daily_off_peak_cap_note": (
            "No separate standard off-peak cap. Lower off-peak caps require "
            "an eligible Railcard linked to Oyster."
        ),
        "weekly": weekly,
        "monthly": monthly,
        "edition": FARE_EDITION,
        "effective_date": FARE_EFFECTIVE_DATE,
        "source": FARE_SOURCE,
        "source_url": source_url,
        "fare_change_decision_url": FARE_DECISION_URL,
    }
