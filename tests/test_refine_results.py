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


def test_describe_refinement_is_plain_and_countable():
    spec, kept = rr.plan_refinement(TURN2, _six())
    text = rr.describe_refinement(spec, 6, len(kept))
    assert "3 removed" in text and "3 remain" in text
    assert "£2000" in text


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
    # A pre-resolved observation short-circuits straight to the answer generator, so no
    # tool node — and therefore no scrape / embedding / FAISS pass — can run.
    assert cmd.goto == "generate_response"
    assert cmd.update.get("context_tainted") is True   # listing text stays untrusted


def test_turn2_carries_the_filtered_listings_for_the_panel(lga):
    cmd = _decide(lga, TURN2, _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    raw = cmd.update["tool_raw_data"]
    assert _names(raw["recommendations"]) == UNDER_2000
    assert raw["refinement"]["previous_count"] == 6
    assert raw["refinement"]["kept_count"] == 3


def test_turn2_observation_matches_the_panel_and_flags_the_impossible_sort(lga):
    cmd = _decide(lga, TURN2, _NoVoteLLM(), extra_ctx={"last_results_full": _six()})
    obs = cmd.update["tool_observation"]
    # The evidence surface names exactly the listings the panel will show...
    for name in UNDER_2000:
        assert name in obs
    for dropped in ("Maple House", "Gower Mews", "Rosewood"):
        assert dropped not in obs
    # ...and tells the generator, in so many words, not to claim the tube ordering.
    assert "NOT DONE" in obs
    assert "distance to the tube" in obs
    assert "No new search was run" in obs


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
        "final_response": "Only three of the six are at or under £2000.",
        "user_preferences": {},
        "accumulated_search_criteria": {"area": "London", "max_budget": 2000,
                                        "criteria_gate_shown": True},
        "observations": [],
    })
    tool_data = out["tool_data"]
    assert _names(tool_data["recommendations"]) == UNDER_2000
    # The prose the generator wrote is the answer; format_output must not overwrite it
    # with a search card.
    assert out["final_response"] == "Only three of the six are at or under £2000."
    # The criteria panel mirrors the tightened budget instead of being reset by an
    # empty dict, and internal bookkeeping stays server-side.
    assert tool_data["search_criteria"]["max_budget"] == 2000
    assert tool_data["search_criteria"]["areas"] == ["London"]
    assert "criteria_gate_shown" not in tool_data["search_criteria"]
    assert tool_data["refinement"]["kept_count"] == 3


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


def test_prose_and_panel_see_the_same_preference_filtered_pool(lga):
    """format_output re-applies apply_preference_filter to the refined set. If the
    observation were built from an UNFILTERED pool the prose could name a listing the
    panel then removes, so the pool is preference-filtered before it is narrowed."""
    recs = _six()
    recs[0]["address"] = "Tavistock Court, Brent Cross, NW4"   # the one excluded area
    node = lga._make_decide_tool_node(_DummyRegistry(), _NoVoteLLM())
    cmd = node({
        "user_query": "under £2000",
        "extracted_context": {"current_message": "under £2000", "last_results_full": recs},
        "accumulated_search_criteria": {},
        "user_preferences": {"excluded_areas": ["Brent Cross"]},
    })
    kept = _names(cmd.update["tool_raw_data"]["recommendations"])
    assert kept == ["Elm Court", "Kings Wharf"]
    assert "Brent Cross" not in cmd.update["tool_observation"]


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
    """Answers from the observation it is handed (keeps the critic's grounding happy)."""

    def __init__(self):
        self.prompts = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return types.SimpleNamespace(
            content="Three of the six are at or under £2000: Tavistock Court "
                    "(£1,950/month), Elm Court (£1,800/month) and Kings Wharf "
                    "(£1,700/month). I cannot order them by distance to the tube — "
                    "that is not in the listing data I have.")


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
    # The panel payload the frontend will repaint from.
    assert _names(out["tool_data"]["recommendations"]) == UNDER_2000
    # ...and /api/alex turns exactly this into response_type == "search".
    assert out["tool_data"]["recommendations"]
    # The generator reasoned over the refined evidence, not the original six.
    assert "Maple House" not in gen.prompts[0]
    assert "Tavistock Court" in gen.prompts[0]


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
