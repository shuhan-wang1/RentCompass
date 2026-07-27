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
# 2. The amendment reached every shard.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case_id", ["E10", "E4"])
def test_the_commute_dimension_constraint_is_in_every_shard_defining_the_case(case_id):
    """Same failure mode as G2/G3/E11: amending cases.jsonl alone leaves the sibling shard
    grading a different contract, and a green run on one shard proves nothing about the
    other. E10 also lives in cases_ext_CDE; E4 also lives in cases_base45."""
    shards = _cases_by_id()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    for name, case in shards.items():
        assert _covers_kind(case, COMMUTE_KIND), (
            f"{case_id} in {name} declares no constraint over a commute figure: "
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
