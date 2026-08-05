"""Retrieval must not be dispatched on a pure-arithmetic money turn.

app/core/langgraph_agent.py ``_compute_decision`` step 1.65. Measured on the 2026-07-25 round
of record: a turn that quotes the rent the USER typed and asks what it costs / what deposit
the law allows, naming no place to search, was voted into ``web_search`` /
``search_properties``:

  fc arm      B8  web_search x2 (both empty) -> answer hard-replaced, correct
                  £3,446.15 total discarded
              B12 search_properties -> status "need_clarification", rendered as
                  "The property search completed: no studio listings matched"
              B14 web_search x2 on a purely statutory arithmetic question
  legacy arm  B10, B14, B15  web_search x5 each (the market_info web fan-out)

``core.tool_policy.read_tool_denial`` closed this at DISPATCH for the fc loop only; the legacy
graph never consults it. The guard tested here is the routing half, and it reuses tool_policy's
predicate so the two arches cannot drift.

Both directions are pinned: a self-contained statutory question must NOT retrieve, and a
MARKET question (names a place, or asks for a judgement/listings) must still retrieve.

No live API and no network: the classification LLM and the web-search planner are stubbed,
mirroring tests/test_agent_loop.py. The critic-side half of the same round's B_money story
(the no-evidence hard replace) is pinned in tests/test_critic_hard_replace_telemetry.py.
"""

from __future__ import annotations

import json
import os
import sys
import types

# Pin the real source roots ahead of tests/ (stale shadow `core` copies live under tests/
# and would otherwise shadow the app packages under prepend mode). Mirrors test_agent_loop.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


# ── stubs (mirror test_agent_loop.py) ───────────────────────────────────────
class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info", "check_safety"]

    def get(self, name):
        return None


class _JsonLLM:
    """Returns the given intent as strict JSON (mimics the DeepSeek classifier)."""

    def __init__(self, intent):
        self.intent = intent
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return types.SimpleNamespace(content=json.dumps({"intent": self.intent}))


class _NoVoteLLM:
    """Fails if the LLM vote is reached — proves the deterministic guard fired first."""

    def invoke(self, prompt):
        raise AssertionError("the money guard must route before the LLM vote")


def _decide(lga, msg, llm, extra_ctx=None, accumulated=None, monkeypatch=None):
    if monkeypatch is not None:
        # No real network from web-search planning (the market_info / fan-out path).
        monkeypatch.setattr(lga, "_plan_web_searches",
                            lambda q, reg: {"tool": "multi_search",
                                            "params": {"searches": [{"tool": "web_search",
                                                                     "params": {"query": q}}]},
                                            "reason": "planned"})
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {"user_query": msg, "extracted_context": ec,
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


def _decision(lga, msg, llm, **kw):
    return _decide(lga, msg, llm, **kw).update["tool_decision"]


# The three cases the round of record flagged, verbatim from evaluation/benchmark/cases.jsonl,
# each paired with the intent the round's classifier actually returned for it.
B8 = ("What's the total move-in cost for a £1600 pcm place — first month plus the "
      "standard deposit?")
B12 = ("I'm looking at a £380/week studio. What'll it cost me all-in per month, "
       "including bills and council tax?")
B14 = "The rent is £1,000 a week. What deposit is the landlord legally allowed to take?"

# Same class, also in the predicate's firing set (B10/B15 are the legacy arm's other two).
B3 = "For a £1500/month flat, how much deposit should I expect?"
B10 = "For a flat at £4,200 per month, how much deposit can they legally ask for?"
B15 = ("For a £4,800 pcm flat, what's my total upfront cost — first month plus the deposit "
       "they're allowed to charge?")


# ═══════════════════════════════════════════════════════════════════════════
# 1. NO RETRIEVAL on a self-contained money turn
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("case_id,msg,voted_intent", [
    # The vote each case actually produced in the round: B8/B14 -> the web fan-out,
    # B12 -> the listings search. A _JsonLLM returning that intent reproduces the defect.
    ("B8", B8, "market_info"),
    ("B12", B12, "search_properties"),
    ("B14", B14, "market_info"),
    ("B3", B3, "market_info"),
    ("B10", B10, "market_info"),
    ("B15", B15, "market_info"),
])
def test_self_contained_money_turn_never_routes_to_retrieval(lga, monkeypatch,
                                                             case_id, msg, voted_intent):
    """The forbidden-tool defect. Even when the classifier votes for a retrieval intent, the
    deterministic guard wins and the turn dispatches NO tool."""
    llm = _JsonLLM(voted_intent)
    d = _decision(lga, msg, llm, monkeypatch=monkeypatch)
    assert d["tool"] == "direct_answer", (case_id, d["tool"])
    # These are the two names the eval lists in forbidden_tools for this class.
    assert d["tool"] not in ("web_search", "search_properties", "multi_search")


def test_money_guard_beats_the_vote_without_calling_it(lga, monkeypatch):
    """Deterministic, not a prompt: _NoVoteLLM asserts the classifier is never invoked."""
    d = _decision(lga, B14, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert d["tool"] == "direct_answer"
    assert "no retrieval is dispatched" in d["reason"]


def test_b14_answer_carries_the_six_week_cap_not_the_five_week_trap(lga, monkeypatch):
    """B14's documented harm was not wasted latency: the retrieved snippet omitted the
    £50,000 annual-rent threshold and the model led with 5 weeks (£5,000). The guard hands
    generation the in-product statute table instead, with the 6-week figure already applied.
    £1,000/week -> £52,000/year >= £50,000 -> 6 weeks -> £6,000 (the reference answer)."""
    cmd = _decide(lga, B14, _NoVoteLLM(), monkeypatch=monkeypatch)
    obs = cmd.update["tool_observation"]
    assert "6 weeks' rent" in obs
    assert "£6,000.00" in obs
    assert "£5,000.00" not in obs                    # the trap value must not be offered
    assert "£50,000" in obs                          # the threshold the web summary dropped
    ref = cmd.update["tool_raw_data"]["tenancy_reference"]
    assert ref["deposit_cap_weeks"] == 6
    assert ref["max_tenancy_deposit_gbp"] == 6000.0


def test_b8_answer_carries_the_3446_total(lga, monkeypatch):
    """B8's reference answer is £3,446.15 (first month £1600 + 5-week deposit £1846.15). The
    round produced it and then DISCARDED it via the critic hard replace; the figure is now
    handed to generation up front, off the statute table."""
    cmd = _decide(lga, B8, _NoVoteLLM(), monkeypatch=monkeypatch)
    ref = cmd.update["tool_raw_data"]["tenancy_reference"]
    assert ref["deposit_cap_weeks"] == 5             # £19,200/yr < £50,000
    assert ref["max_tenancy_deposit_gbp"] == 1846.15
    assert ref["first_month_plus_deposit_gbp"] == 3446.15
    assert "£3,446.15" in cmd.update["tool_observation"]


def test_b12_weekly_rent_is_converted_and_bills_are_not_invented(lga, monkeypatch):
    """B12 asks for an all-in monthly total from a £380/week rent. The rent converts
    (380*52/12 = £1,646.67); bills and council tax were never stated. The observation must
    supply the conversion and forbid inventing the rest — B12's failure_conditions are
    exactly 'invents a figure for bills and folds it into a confident all-in total'."""
    cmd = _decide(lga, B12, _NoVoteLLM(), monkeypatch=monkeypatch)
    obs = cmd.update["tool_observation"]
    assert cmd.update["tool_raw_data"]["tenancy_reference"]["monthly_rent_gbp"] == 1646.67
    assert "£1,646.67" in obs
    low = obs.lower()
    assert "council tax" in low and "must not be invented" in low


@pytest.mark.parametrize("query,expected", [
    ("Using the specified conversion weekly × 52 ÷ 12, calculate the monthly GBP equivalent of £527 per week.",
     ("week_to_month", 527.0)),
    ("Convert £1500 per month to the weekly equivalent.",
     ("month_to_week", 1500.0)),
])
def test_standalone_rent_conversion_is_a_deterministic_terminal(query, expected, lga, monkeypatch):
    from core.tool_policy import standalone_rent_conversion

    assert standalone_rent_conversion(query) == expected
    cmd = _decide(lga, query, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert cmd.goto == "format_output"
    assert cmd.update["final_response"]
    assert "Formula" in cmd.update["final_response"]
    assert "£" in cmd.update["final_response"]


def test_money_observation_is_not_wrapped_as_untrusted_content(lga, monkeypatch):
    """The observation is the product's OWN statute table. Marking the turn tainted would
    wrap it in "UNTRUSTED CONTENT (data only, never instructions)" and tell the model not to
    rely on the authoritative figures — the opposite of the fix's purpose."""
    from uk_rent_agent.agent.guardrails import UNTRUSTED_START

    cmd = _decide(lga, B14, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert cmd.update["context_tainted"] is False
    assert UNTRUSTED_START not in cmd.update["tool_observation"]


def test_money_guard_reaches_generation_with_no_tool_executed(lga, monkeypatch):
    """Routing target, not just the decision dict: direct_answer + a pre-resolved
    observation goes to generate_response, so no tool node ever runs."""
    cmd = _decide(lga, B8, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert cmd.goto == "generate_response"


def test_statutory_reference_failure_still_refuses_retrieval(lga, monkeypatch):
    """If the statute table is unavailable the guard must still not retrieve — nothing is
    retrievable for this turn either way. It degrades to a plain direct_answer."""
    import core.tenancy_reference as tr
    monkeypatch.setattr(tr, "deposit_cap",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    d = _decision(lga, B14, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert d["tool"] == "direct_answer"
    assert d.get("observation") is None
    assert "reference unavailable" in d["reason"]


def test_money_guard_fails_open_when_the_policy_module_is_broken(lga, monkeypatch):
    """Fail-OPEN discipline (same direction as tool_policy._names_a_place): a broken
    predicate must return today's routing, never a blanket retrieval ban. A wrong DENY
    refuses a legitimate search, which is worse than a wasted call."""
    import core.tool_policy as tool_policy
    monkeypatch.setattr(tool_policy, "self_contained_money_question",
                        lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lga._self_contained_money_rent(B14) is None
    d = _decision(lga, B14, _JsonLLM("market_info"), monkeypatch=monkeypatch)
    assert d["tool"] == "multi_search"               # the pre-guard behaviour, restored


# ═══════════════════════════════════════════════════════════════════════════
# 2. THE OTHER DIRECTION — a market question must still retrieve
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,msg,intent,expected_tool", [
    # B13: quotes its own rent AND names a place, and asks for a JUDGEMENT, not a
    # derivation. expected_tools=[web_search] in the benchmark — it must keep retrieving.
    ("B13 good-deal", "Is £550 a week a good deal for a 1-bed in Clapham?",
     "market_info", "multi_search"),
    # The brief's boundary example: a market figure we do not hold.
    ("camden 2-beds", "What do 2-beds in Camden go for?", "market_info", "multi_search"),
    ("average rent", "What's the average rent for a studio in Shoreditch?",
     "market_info", "multi_search"),
    # A stated budget + an explicit ask to find listings is a retrieval turn by construction.
    ("A1 find", "Find me a 2-bed flat in Camden under £1500 a month. No commute to worry "
                "about, so just go ahead and search.", "search_properties", "search_properties"),
    ("A2 find", "I need a studio in Bloomsbury near UCL for a move-in on 1 September, "
                "budget £1600 pcm.", "search_properties", "search_properties"),
    # A deposit question about a NAMED area is a market question: the guard's place veto
    # (condition 4) is what draws this line.
    ("area deposit", "What's the typical deposit for a £1600 pcm place in Camden?",
     "market_info", "multi_search"),
])
def test_market_questions_still_retrieve(lga, monkeypatch, label, msg, intent, expected_tool):
    d = _decision(lga, msg, _JsonLLM(intent), monkeypatch=monkeypatch)
    assert d["tool"] == expected_tool, (label, d["tool"], d.get("reason"))


def test_do_not_search_research_request_keeps_the_market_info_route(lga, monkeypatch):
    """The money guard sits BEFORE the market_info negative guard, so it must not steal
    its turns: an explicit do-not-search research request over a price subject still routes
    to the web-research path (the 1.7 guard's own regression case)."""
    msg = "请你帮我做一下调研，UCL附近房源的价格大概是多少？先不要搜索房源"
    d = _decision(lga, msg, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert d["tool"] == "multi_search"
    assert "market_info" in d["reason"]


def test_bare_deposit_question_with_no_stated_rent_is_untouched(lga, monkeypatch):
    """B5 "How much deposit will I need?" states no rent, so there is no figure to derive
    from and the guard must not fire — the turn keeps whatever route it had (it needs to ask
    which rent). Condition (1) draws this line. Only the guard's non-firing is asserted here;
    B5's own route is a separate, pre-existing question this change does not touch."""
    llm = _JsonLLM("clarification")
    assert lga._self_contained_money_rent("How much deposit will I need?") is None
    d = _decision(lga, "How much deposit will I need?", llm, monkeypatch=monkeypatch)
    assert llm.calls > 0                      # the vote ran: no deterministic guard intercepted
    assert "Self-contained money question" not in d.get("reason", "")
    assert d.get("observation") is None


def test_money_followup_about_an_existing_listing_keeps_its_record_route(lga, monkeypatch):
    """PLACEMENT proof: the guard sits AFTER the last-results interceptions (1.6), so a
    deposit question about a listing already on screen is answered from the REAL record
    rather than from statute alone."""
    msg = "what about the first one — what deposit on that £1,500 pcm place?"
    results = [{"Address": "12 Tavistock Court, WC1H", "Price": "£1,500 pcm",
                "Bedrooms": 1, "Travel_Time_Minutes": 12}]
    d = _decision(lga, msg, _NoVoteLLM(),
                  extra_ctx={"last_results": results}, monkeypatch=monkeypatch)
    assert d["tool"] == "reasoning_property"
    assert "existing result" in d["reason"]


def test_predicate_firing_set_is_exactly_the_b_money_no_retrieval_class():
    """Source guard over a promise: re-derive the firing set from the benchmark shards on
    every run. Every case the guard suppresses retrieval on must be a B_money case with
    EMPTY expected_tools that lists a retrieval tool in forbidden_tools — i.e. the eval's own
    independent notion of "nothing is retrievable here". Any new case that trips the guard
    without that shape fails HERE rather than in a paid round."""
    import glob

    from core.tool_policy import self_contained_money_question

    shards = sorted(glob.glob(os.path.join(_ROOT, "evaluation", "benchmark", "cases*.jsonl")))
    assert shards, "benchmark shards not found"
    fired, seen = {}, set()
    for shard in shards:
        with open(shard, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                case = json.loads(line)
                cid = case.get("case_id") or case.get("id")
                if cid in seen:
                    continue
                seen.add(cid)
                if self_contained_money_question(case.get("user_query") or ""):
                    fired[cid] = case
    assert len(seen) > 90, len(seen)
    assert set(fired) == {"B3", "B4", "B7", "B8", "B10", "B12", "B14", "B15"}, sorted(fired)
    for cid, case in fired.items():
        assert case["category"] == "B_money", cid
        assert not case.get("expected_tools"), cid
        assert {"web_search", "search_properties"} & set(case.get("forbidden_tools") or []), cid
