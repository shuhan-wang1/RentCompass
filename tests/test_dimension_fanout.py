"""Multi-dimension requests must FETCH every dimension they cue, not apologise for it.

WHY THIS FILE EXISTS. ``_DIMENSION_CUES`` + ``_missing_requested_dimension_lines`` already
knew, deterministically and bilingually, which dimensions a message asks about and which of
them have no completed tool result. Its ONLY consumer in the whole repo was
``_artifact_grounded_fallback_answer`` — the DEGRADED answer builder, reached only once the
turn had already blown its time budget or the wrap-up LLM call had failed. So the loop knew
"the user asked about safety and we never fetched it" and used that knowledge exclusively to
write an apology. It never ran on the normal path and it never caused the fetch. That is
HANDOFF §0 instance #12: a value computed, stored where a reader could find it, never acted on.

The three measured consequences, from the round of record
(`.runtime/round-8793c0b-internal-2026-07-25/eval/sweep`), are pinned here as regressions:

  * **E1** answered a 4-tool request from ``search_properties`` alone (two batches, one tool
    each) and failed ``must_mention_source: data.police.uk`` because the source was
    unobtainable — it never called ``check_safety``.
  * **E5** ended by NARRATING the remaining work and stopping. Its literal last line is pinned
    in ``test_e5_narration_is_no_longer_the_end_of_the_turn``.
  * **E11** answered a 3-dimension request with world-knowledge estimates ("about 15-20 min to
    Canary Wharf", where 15 and 20 occur ZERO times in its evidence) and failed
    ``must_complete_requested_dimensions`` with ``missing=['commute','nearby']``. That exact
    grader verdict is pinned in ``test_e11_grader_verdict_missing_commute_and_nearby``, run
    through the grader's own constraint function.

WHAT IS NOT BEING FIXED HERE. Intra-batch dispatch was already fully concurrent
(tests/test_parallel_tool_batch.py: 8 independent 1s reads finish in ~1s). These tests are
about getting more than one tool INTO a batch.

THE TRAP THIS FILE ALSO GUARDS. PR #29 measured that a mandatory extra hop made the product
WORSE — better p50, but turns-under-bar fell 26 -> 21, because 12 fast zero-tool turns paid
for a hop they did not need. ``test_greeting_*`` / ``test_single_dimension_*`` /
``test_zero_tool_turn_*`` assert the un-cued turn is untouched, byte for byte.

No network, no LLM, no real tools: the FakeChat/FakeProvider harness from tests/test_fc_loop.py.
"""
from __future__ import annotations

import re

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import core.agent_loop as agent_loop
from core.agent_loop import build_fc_nodes
from tests.test_fc_loop import (FakeChat, FakeProvider, FakeResult, FakeSpec, _base_state,
                                _drive, _run, _tc)

# The literal user messages from the round of record. Cited, not paraphrased: the cue matching
# is substring-based, so a paraphrase would not be the same test.
E1_QUERY = ("Find me a 1-bed in Islington under £1600/month with a commute to UCL under 40 "
            "minutes, a supermarket nearby, and avoid high-crime areas — go ahead and search.")
E5_QUERY = ("帮我找伦敦月租不超过1400镑的单间，通勤到帝国理工不超过35分钟，"
            "附近要有超市，尽量避开治安差的区域。")
E5_NARRATION = "让我进一步搜索这些区域的实际房源，同时查一下周边设施和治安情况。"
E11_QUERY = ("Find a studio in Stratford under £1300/month, with a commute to Canary Wharf "
             "under 25 minutes, a pharmacy nearby, and steer clear of high-crime spots — "
             "go ahead and search.")
# A1 from the same corpus: ONE dimension (listings). Note it contains the word "commute" —
# in the negative. It must never trigger a commute fetch.
A1_QUERY = ("Find me a 2-bed flat in Camden under £1500 a month. No commute to worry about, "
            "so just go ahead and search.")

_ALL_DIMENSION_TOOLS = ["search_properties", "check_safety", "calculate_commute",
                        "search_nearby_pois"]


def _specs(names=None, **over):
    names = list(names or _ALL_DIMENSION_TOOLS)
    return [FakeSpec(n, **over.get(n, {})) for n in names]


def _provider(names=None, **over):
    return FakeProvider(_specs(names, **over), {
        "search_properties": FakeResult(True, {
            "success": True, "status": "no_results", "recommendations": [],
            "data_source": "OnTheMarket", "partial": False,
            "search_criteria": {"area": "Stratford", "areas": ["Stratford"],
                                "commute_destination": "Canary Wharf", "no_commute": False,
                                "room_type": "studio", "max_budget": 1300}}),
        "check_safety": FakeResult(True, {"safety_score": 50, "safety_level": "Moderate",
                                          "address": "Stratford",
                                          "data_source": "data.police.uk"}),
        "calculate_commute": FakeResult(True, {"duration_minutes": 14, "mode": "transit",
                                               "route_source": "tfl"}),
        # shape as _format_pois consumes it: {poi_type: [poi, ...]}
        "search_nearby_pois": FakeResult(True, {
            "address": "Stratford",
            "pois": {"pharmacy": [{"name": "Boots", "distance_display": "120m"}]}}),
    })


def _state_for(query, **over):
    st = _base_state(user_query=query,
                     extracted_context={"current_message": query, "reply_language": "en"},
                     **over)
    return st


def _first_batch(state):
    """The tool names in the FIRST batch — i.e. answered against the first assistant message
    that carried tool calls. This is the number the fix has to move."""
    for m in state["messages"]:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            return [tc["name"] for tc in m.tool_calls]
    return []


def _batches(state):
    return [[tc["name"] for tc in m.tool_calls] for m in state["messages"]
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)]


# ═══════════════════════════════════════════════════════════════════
# 1. Plan-time expansion: N cued dimensions -> ONE batch
# ═══════════════════════════════════════════════════════════════════

def test_e11_four_dimension_request_issues_one_four_tool_batch():
    """THE regression. On the old code this batch is ``['search_properties']`` and the other
    three dimensions each cost their own LLM round-trip — E11 never reached two of them."""
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Stratford"}, "c1")]),
        AIMessage(content="Here is everything, with sources."),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E11_QUERY, accumulated_search_criteria={
        "area": "Stratford", "commute_destination": "Canary Wharf", "room_type": "studio",
        "max_budget": 1300})))

    first = _first_batch(state)
    assert sorted(first) == sorted(_ALL_DIMENSION_TOOLS), (
        f"first batch was {first}; the harness must add the reads for every cued dimension "
        "so they run concurrently instead of one per LLM round-trip")
    # ...and they really executed, each with a ToolMessage answering its OWN tool_call_id —
    # every id in the assistant message is answered exactly once, which is what keeps the
    # provider from rejecting the next round-trip.
    assert sorted(c[0] for c in provider.calls) == sorted(_ALL_DIMENSION_TOOLS)
    ai = next(m for m in state["messages"]
              if isinstance(m, AIMessage) and getattr(m, "tool_calls", None))
    tmsgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in tmsgs} == {tc["id"] for tc in ai.tool_calls}
    assert len(tmsgs) == 4
    # ONE batch, so ONE extra LLM round-trip was NOT paid three times over.
    assert _batches(state) == [first], f"expected a single batch, got {_batches(state)}"


def test_e11_grader_verdict_missing_commute_and_nearby():
    """Pinned against the GRADER's own constraint function, with the exact observed detail.

    Round of record, fc arm, E11:
        must_complete_requested_dimensions dimensions=['commute','nearby','safety']
                                           missing=['commute','nearby']   -> FAIL
    """
    from evaluation.metrics.graders import GradeContext, _c_must_complete_requested_dimensions

    # The tools E11 actually executed on the old code, verbatim from raw_runs.jsonl.
    observed = ["search_properties", "compare_or_rank_areas", "check_safety", "search_properties"]
    r = _c_must_complete_requested_dimensions(
        {"dimensions": ["commute", "nearby", "safety"]},
        GradeContext(final_answer="", tools_called=observed, tool_call_events=[], evidence=[]))
    assert r.passed is False and "missing=['commute', 'nearby']" in r.detail, (
        "the baseline failure this fix targets has changed shape; re-derive it before editing")

    # Now the same grader over what the fixed loop executes.
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Stratford"}, "c1")]),
        AIMessage(content="done"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E11_QUERY, accumulated_search_criteria={
        "area": "Stratford", "commute_destination": "Canary Wharf"})))
    executed = [a["tool"] for a in state["tool_artifacts"] if agent_loop._is_executed(a)]
    r2 = _c_must_complete_requested_dimensions(
        {"dimensions": ["commute", "nearby", "safety"]},
        GradeContext(final_answer="", tools_called=executed, tool_call_events=[], evidence=[]))
    assert r2.passed is True, r2.detail


def test_e1_safety_source_becomes_obtainable():
    """E1 failed ``must_mention_source: data.police.uk`` because check_safety never ran, so the
    source was unobtainable at any prose quality. The fan-out makes it obtainable."""
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Islington"}, "c1")]),
        AIMessage(content="Safety from data.police.uk ..."),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E1_QUERY, accumulated_search_criteria={
        "area": "Islington", "commute_destination": "UCL", "max_budget": 1600})))

    safety = [a for a in state["tool_artifacts"] if a["tool"] == "check_safety"]
    assert len(safety) == 1 and safety[0]["raw_data"]["data_source"] == "data.police.uk"
    assert "check_safety" in _first_batch(state)


def test_e5_narration_is_no_longer_the_end_of_the_turn():
    """E5's literal last line, pinned. The old loop let the model SAY it would look up the
    nearby amenities and the crime data and then stop; both reads are now already in flight in
    the same batch, so the narration cannot be the whole of the turn's work."""
    provider = _provider()
    chat = FakeChat([
        AIMessage(content=E5_NARRATION,
                  tool_calls=[_tc("search_properties", {"area": "South Kensington"}, "c1")]),
        AIMessage(content="房源、周边设施与治安数据如下……"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E5_QUERY, accumulated_search_criteria={
        "area": "South Kensington", "commute_destination": "Imperial College London"})))

    executed = {a["tool"] for a in state["tool_artifacts"] if agent_loop._is_executed(a)}
    assert {"search_nearby_pois", "check_safety"} <= executed, (
        f"E5 narrated 「{E5_NARRATION}」 and stopped; both reads it names must now be in the "
        f"batch. executed={sorted(executed)}")


def test_cjk_cues_fan_out_too():
    """The cue table is bilingual and so is the fan-out: CJK cues match the raw text."""
    q = "帮我看看肯辛顿的治安和周边超市，通勤到 King's Cross 多久？"
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Kensington"}, "c1")]),
        AIMessage(content="好"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(q, accumulated_search_criteria={
        "area": "Kensington", "commute_destination": "King's Cross"})))
    assert sorted(_first_batch(state)) == sorted(_ALL_DIMENSION_TOOLS)


def test_fanout_uses_the_area_the_search_resolved_not_a_guess():
    """The added reads must be argued from state, never invented. Here the ONLY source of an
    area is the model's own search_properties args."""
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties",
                                              {"area": "Whitechapel",
                                               "commute_destination": "Canary Wharf"}, "c1")]),
        AIMessage(content="ok"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    _run(_drive(nodes, _state_for(E11_QUERY)))
    args = {name: params for name, params in provider.calls}
    assert args["check_safety"]["area"] == "Whitechapel"
    assert args["search_nearby_pois"]["address"] == "Whitechapel"
    assert args["calculate_commute"] == {
        "from_address": "Whitechapel", "to_address": "Canary Wharf",
        "idempotency_key": args["calculate_commute"]["idempotency_key"]}
    # poi_type is deliberately NOT set, so search_nearby_pois infers it from the user's own
    # words ("a pharmacy nearby") instead of the harness guessing a type.
    assert "poi_type" not in args["search_nearby_pois"]
    assert args["search_nearby_pois"]["user_query"] == E11_QUERY


def test_no_derivable_area_means_no_call_and_no_invented_location():
    """No area anywhere in state -> the dimensions stay unfetched and the honest 'not done yet'
    line stands. Guessing a location would be a fabrication with a source attached."""
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {}, "c1")]),
        AIMessage(content="ok"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E11_QUERY)))
    assert _first_batch(state) == ["search_properties"]
    assert agent_loop._missing_requested_dimension_lines(
        E11_QUERY, {"search_properties"}, "en") == [
        "Safety has not been verified yet (crime data was not retrieved).",
        "Commute time has not been calculated yet.",
        "Nearby amenities have not been looked up yet."]


# ═══════════════════════════════════════════════════════════════════
# 2. PR #29's trap: an un-cued turn must be untouched
# ═══════════════════════════════════════════════════════════════════

def test_greeting_turn_is_unaffected():
    """A greeting makes exactly one LLM call, dispatches nothing and returns the model's text.
    PR #29: 12 fast zero-tool turns paid for a hop they did not need and turns-under-bar fell
    26 -> 21. Nothing here may add a hop, a batch or a tool."""
    provider = _provider()
    chat = FakeChat([AIMessage(content="Hello! How can I help with your rental search?")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for("hi there"), start="agent"))

    assert provider.calls == []
    assert state["tool_artifacts"] == []
    assert state["final_response"] == "Hello! How can I help with your rental search?"
    assert chat._scripted == []  # exactly the one scripted call was consumed
    assert _batches(state) == []


def test_single_dimension_request_batch_is_byte_for_byte_unchanged():
    """A1: one dimension. The batch must stay exactly one call with exactly the model's args."""
    provider = _provider()
    tc = _tc("search_properties", {"area": "Camden", "max_budget": 1500, "bedrooms": 2}, "c1")
    chat = FakeChat([AIMessage(content="", tool_calls=[dict(tc)]),
                     AIMessage(content="Here are 5 listings in Camden.")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(A1_QUERY, accumulated_search_criteria={
        "area": "Camden", "max_budget": 1500, "bedrooms": 2, "no_commute": True})))

    assert _first_batch(state) == ["search_properties"]
    assert [c[0] for c in provider.calls] == ["search_properties"]


def test_negated_commute_cue_never_triggers_a_commute_fetch():
    """A1 literally contains the word "commute" and the cue table matches on substrings, so
    the cue FIRES. ``no_commute`` and the absent destination are the two deterministic gates
    that stop it becoming a call — the user said they do not commute."""
    assert "commute" in agent_loop._cued_dimensions(A1_QUERY)
    ctx = {"area": "Camden", "commute_destination": None, "no_commute": True}
    assert agent_loop._dimension_read_args("commute", ctx, A1_QUERY) is None
    # and with no_commute unset but no destination either:
    assert agent_loop._dimension_read_args(
        "commute", {"area": "Camden", "commute_destination": None, "no_commute": False},
        A1_QUERY) is None


def test_zero_tool_turn_that_cues_nothing_adds_nothing():
    """A statutory-arithmetic turn (no tools, no dimension cues) is untouched."""
    q = "My rent is £1,500 a month. What is the maximum deposit they can ask for?"
    assert agent_loop._cued_dimensions(q) == []
    assert agent_loop._dimension_fanout_calls(
        _state_for(q), [], q, specs={}, read_policy=None) == []


def test_fanout_can_be_disabled_by_ops_without_a_deploy(monkeypatch):
    monkeypatch.setenv("FC_DIMENSION_FANOUT_MAX", "0")
    provider = _provider()
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Stratford"}, "c1")]),
        AIMessage(content="ok")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _state_for(E11_QUERY, accumulated_search_criteria={
        "area": "Stratford", "commute_destination": "Canary Wharf"})))
    assert _first_batch(state) == ["search_properties"]


# ═══════════════════════════════════════════════════════════════════
# 3. What may never be swept into an expansion
# ═══════════════════════════════════════════════════════════════════

def test_expansion_never_adds_a_write_tool():
    """``remember`` is the only side_effect="write" tool and it drives the taint gate, the write
    audit and the zero-tolerance records. Asserted at the mechanism, by declaring a dimension's
    canonical tool a write: the fan-out must refuse it rather than sweep it in."""
    st = _state_for(E11_QUERY, accumulated_search_criteria={"area": "Stratford"})
    write_specs = {s.name: s for s in
                   [FakeSpec("check_safety", side_effect="write"),
                    FakeSpec("search_nearby_pois")]}
    added = agent_loop._dimension_fanout_calls(st, [], E11_QUERY, specs=write_specs)
    assert [n for n, _a in added] == ["search_nearby_pois"]
    # and no canonical dimension tool is a write in the real registry's terms
    assert "remember" not in {agent_loop._canonical_dimension_tool(d)
                              for d, *_ in agent_loop._DIMENSION_CUES}


def test_expansion_never_adds_a_terminal_tool_or_ask_user():
    st = _state_for(E11_QUERY, accumulated_search_criteria={"area": "Stratford"})
    terminal_specs = {s.name: s for s in [FakeSpec("check_safety", terminal=True)]}
    assert agent_loop._dimension_fanout_calls(st, [], E11_QUERY, specs=terminal_specs) == []
    assert "ask_user" not in {agent_loop._canonical_dimension_tool(d)
                             for d, *_ in agent_loop._DIMENSION_CUES}


def test_expansion_respects_the_read_policy():
    """A read core.tool_policy would refuse is never dispatched — consulted with the SAME
    helper execute_tools uses, so the turn does not pay a batch plus a hop to learn it."""
    class _DenyAll:
        @staticmethod
        def read_tool_denial(name, args, *, current_message):
            return agent_loop.__dict__ and type("D", (), {"reason": "no", "guidance": "no",
                                                          "reference": None})()

    st = _state_for(E11_QUERY, accumulated_search_criteria={"area": "Stratford"})
    specs = {s.name: s for s in _specs()}
    assert agent_loop._dimension_fanout_calls(
        st, [], E11_QUERY, specs=specs, read_policy=_DenyAll) == []


def test_expansion_skips_a_dimension_the_model_already_asked_for():
    """No duplicate work: a dimension the model itself put in the batch is already covered."""
    st = _state_for(E11_QUERY, accumulated_search_criteria={
        "area": "Stratford", "commute_destination": "Canary Wharf"})
    specs = {s.name: s for s in _specs()}
    batch = [_tc("search_properties", {"area": "Stratford"}, "c1"),
             _tc("check_safety", {"area": "Stratford"}, "c2")]
    added = agent_loop._dimension_fanout_calls(st, batch, E11_QUERY, specs=specs)
    assert sorted(n for n, _a in added) == ["calculate_commute", "search_nearby_pois"]


def test_expansion_skips_a_dimension_already_attempted_this_turn():
    """"Attempted", not "completed": an abandoned fetch already spent its budget, and retrying
    it is how a bounded loop becomes an unbounded one. It degrades to the apology instead."""
    st = _state_for(E11_QUERY, accumulated_search_criteria={"area": "Stratford"})
    st["tool_artifacts"] = [{"tool": "check_safety", "raw_data": None, "timed_out": True,
                             "abandoned": True, "outcome_unknown": True}]
    specs = {s.name: s for s in _specs()}
    added = agent_loop._dimension_fanout_calls(st, [], E11_QUERY, specs=specs)
    assert "check_safety" not in [n for n, _a in added]
    # ...and the honest line for it still stands, because it keys on COMPLETED results.
    assert "Safety has not been verified yet (crime data was not retrieved)." in \
        agent_loop._missing_requested_dimension_lines(E11_QUERY, set(), "en")


# ═══════════════════════════════════════════════════════════════════
# 4. Source guards (practice 3): ONE cue table, ONE matcher, both consumers
# ═══════════════════════════════════════════════════════════════════

def _module_source():
    import inspect
    return inspect.getsource(agent_loop)


@pytest.mark.parametrize("cue", ["治安", "unsafe", "supermarket", "amenit", "通勤"])
def test_cue_vocabulary_appears_exactly_once_in_the_module(cue):
    """A source guard, not a promise. Instances 1-6 of the §0 defect class were each fixed
    individually and only a guard stopped the eighth; the divergence THIS module keeps
    producing is a second copy of a table. If a cue word appears twice, someone has pasted a
    second cue table and the fetcher and the apology can now disagree."""
    src = _module_source()
    assert src.count(f'"{cue}"') == 1, (
        f"cue {cue!r} occurs {src.count(chr(34) + cue + chr(34))} times in agent_loop.py — "
        "_DIMENSION_CUES must remain the only cue table")


def test_cue_matching_happens_in_exactly_one_place():
    """The ascii/CJK split is the subtle half of the cue contract. It must exist once."""
    src = _module_source()
    assert src.count("cue.isascii()") == 1, (
        "cue matching is duplicated; both consumers must route through _cued_dimensions")


def test_both_consumers_read_the_one_cue_table():
    """Behavioural half of the guard: the dimensions the apology would name (nothing executed)
    are EXACTLY the dimensions the fan-out considers."""
    for q in (E1_QUERY, E5_QUERY, E11_QUERY, A1_QUERY, "hi", "帮我查治安"):
        cued = agent_loop._cued_dimensions(q)
        lines = agent_loop._missing_requested_dimension_lines(q, set(), "en")
        assert len(lines) == len(cued), q
        considered = agent_loop._unserved_cued_dimensions(q, [])
        assert considered == cued, q


def test_canonical_tool_agrees_with_the_graders_dimension_table():
    """``evaluation/metrics/graders.py`` keeps its OWN ``_DIMENSION_TOOLS`` (it grades a
    contract and must not import the product). The two are allowed to exist; they are NOT
    allowed to disagree, or the loop can fetch a tool the grader does not count."""
    from evaluation.metrics.graders import _DIMENSION_TOOLS
    for dim, _cues, tools, _zh, _en in agent_loop._DIMENSION_CUES:
        assert dim in _DIMENSION_TOOLS, f"{dim} unknown to the grader"
        assert set(tools) == set(_DIMENSION_TOOLS[dim]), (
            f"{dim}: loop={sorted(tools)} grader={sorted(_DIMENSION_TOOLS[dim])}")
        assert agent_loop._canonical_dimension_tool(dim) in _DIMENSION_TOOLS[dim]


def test_canonical_tool_is_the_first_of_the_satisfying_tuple():
    """No second dimension->tool mapping: the canonical read is derived from the cue table."""
    for dim, _cues, tools, _zh, _en in agent_loop._DIMENSION_CUES:
        assert agent_loop._canonical_dimension_tool(dim) == tools[0]
    assert agent_loop._canonical_dimension_tool("no_such_dimension") is None
