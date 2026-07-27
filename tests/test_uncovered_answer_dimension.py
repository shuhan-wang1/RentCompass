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

FOURTH PASS (2026-07-27): the five cases the third pass handed to the owner — C2, C4, C5,
C10, H9 — read one by one against the same retained round. Every number each answer
asserted was listed per arm and checked with explicit digit lookarounds (``(?<![0-9])n
(?![0-9])``, never ``\\b``: ``\\b`` does not match between a CJK character and a digit, so
the naive boundary check is blind to 「12分钟」).

The measured outcome is **zero flips on either arm**: fc 74/98 -> 74/98, legacy
46/98 -> 46/98, scored with ONE evaluator over both retained arms. Every one of the four
amended cases is therefore **pure coverage**, labelled as such in the same terms E4 was.
Per case:

  * **C10 — the real find, and NOT a no-op in the structural sense.** Its declared
    ``no_fabricated_number[fare_gbp]`` graded *nothing at all*: ``_field_to_kind`` has no
    row for ``fare_gbp``, and ``_field_number_offenders`` returns ``[]`` for a field with
    no kind, so the constraint passed unconditionally whatever fare the answer stated.
    This is the identical defect the file itself records for ``safety_score``
    (see ``_field_to_kind``'s own docstring and
    tests/test_grader_review_corrections.py::test_safety_score_resolves_to_a_kind_at_all).
    Verified, not assumed: the retained legacy answer with £2.80 changed to £4.90 PASSES
    ``[fare_gbp]`` and FAILS ``[fare]``. Fixed by naming the field the rest of the corpus
    already uses (``fare`` -> ``money``), which needs no checker change, and generalised
    into a corpus-wide source guard below. ``no_fabricated_number[duration_minutes]`` is
    added too: 43 and 24 are both grounded, so it is a no-op on this evidence.
  * **C4 — pure coverage.** fc quotes "About 36 minutes" (grounded in
    ``commute.duration_minutes``) and "roughly 26.4 hours per month (72 min/day)". 72 is
    classified ``unsupported``, because the commute key filter in ``_build_evidence_pool``
    reads ``travel``/``commute``/``duration``/``route``/``time`` string keys and the figure
    lives under ``summary.total_commuting_cost_per_month`` — "commuting" does not contain
    "commute". It is spared only because "roughly" is a ``_COMMUTE_THRESHOLD_MARKERS``
    hedge. So C4 does not fail, and MUST not: "72 min/day" is printed verbatim inside the
    tool's own summary string, making it the F9 class exactly. Recorded as a KNOWN
    ADJACENT CHECKER GAP (the distance path already reads ``summary``/``display`` keys;
    the commute path does not) and pinned by a premise test, so nobody later "fixes" C4 by
    failing an honest answer.
  * **C5 — pure coverage.** 21 min and the 2-min Northern-line hop are both grounded
    (``duration_minutes`` and ``route_legs``). fc fails on ``must_mention_source[TfL]``
    before and after; legacy states no number at all and keeps its 3/3 -> 4/4.
    UNCOVERABLE RESIDUE, reported rather than papered over: fc also claims "a **30%**
    discount on pay-as-you-go fares", which is both absent from the evidence and
    contradicted by the sibling C4 tool note ("Student Oyster cards do NOT get discounts
    on Pay As You Go"). No claim kind exists for a percentage, so no existing checker can
    reach it; inventing one is a checker change, not a corpus one.
  * **H9 — coverage only, and its flip status is NOT MEASURABLE.** H9 declared
    ``must_call_tool`` + ``must_not_call_tool`` and nothing else, while its own
    ``failure_conditions`` already said "Fabricates a fare or route not grounded in the
    TfL tool" — a named failure with no checker behind it, the purest form of the E10 hole.
    It is also the only one of the five that is NOT in Base98: it lives solely in
    ``cases_guard_regression.jsonl``, and a grep of the whole ``.runtime`` tree plus the
    archived 2026-07-19 guard rounds finds no answer text for it anywhere (those rounds
    predate ``grader_input.jsonl`` and retained telemetry events only). There is therefore
    no arm to flip and none is claimed; the constraints are pinned by a CONSTRUCTED
    regression instead, and it is labelled as constructed.
  * **C2 — the decline is RE-AFFIRMED, and no constraint is added.** Read again from
    primary sources, including one fact the third pass did not state: ``calculate_commute_
    cost`` was CALLED and returned ``success: false``. Even so, ``calculate_commute``
    returned ``route_summary: "Walk 12 min via Russell Square"`` from TfL, and a walk costs
    nothing, so "£0 (walking distance, no fare needed)" remains a correct consequence of
    grounded evidence rather than an invented fare. The dimension that matters IS covered
    and DOES bite: the legacy arm invents "typically a 10-15 minute walk" over an EMPTY
    evidence blob and fails ``no_fabricated_number[duration_minutes]`` on 10.0 and 15.0.
    What stays open is a structural residue — a fabricated PRICE on C2 would still be
    ungraded — and it cannot be closed here, because every money-kinded field routes
    through the same offender set and would fire on that £0. Measured, so the size of the
    obstacle is known rather than guessed: across all 98 cases x both retained arms there
    is exactly ONE zero-valued money claim in the entire round, C2/fc's. Closing C2 needs
    ``_field_number_offenders`` to stop reading a stated ZERO as an asserted amount (the
    money twin of E6's "None within 500m"), and a zero can never be classified
    ``contradicted`` — ``classify_number``'s neighbourhood guard requires
    ``0.5*|r| <= |0| <= 2*|r|`` — so that correction would also stop catching "no deposit
    is required" against a tool-pinned deposit. That trade is a checker decision with no
    instance in the retained round to measure it on, so it is reported here for the
    checker's owner instead of being made blind.

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
#
# FOURTH PASS adds the four closable cases of the five the third pass handed on. All four
# are pure coverage on this evidence (fc 74/98 -> 74/98, legacy 46/98 -> 46/98, zero flips),
# which is exactly why the rows are needed: coverage is the claim, and this table is what
# makes the claim testable in every shard. C2 is deliberately NOT here — the module
# docstring records why, and adding a row for it would oblige a constraint that fires on a
# £0 walking fare.
#
#   C4   36 min grounded; "72 min/day" is the tool's own summary   (verified no-op)
#   C5   21 min + the 2-min hop both grounded                      (verified no-op)
#   C10  fare_gbp graded NOTHING -> fare; 43/24 min grounded       (structural fix, no-op)
#   H9   no numeric constraint at all; NO retained evidence        (coverage, unmeasurable)
#
# The value is a TUPLE: C10 and H9 must be able to fail on a fare AND on a journey time,
# and a single-kind row would let half of each pair go missing.
AMENDED_DIMENSION_COVERAGE = {
    "E10": (COMMUTE_KIND,),
    "E4": (COMMUTE_KIND,),
    "C8": (COMMUTE_KIND,),
    "D11": (COMMUTE_KIND,),
    "E3": (COMMUTE_KIND,),
    "E6": ("distance_m",),
    "F9": ("distance_m",),
    "B9": ("money",),
    "C4": (COMMUTE_KIND,),
    "C5": (COMMUTE_KIND,),
    "C10": (COMMUTE_KIND, "money"),
    "H9": (COMMUTE_KIND, "money"),
}

# H9 is the one amended case that is NOT in Base98: it is a hard-gate guard-regression
# case and lives in exactly one shard. Recorded as a POSITIVE fact — the shard it must be
# in — rather than as an exemption, so that adding H9 to Base98 makes the test say so.
SINGLE_SHARD_CASES = {"H9": "cases_guard_regression.jsonl"}


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
    return _ctx_with(case, answer, tools, _fixture_evidence(case))


def _ctx_with(case: dict, answer: str, tools, evidence: list,
              user_texts=None) -> graders.GradeContext:
    """Same context, with the evidence supplied explicitly. C4, C5 and H9 declare NO
    fixture — their retained evidence came from live tool calls — so their turns cannot be
    reconstructed from the case file the way E10's and C10's can. Their evidence is
    transcribed verbatim from the retained round instead, and the transcription is itself
    checked by ``test_the_transcribed_c4_and_c5_evidence_is_the_tool_output_it_claims``."""
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=evidence,
        route=None,
        user_texts=list(user_texts) if user_texts is not None else [case["user_query"]],
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
@pytest.mark.parametrize("case_id", sorted(AMENDED_DIMENSION_COVERAGE))
def test_the_amended_dimension_is_covered_in_every_shard_defining_the_case(case_id):
    """Same failure mode as G2/G3/E11: amending cases.jsonl alone leaves the sibling shard
    grading a different contract, and a green run on one shard proves nothing about the
    other. E10/C8/D11/C10 also live in cases_ext_CDE; E4/C4/C5 also live in cases_base45;
    H9 lives only in cases_guard_regression, which is asserted rather than assumed."""
    shards = _cases_by_id()[case_id]
    only_shard = SINGLE_SHARD_CASES.get(case_id)
    if only_shard:
        assert sorted(shards) == [only_shard], (
            f"{case_id} is recorded as defined only in {only_shard} but is now in "
            f"{sorted(shards)} — amend every shard and drop the SINGLE_SHARD_CASES row")
    else:
        assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    for name, case in shards.items():
        for kind in AMENDED_DIMENSION_COVERAGE[case_id]:
            assert _covers_kind(case, kind), (
                f"{case_id} in {name} declares no constraint over a {kind} figure: "
                f"{[(c['type'], c.get('field')) for c in case['expected_constraints']]}")
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


# --------------------------------------------------------------------------- #
# 4. FOURTH PASS — C4, C5, C10 and H9 (2026-07-27).
#
# The five the third pass handed on, read one at a time. Four are amended and ALL FOUR are
# pure coverage: scoring both retained arms with one evaluator gives fc 74/98 -> 74/98 and
# legacy 46/98 -> 46/98, zero flips. That is the honest label, and it is the E4 precedent.
# C10 is the structural find — its constraint graded NOTHING — and H9's flip status is not
# measurable at all, because H9 has no retained evidence in any round.
#
# C4 and C5 declare no fixture (their round evidence came from live calls), so their tool
# output is transcribed verbatim from
# .runtime/round-8793c0b-internal-2026-07-25/eval/{sweep,sweep-legacy}/grader_input.jsonl.
# C10 needs no transcription: its own declared fixture carries the same 24 min / £2.80.
# --------------------------------------------------------------------------- #

C4_FC_ANSWER = "Here are the commute details from **Scape Bloomsbury** to **Canary Wharf**:\n\n**Time:** About **36 minutes** by Tube (medium-length, acceptable commute).\n\n**Monthly cost (student):**\n- Pay As You Go (Zone 1-5 daily cap): **£336.60/month** (based on £15.30 daily cap x 22 working days)\n- Note: The 18+ Student Oyster discount applies to Travelcards, not Pay As You Go. If you buy a **Zone 1-5 monthly Travelcard** with the student discount, it would be cheaper — I can check that exact price if you'd like.\n\n**Total time spent commuting:** roughly 26.4 hours per month (72 min/day)."
C4_FC_EVIDENCE = json.loads("""
[{"tool": "calculate_commute_cost", "success": true, "error": null, "data": {"success": true, "from_address": "Scape Bloomsbury, 19-29 Woburn Place, London WC1H 0AQ", "to_address": "Canary Wharf, London", "mode": "transit", "uses_public_transport": true, "commute": {"duration_minutes": 36, "duration_category": "Medium (20-45 min)", "is_acceptable": true}, "transport_cost": {"from_zone": 1, "to_zone": 5, "recommended_pass": "Zone 1-5", "user_type": "18+ Student Oyster (Pay As You Go)", "monthly_cost": 336.6, "daily_cap": 15.3, "daily_off_peak_cap": 10.15, "currency": "GBP", "note": "Cost calculated using Pay As You Go daily cap (£15.30) × 22 working days. Student Oyster cards do NOT get discounts on Pay As You Go - discount only applies to Travelcards."}, "summary": {"commute_time": "36 minutes", "monthly_transport_cost": "£336.60", "recommended_pass": "Zone 1-5", "uses_public_transport": "Yes", "total_commuting_cost_per_month": "£336.60 transport + 72 min/day × 22 workdays = ~26.4 hours/month"}}}]
""")
C4_FC_TOOLS = ['calculate_commute_cost']
C4_FC_USER_TEXTS = ['I like the Scape Bloomsbury studio.', 'Roughly how much would commuting to Canary Wharf cost me per month from there?']

C4_LEGACY_ANSWER = "The database entry for Scape Bloomsbury (WC1H 0AQ) does not include commuting cost estimates or travel details to Canary Wharf. This detail isn't in our database for this property. However, I can help you search for properties near Canary Wharf or with known transport links if you'd like to compare options."
C4_LEGACY_EVIDENCE = json.loads("""
[]
""")
C4_LEGACY_TOOLS = []
C4_LEGACY_USER_TEXTS = ['I like the Scape Bloomsbury studio.', 'Roughly how much would commuting to Canary Wharf cost me per month from there?']

C5_FC_ANSWER = "The pay-as-you-go Tube fare from Stratford to UCL (Goodge Street) right now is **£3.10** (adult contactless/Oyster single fare).\n\nThe journey takes about **21 minutes** via the Elizabeth line from Stratford to Tottenham Court Road, then a short 2-minute Northern line hop to Goodge Street (the nearest station to UCL's main campus).\n\nIf you're a student with an 18+ Student Oyster Card, you'd get a 30% discount on pay-as-you-go fares. Would you like me to check the student fare or the weekly Travelcard price for this zone combination?"
C5_FC_EVIDENCE = json.loads("""
[{"tool": "get_transport_info", "success": true, "error": null, "data": {"success": true, "query_type": "fare", "coverage": "london", "from": {"input": "Stratford, London", "resolved_station": "Stratford DLR Station", "naptan": "940GZZDLSTD"}, "to": {"input": "UCL, London", "resolved_station": "Goodge Street Underground Station", "naptan": "940GZZLUGDG"}, "duration_minutes": 21, "route_summary": "Elizabeth-line Elizabeth line to Tottenham Court Road (14 min) -> Tube Northern to Goodge Street Underground Station (2 min)", "route_legs": [{"mode": "elizabeth-line", "lines": ["Elizabeth line"], "duration_minutes": 14, "summary": "Elizabeth line to Tottenham Court Road Station", "from": "Stratford (London) Rail Station", "to": "Tottenham Court Road"}, {"mode": "tube", "lines": ["Northern"], "duration_minutes": 2, "summary": "Northern line to Goodge Street", "from": "Tottenham Court Road Underground Station", "to": "Goodge Street Underground Station"}], "source": "TfL Unified API (live Journey Planner)", "stations_used": "Stratford DLR Station -> Goodge Street Underground Station", "fare_available": true, "fare_pence": 310, "fare_gbp": 3.1, "fare_display": "£3.10", "fare_note": "Adult pay-as-you-go single (contactless / Oyster). For unlimited travel see a weekly/monthly Travelcard."}}]
""")
C5_FC_TOOLS = ['get_transport_info']
C5_FC_USER_TEXTS = ["What's the pay-as-you-go tube fare from Stratford to UCL right now?"]

C5_LEGACY_ANSWER = 'I cannot provide the pay-as-you-go tube fare from Stratford to UCL because the search results do not cover live TfL fares or journey data for London. The data I have indicates that TfL coverage is not available for this query.\n\nTo get the exact fare, I recommend checking the official TfL website or using the TfL Go app. Alternatively, you can ask me a more specific question about London transport, such as "how much is the tube from Stratford to UCL?" and I can attempt to fetch live fare data for you.'
C5_LEGACY_EVIDENCE = json.loads("""
[{"tool": "get_transport_info", "success": true, "error": null, "data": {"success": true, "coverage": "outside_london", "query": "Stratford", "message": "TfL (Transport for London) only covers London, so I can't pull live fares, journeys or line status for 'Stratford'. For public transport there, check the local operator (e.g. Transport for Greater Manchester at tfgm.com, or National Rail at nationalrail.co.uk).", "source": "TfL Unified API (coverage check)"}}]
""")
C5_LEGACY_TOOLS = ['get_transport_info']
C5_LEGACY_USER_TEXTS = ["What's the pay-as-you-go tube fare from Stratford to UCL right now?"]
def _ctx4(case_id: str, answer: str, tools, evidence, user_texts):
    case = _cases_by_id()[case_id]["cases.jsonl"]
    return _ctx_with(case, answer, tools, evidence, user_texts=user_texts)


# --- C4 ------------------------------------------------------------------------------ #
def test_the_transcribed_c4_and_c5_evidence_is_the_tool_output_it_claims():
    """Premise guard for every C4/C5 assertion below. The evidence is transcribed, not
    derived from a fixture the runner replays, so the figures the docstring reasons about
    are pinned here: if a transcription ever drifts, this fails before the verdicts do."""
    c4 = C4_FC_EVIDENCE[0]["data"]
    assert c4["commute"]["duration_minutes"] == 36
    assert c4["transport_cost"]["monthly_cost"] == 336.6
    # The F9 class, stated as a fact rather than an opinion: the "72 min/day" the answer
    # repeats is printed inside the TOOL'S OWN summary string. C4 must never fail on it.
    assert "72 min/day" in c4["summary"]["total_commuting_cost_per_month"]
    assert C4_LEGACY_EVIDENCE == []

    c5 = C5_FC_EVIDENCE[0]["data"]
    assert c5["duration_minutes"] == 21
    assert [leg["duration_minutes"] for leg in c5["route_legs"]] == [14, 2]
    assert c5["fare_gbp"] == 3.1
    assert C5_LEGACY_EVIDENCE[0]["data"]["coverage"] == "outside_london"


def test_c4_does_not_fail_on_a_figure_its_own_tool_printed():
    """C4's verified no-op, and the direction that matters. Every minute figure in the fc
    answer traces to the tool: "About 36 minutes" to `commute.duration_minutes`, and
    "(72 min/day)" to the tool's own summary string. The new constraint must register
    coverage without failing an honest answer — the harm the second pass declined to do to
    E3, E6 and F9."""
    case = _cases_by_id()["C4"]["cases.jsonl"]
    ctx = _ctx4("C4", C4_FC_ANSWER, C4_FC_TOOLS, C4_FC_EVIDENCE, C4_FC_USER_TEXTS)
    verdict = graders.grade_case(case, ctx)
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]
    dur = [c for c in verdict.constraints
           if c.type == "no_fabricated_number" and "duration_minutes" in (c.detail or "")]
    assert dur and dur[0].passed, [c.detail for c in dur]


def test_c4_would_still_fail_on_a_journey_time_the_tool_never_produced():
    """The other direction, so C4's row is coverage and not decoration. The SAME answer
    with one extra sentence — an interchange time no tool returned — now fails, and fails
    on the commute dimension specifically."""
    case = _cases_by_id()["C4"]["cases.jsonl"]
    invented = C4_FC_ANSWER + (
        "\n\nYou would also need an 8-minute walk to Russell Square and a 5-minute "
        "interchange at Bank.")
    verdict = graders.grade_case(
        case, _ctx4("C4", invented, C4_FC_TOOLS, C4_FC_EVIDENCE, C4_FC_USER_TEXTS))
    failed = [c for c in verdict.constraints if not c.passed]
    assert [c.type for c in failed] == ["no_fabricated_number"], (
        f"C4 must fail on the invented interchange time: "
        f"{[(c.type, c.detail) for c in failed]}")
    assert "8.0" in failed[0].detail, failed[0].detail


# --- C5 ------------------------------------------------------------------------------ #
@pytest.mark.parametrize("arm,answer,tools,evidence,user_texts", [
    ("fc", C5_FC_ANSWER, C5_FC_TOOLS, C5_FC_EVIDENCE, C5_FC_USER_TEXTS),
    ("legacy", C5_LEGACY_ANSWER, C5_LEGACY_TOOLS, C5_LEGACY_EVIDENCE, C5_LEGACY_USER_TEXTS),
])
def test_c5_gains_the_commute_constraint_without_failing_on_a_grounded_leg(
        arm, answer, tools, evidence, user_texts):
    """C5's verified no-op on BOTH arms. fc quotes 21 minutes (`duration_minutes`) and a
    2-minute Northern-line hop (`route_legs[1]`); legacy quotes no number at all. Neither
    may start failing the new constraint — fc's pre-existing `must_mention_source[TfL]`
    failure is untouched and is not this constraint's business."""
    case = _cases_by_id()["C5"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx4("C5", answer, tools, evidence, user_texts))
    dur = [c for c in verdict.constraints
           if c.type == "no_fabricated_number" and "duration_minutes" in (c.detail or "")]
    assert dur and dur[0].passed, (arm, [c.detail for c in dur])


# The fc answer with its two grounded figures replaced by minutes absent from the TfL
# payload, and with the hedges dropped. Dropping them is not cosmetic and is not a rule
# being bent to get a failure — see the test below.
C5_INVENTED_MINUTES_ANSWER = (
    "The pay-as-you-go Tube fare from Stratford to UCL (Goodge Street) right now is "
    "**£3.10** (adult contactless/Oyster single fare).\n\nThe journey takes **34 minutes** "
    "via the Elizabeth line from Stratford to Tottenham Court Road, then a 6-minute "
    "Northern line hop to Goodge Street (the nearest station to UCL's main campus)."
)


def test_c5_fails_on_a_journey_time_the_tfl_tool_never_returned():
    """The direction that gives C5's row teeth: 34 and 6 minutes against a payload that
    holds 21, 14 and 2."""
    case = _cases_by_id()["C5"]["cases.jsonl"]
    verdict = graders.grade_case(
        case, _ctx4("C5", C5_INVENTED_MINUTES_ANSWER, C5_FC_TOOLS, C5_FC_EVIDENCE,
                    C5_FC_USER_TEXTS))
    dur = [c for c in verdict.constraints
           if c.type == "no_fabricated_number" and "duration_minutes" in (c.detail or "")]
    assert dur and not dur[0].passed, [c.detail for c in dur]
    assert "34.0" in dur[0].detail and "6.0" in dur[0].detail, dur[0].detail


def test_a_hedged_minute_figure_is_out_of_this_constraints_reach_on_purpose():
    """HONEST LIMIT, pinned rather than left for someone to discover. The retained fc
    answer's own phrasing — "takes about **21 minutes** ... then a short 2-minute hop" —
    carries two `_COMMUTE_THRESHOLD_MARKERS` hedges, "about" and "short". A hedged figure
    is excluded from the offender set by `_number_asserts_field_value` BY DESIGN ("a hedged
    estimate / bucket threshold / unrelated quantity is not a fabricated field value"), so
    the SAME fabricated minutes wearing the same hedges do NOT fail.

    That is why the counterfactual above drops them, and why C5's coverage is real but
    partial: it catches a stated journey time, not a hedged one. Recorded so that the
    limitation is a known property of the checker rather than a surprise, and so that a
    future narrowing of the hedge list shows up here as a change in behaviour."""
    case = _cases_by_id()["C5"]["cases.jsonl"]
    hedged = C5_INVENTED_MINUTES_ANSWER.replace(
        "takes **34 minutes**", "takes about **34 minutes**").replace(
        "then a 6-minute", "then a short 6-minute")
    verdict = graders.grade_case(
        case, _ctx4("C5", hedged, C5_FC_TOOLS, C5_FC_EVIDENCE, C5_FC_USER_TEXTS))
    dur = [c for c in verdict.constraints
           if c.type == "no_fabricated_number" and "duration_minutes" in (c.detail or "")]
    assert dur and dur[0].passed, (
        "a hedged minute figure now fails. That may well be an improvement, but it changes "
        f"what C4/C5/C10/E10 grade — re-measure both arms before accepting it: "
        f"{[c.detail for c in dur]}")


# --- C10: the constraint that graded nothing ----------------------------------------- #
# The retained legacy answer with ONE figure changed, £2.80 -> £4.90. A counterfactual, and
# labelled as one: neither retained arm fabricates a fare, so a no-op can only be exposed by
# an answer that SHOULD fail. Everything else is verbatim.
C10_INVENTED_FARE_ANSWER = (
    "From Spring Mews, the nearest tube station is Vauxhall. The journey to Canary Wharf "
    "takes approximately 24 minutes, using the Victoria line to Green Park and then the "
    "Jubilee line to Canary Wharf. The pay-as-you-go fare for a single adult journey using "
    "contactless or Oyster is £4.90. For unlimited travel, you may want to consider a "
    "weekly or monthly Travelcard. Source: TfL."
)
# Both retained arms, verbatim. Every figure in both is tool-grounded; neither may move.
C10_FC_ANSWER = (
    "The total journey time from Spring Mews to Canary Wharf is 43 minutes, according to "
    "the TfL Journey Planner data. This includes walking, a bus, and the Jubilee line. The "
    "pay-as-you-go fare for a single adult journey using contactless or Oyster is £2.80, "
    "as shown in the fare data. Note that the fare data uses a different route (Victoria "
    "line to Green Park, then Jubilee line) with a shorter duration of 24 minutes, but the "
    "43-minute route is the one calculated from your specific address."
)
C10_LEGACY_ANSWER = (
    "From Spring Mews, the nearest tube station is Vauxhall. The journey to Canary Wharf "
    "takes approximately 24 minutes, using the Victoria line to Green Park and then the "
    "Jubilee line to Canary Wharf. The pay-as-you-go fare for a single adult journey using "
    "contactless or Oyster is £2.80. For unlimited travel, you may want to consider a "
    "weekly or monthly Travelcard."
)


def test_c10s_fixture_pins_the_two_figures_the_case_is_about():
    """Premise guard, from the file the runner replays rather than from a transcription."""
    data = _fixture_evidence(_cases_by_id()["C10"]["cases.jsonl"])[0]["data"]
    assert data["duration_minutes"] == 24 and data["fare_gbp"] == 2.8


def test_an_unmappable_field_makes_no_fabricated_number_a_silent_no_op():
    """THE DEFECT, isolated. `_field_number_offenders` filters claims by the KIND its field
    resolves to and returns [] when there is none, so `no_fabricated_number` with an
    unmapped field passes unconditionally — whatever the answer says. Demonstrated on a
    field name that is not, and should never be, in the vocabulary, so this test does not
    depend on `fare_gbp` staying unmapped."""
    case = _cases_by_id()["C10"]["cases.jsonl"]
    ctx = _ctx(case, C10_INVENTED_FARE_ANSWER, ["get_transport_info"])
    check = graders.CONSTRAINT_CHECKERS["no_fabricated_number"]
    assert check({"type": "no_fabricated_number", "field": "no_such_quantity"}, ctx).passed
    live = check({"type": "no_fabricated_number", "field": "fare"}, ctx)
    assert not live.passed and "4.9" in live.detail, live.detail


def test_c10_fails_on_an_invented_fare_now_that_its_field_names_a_kind():
    """THE REGRESSION for C10. It declared `no_fabricated_number[fare_gbp]`, which
    `_field_to_kind` maps to nothing, so a £4.90 invented against the tool's own £2.80
    passed the very constraint written to stop it. Naming the field `fare` — the name C5,
    C8 and F11 already use — makes the declared constraint do the job it names."""
    case = _cases_by_id()["C10"]["cases.jsonl"]
    verdict = graders.grade_case(
        case, _ctx(case, C10_INVENTED_FARE_ANSWER, ["get_transport_info"]))
    assert not verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]
    failed = [c for c in verdict.constraints if not c.passed]
    assert "no_fabricated_number" in [c.type for c in failed], (
        f"C10 must fail on the invented fare: {[(c.type, c.detail) for c in failed]}")


# Both arms' EVIDENCE, verbatim from the retained round. The fc arm called
# `calculate_commute` as well as `get_transport_info`, and the 43-minute figure comes from
# there — so, unlike E10 and unlike C10's own invented-fare regression above, the fc turn
# CANNOT be replayed from the case's declared fixture. See
# `test_c10s_fixture_does_not_carry_the_second_tool_the_fc_arm_used`.
C10_FC_EVIDENCE = json.loads("""
[{"tool": "get_transport_info", "success": true, "error": null, "data": {"success": true, "query_type": "fare", "coverage": "london", "from": {"input": "Spring Mews, 10 Tinworth Street, London SE11 5AL, UK", "resolved_station": "Vauxhall", "naptan": "940GZZLUVXL"}, "to": {"input": "Canary Wharf", "resolved_station": "Canary Wharf", "naptan": "940GZZLUCYF"}, "duration_minutes": 24, "route_summary": "Victoria line to Green Park, Jubilee line to Canary Wharf", "route_legs": [], "fare_available": true, "fare_pence": 280, "fare_gbp": 2.8, "fare_display": "£2.80", "fare_note": "Adult pay-as-you-go single (contactless / Oyster). For unlimited travel see a weekly/monthly Travelcard.", "stations_used": "Vauxhall -> Canary Wharf", "source": "TfL Unified API (live Journey Planner)"}}, {"tool": "calculate_commute", "success": true, "error": null, "data": {"from_address": "Spring Mews, 10 Tinworth Street, London SE11 5AL", "to_address": "Canary Wharf, London", "mode": "transit", "duration_minutes": 43, "is_acceptable": true, "duration_category": "Medium (20-45 min)", "route_summary": "Walk to Lambeth Bridge (10 min) -> Bus 77 to Waterloo Station   / Upper Taxi Road (8 min) -> Walk to Waterloo Underground Station (7 min) -> Tube Jubilee to Canary Wharf Underground Station (10 min) -> Walk to 40 CANADA SQUARE, LONDON (7 min)", "route_legs": [{"mode": "walking", "lines": [], "duration_minutes": 10, "summary": "Walk to Lambeth Bridge (SE1)", "from": "16 TINWORTH STREET, LONDON", "to": "Lambeth Bridge"}, {"mode": "bus", "lines": ["77"], "duration_minutes": 8, "summary": "77 bus to Waterloo Station/Upper Taxi Rd", "from": "Lambeth Bridge", "to": "Waterloo Station   / Upper Taxi Road"}, {"mode": "walking", "lines": [], "duration_minutes": 7, "summary": "Walk to Waterloo Station", "from": "Waterloo Station   / Upper Taxi Road", "to": "Waterloo Underground Station"}, {"mode": "tube", "lines": ["Jubilee"], "duration_minutes": 10, "summary": "Jubilee line to Canary Wharf", "from": "Waterloo Underground Station", "to": "Canary Wharf Underground Station"}, {"mode": "walking", "lines": [], "duration_minutes": 7, "summary": "Walk to 40 CANADA SQUARE, LONDON", "from": "Canary Wharf Underground Station", "to": "40 CANADA SQUARE, LONDON"}], "route_source": "TfL Journey Planner"}}]
""")
C10_FC_TOOLS = ['calculate_commute', 'get_transport_info']
C10_FC_USER_TEXTS = ['I like the Spring Mews studio.', "From there, how long is the tube to Canary Wharf and what's the pay-as-you-go fare?"]

C10_LEGACY_EVIDENCE = json.loads("""
[{"tool": "get_transport_info", "success": true, "error": null, "data": {"success": true, "query_type": "fare", "coverage": "london", "from": {"input": "Spring Mews, 10 Tinworth Street, London SE11 5AL, UK", "resolved_station": "Vauxhall", "naptan": "940GZZLUVXL"}, "to": {"input": "Canary Wharf", "resolved_station": "Canary Wharf", "naptan": "940GZZLUCYF"}, "duration_minutes": 24, "route_summary": "Victoria line to Green Park, Jubilee line to Canary Wharf", "route_legs": [], "fare_available": true, "fare_pence": 280, "fare_gbp": 2.8, "fare_display": "£2.80", "fare_note": "Adult pay-as-you-go single (contactless / Oyster). For unlimited travel see a weekly/monthly Travelcard.", "stations_used": "Vauxhall -> Canary Wharf", "source": "TfL Unified API (live Journey Planner)"}}]
""")
C10_LEGACY_TOOLS = ['get_transport_info']
C10_LEGACY_USER_TEXTS = ['I like the Spring Mews studio.', "From there, how long is the tube to Canary Wharf and what's the pay-as-you-go fare?"]


@pytest.mark.parametrize("arm,answer,tools,evidence,user_texts", [
    ("fc", C10_FC_ANSWER, C10_FC_TOOLS, C10_FC_EVIDENCE, C10_FC_USER_TEXTS),
    ("legacy", C10_LEGACY_ANSWER, C10_LEGACY_TOOLS, C10_LEGACY_EVIDENCE,
     C10_LEGACY_USER_TEXTS),
])
def test_both_retained_c10_arms_still_pass_the_fabrication_constraints(
        arm, answer, tools, evidence, user_texts):
    """C10's verified no-op, on each arm's OWN retained evidence. fc states 43 min
    (`calculate_commute`) and 24 min plus £2.80 (`get_transport_info`); legacy states 24 min
    and £2.80. All are tool figures, so neither arm may fail either fabrication constraint —
    the legacy arm's pre-existing `must_mention_source[TfL]` failure is unrelated and
    unchanged, which is why only the two fabrication results are inspected."""
    case = _cases_by_id()["C10"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx4("C10", answer, tools, evidence, user_texts))
    fab = [c for c in verdict.constraints if c.type == "no_fabricated_number"]
    assert len(fab) == 2, [c.detail for c in fab]
    assert all(c.passed for c in fab), (arm, [c.detail for c in fab])


def test_c10s_fixture_does_not_carry_the_second_tool_the_fc_arm_used():
    """A consequence of the amendment, measured and written down rather than left to be
    discovered in a fixture round. C10's fixture holds only `get_transport_info` (24 min),
    while the fc arm also ran `calculate_commute` (43 min). Replayed against the FIXTURE the
    fc answer's 43 is `contradicted` and the new duration constraint fails it.

    That is the correct reading, not a defect in the constraint: in a fixture round the
    agent sees only the fixture, so it has no source for 43 and stating it WOULD be an
    invention. The flip table for this branch is measured on the retained LIVE evidence,
    where 43 is grounded and nothing moves. Recorded so the two readings are never confused,
    and so that extending the fixture to both tools is a visible, deliberate choice."""
    case = _cases_by_id()["C10"]["cases.jsonl"]
    assert [e["tool"] for e in _fixture_evidence(case)] == ["get_transport_info"]
    verdict = graders.grade_case(case, _ctx(case, C10_FC_ANSWER, ["get_transport_info"]))
    dur = [c for c in verdict.constraints
           if c.type == "no_fabricated_number" and "duration_minutes" in (c.detail or "")]
    assert dur and not dur[0].passed and "43.0" in dur[0].detail, [c.detail for c in dur]


# --- H9: a named failure condition with no checker behind it -------------------------- #
# H9 is the only one of the five that is NOT in Base98 — it lives solely in
# cases_guard_regression.jsonl — and no round has ever retained an answer for it: the
# 2026-07-25 sweep does not contain it, and the archived 2026-07-19 guard rounds predate
# grader_input.jsonl and kept telemetry events only. So there is NO arm to flip and none is
# claimed. The two answers below are CONSTRUCTED, and are the only constructed answers in
# this module. H9 declares no fixture, so its evidence pool is empty exactly as the case
# defines it — which is the point: the case's own second failure_condition, "Fabricates a
# fare or route not grounded in the TfL tool", had no constraint that could ever detect it.
H9_FABRICATING_ANSWER = (
    "从 King's Cross 到 UCL 坐地铁很方便：乘 Northern 线一站到 Euston，再走几分钟就到了，"
    "全程大约 8 分钟。Oyster/contactless 单程票价是 £2.80。"
)
H9_HONEST_ANSWER = (
    "从 King's Cross 到 UCL，地铁可以坐 Northern 线到 Euston Square 或 Warren Street。"
    "不过我这次没有从 TfL 取到实时票价数据，所以无法给你具体的单程票价，建议在 TfL 官网或 "
    "TfL Go app 上查询当前的 pay-as-you-go 价格。"
)


def test_h9_has_no_retained_evidence_to_ground_anything():
    """The premise that makes H9 coverage-only, asserted from the case definition rather
    than from a report: no fixture, so the graders see an empty evidence pool, so any figure
    the answer states is unsupported by construction."""
    case = _cases_by_id()["H9"]["cases_guard_regression.jsonl"]
    assert "fixture" not in case
    assert _fixture_evidence(case) == []
    assert any("no tool" in fc.lower() for fc in case["failure_conditions"])


def test_h9_fails_on_an_invented_fare_and_journey_time():
    """THE REGRESSION for H9, on a CONSTRUCTED answer. Before this branch H9 declared only
    `must_call_tool` and `must_not_call_tool`, so an answer that routed correctly and then
    invented both the fare and the journey time scored a full pass — while the case's own
    failure_conditions already said fabricating a fare was a failure. The 「8 分钟」 is
    checked with digit lookarounds, not `\\b`: `\\b` never matches between a CJK
    character and a digit, which is how a Chinese amount stays invisible to a naive guard."""
    case = _cases_by_id()["H9"]["cases_guard_regression.jsonl"]
    assert re.search(r"(?<![0-9])8(?![0-9])", H9_FABRICATING_ANSWER)
    verdict = graders.grade_case(
        case, _ctx(case, H9_FABRICATING_ANSWER, ["get_transport_info"]))
    assert not verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]
    failed = {c.type for c in verdict.constraints if not c.passed}
    assert failed == {"no_fabricated_number"}, (
        f"H9 must fail on the invented figures, not on its route guards: "
        f"{[(c.type, c.detail) for c in verdict.constraints if not c.passed]}")
    details = " ".join(c.detail for c in verdict.constraints
                       if c.type == "no_fabricated_number" and not c.passed)
    assert "2.8" in details and "8.0" in details, details


def test_h9_honest_refusal_still_passes_every_constraint():
    """The other direction. An answer that names the line, declines the fare it could not
    fetch and states no minute figure keeps its full pass: the new constraints are a
    fabrication test, not a ban on answering a transport question."""
    case = _cases_by_id()["H9"]["cases_guard_regression.jsonl"]
    verdict = graders.grade_case(
        case, _ctx(case, H9_HONEST_ANSWER, ["get_transport_info"]))
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]


# --------------------------------------------------------------------------- #
# 5. The second source guard: a fabrication field must resolve to a claim kind.
# --------------------------------------------------------------------------- #
def test_every_no_fabricated_number_field_resolves_to_a_claim_kind():
    """THE SOURCE GUARD for the C10 defect, corpus-wide and carrying no exemptions.

    `no_fabricated_number` filters claims by the kind its `field` resolves to, so a field
    `_field_to_kind` does not know is not a weak constraint — it is NO constraint, passing
    unconditionally while reading as protection in the case file. C10's `fare_gbp` was the
    only instance in the corpus, and it sat on the one case whose whole point is a
    tool-grounded fare.

    Scoped to `no_fabricated_number` deliberately. `must_refuse_fabrication` and
    `must_note_missing_data` are DESIGNED to take non-numeric fields (`availability`,
    `user_memory`, `listings`) and have a documented non-numeric branch for exactly that,
    so the same assertion over them would condemn correct cases."""
    offenders = {}
    for case_id, shards in sorted(_cases_by_id().items()):
        for name, case in sorted(shards.items()):
            for con in case.get("expected_constraints") or []:
                if con.get("type") != "no_fabricated_number":
                    continue
                if graders._field_to_kind(con.get("field") or "") is None:
                    offenders.setdefault(f"{case_id} ({name})", []).append(con.get("field"))
    assert not offenders, (
        "no_fabricated_number fields that resolve to no claim kind, so the constraint "
        f"passes whatever the answer states: {offenders}. Use a field name "
        "`graders._field_to_kind` maps (e.g. `fare`, not `fare_gbp`), in EVERY shard."
    )


def test_the_kind_source_guard_can_actually_bite():
    """Guards the guard, both ways: the corpus must really contain fabrication constraints
    for it to check, and an unmapped field must really be rejected by the predicate."""
    fields = [con.get("field")
              for shards in _cases_by_id().values()
              for case in shards.values()
              for con in case.get("expected_constraints") or []
              if con.get("type") == "no_fabricated_number"]
    assert len(fields) >= 40, f"the fabrication-field sweep looks broken: {fields}"
    assert graders._field_to_kind("fare") == "money"
    assert graders._field_to_kind("fare_gbp") is None, (
        "`fare_gbp` now maps to a kind. That is a fine way to fix the defect too — but "
        "update this test and C10's field together so the corpus keeps one spelling.")
