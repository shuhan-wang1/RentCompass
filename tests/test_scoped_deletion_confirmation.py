"""A SELECTIVE forget must be reported as selective — and that is gradable structurally.

THE HOLE. G14 (``G_memory``) stores "£1400 and Camden", the user says "forget just my
budget, but keep the Camden preference", and the graded turn asks "What do you still have
saved for me?". Its declared contract was::

    must_forget[1400]
    must_not_mention_value[1400]
    must_recall_value[Camden]

Its third failure condition — **"Claims everything or nothing was deleted"** — had no
checker. Trace each mechanism against that failure mode:

  * ``must_forget[1400]`` PASSES on "everything has been deleted": ``acked`` is any of
    ``_FORGET_MARKERS`` and ``still_kept`` is only "retained"/"still saved"/"keep it
    saved". A blanket wipe is the most emphatic acknowledgement there is.
  * ``must_not_mention_value[1400]`` PASSES: the stale figure is not stated as live —
    it is stated as deleted, which is exactly what that checker's deletion window credits.
  * ``must_recall_value[Camden]`` PASSES: it asks only "does the string appear?". It
    appears — inside the sentence saying it was deleted too.

So the answer "Everything has been deleted — your £1400/month budget and your Camden
preference were both removed" scored **3/3, a full pass**, while doing the one thing the
case exists to detect. The kept value is located in the answer and its POLARITY is never
asserted on: HANDOFF §0's recurring shape, one field over from ``no_fabricated_number``.

THE STANCE (stated before measuring, §3.5). A case whose true memory state is MIXED — one
item deleted, one kept — requires the answer's asserted state to match the partition, and
that is judged on STRUCTURE, not wording:

  * The kept value must occur at least once OUTSIDE a deletion window. No phrasing can
    launder "Camden was removed too" into a retention, so a model cannot talk its way past
    it. This is ``must_retain_value``, the polarity-aware sibling of ``must_recall_value``,
    reusing ``must_not_mention_value``'s own cue lists and ±40-char window so the two can
    never disagree about what "stated as deleted" means.
  * NOTHING is required to be SAID. A terse honest answer — "Camden." — carries no deletion
    cue anywhere, so its single occurrence is outside every window and it passes. Failing an
    honest answer for saying less would itself be a defect, so the constraint is a veto on
    asserting a wrong state, never an obligation to recite a confirmation sentence. This is
    why the stance is NOT "G14 must contain the words 'your budget is removed, Camden is
    kept'": that would grade prose style, and it would fail the correct one-word answer.
  * The "nothing was deleted" half stays where it already works. An answer that resurfaces
    the budget as live fails ``must_not_mention_value[1400]`` today; ``test_the_nothing_was
    _deleted_half_is_already_enforced`` pins that, so this branch adds no second mechanism
    for a failure mode already covered.

RESIDUE, recorded rather than quietly closed. One sub-shape of "nothing was deleted"
remains ungraded: an answer that claims a retained budget WITHOUT naming a figure ("I still
have your budget saved"). Closing it means widening ``_c_must_forget``'s ``still_kept``
predicate, which also re-grades G5 and G9 (blanket forgets) and therefore needs its own
measured round. It is registered as accepted debt, class ``memory_self_report``, in
tests/test_failure_condition_enforceability.py.

EVIDENCE FIRST — what both arms actually did, checked before the constraint was designed.
Round of record ``.runtime/round-8793c0b-internal-2026-07-25``, re-scored under mainline
81aa7cf's grader and contract:

    arm      G14 answer                                          before   after
    fc       "- **Camden** … That's it. Your budget of £1400/     3/3 PASS  4/4 PASS
             month was removed as you asked."
    legacy   "…you want to live in Camden. Your budget of £1400   FAIL      FAIL
             per month was removed as you requested." then
             quotes the history line as a database record         (must_not_mention_value)

BOTH arms already produce a correctly scoped confirmation. The constraint is therefore pure
COVERAGE on the retained evidence — zero verdict flips, in either direction — exactly as E4
was in tests/test_uncovered_answer_dimension.py. The direction it CAN move is stated
plainly and pinned below: the blanket-wipe answer flips 3/3 PASS -> FAIL. Because no arm
produced that answer, the regression below is the real fc answer with the MINIMAL edit that
turns it into the failure mode, rather than a freshly invented string.

legacy's failure is on ``must_not_mention_value``, not on the new constraint, and the test
below asserts that specifically: this branch must not be readable as "it broke legacy".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.metrics import graders

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"

# Verbatim from .runtime/round-8793c0b-internal-2026-07-25/eval/sweep/grader_input.jsonl,
# case G14, fc arm. A correctly scoped confirmation: Camden reported as kept, the budget
# reported as gone, the figure named only inside its own deletion clause.
G14_FC_ANSWER = (
    "Here's what I have saved about you:\n\n"
    "- **Camden** — you want to live in Camden.\n\n"
    "That's it. Your budget of £1400/month was removed as you asked."
)

# Verbatim, legacy arm of the same round. The first paragraph is a correct scoped
# confirmation; the answer then quotes the history line "Saved both — £1400/month and
# Camden" as though it were a database record, which is a LIVE £1400 with no deletion cue
# in its window. That is why legacy fails, and it fails on must_not_mention_value.
G14_LEGACY_ANSWER = (
    "Based on the property information in our database, I currently have saved for you "
    "that you want to live in Camden. Your budget of £1400 per month was removed as you "
    "requested.\n\nThe property listed in the database is described as \"Saved both — "
    "£1400/month and Camden,\" but this appears to be a record of our previous "
    "conversation rather than a specific property listing. There is no actual property "
    "address, price, room type, amenities, or policies provided in the database for this "
    "entry.\n\nSince no real listing details are available, I cannot assess suitability or "
    "make a recommendation. If you would like, I can search for actual properties in "
    "Camden that match your preferences."
)

# THE REGRESSION. The fc answer above with the minimal edit that turns a scoped
# confirmation into a blanket one: the two items are moved into a single deletion clause.
# Nothing else about it is invented — same greeting, same figure, same cue vocabulary.
G14_BLANKET_WIPE_ANSWER = (
    "Here's what I had saved about you:\n\n"
    "Everything has been deleted — your £1400/month budget and your Camden preference "
    "were both removed as you asked.\n\n"
    "That's it. There is nothing left on file."
)

# The other half of the failure condition: nothing was actually deleted, and the answer
# reports the budget as live while claiming the change was made.
G14_NOTHING_DELETED_ANSWER = (
    "Here's what I have saved about you:\n\n"
    "- **Camden** — you want to live in Camden.\n"
    "- **Budget** — £1400/month.\n\n"
    "All set — I've made the change you asked for."
)

# The honest terse answer. It says strictly LESS than any of the above and must pass.
G14_TERSE_HONEST_ANSWER = "Camden."


def _cases_by_id() -> dict:
    by_case: dict = {}
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                by_case.setdefault(case["case_id"], {})[path.name] = case
    return by_case


def _g14_evidence() -> list:
    """G14's own fixture, flattened to the shape the graders consume. Derived from the file
    the runner replays so the test cannot drift from what the case is graded against."""
    from evaluation.run_benchmark import load_fixture_queue

    case = _cases_by_id()["G14"]["cases.jsonl"]
    evidence = []
    for tool, records in load_fixture_queue(case).items():
        for rec in records:
            evidence.append({"tool": tool, "success": rec.get("success", True),
                             "error": rec.get("error"), "data": rec.get("data")})
    return evidence


def _ctx(answer: str, tools=("recall_memory",)) -> graders.GradeContext:
    case = _cases_by_id()["G14"]["cases.jsonl"]
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=_g14_evidence(),
        route=None,
        user_texts=[case["user_query"]],
        reference_calculations=case.get("reference_calculations"),
        error=None,
        reconstructed_context=None,
        history_texts=[t["content"] for t in case["conversation_history"]],
    )


def _pre_amendment_case() -> dict:
    """G14 exactly as mainline 81aa7cf defined it — the amendment removed. Used to
    demonstrate the OLD behaviour rather than assert it from memory."""
    case = json.loads(json.dumps(_cases_by_id()["G14"]["cases.jsonl"]))
    case["expected_constraints"] = [c for c in case["expected_constraints"]
                                    if c["type"] != "must_retain_value"]
    return case


def _failed(verdict) -> list:
    return [(c.type, c.detail) for c in verdict.constraints if not c.passed]


# --------------------------------------------------------------------------- #
# 1. The regression: FAILS on the new contract, PASSED on the old one.
# --------------------------------------------------------------------------- #
def test_the_blanket_wipe_answer_used_to_pass_every_declared_constraint():
    """THE OLD BEHAVIOUR, executed rather than asserted from memory. Under mainline's
    three-constraint contract, an answer that deletes the kept preference and says so
    scores a full pass: must_forget sees the most emphatic acknowledgement there is,
    must_not_mention_value sees the figure only inside a deletion window, and
    must_recall_value sees the string "Camden" and asks nothing further."""
    verdict = graders.grade_case(_pre_amendment_case(), _ctx(G14_BLANKET_WIPE_ANSWER))
    assert verdict.passed, _failed(verdict)
    assert verdict.constraints_passed == verdict.constraints_total == 3, _failed(verdict)


def test_the_blanket_wipe_answer_now_fails_on_the_deletion_scope():
    """THE NEW BEHAVIOUR. Same answer, amended contract: it fails, and it fails on the
    scope of the deletion specifically — not incidentally on some other constraint."""
    case = _cases_by_id()["G14"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(G14_BLANKET_WIPE_ANSWER))
    assert not verdict.passed, _failed(verdict)
    assert [c.type for c in verdict.constraints if not c.passed] == ["must_retain_value"], (
        f"G14 must fail on the retained preference, not incidentally: {_failed(verdict)}")
    detail = [c.detail for c in verdict.constraints if c.type == "must_retain_value"][0]
    assert "as_retained=0" in detail, detail


def test_the_nothing_was_deleted_half_is_already_enforced():
    """The other half of the failure condition, pinned where it already works. An answer
    that lists the budget as live fails must_not_mention_value — so this branch does NOT
    add a second mechanism for it, and a future edit cannot quietly drop the coverage on
    the grounds that "the new constraint handles it"."""
    case = _cases_by_id()["G14"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(G14_NOTHING_DELETED_ANSWER))
    assert not verdict.passed, _failed(verdict)
    types = [c.type for c in verdict.constraints if not c.passed]
    assert "must_not_mention_value" in types, _failed(verdict)
    assert "must_retain_value" not in types, (
        "Camden is correctly reported as kept here; only the budget claim is wrong. "
        f"{_failed(verdict)}")


# --------------------------------------------------------------------------- #
# 2. The other direction: nothing honest may start failing.
# --------------------------------------------------------------------------- #
def test_the_real_fc_answer_still_passes_every_constraint():
    """The fc arm of the round of record. Its confirmation is already scoped, so the
    amendment is a verified no-op on it: 3/3 -> 4/4."""
    case = _cases_by_id()["G14"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(G14_FC_ANSWER))
    assert verdict.passed, _failed(verdict)
    assert verdict.constraints_passed == verdict.constraints_total == 4, _failed(verdict)


def test_the_terse_honest_answer_is_not_punished_for_saying_less():
    """"Camden." — no confirmation sentence, no figure, no cue. The constraint is a veto on
    asserting a wrong state, not an obligation to recite one, so this must pass it. (The
    case's other constraints are a separate question; this asserts the new one only, which
    is the property that keeps the stance from becoming prose grading.)"""
    con = {"type": "must_retain_value", "value": "Camden"}
    res = graders._c_must_retain_value(con, _ctx(G14_TERSE_HONEST_ANSWER))
    assert res.passed, res.detail
    assert "as_deleted_or_superseded=0" in res.detail, res.detail


def test_the_legacy_arm_still_fails_where_it_already_failed_and_not_here():
    """legacy's G14 quotes the stored history line as a database record, which is a LIVE
    £1400. It failed under mainline and still fails — on must_not_mention_value. The new
    constraint must NOT be among its failures: its first paragraph reports Camden as kept,
    so this branch cannot be read as "it broke legacy"."""
    case = _cases_by_id()["G14"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(G14_LEGACY_ANSWER, tools=()))
    assert not verdict.passed
    failed_types = [c.type for c in verdict.constraints if not c.passed]
    assert failed_types == ["must_not_mention_value"], _failed(verdict)


# --------------------------------------------------------------------------- #
# 3. Guarding the guard.
# --------------------------------------------------------------------------- #
def test_must_retain_value_shares_the_deletion_window_with_its_sibling():
    """The checker's whole claim to being structural is that "stated as deleted" means the
    same thing here as in must_not_mention_value. If either cue list is forked, one of them
    starts crediting a phrasing the other rejects and the pair becomes incoherent."""
    src = (Path(graders.__file__).read_text(encoding="utf-8"))
    body = src.split("def _c_must_retain_value", 1)[1].split("\ndef ", 1)[0]
    assert "_SUPERSEDE_CUES + _FORGET_MARKERS" in body, (
        "must_retain_value no longer uses the shared cue lists")
    assert "s - 40" in body and "e + 40" in body, (
        "must_retain_value no longer uses the shared ±40-char window")
    assert graders._locate_number(G14_FC_ANSWER, "Camden"), (
        "_locate_number no longer localises a non-numeric value; the checker would see "
        "zero occurrences and pass everything")


def test_the_old_recall_checker_really_does_pass_the_blanket_wipe():
    """The premise of this whole module, asserted directly. If must_recall_value ever grew
    polarity awareness of its own, must_retain_value would be redundant and this module
    would be pinning a hole that no longer exists."""
    con = {"type": "must_recall_value", "value": "Camden"}
    assert graders._c_must_recall_value(con, _ctx(G14_BLANKET_WIPE_ANSWER)).passed, (
        "must_recall_value now rejects the blanket wipe — re-derive whether "
        "must_retain_value is still needed before deleting this module")


def test_the_new_type_is_in_the_schema_and_the_checker_registry():
    """A type present in a case but absent from CONSTRAINT_CHECKERS scores "no checker",
    which grade_case records as a FAILED constraint — a case failure blamed on the agent.
    A type present in the checkers but absent from schema.json fails validate.py."""
    schema = json.loads((BENCH / "schema.json").read_text(encoding="utf-8"))
    enum = schema["properties"]["expected_constraints"]["items"]["properties"]["type"]["enum"]
    assert "must_retain_value" in enum
    assert "must_retain_value" in graders.CONSTRAINT_CHECKERS


# --------------------------------------------------------------------------- #
# 4. The source guard: no selective-forget case may go ungraded on its scope.
# --------------------------------------------------------------------------- #
def _is_selective_forget(case: dict) -> bool:
    """Derived from the contract, not a hand-listed case id: a case that declares BOTH a
    deletion (``must_forget``) and a recall (``must_recall_value``) is asserting a MIXED
    end state — something goes, something stays — which is precisely the shape whose scope
    can be misreported."""
    types = {c["type"] for c in case.get("expected_constraints") or []}
    return "must_forget" in types and "must_recall_value" in types


def test_every_selective_forget_case_grades_the_scope_of_its_deletion():
    """THE SOURCE GUARD, and the reason this is not one hand-patched case. Any case that
    declares a deletion alongside a recall must also declare must_retain_value for the kept
    value, in EVERY shard defining it — otherwise the same hole reopens under a new id."""
    offenders = {}
    for case_id, shards in sorted(_cases_by_id().items()):
        for name, case in sorted(shards.items()):
            if not _is_selective_forget(case):
                continue
            kept = {str(c.get("value")) for c in case["expected_constraints"]
                    if c["type"] == "must_recall_value"}
            covered = {str(c.get("value")) for c in case["expected_constraints"]
                       if c["type"] == "must_retain_value"}
            if not kept <= covered:
                offenders[f"{case_id} ({name})"] = sorted(kept - covered)
    assert not offenders, (
        "these cases delete one thing and keep another but declare no must_retain_value "
        f"for the kept value — the G14 hole: {offenders}")


def test_the_source_guard_can_actually_bite():
    """Three ways, so a typo cannot leave the guard above passing vacuously: the predicate
    must select G14; it must NOT select the blanket forgets (which keep nothing, so there is
    no scope to misreport and a constraint would be noise); and stripping the amendment must
    make the guard fire."""
    by_case = _cases_by_id()
    selected = [cid for cid, shards in by_case.items()
                if _is_selective_forget(next(iter(shards.values())))]
    assert selected == ["G14"], (
        f"expected G14 to be the corpus's only selective forget, got {sorted(selected)}")
    for blanket in ("G5", "G9"):
        assert not _is_selective_forget(by_case[blanket]["cases.jsonl"]), (
            f"{blanket} is a BLANKET forget — it keeps nothing, so must_retain_value would "
            "be a constraint over nothing")

    stripped = _pre_amendment_case()
    assert _is_selective_forget(stripped), "the predicate must still select the old G14"
    kept = {str(c.get("value")) for c in stripped["expected_constraints"]
            if c["type"] == "must_recall_value"}
    covered = {str(c.get("value")) for c in stripped["expected_constraints"]
               if c["type"] == "must_retain_value"}
    assert not kept <= covered, "the guard would not have fired on mainline's G14"


@pytest.mark.parametrize("shard", ["cases.jsonl", "cases_ext_FG.jsonl"])
def test_the_amendment_reached_every_shard_defining_g14(shard):
    """Same failure mode as G2/G3/E11: amending cases.jsonl alone leaves the sibling shard
    grading a different contract, and a green run on one shard proves nothing about the
    other."""
    shards = _cases_by_id()["G14"]
    assert shard in shards, f"G14 should be defined in {shard}"
    con = [c for c in shards[shard]["expected_constraints"]
           if c["type"] == "must_retain_value"]
    assert con == [{"type": "must_retain_value", "value": "Camden"}], con
    assert "must_retain_value" in shards[shard]["failure_conditions"][2], (
        "the failure condition must say in plain language what the constraint forbids")
