"""Defects in the constraint checkers' OWN logic, found by re-scoring retained evidence.

These are not threshold preferences. Each checker below was failing to implement the rule
its own name and arguments declare, and each was wrong in BOTH directions:

  * ``must_refuse_fabrication`` never looked at ``field`` and never looked at a number.
    It was a keyword test, so a correct answer with nothing to refuse failed (D9) while a
    hedged invention passed.

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
