"""Refinement-in-place: a follow-up that NARROWS the listings already on screen is
served from the previous result set instead of re-running search_properties.

THE REPORTED DEFECT (product owner, two-turn session)
    turn 1  "find something between KCL and Imperial, £2500, within 30 min,
             not ground floor"                       -> a search ran, six listings
    turn 2  "drop anything over £2000, then sort the rest by distance to the tube"

    Turn 2 needs no new data at all. Before this change it had exactly two possible
    outcomes, both wrong:
      * the LLM router voted search_properties -> a live scrape + embeddings + FAISS +
        commute pass, and the panel repainted with a DIFFERENT set than the prose
        discussed; or
      * it voted listing_advice / direct_answer -> the turn returned a `chat` payload
        with no `recommendations` key at all, so unified-ui.html never called
        paintResults() and the panel kept rendering the pre-refinement six while the
        prose said only some of them qualified.
    Note that the deterministic step-1.5 interceptions could not save it either:
    _is_comparative_followup / _is_detail_followup / _is_advice_followup all bail on
    _LOCATION_INTENT_KWS, and "tube" is in that list.

WHAT IS PINNED HERE
    §0  the pure narrowing parser/applier (core.refine_results)
    §1  the regression itself — turn 2 routes to refine_results, never search_properties
    §2  the guards: a widening, a changed area, an explicit new search, an unsupported
        sort on its own, and a filter that would empty the panel all still reach a real
        search
    §3  panel fidelity — the refined set rides back out in tool_data so /api/alex emits
        a `search` payload, and refinement operates on the FULL shown list rather than
        the 6-row prompt digest
    §4  end to end over the compiled graph with every LLM stubbed: search_properties is
        never executed and the rendered set equals the filter

No live LLM call anywhere; the classifier stub in §1 raises if the vote is reached, which
is what proves the deterministic interception fired first.
"""

import asyncio
import json
import os
import sys
import types

# --- Pin the real source roots ahead of tests/ (stale shadow copies of `core` live
# under tests/ and would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core import refine_results as rr


# ── the six listings from the reported session ────────────────────────────────
def _six():
    return [
        {"name": "Tavistock Court", "address": "Tavistock Court, Bloomsbury, WC1H",
         "price": "£1,950/month", "travel_time": "18 min to KCL", "bedrooms": 1,
         "property_type": "Flat", "area": "Bloomsbury", "images": ["a.jpg"], "score": 91,
         "url": "https://onthemarket.com/details/1/", "description": "Second floor flat."},
        {"name": "Maple House", "address": "Maple House, South Kensington, SW7",
         "price": "£2,400/month", "travel_time": "12 min to Imperial", "bedrooms": 1,
         "property_type": "Studio", "area": "South Kensington", "images": ["b.jpg"],
         "score": 88, "url": "https://onthemarket.com/details/2/",
         "description": "Studio apartment, third floor."},
        {"name": "Gower Mews", "address": "Gower Mews, Bloomsbury, WC1E",
         "price": "£2,100/month", "travel_time": "9 min to KCL", "bedrooms": 2,
         "property_type": "Flat", "area": "Bloomsbury", "images": ["c.jpg"], "score": 84,
         "url": "https://onthemarket.com/details/3/", "description": "Two bed, first floor."},
        {"name": "Elm Court", "address": "Elm Court, Pimlico, SW1V",
         "price": "£1,800/month", "travel_time": "25 min to Imperial", "bedrooms": 1,
         "property_type": "En-suite Room", "area": "Pimlico", "images": ["d.jpg"],
         "score": 80, "url": "https://onthemarket.com/details/4/",
         "description": "En-suite room in a modern block."},
        {"name": "Rosewood", "address": "Rosewood, Camden, NW1",
         "price": "£2,500/month", "travel_time": "30 min to KCL", "bedrooms": 3,
         "property_type": "Flat", "area": "Camden", "images": ["e.jpg"], "score": 76,
         "url": "https://onthemarket.com/details/5/", "description": "Three bed maisonette."},
        {"name": "Kings Wharf", "address": "Kings Wharf, Southwark, SE1",
         "price": "£1,700/month", "travel_time": "14 min to KCL", "bedrooms": 1,
         "property_type": "Shared", "area": "Southwark", "images": ["f.jpg"], "score": 70,
         "url": "https://onthemarket.com/details/6/", "description": "Room in a flat share."},
    ]


TURN2 = "drop anything over £2000, then sort the rest by distance to the tube"
# The three of the six that cost £2000/month or less.
UNDER_2000 = ["Tavistock Court", "Elm Court", "Kings Wharf"]


def _names(records):
    return [r["name"] for r in records]


# ═══════════════════════════════════════════════════════════════════════════
# §0  the pure parser / applier
# ═══════════════════════════════════════════════════════════════════════════
def test_reported_turn2_parses_as_a_price_narrowing():
    plan = rr.plan_refinement(TURN2, _six())
    assert plan is not None, "the reported turn 2 must be recognised as a narrowing"
    spec, kept = plan
    assert spec["filters"] == [{"kind": "max_price", "value": 2000}]
    assert _names(kept) == UNDER_2000


def test_unsupported_sort_key_is_reported_never_faked():
    # "distance to the tube" is not derivable from a cached listing record. The spec must
    # say so (the observation turns it into an explicit "NOT DONE" note) and must NOT
    # invent an ordering.
    spec, kept = rr.plan_refinement(TURN2, _six())
    assert spec["sort"] is None
    assert spec["unsupported_sort"] == "distance to the tube"
    # Surviving listings keep their previous relative order — nothing was re-sorted.
    assert _names(kept) == UNDER_2000


def test_unsupported_sort_alone_is_not_a_refinement():
    # Nothing can be served from cache, so this must fall through to normal routing.
    assert rr.plan_refinement("sort them by distance to the tube", _six()) is None


@pytest.mark.parametrize("msg,expected", [
    ("under £2000 please", UNDER_2000),
    ("no more than 2000", UNDER_2000),
    ("keep the ones below £2000", UNDER_2000),
    ("把超过2000的去掉", UNDER_2000),
    ("2000以下的", UNDER_2000),
    ("预算降到2000", UNDER_2000),
])
def test_budget_cap_phrasings(msg, expected):
    plan = rr.plan_refinement(msg, _six())
    assert plan is not None, msg
    assert _names(plan[1]) == expected


def test_price_floor_is_also_a_narrowing():
    plan = rr.plan_refinement("drop anything under £2000", _six())
    assert plan is not None
    assert _names(plan[1]) == ["Maple House", "Gower Mews", "Rosewood"]


@pytest.mark.parametrize("msg,key,first", [
    ("sort them by price", "price", "Kings Wharf"),
    ("按价格排序", "price", "Kings Wharf"),
    ("sort the results by commute time", "commute", "Gower Mews"),
    ("order them by price, highest first", "price", "Rosewood"),
])
def test_supported_sorts(msg, key, first):
    plan = rr.plan_refinement(msg, _six())
    assert plan is not None, msg
    spec, kept = plan
    assert spec["sort"]["key"] == key
    assert kept[0]["name"] == first
    assert len(kept) == 6                      # a re-sort drops nothing


@pytest.mark.parametrize("msg,expected", [
    ("just the top 3", ["Tavistock Court", "Maple House", "Gower Mews"]),
    ("前三个", ["Tavistock Court", "Maple House", "Gower Mews"]),
])
def test_top_n(msg, expected):
    plan = rr.plan_refinement(msg, _six())
    assert plan is not None, msg
    assert _names(plan[1]) == expected


def test_room_type_polarity():
    keep = rr.plan_refinement("only the ensuite ones", _six())
    assert keep is not None and _names(keep[1]) == ["Elm Court"]
    drop = rr.plan_refinement("drop the shared ones", _six())
    assert drop is not None and "Kings Wharf" not in _names(drop[1])


def test_an_earlier_clause_cannot_flip_room_type_polarity():
    # The "no" in "no more than £2000" must not turn "keep the ensuite ones" into an
    # exclusion — the exclusion cue window is deliberately short.
    plan = rr.plan_refinement("no more than £2000 and keep the ensuite ones", _six())
    assert plan is not None
    assert _names(plan[1]) == ["Elm Court"]


def test_bedroom_subset():
    plan = rr.plan_refinement("at least 2 bedrooms", _six())
    assert plan is not None
    assert _names(plan[1]) == ["Gower Mews", "Rosewood"]


def test_area_subset_uses_the_shown_sets_own_labels():
    plan = rr.plan_refinement("only the Bloomsbury ones", _six())
    assert plan is not None
    assert _names(plan[1]) == ["Tavistock Court", "Gower Mews"]


def test_two_different_areas_in_one_message_is_declined():
    # Half-applying it would silently drop listings the user asked to keep.
    assert rr.plan_refinement("keep the Bloomsbury and Camden ones", _six()) is None


def test_an_unreadable_price_is_kept_not_silently_dropped():
    recs = _six()
    recs[0]["price"] = "POA"
    spec, kept = rr.plan_refinement("under £2000", recs)
    assert "Tavistock Court" in _names(kept)


def test_summary_is_countable_and_admits_what_it_could_not_do():
    spec, kept = rr.plan_refinement(TURN2, _six())
    text = rr.summarize_refinement(spec, _six(), kept, language="en")
    assert "without running a new search" in text
    assert "at or under £2000/month" in text
    assert "3 removed, 3 remain" in text
    assert "£1700–£1950/month" in text          # the range of what actually survived
    assert "distance to the tube" in text and "isn't in the listing data" in text
    assert "on the right" in text


def test_summary_is_localized():
    spec, kept = rr.plan_refinement(TURN2, _six())
    zh = rr.summarize_refinement(spec, _six(), kept, language="zh")
    assert "未重新搜索" in zh
    assert "剩余 3 套" in zh
    assert "无法按" in zh                        # the same honesty clause, in Chinese
    assert "见右侧" in zh
    assert "removed" not in zh                  # no language mixing


def test_summary_never_claims_a_sort_it_did_not_do():
    # A supported sort IS claimed...
    spec, kept = rr.plan_refinement("sort them by price", _six())
    assert "re-sorted by price" in rr.summarize_refinement(spec, _six(), kept)
    # ...and an unsupported one never is.
    spec, kept = rr.plan_refinement(TURN2, _six())
    assert "re-sorted" not in rr.summarize_refinement(spec, _six(), kept)


# ── guards: these are NOT narrowings ─────────────────────────────────────────
@pytest.mark.parametrize("msg", [
    "up to £3000",                       # widening: removes nothing from this set
    "raise the budget to 3000",
    "any price",                         # budget cleared
    "预算不限",
    "find me something in Camden instead",
    "show me more options",
    "搜索房源",
    "just the ones in Manchester",       # an area the shown set does not cover
    "which of these is cheapest?",       # a question about the set, not a change to it
    "tell me more about the second one",
    "is the second one near a tube station?",
    "under £500",                        # would empty the panel
    "top 10",                            # no-op over six listings
])
def test_not_a_refinement(msg):
    assert rr.plan_refinement(msg, _six()) is None, msg


def test_no_previous_results_means_no_refinement():
    assert rr.plan_refinement(TURN2, []) is None


# ═══════════════════════════════════════════════════════════════════════════
# graph-level fixtures
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info", "check_safety"]

    def get(self, name):
        return None


class _JsonLLM:
    def __init__(self, intent):
        self.intent = intent

    def invoke(self, prompt):
        return types.SimpleNamespace(content=json.dumps({"intent": self.intent}))


class _NoVoteLLM:
    """Fails if the LLM vote is reached — proves the deterministic interception fired."""

    def invoke(self, prompt):
        raise AssertionError("the refinement interception must route before the LLM vote")


def _decide(lga, msg, llm, extra_ctx=None, accumulated=None):
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {"user_query": msg, "extracted_context": ec,
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


# ═══════════════════════════════════════════════════════════════════════════
# §1  THE REGRESSION — turn 2 must not start a second search
# ═══════════════════════════════════════════════════════════════════════════
def test_turn2_narrowing_routes_to_refinement_not_search(lga):
    cmd = _decide(lga, TURN2, _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    decision = cmd.update["tool_decision"]

    assert decision["tool"] == lga.REFINE_TOOL_NAME
    assert decision["tool"] != "search_properties"
    # Straight to the formatter: no execute_tool (so no scrape / embedding / FAISS) and no
    # generate_response (so not even an answer-generation LLM call).
    assert cmd.goto == "format_output"
    assert cmd.update.get("context_tainted") is True   # listing text stays untrusted


def test_turn2_carries_the_filtered_listings_for_the_panel(lga):
    cmd = _decide(lga, TURN2, _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    raw = cmd.update["tool_raw_data"]
    assert _names(raw["recommendations"]) == UNDER_2000
    assert raw["refinement"]["previous_count"] == 6
    assert raw["refinement"]["kept_count"] == 3


def test_turn2_carries_no_observation_because_nothing_is_generated(lga):
    # The old design handed a pre-resolved observation to generate_response. The answer is
    # now composed from the refined set itself, so there is no prompt and no LLM hop at all.
    cmd = _decide(lga, TURN2, _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    assert "tool_observation" not in cmd.update
    assert cmd.update["tool_raw_data"]["refinement"]["kept_count"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# §2  the guards still reach a real search / the existing routes
# ═══════════════════════════════════════════════════════════════════════════
def test_widening_still_reaches_the_router(lga):
    # "up to £3000" removes nothing from a set topping out at £2500 — the user wants
    # options we do not hold, which only a fresh search can supply.
    cmd = _decide(lga, "actually make it up to £3000", _JsonLLM("search_properties"),
                  extra_ctx={"last_results_full": _six()})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_changed_area_still_reaches_the_router(lga):
    cmd = _decide(lga, "same budget but in Manchester", _JsonLLM("search_properties"),
                  extra_ctx={"last_results_full": _six()})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_comparative_followup_keeps_its_existing_route(lga):
    cmd = _decide(lga, "which of these is the cheapest?", _NoVoteLLM(),
                  extra_ctx={"last_results": _six(), "last_results_full": _six()})
    decision = cmd.update["tool_decision"]
    assert decision["tool"] == "direct_answer"
    assert decision["tool"] != lga.REFINE_TOOL_NAME


def test_detail_followup_keeps_its_existing_route(lga):
    cmd = _decide(lga, "tell me more about the second one", _NoVoteLLM(),
                  extra_ctx={"last_results": _six(), "last_results_full": _six()})
    assert cmd.update["tool_decision"]["tool"] == "reasoning_property"


def test_no_previous_listings_means_no_interception(lga):
    cmd = _decide(lga, TURN2, _JsonLLM("search_properties"))
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


# ═══════════════════════════════════════════════════════════════════════════
# §3  panel fidelity
# ═══════════════════════════════════════════════════════════════════════════
def test_refinement_uses_the_full_shown_list_not_the_six_row_digest(lga):
    """extracted_context['last_results'] is truncated to 6 by app._build_results_context.
    Narrowing over that digest would drop listings 7..N from the panel, so the graph must
    prefer the full list app.py stashes under last_results_full."""
    full = _six() + [
        {"name": "Seventh Place", "address": "Seventh Place, Bloomsbury",
         "price": "£1,600/month", "travel_time": "20 min to KCL", "bedrooms": 1,
         "area": "Bloomsbury", "property_type": "Flat"},
        {"name": "Eighth Court", "address": "Eighth Court, Camden",
         "price": "£1,650/month", "travel_time": "22 min to KCL", "bedrooms": 1,
         "area": "Camden", "property_type": "Flat"},
    ]
    pool = lga._refinable_previous_results(
        {"last_results": _six(), "last_results_full": full})
    assert len(pool) == 8

    cmd = _decide(lga, "under £2000", _NoVoteLLM(),
                  extra_ctx={"last_results": _six(), "last_results_full": full})
    kept = _names(cmd.update["tool_raw_data"]["recommendations"])
    assert "Seventh Place" in kept and "Eighth Court" in kept
    assert len(kept) == 5


def test_refinement_falls_back_to_the_digest_when_the_full_list_is_gone(lga):
    # e.g. after a process restart, where only the persisted turn snapshot survived.
    pool = lga._refinable_previous_results({"last_results": _six()})
    assert len(pool) == 6
    cmd = _decide(lga, "under £2000", _NoVoteLLM(), extra_ctx={"last_results": _six()})
    assert _names(cmd.update["tool_raw_data"]["recommendations"]) == UNDER_2000


def test_refined_records_keep_every_card_field(lga):
    """The panel renders images / url / score straight off these records; a refinement
    must hand back the ORIGINAL records, not a reduced projection of them."""
    cmd = _decide(lga, "under £2000", _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    first = cmd.update["tool_raw_data"]["recommendations"][0]
    assert first["images"] == ["a.jpg"]
    assert first["url"].endswith("/1/")
    assert first["score"] == 91


def test_format_output_hands_the_refined_set_back_to_the_frontend(lga):
    """This is the bit that makes the panel repaint: /api/alex returns the `search`
    payload (and therefore calls paintResults) only when tool_data carries
    recommendations. Without it the turn is a `chat` payload and the panel keeps
    showing the pre-refinement six."""
    spec, kept = rr.plan_refinement(TURN2, _six())
    decision = lga._build_refinement_decision(_six(), spec, kept)
    node = lga._make_format_output_node()
    out = node({
        "tool_decision": {k: decision[k] for k in ("tool", "params", "reason")},
        "tool_raw_data": decision["raw_data"],
        "final_response": "",
        "user_preferences": {},
        "accumulated_search_criteria": {"area": "London", "max_budget": 2000,
                                        "criteria_gate_shown": True},
        "extracted_context": {"reply_language": "en"},
        "observations": [],
    })
    tool_data = out["tool_data"]
    assert _names(tool_data["recommendations"]) == UNDER_2000
    # The answer is composed here, from the same list that is going to the panel.
    assert "3 removed, 3 remain" in out["final_response"]
    for name in ("Maple House", "Gower Mews", "Rosewood"):
        assert name not in out["final_response"]
    # The criteria panel mirrors the tightened budget instead of being reset by an
    # empty dict, and internal bookkeeping stays server-side.
    assert tool_data["search_criteria"]["max_budget"] == 2000
    assert tool_data["search_criteria"]["areas"] == ["London"]
    assert "criteria_gate_shown" not in tool_data["search_criteria"]
    assert tool_data["refinement"]["kept_count"] == 3
    # The pre-refinement records are provenance for the formatter, not payload.
    assert "previous" not in tool_data["refinement"]


def test_app_feeds_the_full_shown_list_into_the_graph_context():
    """app.handle_with_react_agent already snapshots the complete recommendation list
    under the turn lock for focus resolution; the refinement path reads the same
    snapshot. Pinning the wiring here because the graph cannot see app-side state."""
    path = os.path.join(_ROOT, "app", "app.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("async def handle_with_react_agent"):
               src.index("# Deterministic direct-search endpoint")]
    assert "extracted_context['last_results_full'] = last_results_snapshot" in body
    # It must NOT be persisted or echoed to the client.
    assert "last_results_full" not in src[:src.index("async def handle_with_react_agent")]
    from core.context_assembler import build_turn_snapshot
    snap = build_turn_snapshot(
        turn_id="t1",
        persistent_state={"extracted_context": {"last_results": _six(),
                                                "last_results_full": _six()}},
        context_revision=1)
    assert "last_results_full" not in snap


def test_the_narrowed_pool_is_preference_filtered_before_it_is_counted(lga):
    """format_refinement_output re-applies apply_preference_filter to the refined set, and
    the summary reports counts from that same list. Narrowing an UNFILTERED pool would make
    the "N remain" claim disagree with the panel beside it."""
    recs = _six()
    recs[0]["address"] = "Tavistock Court, Brent Cross, NW4"   # the one excluded area
    node = lga._make_decide_tool_node(_DummyRegistry(), _NoVoteLLM())
    cmd = node({
        "user_query": "under £2000",
        "extracted_context": {"current_message": "under £2000", "last_results_full": recs},
        "accumulated_search_criteria": {},
        "user_preferences": {"excluded_areas": ["Brent Cross"]},
    })
    raw = cmd.update["tool_raw_data"]
    assert _names(raw["recommendations"]) == ["Elm Court", "Kings Wharf"]
    assert raw["refinement"]["previous_count"] == 5            # not 6: Brent Cross is gone
    response, tool_data = lga.format_refinement_output(
        raw, {"excluded_areas": ["Brent Cross"]}, {}, "en")
    assert len(tool_data["recommendations"]) == 2
    assert "3 removed, 2 remain" in response


# ═══════════════════════════════════════════════════════════════════════════
# §4  end to end over the compiled graph — search_properties never executes
# ═══════════════════════════════════════════════════════════════════════════
class _SearchCountingRegistry:
    """Records every tool execution. search_properties raises outright: reaching it at
    all on a pure narrowing is the defect, and a live search here would scrape."""

    def __init__(self):
        self.calls = []

    def list_tool_names(self):
        return ["search_properties", "web_search", "check_safety"]

    def get(self, _name):
        return types.SimpleNamespace(version="1", side_effect="none")

    async def execute_tool(self, name, **_kw):
        self.calls.append(name)
        if name == "search_properties":
            raise AssertionError(
                "search_properties must not run for a pure narrowing of the shown set")
        from core.tool_system import ToolResult
        return ToolResult(success=True, data={"results": "OBS"}, tool_name=name)


class _GenLLM:
    """Records every generation call. On a refinement there must be none."""

    def __init__(self):
        self.prompts = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return types.SimpleNamespace(content="generated prose")


def test_end_to_end_narrowing_never_touches_the_search_tool(lga, monkeypatch):
    from core import llm_config
    from uk_rent_agent.agent.state import create_initial_state

    registry = _SearchCountingRegistry()
    gen = _GenLLM()
    # If the router were reached it would vote for a search; the deterministic
    # interception must beat it.
    monkeypatch.setattr(llm_config, "get_classification_llm",
                        lambda: _JsonLLM("search_properties"))
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: gen)
    graph = lga.build_agent_graph(registry)

    state = create_initial_state(
        TURN2,
        extracted_context={"current_message": TURN2, "last_results": _six(),
                           "last_results_full": _six(), "reply_language": "en"},
        accumulated_search_criteria={"area": "London", "max_budget": 2000},
        user_id="u", session_id="c")
    out = asyncio.run(graph.ainvoke(
        state, config={"recursion_limit": lga.GRAPH_RECURSION_LIMIT}))

    assert registry.calls == [], f"no tool may execute, saw {registry.calls}"
    assert gen.prompts == [], "a refinement must not cost an answer-generation call"
    # The panel payload the frontend will repaint from — this is what makes /api/alex
    # return response_type == "search" and the panel repaint.
    assert _names(out["tool_data"]["recommendations"]) == UNDER_2000
    # The answer describes exactly that set and nothing else.
    assert "3 removed, 3 remain" in out["final_response"]
    for dropped in ("Maple House", "Gower Mews", "Rosewood"):
        assert dropped not in out["final_response"]


def test_end_to_end_widening_still_reaches_the_search_tool(lga, monkeypatch):
    """The other half of the contract: a request the cached set genuinely cannot serve
    must still run a real search."""
    from core import llm_config
    from uk_rent_agent.agent.state import create_initial_state

    class _Registry(_SearchCountingRegistry):
        async def execute_tool(self, name, **_kw):
            self.calls.append(name)
            from core.tool_system import ToolResult
            return ToolResult(success=True, data={"status": "found", "recommendations": [],
                                                  "results": "OBS"}, tool_name=name)

    registry = _Registry()
    monkeypatch.setattr(llm_config, "get_classification_llm",
                        lambda: _JsonLLM("search_properties"))
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: _GenLLM())
    graph = lga.build_agent_graph(registry)

    msg = "actually raise the budget to £3000"
    state = create_initial_state(
        msg,
        extracted_context={"current_message": msg, "last_results": _six(),
                           "last_results_full": _six(), "reply_language": "en"},
        accumulated_search_criteria={"area": "London"},
        user_id="u", session_id="c")
    asyncio.run(graph.ainvoke(state, config={"recursion_limit": lga.GRAPH_RECURSION_LIMIT}))
    assert "search_properties" in registry.calls


# ═══════════════════════════════════════════════════════════════════════════
# §5  fc_loop — THE ARCH ACTUALLY SERVING PUBLIC TRAFFIC
# ---------------------------------------------------------------------------
# The public edge was cut over to fc_loop on 2026-07-26 (deploy/switch_pool.sh;
# deploy/monitoring/rentcompass-monitor.sh now defaults MON_EXPECTED_PUBLIC_ARCH to
# fc_loop). A fix that only lands in the legacy graph changes nothing for a real user,
# so the interception is pinned on BOTH architectures — and pinned to produce the
# IDENTICAL answer, because legacy is the rollback target and a rollback must not
# change what the product says.
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def fc():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.agent_loop")


class _FcSpec:
    def __init__(self, name, side_effect="none", terminal=False):
        self.name = name
        self.description = "desc"
        self.input_schema = {"type": "object", "properties": {}}
        self.side_effect = side_effect
        self.retry_safe = True
        self.version = "1"
        self.terminal = terminal


class _FcProvider:
    """Records every tool execution; search_properties raises, since reaching it at all
    on a pure narrowing is the defect."""

    def __init__(self):
        self.calls = []

    def list_specs(self):
        return [_FcSpec("search_properties"), _FcSpec("check_safety")]

    def get(self, name):
        return _FcSpec(name)

    async def execute_tool(self, name, **params):
        self.calls.append(name)
        if name == "search_properties":
            raise AssertionError(
                "search_properties must not run for a pure narrowing of the shown set")
        from core.tool_system import ToolResult
        return ToolResult(True, {"ok": True}, tool_name=name)


class _FcNoCallChat:
    """The bound-tools model. Any call at all fails: the interception is pre-loop, so the
    turn must be answered without ever reaching the LLM."""

    def bind_tools(self, tools, **kw):
        return self

    async def ainvoke(self, messages):
        raise AssertionError("fc_loop must not make an LLM call for a pure narrowing")


def _fc_state(msg, **over):
    st = {
        "user_query": msg,
        "extracted_context": {"current_message": msg, "reply_language": "en",
                              "last_results": _six(), "last_results_full": _six()},
        "accumulated_search_criteria": {"area": "London", "max_budget": 2000},
        "user_preferences": {"excluded_areas": []},
        "session_id": "s1", "run_id": "r1", "loop_turn": 0,
        "messages": [], "tool_artifacts": [], "context_tainted": False,
        "final_response": "", "response_type": "answer",
    }
    st.update(over)
    return st


def _fc_drive(fc, provider, state, chat=None):
    """guard -> ... -> format_output_fc, mirroring tests/test_fc_loop.py::_drive."""
    nodes = fc.build_fc_nodes(provider, agent_llm=chat or _FcNoCallChat())

    async def _go():
        name = "guard"
        while True:
            res = nodes[name](state)
            if asyncio.iscoroutine(res):
                res = await res
            state.update(res.update or {})
            goto = res.goto
            if goto in ("critic", "format_output_fc"):
                state.update(nodes["format_output_fc"](state))
                return state
            name = goto

    return asyncio.run(_go())


def test_fc_turn2_is_answered_without_a_search_and_without_an_llm_call(fc):
    provider = _FcProvider()
    out = _fc_drive(fc, provider, _fc_state(TURN2))

    assert provider.calls == [], f"no tool may execute, saw {provider.calls}"
    # The panel payload — this is what /api/alex turns into a `search` response.
    assert _names(out["tool_data"]["recommendations"]) == UNDER_2000
    assert out["response_type"] == "search"
    assert "3 removed, 3 remain" in out["final_response"]
    assert "distance to the tube" in out["final_response"]
    # Pre-loop: the bound-tools call never happened, so no batch was ever planned.
    assert out.get("loop_turn", 0) == 0
    assert out.get("tool_artifacts") == []


def test_fc_and_legacy_answer_a_refinement_identically(fc, lga):
    """Legacy is the rollback target. Rolling back must not change what the product says,
    so both formatters go through the same shared helper over the same payload."""
    fc_out = _fc_drive(_FcProvider() and fc, _FcProvider(), _fc_state(TURN2))

    cmd = _decide(lga, TURN2, _NoVoteLLM(),
                  extra_ctx={"last_results": _six(), "last_results_full": _six()},
                  accumulated={"area": "London", "max_budget": 2000})
    legacy_out = lga._make_format_output_node()({
        "tool_decision": cmd.update["tool_decision"],
        "tool_raw_data": cmd.update["tool_raw_data"],
        "final_response": "",
        "user_preferences": {},
        "accumulated_search_criteria": {"area": "London", "max_budget": 2000},
        "extracted_context": {"reply_language": "en"},
        "observations": [],
    })

    assert fc_out["final_response"] == legacy_out["final_response"]
    assert (_names(fc_out["tool_data"]["recommendations"])
            == _names(legacy_out["tool_data"]["recommendations"]))
    assert fc_out["tool_data"]["search_criteria"] == legacy_out["tool_data"]["search_criteria"]


def test_fc_reply_language_follows_the_conversation(fc):
    state = _fc_state("把超过2000的去掉")
    state["extracted_context"]["reply_language"] = "zh"
    out = _fc_drive(fc, _FcProvider(), state)
    assert "剩余 3 套" in out["final_response"]
    assert "removed" not in out["final_response"]


def test_fc_guard_order_fair_housing_still_wins(fc):
    """The refinement check sits AFTER the fair-housing refusal; a discriminatory message
    that also happens to carry a price cap must still be refused, not quietly filtered."""
    msg = "drop anything over £2000 and avoid areas with too many immigrants"
    out = _fc_drive(fc, _FcProvider(), _fc_state(msg))
    assert out["response_type"] == "clarification"
    assert "tool_data" in out and not out["tool_data"].get("recommendations")


@pytest.mark.parametrize("msg", [
    "actually raise the budget to £3000",   # widening
    "find me something in Manchester",      # new search + changed area
    "which of these is the cheapest?",      # a question about the set
])
def test_fc_non_refinements_still_reach_the_model(fc, msg):
    """The guard must not swallow anything that genuinely needs the loop. The model stub
    here answers in plain text, so reaching it is the assertion."""
    class _Chat:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kw):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            from langchain_core.messages import AIMessage
            return AIMessage(content="reached the model")

    chat = _Chat()
    out = _fc_drive(fc, _FcProvider(), _fc_state(msg), chat=chat)
    assert chat.calls == 1, msg
    assert not out["tool_data"].get("recommendations")


def test_fc_uses_the_full_shown_list_not_the_six_row_digest(fc):
    full = _six() + [
        {"name": "Seventh Place", "address": "Seventh Place, Bloomsbury",
         "price": "£1,600/month", "travel_time": "20 min to KCL", "bedrooms": 1,
         "area": "Bloomsbury", "property_type": "Flat"},
    ]
    state = _fc_state("under £2000")
    state["extracted_context"]["last_results_full"] = full
    out = _fc_drive(fc, _FcProvider(), state)
    assert "Seventh Place" in _names(out["tool_data"]["recommendations"])


def test_fc_dispatch_path_is_untouched(fc):
    """The interception is pre-loop by design, so it adds no second 'should this call run?'
    decision next to the dispatch-time gates in execute_tools."""
    import inspect
    src = inspect.getsource(fc.build_fc_nodes)
    guard_src = src[src.index("def guard_node"):src.index("async def _resolve_pending_memory")]
    exec_src = src[src.index("async def execute_tools_node"):
                   src.index("def format_output_fc_node")]
    assert "plan_refinement" in guard_src
    for token in ("plan_refinement", "refine_results", "build_refinement_raw_data",
                  "format_refinement_output"):
        assert token not in exec_src, f"{token} must not appear in the dispatch path"
