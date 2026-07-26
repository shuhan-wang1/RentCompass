"""`no_false_retrieval_provenance` — the constraint held back from PR #7, re-enabled.

The 2026-07-22 ruling: an answer may not DENY having searched when a retrieval tool
actually EXECUTED and returned usable evidence on that turn. A provenance
contradiction misleads exactly as much as a fabricated figure. An HONEST denial over
genuinely empty or failed retrieval is not a violation.

Why this file exists in this shape
----------------------------------
The constraint was written, its H3 guard case was amended, and then all of it was
reverted out of PR #7 for one reason: the grader imports ``claims_no_retrieval`` from
``uk_rent_agent.agent.critic``, and that predicate lived only on the terminated
``hardening/correctness-only`` branch. So the constraint existed on paper — named in
two docs — while nothing anywhere asserted it. That is the repo's recurring defect
shape: a value computed and parked where a reader could find it, never asserted on.

The tests below therefore assert the whole chain end to end, not just the predicate:

  1. the predicate itself (both languages, both directions);
  2. the grader's verdict on the three interesting evidence shapes;
  3. **wiring** — the constraint type is in the schema vocabulary AND in
     ``CONSTRAINT_CHECKERS``, checked as a two-way equality over the entire vocabulary.
     Half-landing an amendment is not silent here, but it is misleading in both
     directions: a type with no checker makes ``grade_case`` record
     ``ConstraintResult(type, False, "no checker")``, so the case fails as if the agent
     had misbehaved when in fact the evaluator was never wired; and a checker missing
     from the enum is unreachable dead code, because every shard carrying the type
     fails schema validation and the preflight refuses the run outright;
  4. **H3 actually carries it**, in every shard that defines H3 — the G2/G3/E11
     cross-shard scar;
  5. the critic's own honest fallbacks do not self-trip the predicate.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from evaluation.metrics import graders
from uk_rent_agent.agent.critic import (
    claims_no_retrieval,
    no_reliable_data_message,
    LEGACY_INCONSISTENCY_FALLBACK,
    LEGACY_RETRIEVAL_MISS_FALLBACK,
)

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"
CONSTRAINT = "no_false_retrieval_provenance"


def _grade_ctx(**kw):
    base = dict(final_answer="", tools_called=[], tool_call_events=[], evidence=[])
    base.update(kw)
    return graders.GradeContext(**base)


def _schema_enum():
    schema = json.loads((BENCH / "schema.json").read_text(encoding="utf-8"))
    return schema["properties"]["expected_constraints"]["items"]["properties"]["type"]["enum"]


def _cases_by_id():
    by_case = defaultdict(dict)
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                by_case[case["case_id"]][path.name] = case
    return by_case


# ── 1. the ported predicate ──────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "我目前没有搜索任何具体房源数据，因此无法提供准确租金。",
    "抱歉，无法搜索到相关数据。",
    "我未进行检索，所以只能给出大致印象。",
    "我没有任何搜索数据可以参考。",
    "I couldn't search for current listings.",
    "No search was performed this turn.",
    "I was not able to search the web for this.",
    "Answering without searching, based on general knowledge.",
])
def test_a_denial_of_having_searched_is_detected(text):
    assert claims_no_retrieval(text) is True, text


@pytest.mark.parametrize("text", [
    "根据网页检索结果，Zone 2 租金范围较大。",
    "如果你希望，我可以为你搜索最新数据。",     # an OFFER to search is not a denial
    "先不要搜索，我直接根据市场信息回答。",       # quoting the USER's instruction
    "The search found 3 listings.",
    # REGRESSION GUARD, carried over from the source branch: the system's OWN honest
    # fallback openers say a FIGURE could not be retrieved. That is not a claim that no
    # search happened. An earlier, broader regex matched a bare "couldn't retrieve" and
    # turned every honest fallback into a self-reported provenance violation.
    "Sorry — I couldn't retrieve reliable specific figures right now, so here is "
    "what I have verified:",
    "抱歉，我未能获取到可靠的具体数字，先按已核实的信息回答：",
])
def test_ordinary_prose_and_offers_to_search_are_not_denials(text):
    assert claims_no_retrieval(text) is False, text


def test_predicate_tolerates_empty_and_none():
    assert claims_no_retrieval("") is False
    assert claims_no_retrieval(None) is False


# ── 2. the grader ────────────────────────────────────────────────────────────
def test_provenance_denial_with_usable_web_evidence_fails():
    ctx = _grade_ctx(
        final_answer="我目前没有搜索任何具体房源数据，因此无法提供 Zone 2 的准确租金水平。",
        evidence=[{"tool": "web_search",
                   "data": {"summary": "Zone 2 typical rents £1,500-£2,200 pcm (2026)."}}])
    res = graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx)
    assert res.passed is False
    assert "usable_web_evidence=True" in res.detail
    assert "claims_no_retrieval=True" in res.detail


def test_honest_denial_over_empty_retrieval_passes():
    # SearXNG down / nothing found: denying a search RESULT is honest, not a violation.
    ctx = _grade_ctx(
        final_answer="抱歉，无法搜索到相关数据，暂时无法给出租金水平。",
        evidence=[{"tool": "web_search",
                   "data": "No search results found for this query."}])
    assert graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx).passed is True


def test_honest_denial_when_the_tool_reported_failure_passes():
    ctx = _grade_ctx(
        final_answer="I couldn't search the web just now, so I can't quote a figure.",
        evidence=[{"tool": "web_search", "data": {"success": False, "error": "timeout"}}])
    assert graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx).passed is True


def test_grounded_answer_over_usable_evidence_passes():
    ctx = _grade_ctx(
        final_answer="根据网页检索，Zone 2 租金大致在 £1,500–£2,200/月。",
        evidence=[{"tool": "web_search",
                   "data": {"summary": "Zone 2 typical rents £1,500-£2,200 pcm (2026)."}}])
    assert graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx).passed is True


def test_only_web_search_evidence_counts():
    """The constraint is about the RETRIEVAL the turn is supposed to have done. A
    listing search does not license the claim that a web search happened, so a denial
    alongside only search_properties evidence is not judged here."""
    ctx = _grade_ctx(
        final_answer="我没有搜索网页数据。",
        evidence=[{"tool": "search_properties", "data": {"recommendations": [{"a": 1}]}}])
    res = graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx)
    assert res.passed is True
    assert "usable_web_evidence=False" in res.detail


def test_no_evidence_at_all_passes():
    ctx = _grade_ctx(final_answer="我没有搜索任何数据。", evidence=[])
    assert graders._c_no_false_retrieval_provenance({"type": CONSTRAINT}, ctx).passed is True


# ── 3. wiring: declared in the vocabulary AND actually checkable ─────────────
def test_the_constraint_is_both_declared_and_graded():
    assert CONSTRAINT in _schema_enum(), "missing from schema.json enum: shards won't validate"
    assert CONSTRAINT in graders.CONSTRAINT_CHECKERS, (
        'no grader: every case carrying it scores False with detail "no checker"')


def test_every_declared_constraint_type_has_a_grader_and_vice_versa():
    """Pins both halves of the wiring for the WHOLE vocabulary, not just this constraint.

    Declared-but-ungraded reads as a behaviour regression: ``grade_case`` records
    ``ConstraintResult(type, False, "no checker")``, so the case fails and the report
    blames the agent for a missing evaluator. Graded-but-undeclared is dead code: the
    checker can never be reached, since any shard using the type fails schema
    validation and ``validate_all_shards`` refuses the run first."""
    enum = set(_schema_enum())
    registry = set(graders.CONSTRAINT_CHECKERS)
    assert enum - registry == set(), f"declared but ungraded (silently skipped): {enum - registry}"
    assert registry - enum == set(), f"graded but not in the vocabulary: {registry - enum}"


def test_every_constraint_used_by_any_shard_is_declared_and_graded():
    used = set()
    for shards in _cases_by_id().values():
        for case in shards.values():
            for con in case.get("expected_constraints") or []:
                used.add(con["type"])
    assert used <= set(_schema_enum())
    assert used <= set(graders.CONSTRAINT_CHECKERS)
    assert CONSTRAINT in used, "the constraint is graded but no case exercises it"


def test_the_checker_is_reached_through_the_registry():
    """Belt and braces: go through CONSTRAINT_CHECKERS rather than the private name, so a
    registry key typo cannot pass this file while a real grader run scores "no checker"."""
    ctx = _grade_ctx(
        final_answer="No search was performed, so I cannot give a figure.",
        evidence=[{"tool": "web_search", "data": {"summary": "Zone 2 rents £1,500 pcm."}}])
    assert graders.CONSTRAINT_CHECKERS[CONSTRAINT]({"type": CONSTRAINT}, ctx).passed is False


# ── 4. the H3 guard-case amendment, in every shard that defines H3 ───────────
def test_h3_carries_the_provenance_constraint_in_every_shard_defining_it():
    """H3 is the market_info negative guard: '先不要搜索' still routes to web synthesis,
    so the turn DOES retrieve — which is exactly the setup in which denying the search
    is a lie. The amendment must be present in every shard that defines H3; amending one
    shard and not its siblings is how G2/G3/E11 drifted."""
    shards = _cases_by_id()["H3"]
    assert shards, "H3 disappeared from the benchmark"
    for name, case in shards.items():
        types = [c["type"] for c in case.get("expected_constraints") or []]
        assert CONSTRAINT in types, f"H3 in {name} lost {CONSTRAINT}: {types}"
        assert any("provenance" in fc for fc in case.get("failure_conditions") or []), (
            f"H3 in {name} has the constraint but no failure_condition explaining it")


def test_h3_expects_web_retrieval_so_the_constraint_is_meaningful():
    """Guards the guard: the constraint can only ever fire on a case whose expected
    retrieval is web_search. If H3 stopped expecting web_search the assertion above
    would still pass while checking nothing."""
    case = next(iter(_cases_by_id()["H3"].values()))
    assert "web_search" in case["expected_tools"]


# ── 5. the critic's own honest fallbacks must not self-trip ──────────────────
@pytest.mark.parametrize("message", [
    no_reliable_data_message("zh"),
    no_reliable_data_message("en"),
    LEGACY_RETRIEVAL_MISS_FALLBACK,
    LEGACY_INCONSISTENCY_FALLBACK,
])
def test_the_critics_own_honest_fallbacks_do_not_self_trip(message):
    """These are the deterministic replies the critic itself emits when retrieval gave
    it nothing. They say a FIGURE could not be verified or retrieved — never that no
    search happened. If the predicate flagged them, every honest fallback would be
    scored as a provenance lie, and the constraint would punish the correct behaviour."""
    assert claims_no_retrieval(message) is False, message


def test_the_english_cue_regex_requires_a_search_verb_object():
    """Pins WHY the fallbacks above are safe, so a future widening of the regex fails
    here with an explanation instead of silently re-breaking them: the denial verb must
    govern a search/retrieval object, not any object at all."""
    assert claims_no_retrieval("I couldn't verify the exact rent.") is False
    assert claims_no_retrieval("I couldn't retrieve reliable figures.") is False
    assert claims_no_retrieval("I couldn't retrieve any search results.") is True
    assert claims_no_retrieval("I couldn't run a search.") is True


def test_the_evaluator_module_does_not_import_product_code_at_module_scope():
    """The grader's ``from uk_rent_agent...`` import is function-local on purpose: the
    re-scorer and the shard preflight load ``evaluation.metrics.graders`` without the
    product package importable. A module-scope import would break both."""
    src = (Path(graders.__file__)).read_text(encoding="utf-8")
    module_scope = [ln for ln in src.splitlines()
                    if re.match(r"^(import|from)\s+(uk_rent_agent|app)\b", ln)]
    assert module_scope == [], module_scope


def test_the_grader_binds_the_predicate_from_THIS_tree_under_the_harness_bootstrap():
    """The only cross-tree hazard this port introduces, pinned.

    This is the first constraint whose grader imports product code, so the evaluator's
    judgement now depends on WHICH copy of ``uk_rent_agent`` the interpreter resolves.
    That is not academic: the bench image pip-installs its own snapshot of the package
    (a ``.pth`` putting ``/app/src`` on ``sys.path``), so a bare ``python -c`` inside a
    container with the tree bind-mounted elsewhere imports the IMAGE's stale critic, not
    the tree's — observed directly, as ``ImportError: cannot import name
    'claims_no_retrieval' from '/app/src/uk_rent_agent/agent/critic.py'`` while
    ``/patched/src`` had it. ``grade_case`` catches that and records
    ``ConstraintResult(type, False, "checker error: ...")``, so H3 would fail looking
    like an agent regression.

    The real entrypoints prevent it: ``run_benchmark._bootstrap_env`` and
    ``rescore._bootstrap`` both ``sys.path.insert(0, REPO_ROOT/"src")``, which wins over
    the image snapshot. Nothing asserted that, and this suite cannot notice on its own
    because ``tests/conftest.py`` pins the same paths first. So assert it in a CLEAN
    subprocess, with only the harness bootstrap doing the work."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    probe = (
        "from evaluation import rescore; rescore._bootstrap();"
        "import uk_rent_agent.agent.critic as c;"
        "from evaluation.metrics import graders;"
        "print(c.__file__);"
        "print(graders.CONSTRAINT_CHECKERS['no_false_retrieval_provenance']("
        "{'type': 'no_false_retrieval_provenance'},"
        " graders.GradeContext(final_answer='No search was performed.', tools_called=[],"
        " tool_call_events=[], evidence=[{'tool': 'web_search',"
        " 'data': {'summary': 'Zone 2 rents 1500 pcm'}}])).passed)"
    )
    proc = subprocess.run([sys.executable, "-c", probe], cwd=str(repo),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    critic_path, passed = proc.stdout.strip().splitlines()[-2:]
    assert Path(critic_path).resolve().is_relative_to(repo), (
        f"the grader graded with a critic from OUTSIDE the tree under test: {critic_path}")
    assert passed == "False", "the predicate resolved but the grader did not fire"
