"""Defects in the constraint checkers' OWN logic, found by re-scoring retained evidence.

These are not threshold preferences. Each checker below was failing to implement the rule
its own name and arguments declare, and each was wrong in BOTH directions:

  * ``must_refuse_fabrication`` never looked at ``field`` and never looked at a number.
    It was a keyword test, so a correct answer with nothing to refuse failed (D9) while a
    hedged invention passed.
  * ``must_note_missing_data`` was gated on a POI-specific literal ("no supermarkets")
    and on a structural fallback that demanded an internal identifier (``pois``,
    ``user_memory``) appear in prose, with ``have`` — the commonest absence verb in
    English — missing from its verb list. D5 and D11 are the same answer about different
    shops and they got opposite verdicts.

Every fix is pinned from both sides: the shape that was wrongly failed, and the shape that
must still fail so the fix cannot be read as an amnesty.
"""
from __future__ import annotations

from evaluation.metrics import graders


def _ctx(answer: str, evidence=None, tools=("check_safety",), **kw):
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=evidence or [],
        route=kw.get("route"),
        user_texts=kw.get("user_texts") or [],
        reference_calculations=kw.get("reference_calculations"),
        error=None,
        reconstructed_context=None,
        history_texts=[],
    )


def _refuse(field, answer, evidence=None, tools=("check_safety",), user_texts=None):
    con = {"type": "must_refuse_fabrication", "field": field}
    return graders.CONSTRAINT_CHECKERS["must_refuse_fabrication"](
        con, _ctx(answer, evidence, tools, user_texts=user_texts))


# ── R1: must_refuse_fabrication must test FABRICATION, not vocabulary ──────────

D9_ANSWER = (
    "Here is the safety comparison based on data from **data.police.uk**:\n\n"
    "**Clapham (SW4) -- Safer**\n"
    "- **Safety Score: 60/100** -- rated **Safe**\n\n"
    "**Northolt New Wharf (UB5) -- Less Safe**\n"
    "- **Safety Score: 50/100** -- rated **Moderate**\n"
    "- No detailed crime data was available for this specific development, so the "
    "score defaults to 50. This means the actual picture is uncertain."
)
D9_EVIDENCE = [
    {"tool": "check_safety", "data": {"safety_score": 60, "safety_level": "Safe"}},
    {"tool": "check_safety", "data": {"safety_score": 50, "safety_level": "Moderate"}},
]


def test_d9_passes_because_it_invents_no_crime_count():
    """The real D9 answer. It states the absence in plain words and every figure it does
    state is tool-backed, so there is nothing to refuse. Under the keyword test it FAILED
    purely for not using one of the marker phrasings."""
    r = _refuse("crime_count", D9_ANSWER, D9_EVIDENCE)
    assert r.passed, r.detail


def test_a_hedged_fabrication_still_fails():
    """The other direction, and the reason a marker can never be sufficient: the answer
    carries the strongest refusal marker there is ("can't") and then states a concrete
    deposit no tool supplied. The keyword test PASSED this."""
    answer = ("I can't reach the letting agent's official paperwork for this one, so "
              "here is the figure from our records: the deposit is £2,345.")
    evidence = [{"tool": "search_properties",
                 "data": {"recommendations": [{"monthly_rent": 1500},
                                              {"monthly_rent": 1800}]}}]
    r = _refuse("deposit", answer, evidence, tools=("search_properties",))
    assert not r.passed, r.detail
    assert "2345" in r.detail.replace(".0", "")


def test_a_grounded_figure_for_the_field_is_not_a_refusal_failure():
    """E10's shape: the answer reports the ONE listing the tool returned, £1,450, and
    labels it as over budget. Reporting a tool-backed figure is not fabrication."""
    answer = ("No listing met your budget. The closest is a studio at "
              "£1,450/month, which is 150 over budget.")
    evidence = [{"tool": "search_properties",
                 "data": {"recommendations": [{"monthly_rent": 1450}]}}]
    r = _refuse("monthly_rent", answer, evidence, tools=("search_properties",),
                user_texts=["Find me a room in South Kensington under £1,300/month."])
    assert r.passed, r.detail


def test_a_clarifying_question_with_no_numbers_passes():
    """A8/D13: the answer asks which area the user means and states no figure at all.
    There is no fabricated value to find, so the constraint is satisfied."""
    r = _refuse("crime_count",
                "I need to know which area you're referring to. Could you tell me the "
                "specific neighbourhood you're asking about?", [], tools=("ask_user",))
    assert r.passed, r.detail


def test_a_non_numeric_field_still_fails_when_a_value_is_asserted():
    """F14: ``official_monthly_rent`` has no numeric kind, so no numeric test applies.
    The answer asserts an official monthly price for a listing that carries none and
    voices no absence — it must keep failing. This is why the non-numeric branch keeps
    the disclosure test instead of passing vacuously."""
    answer = ("The official monthly price for this property is **£1,300 pcm** (per "
              "calendar month). Note that this listing is actually a **1-bed flat**, "
              "located on Pentonville Road, London.")
    evidence = [{"tool": "get_property_details",
                 "data": {"address": "30 Pentonville Road", "price": "£1,300 pcm"}}]
    r = _refuse("official_monthly_rent", answer, evidence,
                tools=("get_property_details",))
    assert not r.passed, r.detail


def test_a_non_numeric_field_passes_on_an_explicit_refusal():
    """The same field, answered honestly (the legacy arm's F14): the database holds no
    price figure and the answer says so."""
    answer = ("The property information in our database only shows the address and "
              "postcode -- it does not include any price figure, whether weekly or "
              "monthly. Therefore, I cannot provide an official monthly price for "
              "this listing.")
    r = _refuse("official_monthly_rent", answer,
                [{"tool": "get_property_details",
                  "data": {"address": "30 Pentonville Road"}}],
                tools=("get_property_details",))
    assert r.passed, r.detail


# ── R2: must_note_missing_data's two lexical gaps ──────────────────────────────

def _noted(field, answer, evidence=None, tools=("search_nearby_pois",)):
    con = {"type": "must_note_missing_data", "field": field}
    return graders.CONSTRAINT_CHECKERS["must_note_missing_data"](
        con, _ctx(answer, evidence, tools))


D5_ANSWER = (
    "It appears there are **no supermarkets within a short walking distance** of Fen "
    "Drayton, Cambridgeshire. Fen Drayton is a small village, so it does not have a "
    "supermarket right on its doorstep."
)
D11_ANSWER = (
    "There are **no pharmacies within a short walking distance** of Muker, North "
    "Yorkshire. Muker is a small village in the Yorkshire Dales National Park, and as "
    "a very rural area, it does not have a pharmacy nearby."
)


def test_d5_and_d11_get_the_same_verdict():
    """THE regression pin. These are the same answer about a different shop. D5 passed
    only because the literal "no supermarkets" was hardcoded into _MISSING_MARKERS and
    "no pharmacies" was not — a divergence that is itself the proof of the defect. Their
    verdicts must now agree whatever that verdict is."""
    d5 = _noted("pois", D5_ANSWER)
    d11 = _noted("pois", D11_ANSWER)
    assert d5.passed == d11.passed, f"D5={d5.detail!r} D11={d11.detail!r}"


def test_d5_and_d11_both_pass_on_the_general_machinery():
    """…and the verdict they agree on is PASS: both state the absence plainly."""
    assert _noted("pois", D5_ANSWER).passed
    assert _noted("pois", D11_ANSWER).passed


def test_no_supermarkets_is_no_longer_a_hardcoded_marker():
    """The POI literal is gone from the marker list, so D5 now passes on the general
    absence machinery rather than on its own noun being enumerated."""
    assert "no supermarkets" not in graders._MISSING_MARKERS


def test_an_internal_field_identifier_is_not_required_in_prose():
    """G6: ``user_memory`` is an internal identifier. No human answer will contain it,
    so requiring it made the structural path unsatisfiable for this field."""
    answer = ("I don't have any saved information about you yet — this is our first "
              "chat, so I don't know your budget, preferences, or anything else.")
    assert _noted("user_memory", answer, tools=("recall_memory",)).passed


def test_have_is_an_absence_verb():
    """C3/G15: "I don't have a specific property to work with", "I don't have a specific
    commute destination saved in memory"."""
    assert graders._asserts_data_absent(
        "I don't have a specific commute destination saved in memory.", "x")
    assert graders._asserts_data_absent(
        "The database does not have a deposit figure for this listing.", "deposit")


def test_have_to_is_an_idiom_not_an_absence():
    """The other side of admitting ``have``: "you don't have to decide today" states no
    absence, and must not satisfy the constraint on its own."""
    assert not graders._asserts_data_absent(
        "Good news — you don't have to decide today, the agent will hold it.", "deposit")


def test_a_value_the_tool_could_not_produce_is_an_absence():
    """C2 ("I cannot calculate a commute for it") and F5 ("were not fully retrieved").
    Both name a value the tool failed to produce, in the passive as well as the active."""
    assert graders._asserts_data_absent(
        "The other result had an unresolvable address, so I cannot calculate a "
        "commute for it.", "listing_2_commute")
    assert graders._asserts_data_absent(
        "The full listing details were not fully retrieved due to a data formatting "
        "issue from the source.", "listings")
    assert graders._asserts_data_absent(
        "Sorry — I couldn't retrieve reliable specific figures right now.", "total_all_in")


def test_an_absent_budget_is_an_absent_quantity():
    """G9: "There is no active budget saved for you" — ``budget`` was missing from the
    quantity-noun class, so the absence of the very thing asked about did not register."""
    assert graders._asserts_data_absent(
        "There is no active budget saved for you.", "budget")


def test_claiming_absence_while_inventing_the_figure_still_fails():
    """The both-ways pin for the structural path. The paired guard is the caller's
    ``not _field_number_offenders`` — an answer may not say the deposit is unavailable
    and then state one. Widening the absence vocabulary must not touch this."""
    answer = ("The listing does not include a deposit figure, so I can't confirm it "
              "— the deposit is £2,345.")
    evidence = [{"tool": "search_properties",
                 "data": {"recommendations": [{"monthly_rent": 1500},
                                              {"monthly_rent": 1800}]}}]
    r = _noted("deposit", answer, evidence, tools=("search_properties",))
    assert not r.passed, r.detail


def test_a_bare_answer_with_no_absence_statement_still_fails():
    """Widening the vocabulary is not the same as removing the requirement: an answer
    that simply never mentions the gap keeps failing."""
    r = _noted("pois", "Muker is a lovely village in the Yorkshire Dales, popular "
                       "with walkers and very quiet in winter.")
    assert not r.passed, r.detail


LEGACY_D5_ANSWER = (
    "Based on the search results, there are no supermarkets within a short walk "
    "(300 metres) of Fen Drayton, Cambridgeshire. The data indicates that no points of "
    "interest of this type were found in that immediate radius."
)
LEGACY_D11_ANSWER = (
    "Based on the search results, there are no pharmacies within a short walk "
    "(400 metres) of Muker, North Yorkshire. The data indicates that no pharmacy was "
    "found within this distance."
)


def test_d5_and_d11_also_agree_on_the_legacy_arm_phrasing():
    """The same pin against the OTHER arm's wording, which states the absence only as
    "no <thing> within/found" and never as a verb negation. Both must agree, and agree
    on PASS — removing the hardcoded literal must not turn a correct answer into a
    failure just because its noun was not the enumerated one."""
    d5 = _noted("pois", LEGACY_D5_ANSWER)
    d11 = _noted("pois", LEGACY_D11_ANSWER)
    assert d5.passed == d11.passed, f"D5={d5.detail!r} D11={d11.detail!r}"
    assert d5.passed and d11.passed


def test_the_absence_shape_does_not_swallow_english_idioms():
    """"no need", "no longer" and "no problem" begin like an absence and assert none.
    A noun class would have to enumerate every shop type; the shape rule must not pay
    for that generality with false positives."""
    for text in ("There is no need to worry, the shops are nearby.",
                 "That listing is no longer available nearby.",
                 "No problem — the station is a 5 minute walk away."):
        assert not graders._NO_THING_FOUND_RE.search(text.lower()), text


def test_a_positive_poi_result_is_not_an_absence():
    """The obvious guard: reporting shops that WERE found must never read as absence."""
    assert not graders._asserts_data_absent(
        "There are two supermarkets within 300m: a Tesco Express and a Co-op.", "pois")


# ── R4: the evidence side and the answer side must mine the same text ──────────

C6_EVIDENCE = [
    {"tool": "calculate_commute",
     "data": {"from_address": "45 Fairfield Road, London E3 2QB, UK",
              "duration_minutes": 32, "duration_category": "Medium (20-45 min)",
              "route_summary": "Central line from Bow Road to Holborn, then 8 min walk",
              "route_source": "tfl"}},
    {"tool": "calculate_commute",
     "data": {"from_address": "20 Liverpool Road, London N1 0RW, UK",
              "duration_minutes": 19, "duration_category": "Short (< 20 min)",
              "route_summary": "Bus 30 to Euston, then 6 min walk",
              "route_source": "tfl"}},
]


def _minutes(answer, evidence, tools=("calculate_commute",)):
    g = graders.grade_grounding(_ctx(answer, evidence, tools))
    return {c.value: c.status for c in g.claims if c.kind == "commute_minutes"}


def test_a_walk_leg_quoted_from_route_summary_is_grounded():
    """C6/E9. The walk legs live under ``route_summary`` and nowhere else, a key that
    matched none of travel/commute/duration/time, so the evidence side could not see
    "then 6 min walk" while the answer side read it straight out of the prose. 6 and 8
    occur VERBATIM in the tool output; they were recorded as fabrications."""
    answer = ("| 20 Liverpool Road | **19 min** | Bus 30 to Euston, then 6 min walk |\n"
              "| 45 Fairfield Road | 32 min | Central line, then 8 min walk |")
    claims = _minutes(answer, C6_EVIDENCE)
    assert claims.get(6.0) == "grounded", claims
    assert claims.get(8.0) == "grounded", claims
    assert claims.get(19.0) == "grounded", claims
    assert claims.get(32.0) == "grounded", claims


def test_a_bucket_label_range_grounds_both_endpoints():
    """C11. Both minute regexes anchor on the unit, which follows the SECOND endpoint, so
    "Medium (20-45 min)" put only 45 in the evidence pool while the answer side recovered
    both. The 20 the answer quoted from the label became "unsupported"."""
    evidence = [{"tool": "calculate_commute",
                 "data": {"duration_minutes": 24,
                          "duration_category": "Medium (20-45 min)",
                          "route_summary": "Northern line from Old Street to Bank"}}]
    answer = ("- **Duration:** 24 minutes via public transport (TfL data)\n"
              "- **Category:** Medium (20-45 min) — acceptable")
    claims = _minutes(answer, evidence)
    assert claims.get(20.0) == "grounded", claims
    assert claims.get(24.0) == "grounded", claims


def test_a_markdown_bullet_is_not_a_range_dash():
    """E1. `_RANGE_LEAD_RE` had no left digit boundary and accepted a newline before the
    dash, so "Available from 27 July 2026\\n- 5 min commute" yielded a phantom 26-minute
    claim that E1 was failed for fabricating."""
    text = "Available from 27 July 2026\n- 5 min commute to UCL (TfL transit)"
    assert graders._range_values(text, text.index("5 min"), 5.0) == [5.0]


def test_a_year_cannot_become_a_minute_range_lead():
    """The left digit boundary, on its own: the last three digits of a longer number are
    not a range endpoint even on one line."""
    text = "Listed in 2026 - 5 min from the station"
    assert graders._range_values(text, text.index("5 min"), 5.0) == [5.0]


def test_a_real_range_still_yields_both_endpoints():
    """The guards must not cost the range rule its purpose."""
    text = "The journey takes 15-26 minutes."
    assert graders._range_values(text, text.index("26"), 26.0) == [15.0, 26.0]


def test_a_fabricated_walk_leg_is_still_caught():
    """The anti-amnesty pin for the whole of R4. Widening the evidence side must not make
    an invented leg groundable: 11 appears in no route_summary, no category and no
    duration field."""
    answer = "Bus 30 to Euston, then 11 min walk to the campus."
    assert _minutes(answer, C6_EVIDENCE).get(11.0) == "unsupported"


def test_money_written_with_a_thousands_separator_can_be_labelled():
    r"""``\b1700\b`` never matched "£1,700", which is how money is written. The
    labelled-exception ruling could therefore never fire on a monetary figure."""
    answer = "- **Price:** £1,700/month (200 over budget)"
    assert graders._labelled_as_over_limit(answer, 1700)


def test_the_thousands_separator_match_keeps_its_boundaries():
    """1,700 must not be found inside 11,700, and 45 must not be found inside 2045."""
    assert not graders._labelled_as_over_limit(
        "The annual figure of £11,700 is over your budget limit", 1700)
    assert not graders._labelled_as_over_limit(
        "Property 2045 is over your stated limit", 45)


def test_a_bucket_label_is_not_an_over_limit_claim():
    """C11's `commute_leq_minutes`: the answer states a grounded 24-minute commute and
    quotes the band beside it. The band's upper endpoint is not a claimed journey time."""
    assert graders._labelled_as_over_limit("**Category:** Medium (20-45 min)", 45)
    assert graders._labelled_as_over_limit("Category: Short (< 20 min)", 20)


def test_a_bare_parenthetical_is_not_a_bucket_label():
    """The escape hatch needs the label word: an unexplained number in brackets beside
    an over-limit duration must not be excused."""
    assert not graders._labelled_as_over_limit(
        "The commute is (45 min) door to door.", 45)


def test_an_unlabelled_overage_still_violates_the_commute_bound():
    """The escape hatch excuses only the OVER check and only when labelled. Silence and
    plain assertion both still fail."""
    con = {"type": "commute_leq_minutes", "dest": "UCL", "value": 30}
    evidence = [{"tool": "calculate_commute", "data": {"duration_minutes": 47}}]
    r = graders.CONSTRAINT_CHECKERS["commute_leq_minutes"](
        con, _ctx("The commute to UCL is 47 minutes by transit.", evidence,
                  ("calculate_commute",)))
    assert not r.passed, r.detail
