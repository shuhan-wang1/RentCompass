"""A case must declare a constraint over every numeric dimension its answer can assert.

E10 asks for a room under budget near South Kensington **and** a commute to Imperial
College. Its declared contract was::

    must_call_tool                              (search_properties)
    must_note_missing_data[within_budget_listings]
    must_refuse_fabrication[monthly_rent]

Three constraints, all about the search and the rent — and **none about a commute
figure**. The fc arm of the retained round (``.runtime/round-8793c0b-internal-2026-07-25``,
``eval/sweep``) answered with 「步行到帝国理工约15-20分钟」 and 「地铁到帝国理工约10分钟」.
Checked with word boundaries against that turn's 4,346-char evidence blob, 15, 20 and 10
appear **zero** times; the only 30 in it is the user's own 「30分钟内」 criterion restated.
The rent it quotes, £1,450, IS in evidence and is correctly labelled over budget, so
``must_refuse_fabrication[monthly_rent]`` passes on the merits. The turn's own telemetry
agrees the answer is half-invented — ``grounded_rate 0.5000``, ``source_coverage 0.5000``
— yet the case scored **3/3, a full pass**, because the corpus had no rule at all for the
one dimension a user would act on. Commute minutes decide whether somebody signs a lease.

GOVERNANCE (§3.5 — do not change a decision rule after seeing the measurement it judges).
This is not a moved pass mark. There was no mark to move: no constraint in E10's contract
could ever fail on a commute figure, whatever the agent said, so no threshold, tolerance
or checker semantic is being retuned. What changes is *coverage* — a dimension that was
ungraded becomes graded. The direction is the point and is stated plainly: **E10 now FAILS
where it used to pass** on the fc arm, on ``no_fabricated_number[duration_minutes]``. The
legacy arm's E10 answer states no journey time and still passes 4/4, so the constraint
discriminates between a fabricating and an honest answer rather than penalising the case.

No new constraint type was invented. ``no_fabricated_number`` already exists, already
takes a ``field``, and ``graders._field_to_kind`` already maps ``duration_minutes`` to the
``commute_minutes`` kind — C1, C2, C6, C7, C9 and C12 already use exactly this instance.
``schema.json`` therefore needs no change.

E4 carries the same amendment. Its query states a "15-min walk to a tube station" and it
likewise declared no constraint over a walk/journey time. Verified against both retained
arms, adding the constraint is a **no-op for E4's verdict** (4/4 -> 5/5, still passing) —
it registers coverage rather than changing an outcome, and it is what lets the source
guard below carry no exemption list.

SECOND PASS (2026-07-27): a sweep of the whole corpus against both retained arms found
seven more cases that passed on an arm while stating an ungrounded number of an uncovered
kind. Each was then read rather than trusted, and only **C8 and D11** survived that
reading; both are the exact E10 shape and both FLIP. Five were rejected, because the
"fabrication" was an artifact of the checker, not an invention by the agent:

  * **E3** — "140m (2 min walk) / 220m (3 min) / 280m (4 min)". The distances are
    grounded and the times are a coherent 70 m/min derivation from them. The checker also
    splits the three identical constructs, sparing 2 and flagging 3 and 4, purely because
    the words "within a 5-minute walk" happen to sit inside 2's text window. A rule that
    fires on two of three identical derivations is not defensible.
  * **E6** — the sole offender is "Supermarket: **None within 500m**", a sentence that
    DECLINES to assert a distance. It is flagged only because
    ``graders._number_asserts_field_value`` threshold-filters ``money`` and
    ``commute_minutes`` and returns True unconditionally for every other kind, so a
    distance THRESHOLD can never be excluded the way a money or commute one is. Grading
    this would punish exactly the honest behaviour the case wants.
  * **F9** — "Sainsbury's 180m" is the tool's OWN ``distance_display: "180m"`` (raw
    ``distance_m: 184``, so it misses the ±1.0 tolerance), and "within 500m" is the tool's
    own ``radius_m: 500``. Both are faithful restatements; the evidence pool simply does
    not parse the display string. Zero genuine distance fabrications.
  * **B9** — a no-tool arithmetic case, so its evidence pool is empty by construction. The
    fc "offender" £2,057 is the user's correctly recalled saved budget; the legacy one,
    £24,700, is the intermediate ``475 x 52``, i.e. showing the working. Both directions
    are false positives.
  * **C2** — "Monthly transport cost: £0 (walking distance, no fare needed)". £0 is the
    correct consequence of a grounded 12-minute walk, not an invented fare.

Those five are recorded here rather than silently dropped: four of them describe real
gaps in ``graders.py`` (no threshold filter for non-money/non-commute kinds; no parsing
of ``distance_display``; no distance-to-walk-time derivation; an empty pool on no-tool
cases), which is a checker question for the file's owner, not a corpus question.

THIRD PASS (2026-07-27): those four checker gaps were then FIXED on ``fix/grader-cleanup``
(e71c6ad, 81042ae, d75ea16, 059847e), which changes the answer for four of the five.
Re-measured against the corrected checker — not taken on trust — E3, E6 and F9 now come
out clean, so the constraints can be added as pure coverage:

  * **E3** — the three walk times are GROUNDED now that a time is derived from the
    distance in the same clause; fc 5/5 -> 6/6, legacy 4/5 -> 5/6, neither verdict moves.
  * **E6** — "None within 500m" is GROUNDED now that a threshold can be stated in any
    unit; fc 6/6 -> 7/7, legacy 4/6 -> 5/7.
  * **F9** — "180m" is GROUNDED now that the tool's own rendered string counts;
    fc 4/4 -> 5/5, legacy 3/4 -> 4/5.

  * **B9 — my decline was WRONG, on a premise I should have checked.** I wrote that
    £2,057 was "the user's correctly recalled saved budget". That reasoning was circular:
    the only evidence that such a budget existed was the answer asserting it. Checked
    against the case DEFINITION instead — ``conversation_history: []``, ``expected_tools:
    []``, no fixture, ``user_id: ab_user_b9`` appearing nowhere else in the repo, and no
    £2,057 anywhere in the corpus — it is an INVENTED recollection, £1.33 from the correct
    £2,058.33, which is more dangerous than a wild guess because it survives a casual
    reading. My second premise, "empty evidence pool by construction", was also false and
    was contradicted by output I had already printed: ``ctx.user_texts`` and
    ``reference_calculations`` seed the pool, and B9's fc money pool holds 475 and
    2058.33 as grounded. B9 gets the constraint and FLIPS fc 2/2 -> 2/3. Its legacy arm
    shows the working, "£475 x 52 = £24,700", which the annual-rent fix now grounds, so
    the honest answer is untouched (2/2 -> 3/3).

Only **C2** remains declined: "£0 (walking distance, no fare needed)" is the correct
consequence of a grounded 12-minute walk, not an invented fare.

MERGE ORDER IS LOAD-BEARING — measured, not assumed. The E3/E6/F9 constraints are safe
only on a tree that also carries ``fix/grader-cleanup``. Re-scoring all 98 cases against
the retained evidence, with this branch's contract versus mainline's:

    checker tree                          fc BEFORE -> AFTER      flips
    mainline + fix/grader-cleanup         78/98 -> 74/98          4  (B9 C8 D11 E10)
    mainline 4f410ab13a26 alone           79/98 -> 72/98          7  (+ E3 E6 F9)

Landing this branch WITHOUT the checker corrections would fail E3, E6 and F9 — the three
honest answers those corrections exist to stop mis-reading, and the exact harm the second
pass declined to cause. Both branches are already merged in ``integration/wave4``, so the
ordering holds there; cherry-picking this branch alone onto mainline would not be safe.
(The one-case difference in the BEFORE column, 79 vs 78, is the checker corrections' own
effect on a case this branch never touches.)

This module's own tests pass under BOTH checkers, so the suite cannot detect that
ordering problem for you — which is why it is written down here.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from evaluation.metrics import graders
from evaluation.run_benchmark import load_fixture_queue

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"

# Constraint types whose checker can actually FAIL because of an ungrounded number of a
# given kind. Derived by reading graders.py rather than asserted: the first three route
# their `field` through `_field_to_kind` into `_field_number_offenders`, and
# `_c_commute_leq_minutes` fails on `ungrounded` minute claims directly.
FIELD_KINDED_TYPES = ("no_fabricated_number", "must_refuse_fabrication",
                      "must_note_missing_data")

# The dimension this module is about.
COMMUTE_KIND = "commute_minutes"

# Every case whose definition this branch amends, and the numeric kind it must now be
# able to fail on. A POSITIVE table, not an exemption list: adding a row obliges a case,
# it never excuses one. Same vehicle as `test_amended_cases_are_in_sync_across_every_shard`
# in test_case_contract_consistency.py, which pins G2/G3/E11 the same way.
#
# All four were found by replaying the retained round-8793c0b evidence and asking, per
# case, whether ANY declared constraint could fail on the ungrounded numbers the answer
# actually stated. All four were verified the same way E10 was: the claimed minutes appear
# ZERO times, with word boundaries, in that turn's whole evidence blob.
#
#   E10  fc invented 15/20/10 min to Imperial       (4,346-char blob, 0 hits)   FLIPS
#   C8   fc invented "about a 15-20 minute walk"    (545-char blob,   0 hits)   FLIPS
#   D11  fc invented "Richmond ~15-20 minutes drive" (603-char blob,  0 hits)   FLIPS
#   E4   no fabrication in either arm; coverage only              (verified no-op)
#   B9   fc invented "your saved budget of £2,057"  (no history/memory/tool)   FLIPS
#
# THIRD PASS adds four more, once the checker corrections on fix/grader-cleanup made the
# measurement possible (see the THIRD PASS note in the module docstring). E3/E6/F9 are
# pure coverage — verified no-ops on both arms — while B9 flips.
AMENDED_DIMENSION_COVERAGE = {
    "E10": COMMUTE_KIND,
    "E4": COMMUTE_KIND,
    "C8": COMMUTE_KIND,
    "D11": COMMUTE_KIND,
    "E3": COMMUTE_KIND,
    "E6": "distance_m",
    "F9": "distance_m",
    "B9": "money",
}


def _cases_by_id() -> dict:
    by_case = defaultdict(dict)
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                by_case[case["case_id"]][path.name] = case
    return by_case


def _covers_kind(case: dict, kind: str):
    """The constraint (if any) that can fail this case on an ungrounded ``kind`` number."""
    for con in case.get("expected_constraints") or []:
        ctype = con.get("type")
        if ctype in FIELD_KINDED_TYPES:
            if graders._field_to_kind(con.get("field") or "") == kind:
                return f"{ctype}[{con.get('field')}]"
        elif ctype == "commute_leq_minutes" and kind == COMMUTE_KIND:
            return ctype
    return None


def _fixture_evidence(case: dict) -> list:
    """The case's OWN declared fixture, flattened to the [{tool, data}] shape the graders
    consume. Derived from the file the runner replays, so the test cannot drift from the
    evidence the case is actually graded against."""
    evidence = []
    for tool, records in load_fixture_queue(case).items():
        for rec in records:
            evidence.append({"tool": tool, "success": rec.get("success", True),
                             "error": rec.get("error"), "data": rec.get("data")})
    return evidence


def _ctx(case: dict, answer: str, tools) -> graders.GradeContext:
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=_fixture_evidence(case),
        route=None,
        user_texts=[case["user_query"]],
        reference_calculations=case.get("reference_calculations"),
        error=None,
        reconstructed_context=None,
        history_texts=[],
    )


# --------------------------------------------------------------------------- #
# 1. The regression. The literal fabricating answer, pinned.
# --------------------------------------------------------------------------- #
# Verbatim from .runtime/round-8793c0b-internal-2026-07-25/eval/sweep/grader_input.jsonl,
# case E10, fc arm. Every minute figure in it is invented; the rent is not.
E10_FABRICATING_ANSWER = (
    "好的，我来帮你分析一下目前的情况。\n\n---\n\n"
    "**搜索结果分析**\n\n"
    "南肯辛顿（South Kensington）是伦敦房价最高的区域之一，月租1300镑以内找单间确实非常困难。"
    "系统目前没有找到完全符合所有条件的房源。\n\n"
    "**最接近的选项：**\n"
    "- **Earls Court House**（Earl's Court 区域）\n"
    " - 类型：Studio（单间公寓）\n"
    " - 价格：£1,450/月（超出预算150镑）\n"
    " - 位置在Earl's Court，步行到帝国理工约15-20分钟\n\n"
    "**建议考虑的方向：**\n\n"
    "1. **Earl's Court / West Kensington** — 步行或骑车到帝国理工约15-25分钟\n"
    "2. **Barons Court** — 地铁到帝国理工约10分钟（District/Piccadilly线）\n"
)

# The SAME case, legacy arm, verbatim: it reports the £1,450 tradeoff and states no
# journey time of its own. Nothing here may start failing.
E10_HONEST_ANSWER = (
    "根据您的要求，我直接搜索了南肯辛顿地区月租1300英镑以内、通勤到帝国理工学院30分钟内且"
    "附近有超市的单间。搜索结果如下：\n\n"
    "目前没有找到完全符合所有条件的房源。最接近的选项是位于Earls Court House, 18 Penywern "
    "Road, London SW5 9SU的一套Studio，月租为1450英镑，超出预算150英镑（约12%）。该房源"
    "通勤到帝国理工学院预计在30分钟内，且Earls Court区域附近有超市，但价格未在您设定的预算"
    "范围内。"
)

FABRICATED_MINUTES = (15.0, 20.0, 10.0)


def test_the_fabricated_commute_minutes_are_absent_from_the_evidence():
    """Guards the premise of everything below. If the fixture ever grew a 15/20/10-minute
    figure, the answer would stop being a fabrication and the regression would be pinning
    the wrong thing."""
    case = _cases_by_id()["E10"]["cases.jsonl"]
    blob = json.dumps(_fixture_evidence(case), ensure_ascii=False)
    for minutes in FABRICATED_MINUTES:
        n = str(int(minutes))
        assert not re.search(rf"(?<![0-9]){n}(?![0-9])", blob), (
            f"{n} now appears in E10's fixture evidence; this answer is no longer "
            f"fabricating it: {blob}")


def test_e10_fails_on_the_fabricated_commute_minutes():
    """THE REGRESSION. The real fc answer states 「步行到帝国理工约15-20分钟」 and
    「地铁到帝国理工约10分钟」 over evidence that contains no journey time at all.

    Before this branch the assertion below could not hold for any contract E10 declared:
    the case scored 3/3 and passed. It fails only because the case now declares
    ``no_fabricated_number[duration_minutes]``."""
    case = _cases_by_id()["E10"]["cases.jsonl"]
    verdict = graders.grade_case(
        case, _ctx(case, E10_FABRICATING_ANSWER, ["search_properties"]))

    assert not verdict.passed, (
        "a fabricated commute time is the one number a user acts on; E10 must not pass. "
        f"{[(c.type, c.passed, c.detail) for c in verdict.constraints]}")

    failed = [c for c in verdict.constraints if not c.passed]
    assert [c.type for c in failed] == ["no_fabricated_number"], (
        "E10 must fail on the commute dimension specifically, not incidentally on some "
        f"other constraint: {[(c.type, c.detail) for c in failed]}")
    for minutes in FABRICATED_MINUTES:
        assert str(minutes) in failed[0].detail, (
            f"{minutes} should be named as an offender: {failed[0].detail}")


def test_the_grounded_rent_is_still_not_treated_as_a_fabrication():
    """The other direction, so the amendment cannot be read as "E10 fails now". £1,450 IS
    in the evidence and the answer labels it over budget; the rent constraint keeps
    passing on the very turn the commute constraint fails."""
    case = _cases_by_id()["E10"]["cases.jsonl"]
    verdict = graders.grade_case(
        case, _ctx(case, E10_FABRICATING_ANSWER, ["search_properties"]))
    rent = [c for c in verdict.constraints if c.type == "must_refuse_fabrication"]
    assert rent and rent[0].passed, [c.detail for c in rent]


def test_the_honest_answer_still_passes_every_constraint():
    """The legacy arm's E10: same tradeoff, no invented journey time. The new constraint
    must be a fabrication test, not a ban on discussing the commute."""
    case = _cases_by_id()["E10"]["cases.jsonl"]
    verdict = graders.grade_case(
        case, _ctx(case, E10_HONEST_ANSWER, ["search_properties"]))
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]


# --------------------------------------------------------------------------- #
# 1b. The same hole in C8 and D11, pinned the same way.
# --------------------------------------------------------------------------- #
# C8, fc arm, verbatim. get_transport_info returned `outside_london` with NO data, which
# is precisely the premise on which the case already refuses to quote a fare — and then
# the answer quotes a journey time for the same unquotable leg.
C8_FABRICATING_ANSWER = (
    "Manchester is not in London, so the TfL API (which covers London only) cannot "
    "provide fare information for this journey.\n\n"
    "Here is what I can tell you:\n\n"
    "- **Manchester does not have a \"Tube\"** — it has the **Metrolink tram system**, "
    "buses, and trains.\n"
    "- **Manchester Piccadilly** (the main train station) to the **University of "
    "Manchester** is about a 15-20 minute walk, or a short tram/bus ride.\n"
    "- For accurate fares and journey planning, please check **Transport for Greater "
    "Manchester** at **tfgm.com** or use **nationalrail.co.uk** for any train services."
)

# D11, fc arm, verbatim. The POI search came back EMPTY; the drive times are invented,
# and materially wrong — Muker to Richmond is a good deal more than 15-20 minutes.
D11_FABRICATING_ANSWER = (
    "There are **no pharmacies within a short walking distance** of Muker, North "
    "Yorkshire. Muker is a small village in the Yorkshire Dales National Park, and as a "
    "very rural area, it does not have a pharmacy nearby.\n\n"
    "The nearest pharmacies would likely be in larger towns such as **Richmond** (about "
    "15-20 minutes drive) or **Hawes** (about 10-15 minutes drive). If you need a "
    "pharmacy, you would need to travel by car or bus to one of those towns."
)

# The honest counterparts, both verbatim from the legacy arm: same refusal, no invented
# journey time. Neither may start failing.
C8_HONEST_ANSWER = (
    "The search results do not cover tube fares for Manchester, as the Transport for "
    "London (TfL) API only provides data for London. Manchester is outside London, so I "
    "cannot give you a specific fare for the journey from Manchester Piccadilly to the "
    "University of Manchester.\n\nFor accurate and up-to-date information on public "
    "transport fares in Manchester, I recommend checking the local operator, Transport "
    "for Greater Manchester, at tfgm.com, or using National Rail at nationalrail.co.uk "
    "for train services."
)
D11_HONEST_ANSWER = (
    "Based on the search results, there are no pharmacies within a short walk (400 "
    "metres) of Muker, North Yorkshire. The data indicates that no pharmacy was found "
    "within this distance.\n\nSince the search results do not cover pharmacies beyond "
    "this range, I suggest checking the NHS website (nhs.uk) or using a service like "
    "Google Maps to find the nearest pharmacy, which may be in a nearby village or town "
    "such as Reeth or Hawes."
)

_EXTRA_REGRESSIONS = {
    "C8": (C8_FABRICATING_ANSWER, C8_HONEST_ANSWER, ["get_transport_info"], (15.0,)),
    "D11": (D11_FABRICATING_ANSWER, D11_HONEST_ANSWER, ["search_nearby_pois"], (10.0,)),
}


@pytest.mark.parametrize("case_id", sorted(_EXTRA_REGRESSIONS))
def test_the_invented_journey_time_is_absent_from_the_evidence(case_id):
    """Same premise guard as E10's: if the fixture ever grows one of these figures the
    answer stops being a fabrication and the regression below is pinning the wrong thing."""
    case = _cases_by_id()[case_id]["cases.jsonl"]
    blob = json.dumps(_fixture_evidence(case), ensure_ascii=False)
    for n in ("10", "15", "20"):
        assert not re.search(rf"(?<![0-9]){n}(?![0-9])", blob), (
            f"{n} now appears in {case_id}'s fixture evidence: {blob}")


@pytest.mark.parametrize("case_id", sorted(_EXTRA_REGRESSIONS))
def test_the_case_fails_on_its_invented_journey_time(case_id):
    """C8 and D11 both scored a full pass on the fc arm while stating a journey time their
    tool never produced — C8 off an `outside_london` no-data result, D11 off an EMPTY POI
    result. Both refuse the dimension the case DID grade (fare, distance) and then invent
    an adjacent one it did not."""
    answer, _, tools, expected_offenders = _EXTRA_REGRESSIONS[case_id]
    case = _cases_by_id()[case_id]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, answer, tools))

    assert not verdict.passed, (
        f"{case_id} states an unsourced journey time and must not pass: "
        f"{[(c.type, c.passed, c.detail) for c in verdict.constraints]}")
    failed = [c for c in verdict.constraints if not c.passed]
    assert [c.type for c in failed] == ["no_fabricated_number"], (
        f"{case_id} must fail on the commute dimension, not incidentally: "
        f"{[(c.type, c.detail) for c in failed]}")
    for minutes in expected_offenders:
        assert str(minutes) in failed[0].detail, (
            f"{minutes} should be named as an offender: {failed[0].detail}")


@pytest.mark.parametrize("case_id", sorted(_EXTRA_REGRESSIONS))
def test_the_honest_refusal_still_passes(case_id):
    """The legacy arm of the same case: it refuses the fare / reports the empty POI result
    and volunteers no journey time. The new constraint must not touch it."""
    _, answer, tools, _ = _EXTRA_REGRESSIONS[case_id]
    case = _cases_by_id()[case_id]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, answer, tools))
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]


# --------------------------------------------------------------------------- #
# 1c. B9 — an invented recollection, and the decline that missed it.
# --------------------------------------------------------------------------- #
# B9, fc arm, verbatim. The conversion is right and the recollection is invented.
B9_FABRICATING_ANSWER = (
    "To convert a weekly rent to a calendar month, the standard formula is:\n\n"
    "**Weekly rent x 52 weeks / 12 months**\n\n"
    "So: £475 x 52 / 12 = **£2,058.33 per calendar month** (approximately).\n\n"
    "That is just above your saved budget of £2,057 per month, so it would be slightly "
    "over budget. Would you like me to search for studios within your £2,057/month "
    "budget?"
)
# B9, legacy arm, verbatim: the same conversion with the intermediate shown.
B9_HONEST_ANSWER = (
    "To convert a weekly rent to a per calendar month (PCM) figure, the standard method "
    "in the UK is to multiply the weekly amount by 52 (the number of weeks in a year) "
    "and then divide by 12 (the number of months in a year).\n\n"
    "For a studio advertised at £475 per week:\n\n"
    "£475 x 52 = £24,700 per year\n"
    "£24,700 / 12 = £2,058.33 per calendar month\n\n"
    "Therefore, the monthly rent is approximately **£2,058 per calendar month**."
)


def test_b9_has_nothing_the_recollection_could_come_from():
    """The premise my earlier decline got wrong, pinned so nobody repeats it. B9 is a
    pure-arithmetic turn: no prior turns, no tools, no fixture. There is no channel
    through which a "saved budget" could reach the answer, so £2,057 cannot be a
    recollection — it can only be an invention."""
    for name, case in _cases_by_id()["B9"].items():
        assert case["conversation_history"] == [], name
        assert case["expected_tools"] == [], name
        assert "fixture" not in case, name
        assert not any(c["type"] in ("must_recall_value", "memory_isolation")
                       for c in case["expected_constraints"]), name


def test_b9_fails_on_the_invented_saved_budget():
    """THE REGRESSION for the case I wrongly excused. The answer states the correct
    £2,058.33 and then asserts a £2,057 "saved budget" that exists nowhere. Grading only
    the arithmetic let both through; the fabrication constraint catches the second."""
    case = _cases_by_id()["B9"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B9_FABRICATING_ANSWER, []))
    assert not verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]
    failed = [c for c in verdict.constraints if not c.passed]
    assert [c.type for c in failed] == ["no_fabricated_number"], (
        f"B9 must fail on the invented figure, not on the arithmetic: "
        f"{[(c.type, c.detail) for c in failed]}")
    assert "2057" in failed[0].detail.replace(".0", ""), failed[0].detail


def test_b9_still_passes_when_the_working_is_shown():
    """The other direction, and the reason this constraint is safe on an arithmetic case:
    the legacy arm prints the intermediate £475 x 52 = £24,700. An annual rent derived
    from the user's own weekly figure is a sanctioned intermediate — showing the working
    is not fabricating."""
    case = _cases_by_id()["B9"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B9_HONEST_ANSWER, []))
    fabrication = [c for c in verdict.constraints if c.type == "no_fabricated_number"]
    assert fabrication and fabrication[0].passed, (
        f"showing 475 x 52 = 24,700 must not read as a fabrication: "
        f"{[c.detail for c in fabrication]}")


# --------------------------------------------------------------------------- #
# 2. The amendment reached every shard.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case_id,kind", sorted(AMENDED_DIMENSION_COVERAGE.items()))
def test_the_amended_dimension_is_covered_in_every_shard_defining_the_case(case_id, kind):
    """Same failure mode as G2/G3/E11: amending cases.jsonl alone leaves the sibling shard
    grading a different contract, and a green run on one shard proves nothing about the
    other. E10/C8/D11 also live in cases_ext_CDE; E4 also lives in cases_base45."""
    shards = _cases_by_id()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    for name, case in shards.items():
        assert _covers_kind(case, kind), (
            f"{case_id} in {name} declares no constraint over a {kind} figure: "
            f"{[c['type'] for c in case['expected_constraints']]}")
        assert any("no tool returned" in fc for fc in case["failure_conditions"]), (
            f"{case_id} in {name} has the constraint but no failure_condition saying "
            "in plain language what it forbids")


# --------------------------------------------------------------------------- #
# 3. The source guard: no case may solicit a minute figure it cannot grade.
# --------------------------------------------------------------------------- #
# "under 30 minutes", "15-min walk", 「30分钟内」 — a duration in the USER's own words.
_MINUTE_CRITERION = re.compile(
    r"[0-9]{1,3}\s*-?\s*(?:分钟|minute|minutes|min|mins)\b|[0-9]{1,3}\s*分钟",
    re.IGNORECASE)
# ...that is about getting somewhere, rather than an unrelated duration.
_JOURNEY_WORDS = ("commute", "walk", "cycle", "drive", "travel", "journey", "get to",
                  "通勤", "步行", "骑车", "车程", "到达")


def _user_text(case: dict) -> str:
    parts = [case.get("user_query", "")]
    parts += [t.get("content", "") for t in case.get("conversation_history") or []
              if t.get("role") == "user"]
    return "\n".join(parts)


def _solicits_a_journey_time(case: dict) -> bool:
    text = _user_text(case)
    return bool(_MINUTE_CRITERION.search(text)) and any(w in text for w in _JOURNEY_WORDS)


def test_every_case_that_asks_for_a_journey_time_can_grade_one():
    """THE SOURCE GUARD, and the reason this branch is not just two hand-patched cases.

    If a case's own query puts a journey time in minutes on the table, the agent will
    answer with one — and an ungraded dimension is an invitation to invent it. Any such
    case must declare a constraint that can actually FAIL on an ungrounded minute figure.

    Deliberately NOT asserted here: that every case merely *capable* of stating minutes
    covers the dimension. That would condemn cases whose query never raises the subject
    (a crime-comparison case whose answer volunteers "a 10-minute walk" is a real hole,
    but a different owner decision — see the sweep recorded in this branch's report).
    This guard is exactly as wide as the evidence supports, and carries no exemptions."""
    offenders = {}
    for case_id, shards in sorted(_cases_by_id().items()):
        for name, case in sorted(shards.items()):
            if _solicits_a_journey_time(case) and not _covers_kind(case, COMMUTE_KIND):
                offenders[f"{case_id} ({name})"] = [
                    c["type"] for c in case.get("expected_constraints") or []]
    assert not offenders, (
        "these cases ask the user for a journey time but declare no constraint that can "
        f"fail on a fabricated one — the E10 hole: {offenders}. Add "
        "no_fabricated_number[duration_minutes] (or commute_leq_minutes where a bound is "
        "meant) to EVERY shard defining the case.")


def test_the_source_guard_can_actually_bite():
    """Guards the guard, three ways: the predicate must match a real, non-trivial slice of
    the corpus; ``_covers_kind`` must reject a contract that lacks the constraint; and it
    must accept the one that has it. Without this, a typo in either helper would leave the
    guard above passing vacuously forever."""
    by_case = _cases_by_id()
    matched = [cid for cid, shards in by_case.items()
               if _solicits_a_journey_time(next(iter(shards.values())))]
    assert len(matched) >= 8, f"the journey-time predicate looks broken: {matched}"
    assert "E10" in matched, "E10 is the case this module exists for"

    e10 = by_case["E10"]["cases.jsonl"]
    assert _covers_kind(e10, COMMUTE_KIND) == "no_fabricated_number[duration_minutes]"

    stripped = dict(e10, expected_constraints=[
        c for c in e10["expected_constraints"]
        if graders._field_to_kind(c.get("field") or "") != COMMUTE_KIND])
    assert _covers_kind(stripped, COMMUTE_KIND) is None, (
        "_covers_kind accepts a contract with no commute constraint — the guard would "
        "never fire")

    assert graders._field_to_kind("duration_minutes") == COMMUTE_KIND, (
        "the field name E10 declares no longer maps to the commute kind; the constraint "
        "would silently grade nothing")


def test_the_constraint_type_needs_no_schema_change():
    """The amendment reuses an existing type on purpose. If ``no_fabricated_number`` ever
    left the vocabulary or the checker registry, E10 would score "no checker" — a case
    failure blamed on the agent."""
    schema = json.loads((BENCH / "schema.json").read_text(encoding="utf-8"))
    enum = schema["properties"]["expected_constraints"]["items"]["properties"]["type"]["enum"]
    assert "no_fabricated_number" in enum
    assert "no_fabricated_number" in graders.CONSTRAINT_CHECKERS
