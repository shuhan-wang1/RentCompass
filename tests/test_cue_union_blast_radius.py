# -*- coding: utf-8 -*-
"""What the cue-table UNION actually costs fc — measured on the benchmark corpus, per cue.

PR #52 merged ``agent_loop._DIMENSION_CUES`` and ``langgraph_agent._SEARCH_DIMENSION_CUES``
into ``core.dimensions.DIMENSION_CUES`` and took the UNION, which gave fc six cues it did not
have: ``safe``; ``travel time`` / ``how long`` / ``how far``; ``药店`` / ``pharmacy``.
tests/test_dimension_table_is_shared.py pins that the merged table IS the union and that both
arches now read it. tests/test_dimension_fanout.py pins the fan-out's gates generically.

Neither answers the question the owner ruling needs, because fc holds the public edge and is
already over the p50 bar: **how much extra work does the widening cause, and on which turns?**
A cue on fc is not only a disclosure line — ``_dimension_fanout_calls`` turns an unserved cued
dimension into a real tool call in the batch. So the union is a latency change, not just a
vocabulary change, and it has to be priced rather than argued about.

WHAT THIS FILE ESTABLISHES

  1. Which dimension and which ONE tool each of the six gained cues reaches (section 1).
  2. The measured blast radius on ``evaluation/benchmark/cases.jsonl``: exactly 10 of 98 cases
     gain a dimension, and on the state the round of record actually carried the union adds
     ZERO tool calls to all ten — 7 of them because the model had already put the canonical
     tool in the same batch, 3 because the fan-out's args gate refuses to invent a location
     (section 2). The three that COULD add a call are named, with which one.
  3. The false-positive surface, in English and Chinese, including the cases where these cues
     genuinely misfire (section 3). Recorded as behaviour, not excused: ``safe`` matches any
     word containing it and ``how long`` / ``how far`` fire on tenancy and rent questions that
     have nothing to do with commuting.

No network, no LLM, no real tools.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies of `core` live there).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

import core.agent_loop as agent_loop
import core.langgraph_agent as lga
from tests.test_fc_loop import FakeSpec, _base_state, _tc

try:
    from core import dimensions
except ImportError:                 # 4f410ab and earlier: the shared table does not exist yet
    dimensions = None

# The behavioural tests deliberately go through the ARCH's own entry points, which exist on both
# sides of the merge (``_cued_dimensions``, ``_canonical_dimension_tool``,
# ``_dimension_satisfying_tools``, ``_dimension_fanout_calls``). That is what makes them RED on
# 4f410ab with a wrong answer rather than with an ImportError: there, fc simply does not
# recognise the six cues, so it adds no call and writes no line for them. The table-shaped tests
# are skipped when the shared module is absent, because on that tree there is nothing to inspect.
needs_shared_table = pytest.mark.skipif(dimensions is None,
                                        reason="pre-union tree has no core.dimensions")

DIMENSION_ORDER = ("safety", "commute", "nearby")

REPO = pathlib.Path(__file__).resolve().parent.parent
CASES_PATH = REPO / "evaluation" / "benchmark" / "cases.jsonl"
FIXTURES = REPO / "evaluation" / "benchmark" / "fixtures"

# fc's cue table as it stood on 4f410ab, i.e. the merge's INPUT on the fc side. The union is
# this plus the six legacy-only cues; keeping the old table here is what makes "gained" a
# computed set rather than a list someone maintains by hand.
FC_BEFORE_THE_UNION = {
    "safety": ("治安", "安全", "犯罪", "crime", "safety", "unsafe", "police"),
    "commute": ("通勤", "commute"),
    "nearby": ("超市", "便利店", "餐厅", "附近", "周边", "设施",
               "supermarket", "grocery", "nearby", "amenit", "restaurant", "poi"),
}

# The six cues, the dimension each joins, and the ONE tool the harness may dispatch for it.
GAINED_CUES = (
    ("safe", "safety", "check_safety"),
    ("travel time", "commute", "calculate_commute"),
    ("how long", "commute", "calculate_commute"),
    ("how far", "commute", "calculate_commute"),
    ("药店", "nearby", "search_nearby_pois"),
    ("pharmacy", "nearby", "search_nearby_pois"),
)

_FANOUT_TOOLS = ("search_properties", "check_safety", "calculate_commute",
                 "search_nearby_pois", "get_property_details", "get_transport_info")


def _specs():
    return {name: FakeSpec(name) for name in _FANOUT_TOOLS}


def _hit(cues, message: str) -> bool:
    """``dimensions.cues_hit``, restated locally so the counterfactual runs on a tree that has
    no shared module. It must stay byte-equivalent in behaviour; the source guard at the bottom
    asserts the product has exactly one copy of the real one."""
    msg = message or ""
    low = msg.lower()
    return any((cue in low) if cue.isascii() else (cue in msg) for cue in cues)


def _fc_dims_before(message: str) -> list:
    """The dimensions fc recognised BEFORE the union: the old vocabulary, the same matcher."""
    return [dim for dim in DIMENSION_ORDER if _hit(FC_BEFORE_THE_UNION[dim], message)]


def _gained(message: str) -> set:
    """Dimensions the union added to fc's reading of `message`, via fc's own matcher."""
    return set(agent_loop._cued_dimensions(message)) - set(_fc_dims_before(message))


def _cases() -> list:
    return [json.loads(line) for line in
            CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fixture_bound_tools(case: dict) -> set:
    fx = case.get("fixture")
    if not fx:
        return set()
    names = [fx] if isinstance(fx, str) else list(fx)
    out = set()
    for name in names:
        raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        items = raw["results"] if isinstance(raw, dict) and "results" in raw else [raw]
        for item in items:
            out.add(item.get("tool_name") or "unknown")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 1. What each gained cue triggers: one dimension, one tool, one batch
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("cue,dim,tool", GAINED_CUES, ids=[c for c, _d, _t in GAINED_CUES])
def test_each_gained_cue_reaches_exactly_one_dimension_and_one_tool(cue, dim, tool):
    """The cue in isolation, so the mapping is pinned per cue rather than per sentence.

    ``canonical_tool`` is the only read the harness may dispatch for a dimension; the rest of
    the tuple are alternates that satisfy it when the MODEL picks them. Both halves matter for
    the cost: the harness adds ONE call, and it stands down if any alternate is present.
    """
    assert cue not in FC_BEFORE_THE_UNION[dim], f"{cue!r} was not gained; the table is stale"
    assert agent_loop._cued_dimensions(cue) == [dim]
    assert agent_loop._canonical_dimension_tool(dim) == tool
    # legacy names the same single tool when it decides to fetch, so the two arches cost the
    # same call for the same cue.
    assert dict(lga._cued_search_dimensions(cue, set()))[dim] == tool


@pytest.mark.parametrize("cue,dim,tool", GAINED_CUES, ids=[c for c, _d, _t in GAINED_CUES])
def test_a_gained_cue_adds_its_tool_to_the_SAME_batch_not_a_second_hop(cue, dim, tool):
    """WHERE the added read lands, because that is what decides its latency.

    ``_dimension_fanout_calls`` returns reads to be appended to the batch already being
    dispatched, and ``execute_tools`` starts every read in a batch before awaiting any. So an
    added read on a turn that ALREADY runs tools costs max(0, its own duration - the batch's),
    not its full duration. On a turn that runs NO tools it costs the full duration plus a hop,
    which is the expensive case and the reason the args gate in section 2 is load-bearing.
    """
    message = f"Find me a flat in Camden — {cue}?"
    state = _base_state(user_query=message,
                        extracted_context={"current_message": message, "reply_language": "en"},
                        accumulated_search_criteria={"area": "Camden",
                                                     "commute_destination": "UCL"})
    batch = [_tc("search_properties", {"area": "Camden"}, "c1")]
    added = agent_loop._dimension_fanout_calls(state, batch, message, specs=_specs())
    assert [name for name, _args in added] == [tool], (
        f"cue {cue!r} must add exactly its canonical read, once")
    # And it is a READ appended to this batch — never a write, never terminal.
    spec = _specs()[tool]
    assert getattr(spec, "side_effect", "none") != "write"
    assert getattr(spec, "terminal", False) is False


@pytest.mark.parametrize("cue,dim,tool", GAINED_CUES, ids=[c for c, _d, _t in GAINED_CUES])
def test_a_gained_cue_adds_nothing_when_the_model_already_asked_for_that_dimension(
        cue, dim, tool):
    """The gate that makes the measured cost zero on 7 of the 10 affected corpus cases.

    A dimension whose satisfying tool is already in the batch is covered, so the union widens
    what fc UNDERSTANDS without widening what fc RUNS. This is asserted per gained cue because
    it is the whole latency argument.
    """
    message = f"Find me a flat in Camden — {cue}?"
    state = _base_state(user_query=message,
                        extracted_context={"current_message": message, "reply_language": "en"},
                        accumulated_search_criteria={"area": "Camden",
                                                     "commute_destination": "UCL"})
    batch = [_tc("search_properties", {"area": "Camden"}, "c1"), _tc(tool, {}, "c2")]
    assert agent_loop._dimension_fanout_calls(state, batch, message, specs=_specs()) == []

    # Any ALTERNATE that satisfies the dimension counts too, not just the canonical one.
    for alternate in agent_loop._dimension_satisfying_tools(dim):
        specs = _specs()
        specs.setdefault(alternate, FakeSpec(alternate))
        alt_batch = [_tc("search_properties", {"area": "Camden"}, "c1"),
                     _tc(alternate, {}, "c2")]
        assert agent_loop._dimension_fanout_calls(
            state, alt_batch, message, specs=specs) == [], alternate


@pytest.mark.parametrize("cue,dim,tool", GAINED_CUES, ids=[c for c, _d, _t in GAINED_CUES])
def test_a_gained_cue_cannot_add_a_call_without_a_location_argued_from_state(cue, dim, tool):
    """The second gate, and the one that protects the clarification turns.

    With no area anywhere in state the fan-out issues nothing, for every gained cue — it would
    rather leave the honest 'not done yet' line standing than geocode a guess. D13 of the
    benchmark ("Is it a safe area to live in?", no history, ``expected_route: clarification``,
    ``check_safety`` in ``forbidden_tools``) depends on exactly this.
    """
    message = f"{cue}?"
    state = _base_state(user_query=message,
                        extracted_context={"current_message": message, "reply_language": "en"})
    assert dim in agent_loop._cued_dimensions(message)
    assert agent_loop._dimension_fanout_calls(state, [], message, specs=_specs()) == []
    # The dimension is still DISCLOSED, which is the cheap half of the union.
    lines = agent_loop._missing_requested_dimension_lines(message, set(), "en")
    assert lines == [agent_loop._DIMENSION_APOLOGY_LINES[dim][1]]


def test_the_commute_cues_still_stand_down_on_a_declared_no_commute_profile():
    """``how long`` / ``how far`` / ``travel time`` widen the cue set on the dimension with the
    most expensive read, so the pre-existing no-commute and self-commute refusals are asserted
    against the NEW cues too — a cue that fires on "I don't commute" must not buy a journey."""
    for cue in ("travel time", "how long", "how far"):
        message = f"I don't commute at all — {cue} is irrelevant, just find me a flat in Camden"
        state = _base_state(
            user_query=message,
            extracted_context={"current_message": message, "reply_language": "en"},
            accumulated_search_criteria={"area": "Camden", "commute_destination": "UCL",
                                         "no_commute": True})
        assert "commute" in agent_loop._cued_dimensions(message)
        assert agent_loop._dimension_fanout_calls(state, [], message, specs=_specs()) == [], cue

    # Self-to-self: an area that IS the destination buys a meaningless "0 minutes".
    for cue in ("travel time", "how long", "how far"):
        message = f"Flat in Docklands, London — {cue} to Docklands?"
        state = _base_state(
            user_query=message,
            extracted_context={"current_message": message, "reply_language": "en"},
            accumulated_search_criteria={"area": "Docklands, London",
                                         "commute_destination": "Docklands"})
        assert agent_loop._dimension_fanout_calls(state, [], message, specs=_specs()) == [], cue


# ═══════════════════════════════════════════════════════════════════════════
# 2. The measured blast radius on the 98-case benchmark corpus
# ═══════════════════════════════════════════════════════════════════════════

# Every case whose CURRENT message gains a dimension under the union, with the cue that does it
# and the tools the fc arm of the round of record
# (.runtime/round-8793c0b-internal-2026-07-25/eval/sweep) shows the model itself put in the
# turn's only batch. `expected` is whether the case contract WANTS that tool.
#
#   case  gained dim  cue          model's own batch (round of record)          wanted?
CORPUS_BLAST_RADIUS = (
    ("C3",  "commute", "how long",  ("get_property_details",),                        True),
    ("D1",  "safety",  "safe",      ("check_safety",),                                True),
    ("D2",  "safety",  "safe",      ("check_safety", "check_safety"),                 True),
    ("E3",  "safety",  "safe",      ("check_safety", "search_nearby_pois"),           True),
    ("F4",  "commute", "how long",  (),                                               True),
    ("C10", "commute", "how long",  ("calculate_commute", "get_transport_info"),       True),
    ("D8",  "nearby",  "pharmacy",  ("search_nearby_pois",),                          True),
    ("D9",  "safety",  "safe",      ("check_safety", "check_safety"),                 True),
    ("D13", "safety",  "safe",      (),                                               False),
    ("E9",  "safety",  "safe",      ("calculate_commute", "check_safety",
                                     "search_nearby_pois"),                           True),
)

# The three cases where the covered-set gate does NOT cover the gained dimension, so the union
# could add a call once a location is in context. Two are quality WINS (C3 and F4 both declare
# expected_route: calculate_commute and both got it wrong in the round of record); the third is
# the one to watch.
COULD_ADD_A_CALL = {"C3": "calculate_commute", "F4": "calculate_commute",
                    "D13": "check_safety"}


def test_exactly_ten_of_the_ninetyeight_cases_gain_a_dimension_and_these_are_they():
    """The corpus measurement, recomputed from the case file rather than quoted.

    10 of 98 = 10.2% of the corpus understands one more dimension than it did. A seventh cue
    added later moves this number and turns this red, which is the point: the widening is
    supposed to be a visible decision.
    """
    measured = {}
    for case in _cases():
        gained = _gained(case["user_query"])
        if gained:
            assert len(gained) == 1, (case["case_id"], gained)
            measured[case["case_id"]] = sorted(gained)[0]

    expected = {cid: dim for cid, dim, _cue, _batch, _want in CORPUS_BLAST_RADIUS}
    assert measured == expected
    assert len(measured) == 10
    assert len(_cases()) == 98


@pytest.mark.parametrize(
    "case_id,dim,cue,own_batch,wanted", CORPUS_BLAST_RADIUS,
    ids=[c for c, _d, _cu, _b, _w in CORPUS_BLAST_RADIUS])
@needs_shared_table
def test_the_gained_dimension_of_each_affected_case_is_gained_by_the_cue_named(
        case_id, dim, cue, own_batch, wanted):
    """Attribution per case: the dimension is gained BY that cue and by no other.

    Removing the one cue from the merged row must put the case back to its pre-union reading.
    Without this the ten cases are a count; with it they are ten attributions.
    """
    case = next(c for c in _cases() if c["case_id"] == case_id)
    query = case["user_query"]
    assert _gained(query) == {dim}
    assert cue in query.lower() or cue in query

    merged = {d: cues for d, cues, _t in dimensions.DIMENSION_CUES}
    without_that_cue = {d: tuple(c for c in cues if c != cue) for d, cues in merged.items()}
    still = [d for d in DIMENSION_ORDER if _hit(without_that_cue[d], query)]
    assert dim not in still, (
        f"{case_id}: removing {cue!r} does not remove {dim}, so the attribution is wrong")

    # The case contract's own view of whether that tool was wanted.
    tool = agent_loop._canonical_dimension_tool(dim)
    satisfying = set(agent_loop._dimension_satisfying_tools(dim))
    assert bool(satisfying & set(case.get("expected_tools") or [])) is wanted, case_id
    if not wanted:
        assert tool in (case.get("forbidden_tools") or []), (
            f"{case_id} is recorded as not wanting {tool}; if that is no longer in "
            "forbidden_tools the risk assessment for this cue has changed")


@pytest.mark.parametrize(
    "case_id,dim,cue,own_batch,wanted", CORPUS_BLAST_RADIUS,
    ids=[c for c, _d, _cu, _b, _w in CORPUS_BLAST_RADIUS])
def test_the_union_adds_no_tool_call_on_any_affected_case_as_the_round_recorded_it(
        case_id, dim, cue, own_batch, wanted):
    """THE latency number: zero added tool calls, on all ten affected cases.

    Replayed against the batch the model itself issued in the round of record, and against the
    state that round carried (no accumulated area or destination reconstructible for these
    turns — the follow-up cases reference "there"/"that place" in prose, which the fan-out
    deliberately will not resolve). Seven are covered by the model's own call; three are
    refused by the args gate.

    So the cue union's measured cost on the fc arm of the 98-case corpus is 0 tool calls and
    0 ms. What it is NOT is zero in general — see the next test.
    """
    case = next(c for c in _cases() if c["case_id"] == case_id)
    query = case["user_query"]
    batch = [_tc(name, {}, f"c{i}") for i, name in enumerate(own_batch)]
    state = _base_state(user_query=query,
                        extracted_context={"current_message": query, "reply_language": "en"})
    added = agent_loop._dimension_fanout_calls(state, batch, query, specs=_specs())
    assert added == [], f"{case_id}: the union added {added}"


@pytest.mark.parametrize(
    "case_id,dim,cue,own_batch,wanted", CORPUS_BLAST_RADIUS,
    ids=[c for c, _d, _cu, _b, _w in CORPUS_BLAST_RADIUS])
def test_which_affected_cases_would_add_a_call_once_a_location_is_in_context(
        case_id, dim, cue, own_batch, wanted):
    """The same ten cases with the args gate satisfied — the honest upper bound.

    Three of ten add exactly one read. C3 and F4 add ``calculate_commute`` and both DECLARE it
    as their expected route and both got it wrong in the round of record, so that call is the
    fix, not the cost. D13 adds ``check_safety`` — and D13's contract says the right answer is
    to ASK, with ``check_safety`` forbidden. With no area it stays silent (previous test); with
    an area in context the union turns that turn into a fetch. That is the one place a
    narrowing of ``safe`` would buy something, and it is recorded here rather than argued.
    """
    case = next(c for c in _cases() if c["case_id"] == case_id)
    query = case["user_query"]
    batch = [_tc(name, {}, f"c{i}") for i, name in enumerate(own_batch)]
    state = _base_state(user_query=query,
                        extracted_context={"current_message": query, "reply_language": "en"},
                        accumulated_search_criteria={"area": "Peckham",
                                                     "commute_destination": "Canary Wharf"})
    added = [name for name, _args in agent_loop._dimension_fanout_calls(
        state, batch, query, specs=_specs())]
    assert added == ([COULD_ADD_A_CALL[case_id]] if case_id in COULD_ADD_A_CALL else [])


# Live (non-fixture-replayed) execution times of the three canonical dimension reads, from the
# fc arm of the round of record. The corpus replays most of these from fixtures at ~0.5 ms, so
# the eval harness CANNOT price a fan-out; these are the only live samples it produced.
#   tool                 n   observed ms
#   check_safety         4   4, 410, 499, 882
#   search_nearby_pois   2   1627, 13568
#   calculate_commute    1   2429
LIVE_READ_MS = {"check_safety": (4, 410, 499, 882),
                "search_nearby_pois": (1627, 13568),
                "calculate_commute": (2429,)}


@pytest.mark.parametrize("case_id", sorted(COULD_ADD_A_CALL))
def test_the_added_read_is_the_one_whose_live_cost_was_measured(case_id):
    """Ties the upper bound to a price, so "we widened the cue set on the arch that is already
    too slow" is a number.

    The added read joins an existing batch and is dispatched concurrently, so on a turn that
    already runs tools it costs max(0, its duration - the batch's). On a ZERO-tool turn (F4 and
    D13 both ran none) it costs its full duration PLUS an extra LLM hop, which is PR #29's
    measured trap. fc's p50 is 8466 ms cold / 7402 ms warm against a 6000 ms bar, so a read
    that can take 13.6 s is not a rounding error on the turns where it fires.
    """
    tool = COULD_ADD_A_CALL[case_id]
    assert tool in LIVE_READ_MS
    assert max(LIVE_READ_MS[tool]) > 0
    # The most expensive canonical read in the table is the POI search, and its own deadline is
    # derived from the batch window rather than tied to it (PR #53), so the batch cannot be
    # abandoned by its straggler.
    assert max(LIVE_READ_MS["search_nearby_pois"]) == 13568
    from core.tools.search_nearby_pois import poi_search_budget_s, _batch_window_s
    assert poi_search_budget_s() < _batch_window_s()


def test_the_fanout_can_be_switched_off_without_narrowing_the_cue_table():
    """The ops lever that makes the latency risk reversible without a deploy or a code change.

    FC_DIMENSION_FANOUT_MAX=0 removes every added call while the union keeps both arches
    agreeing about what the user asked — the disclosure survives, the cost does not. That
    separation is what lets the owner rule on the cue table and the fan-out independently.
    """
    query = "Find me a flat in Camden, is it safe?"
    state = _base_state(user_query=query,
                        extracted_context={"current_message": query, "reply_language": "en"},
                        accumulated_search_criteria={"area": "Camden"})
    assert agent_loop._dimension_fanout_calls(state, [], query, specs=_specs()) == [
        ("check_safety", {"area": "Camden", "user_query": query})]

    old = os.environ.get("FC_DIMENSION_FANOUT_MAX")
    os.environ["FC_DIMENSION_FANOUT_MAX"] = "0"
    try:
        assert agent_loop._dimension_fanout_cap() == 0
        assert agent_loop._dimension_fanout_calls(state, [], query, specs=_specs()) == []
        # ...and the cue is still understood, in both arches.
        assert agent_loop._cued_dimensions(query) == ["safety"]
        assert agent_loop._missing_requested_dimension_lines(query, set(), "en") == [
            "Safety has not been verified yet (crime data was not retrieved)."]
    finally:
        if old is None:
            os.environ.pop("FC_DIMENSION_FANOUT_MAX", None)
        else:
            os.environ["FC_DIMENSION_FANOUT_MAX"] = old


# ═══════════════════════════════════════════════════════════════════════════
# 3. The false-positive surface, both languages
# ═══════════════════════════════════════════════════════════════════════════

# Sentences that fire a gained cue while asking about something the dimension cannot answer.
# Recorded as CURRENT behaviour with the cost of each, not excused: `safe` is a bare substring
# and the two `how ...` cues are bare interrogatives.
FALSE_POSITIVES = (
    ("safe", "safety", "Is Peckham unsafe at night?", "subsumes the pre-existing `unsafe` cue"),
    ("safe", "safety", "Which is the safest area on my shortlist?", "matches `safest`"),
    ("safe", "safety", "Does the tenancy have a safeguard clause about the deposit?",
     "matches `safeguard` — a contract question cues crime data"),
    ("how long", "commute", "How long does the landlord have to return my deposit?",
     "a deposit-timeline question cues a journey"),
    ("how long", "commute", "How long is the tenancy agreement?",
     "a contract-length question cues a journey"),
    ("how far", "commute", "How far can my landlord raise the rent at renewal?",
     "a rent-increase question cues a journey"),
)


@pytest.mark.parametrize("cue,dim,message,why", FALSE_POSITIVES,
                         ids=[f"{c}-{w[:22]}" for c, _d, _m, w in FALSE_POSITIVES])
def test_a_gained_cue_that_misfires_costs_a_disclosure_line_and_not_a_fetch(cue, dim, message, why):
    """The measured cost of each false positive: one honest line, zero tool calls.

    These sentences DO cue the dimension — substring matching cannot tell "safeguard clause"
    from "safe area", and "how long is the tenancy" from "how long to UCL". What stops the
    misfire becoming work is the args gate: none of them carries a resolvable area, so the
    fan-out issues nothing and the turn pays only a line saying the dimension was not checked.

    That line is still wrong on a deposit question, and it is the honest price of the union's
    fail-safe direction: a cue that fires produces a fetch or an apology, a cue that misses
    produces silence, and silence about a dimension the user asked about is what let E11
    invent "about 15-20 min to Canary Wharf". Narrowing these three cues means accepting that
    trade in the other direction, deliberately, with this test red.
    """
    assert dim in agent_loop._cued_dimensions(message), why
    state = _base_state(user_query=message,
                        extracted_context={"current_message": message, "reply_language": "en"})
    assert agent_loop._dimension_fanout_calls(state, [], message, specs=_specs()) == []
    lines = agent_loop._missing_requested_dimension_lines(message, set(), "en")
    assert agent_loop._DIMENSION_APOLOGY_LINES[dim][1] in lines


def test_the_bare_safe_cue_is_a_strict_superset_of_the_cue_it_replaced():
    """``unsafe`` is now redundant: every string containing it contains ``safe``. Kept in the
    table because the union may not DROP a cue either side had, but asserted here so nobody
    reads the row as two independent checks."""
    for word in ("unsafe", "safety", "safest", "safely", "safeguard"):
        assert "safe" in word
        assert agent_loop._cued_dimensions(word) == ["safety"]
    if dimensions is not None:
        safety_cues = dict((d, c) for d, c, _t in dimensions.DIMENSION_CUES)["safety"]
        assert "safe" in safety_cues and "unsafe" in safety_cues


@needs_shared_table
def test_the_chinese_gained_cue_matches_the_raw_text_and_needs_no_word_boundary():
    """``药店`` on the CJK side, plus the adjacent CJK commute-cap contract.

    ``cues_hit`` matches CJK cues against the RAW text by substring. It has to: Python's ``re``
    treats a CJK character as a word character, so ``\\b`` never fires between two of them, nor
    between one and a digit. The cue path is safe because it uses no ``\\b`` at all — asserted
    below. The commute parser likewise uses an ASCII-only lookbehind, so CJK adjacency
    preserves the explicit cap instead of silently dropping it.
    """
    for message in ("附近有药店吗？", "帮我找卡姆登的房子，附近要有药店", "药店"):
        assert agent_loop._cued_dimensions(message) == ["nearby"], message
    # No spaces, no boundaries, still matched — including embedded in an ascii/CJK mix.
    assert agent_loop._cued_dimensions("Camden药店pharmacy") == ["nearby"]

    # Source guard on the CODE, not the prose: the module docstring is allowed to discuss
    # regexes, the matcher is not allowed to use one.
    tree = ast.parse(inspect.getsource(dimensions))
    imported = {n.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for n in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "re" not in imported, (
        "the cue matcher must stay substring-based: a word-boundary regex cannot match a CJK "
        "cue, and would silently narrow the Chinese half of the table")
    code_strings = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value not in {ast.get_docstring(tree)}]
    assert not any("\\b" in s for s in code_strings if len(s) < 200)

    from core.tools.search_properties import _extract_commute_minutes as extract
    assert extract("通勤 30 min") == 30
    assert extract("通勤30 min以内") == 30       # no ASCII spacing required
