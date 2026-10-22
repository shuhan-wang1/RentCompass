"""The commute-claim redactor (QA round r01: R1-10, ledger B-02/C-08 partial).

* "Note: some commute claims could not be verified and have been removed from this answer."
  was appended to answers with no commute claim at all (memory recall 5/5, weather,
  clarification questions; 11/35 turns of one session): a tidy-up that stripped a trailing
  ":" made the text differ, and any difference counted as a removal.
* Removal left orphan fragments ("Cheapest route: with the same line pattern.", "Cons - the
  longest of the three.", "Features: and the listing says students are welcome").
* The listing's own evidenced "3 mins walk to British Library" was withdrawn.
"""
from __future__ import annotations

from core.candidate_validation import _redact_commute_claims, validate_commute_response

NOTE = "Note: some commute claims could not be verified and have been removed from this answer."

RECALL_ANSWER = """I don't have anything saved about you yet. There's no stored budget, target area or university, or room preferences.

If you tell me a few basics, I can use them for your search:
- Where you want to live, or where you'll commute to (for example a university or workplace)
- Your monthly rent budget
- Any must-haves, such as a maximum commute time or safety concerns"""


def _state(message="What do you remember about me, my budget and preferences?", artifacts=()):
    return {"extracted_context": {"current_message": message, "reply_language": "en"},
            "tool_artifacts": list(artifacts)}


def test_r1_10_no_note_when_nothing_was_removed():
    out = validate_commute_response(RECALL_ANSWER, _state(artifacts=[
        {"tool": "recall_memory", "success": True, "raw_data": {"memories": []}}]))
    assert out == RECALL_ANSWER
    assert NOTE not in out


def test_r1_10_note_when_an_unevidenced_figure_is_removed():
    answer = "Flat A in Camden is £1,500/month. The commute to UCL is 12 minutes."
    out = validate_commute_response(answer, _state("Show me flats near UCL"))
    assert "12 minutes" not in out
    assert "£1,500/month" in out
    assert NOTE in out


def test_r1_10_listing_text_quote_is_kept():
    listing = {"tool": "get_property_details", "success": True, "raw_data": {
        "features": ["2 Bed 2 Bath", "3 mins walk to British Library", "Students welcome"]}}
    answer = ("Details for the cheapest one (Camden, London NW1)\n"
              "- Features: 2 bed 2 bath, 3 mins walk to British Library, and the listing says "
              "students are welcome")
    out = validate_commute_response(answer, _state("Show me the cheapest one's details", [listing]))
    assert "3 mins walk to British Library" in out
    assert NOTE not in out


def test_r1_10_no_dangling_fragments():
    for line in ("- Cheapest route: 31 minutes, with the same line pattern.",
                 "- Commute to Bank is about 35 minutes, the longest of the three.",
                 "- Fastest route: 22 minutes with 1 change."):
        removed = []
        assert _redact_commute_claims(line, zh=False, removed=removed) is None, line
        assert removed
    removed = []
    kept = _redact_commute_claims("Flat A, NW1 - £1,500/month, 25 min to UCL, and it has a garden.",
                                  zh=False, removed=removed)
    assert kept == "Flat A, NW1 - £1,500/month, it has a garden."
    assert removed


def test_r1_10_evidenced_total_named_by_its_abbreviation_is_kept():
    """calculate_commute measured 70 min to "University College London, Gower Street …"; the
    answer wrote "从 Limes Avenue 到 UCL 约 70 分钟" and the figure was withdrawn because "UCL"
    was not a label of that route."""
    raw = {"from_address": "Limes Avenue, Chigwell IG7",
           "to_address": "University College London, Gower Street, London WC1E 6BT",
           "mode": "transit", "duration_minutes": 70, "basis": "tfl_journey_plan",
           "route_summary": "Tube Central to Liverpool Street (25 min) -> Tube Metropolitan to "
                            "Euston Square (9 min)"}
    state = {"extracted_context": {"current_message": "最便宜的那套到UCL通勤要多久？",
                                   "reply_language": "zh"},
             "tool_artifacts": [{"tool": "calculate_commute", "success": True, "raw_data": raw}]}
    answer = ("最便宜的一套是 Limes Avenue, Chigwell IG7（£1400/月）。根据 TfL 行程规划（官方数据），"
              "从 Limes Avenue 到 UCL 约 70 分钟，属于较长通勤。")
    out = validate_commute_response(answer, state)
    assert "70 分钟" in out
    assert "注：部分通勤结论" not in out
