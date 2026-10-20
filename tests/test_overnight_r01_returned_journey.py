"""The "Returned journey" evidence line names the stations the planner used (round r01: R1-14).

get_transport_info resolved "Highgate West Hill, London N6" to Archway Underground Station and
UCL to Goodge Street (station to station, 11 min), but the line read "Returned journey:
Highgate West Hill, London N6 → University College London … 11 minutes", i.e. door to door,
next to a card that said ~31 min.
"""
from __future__ import annotations

from core.candidate_validation import render_structured_evidence_fallback

RAW = {
    "success": True, "query_type": "journey", "duration_minutes": 11, "changes_count": 0,
    "source": "TfL Unified API Journey Planner",
    "from": {"input": "Highgate West Hill, London N6",
             "resolved_station": "Archway Underground Station", "naptan": "940GZZLUACY"},
    "to": {"input": "University College London, Gower Street, London WC1E 6BT",
           "resolved_station": "Goodge Street Underground Station", "naptan": "940GZZLUGDG"},
    "route_summary": "Tube Northern to Goodge Street Underground Station (11 min)",
}


def _render(raw, language="en"):
    return render_structured_evidence_fallback({
        "extracted_context": {"reply_language": language},
        "tool_artifacts": [{"tool": "get_transport_info", "success": True, "raw_data": raw}]},
        language=language)


def test_r1_14_journey_line_names_the_resolved_stations():
    text = _render(RAW)
    line = next(l for l in text.splitlines() if l.startswith("Returned journey"))
    assert line.startswith("Returned journey: Archway Underground Station (nearest station to "
                           "Highgate West Hill, London N6) → Goodge Street Underground Station")
    assert "station to station" in line and "walking" in line


def test_r1_14_chinese_line_too():
    text = _render(RAW, "zh")
    assert "Archway Underground Station（Highgate West Hill, London N6 的就近车站）" in text
    assert "站到站 11 分钟" in text


def test_r1_14_plain_addresses_unchanged():
    raw = dict(RAW, **{"from": "Archway Underground Station", "to": "Goodge Street Underground Station"})
    line = next(l for l in _render(raw).splitlines() if l.startswith("Returned journey"))
    assert line == ("Returned journey: Archway Underground Station → Goodge Street Underground "
                    "Station, 11 minutes (the planner's fastest returned itinerary).")
