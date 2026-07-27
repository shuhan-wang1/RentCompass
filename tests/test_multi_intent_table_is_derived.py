"""The FOURTH overlapping cue table — measured, and deliberately NOT merged.

PR #52 merged ``agent_loop._DIMENSION_CUES`` and ``langgraph_agent._SEARCH_DIMENSION_CUES`` into
``core.dimensions`` and declined the fourth, ``langgraph_agent._MULTI_INTENT_CUES``, on the
grounds that it "decides reflect-hop routing on every legacy turn — its own measured change".
This file is that measured change.

WHAT THE MEASUREMENT FOUND (2026-07-27, mainline 81aa7cf)

1. It is NOT a fourth copy. It is keyed by the eight LOOPABLE follow-up INTENTS; five of them
   (cost/transport/weather/details/web) name no user-visible dimension at all, and the
   dimension table's `listings` is deliberately absent from it. Whole-table overlap with
   DIMENSION_CUES was 16 of its 50 cues. Two tables, two taxonomies, two questions.

2. In the three rows that DO share a taxonomy row (safety, commute, poi ≡ nearby) it had
   drifted, and the drift was INTRA-arch, which is worse than the inter-arch drift PR #52
   fixed. On mainline, for "Is there a pharmacy within walking distance, and is the area safe?":

       legacy's post-search fan-out  (_cued_search_dimensions) -> ['safety', 'nearby']
       legacy's reflect/plan router  (_MULTI_INTENT_CUES)      -> ['safety']

   One arch, one sentence, two answers to "what did the user ask about". Because the router saw
   one intent, ``_current_message_has_multi_intent`` was False, reflect took the one-shot
   short-circuit after check_safety, and the pharmacy ask was dropped with nothing said. The
   cues responsible were the ones the dimension table had and this one did not: `pharmacy`,
   `药店`, `便利店`, `餐厅`, `周边`, `设施`, `grocery`, `amenit`, `poi`, `police`.

3. The reverse merge was REFUSED, with a measurement. Completing the union would push this
   table's intent-only cues (`gym`, `park`, `健身`, `多久`, `多远`, `距离`) into the dimension
   vocabulary, which BOTH arches read — fc, which holds the public edge, included. Measured with
   `park` in the nearby row:

       "Find me a 1-bed in Finsbury Park under £1600."  -> fc prints "Nearby amenities have not
       "Does the second one have parking?"                  been looked up yet.", legacy
                                                            dispatches a POI fan-out wave

   for a dimension neither user mentioned. The product already declares `park` an address word
   that "can be part of a real name" (tools/get_property_details.py ``_FILLER_TOKENS``). A cue
   that over-fires costs a ROUTER one reflect hop and costs a FETCHER a tool wave plus a false
   line in the answer, so the containment is ONE-WAY by design, not a union.

4. Routing effect of the chosen change, on the retained 98-case evidence (both arms rescored
   with the mainline grader + contract: fc 74/98, legacy 46/98, 0 digest mismatches), over all
   135 user turns: ZERO turns change their multi-intent verdict, ZERO change the plan trigger,
   ZERO lose the reflect short-circuit. One turn's plannable-intent set changes (D8 turn 1,
   "Is there a pharmacy within walking distance of that place?": {} -> {search_nearby_pois}),
   which does not reach the >= 2 threshold. The pool is silent on it; the defect in §2 is real
   regardless, and the containment makes the class of it impossible.

   The fc arm's effect is ZERO STRUCTURALLY, not statistically: the fc graph has no decide_tool
   and no reflect node, so nothing on that arch reads this table. Section 4 pins that, because
   it is the fact that bounds this change's blast radius to one arch.

WHICH TESTS IN SECTION 1 ARE RED ON MAINLINE: all of them.
"""
from __future__ import annotations

import inspect

import pytest

import core.agent_loop as agent_loop
import core.langgraph_agent as lga
from core import dimensions

# Which intent group is which dimension. Written as a literal HERE, rather than read from
# lga._INTENT_GROUP_DIMENSION, so that section 1 is COLLECTIBLE on mainline 81aa7cf where that
# name does not exist yet: the regression has to be red for a behaviour reason, not an
# AttributeError at import. Section 2 asserts the product agrees with this literal.
ALIGNED = {"safety": "safety", "commute": "commute", "poi": "nearby"}

# The cue the dimension table had that the router did not, with a sentence that pairs it with a
# DIFFERENT intent and a conjunction, so the message is genuinely two asks. On mainline every
# one of these routed as a single intent.
DROPPED_ASKS = [
    ("poi", "nearby", "pharmacy",
     "Is there a pharmacy within walking distance, and is the area safe?"),
    ("poi", "nearby", "药店", "第二个房源楼下有药店吗？治安怎么样？"),
    ("poi", "nearby", "便利店", "第二个房源楼下有便利店吗？治安怎么样？"),
    ("poi", "nearby", "amenit",
     "What amenities are around, and how is the crime rate?"),
    ("poi", "nearby", "grocery",
     "Is there a grocery store close by, and is it safe at night?"),
    ("poi", "nearby", "餐厅", "附有餐厅吗？治安好不好？"),
]


def _fanout_dims(message):
    """The dimensions LEGACY's post-search fan-out says this message asked about."""
    return [dim for dim, _tool in lga._cued_search_dimensions(message, set())]


def _router_groups(message):
    """The intent groups LEGACY's reflect/plan router says this message asked about."""
    return sorted(g for g, cues in lga._MULTI_INTENT_CUES.items()
                  if dimensions.cues_hit(cues, message))


def _dim_cues(dim):
    """A dimension's cue row read straight off DIMENSION_CUES. Section 1 uses this rather than
    the new ``dimensions.cues_for`` helper so it runs unchanged on mainline 81aa7cf and fails
    there on the VOCABULARY, not on a missing accessor."""
    return set(next(c for d, c, _t in dimensions.DIMENSION_CUES if d == dim))


# ═══════════════════════════════════════════════════════════════════
# 1. RED ON MAINLINE — one arch must not disagree with itself
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("group,dim,cue,message", DROPPED_ASKS,
                         ids=[c for _g, _d, c, _m in DROPPED_ASKS])
def test_the_router_never_knows_less_than_the_fanout(group, dim, cue, message):
    """THE regression. Legacy's fetcher and legacy's router read the same sentence; on mainline
    the fetcher saw the POI ask and the router did not, so reflect short-circuited and the ask
    was dropped in silence. Silence about a dimension the user asked about is the shape that
    lets an answer free-associate (HANDOFF §0)."""
    fanout = _fanout_dims(message)
    router = _router_groups(message)
    assert dim in fanout, f"{cue!r}: the fan-out no longer cues {dim} — fixture is stale"
    assert group in router, (
        f"cue {cue!r}: legacy's fan-out would fetch {fanout} but its router only sees {router} "
        f"— the {group} ask is dropped by the reflect short-circuit")


@pytest.mark.parametrize("group,dim", sorted(ALIGNED.items()))
def test_every_dimension_cue_is_recognised_by_the_router(group, dim):
    """The property behind section 1, stated once rather than per sentence: for a group that IS
    a dimension, the router's cue set must CONTAIN the dimension's. Red on mainline for safety
    (`police`, and `unsafe` only because `safe` subsumes it) and for poi (nine cues)."""
    missing = _dim_cues(dim) - set(lga._MULTI_INTENT_CUES[group])
    assert not missing, (
        f"{group}: the router does not recognise dimension cues {sorted(missing)}; a message "
        f"using one of them cues a {dim} fetch but no {group} intent")


# Messages where the SHARED table's `poi` cue is a false positive: `poi` is a substring of
# "appointment", "point" and "disappointing". This predates PR #52 (it was in fc's original
# _DIMENSION_CUES), and on mainline it already made BOTH arches cue `nearby` for these — fc
# printing its nearby-amenities line, legacy dispatching a POI fan-out wave.
SHARED_FALSE_POSITIVES = [
    "Can I book an appointment to view the second one?",
    "What's the point of that one?",
    "Is it a disappointing area?",
]


@pytest.mark.parametrize("message", SHARED_FALSE_POSITIVES)
def test_a_false_positive_is_shared_rather_than_duplicated(message):
    """The trade this change actually makes, stated where it can be checked.

    Deriving the router's rows from the fetcher's means the router inherits the fetcher's FALSE
    positives as well as its true ones. That is the point: one decision about what the user
    asked, not two — so there is one place to fix it. This asserts AGREEMENT, deliberately not
    correctness, so it stays green when someone narrows the cue.

    FOLLOW-UP, NOT FIXED HERE: `poi` is too short to be a cue and should be narrowed in
    core.dimensions. That is a both-arch change touching the public arm and needs its own
    measurement — the same reason this table was left to the owner in the first place.
    """
    fetcher_says_nearby = "nearby" in dimensions.cued_dimensions(message)
    router_says_poi = "poi" in _router_groups(message)
    assert fetcher_says_nearby == router_says_poi, (
        f"{message!r}: fetcher nearby={fetcher_says_nearby}, router poi={router_says_poi}. "
        "The router's view of the nearby vocabulary must be the fetcher's, right or wrong.")


def test_the_two_asks_become_a_concurrent_plan():
    """End of the causal chain, not just the cue table: two distinct plannable intents is what
    the build_execution_plan trigger counts. On mainline this message yields one."""
    msg = "Is there a pharmacy within walking distance, and is the area safe?"
    intents = lga._plannable_intents_in_message(msg)
    assert intents == {"check_safety", "search_nearby_pois"}, intents
    assert lga._current_message_has_multi_intent(msg) is True
    assert len(intents) >= 2          # the trigger's own condition


# ═══════════════════════════════════════════════════════════════════
# 2. The containment is DERIVED, so a second copy cannot exist
# ═══════════════════════════════════════════════════════════════════

def _declared_extra_cue_literals():
    """The cue strings actually written down in _MULTI_INTENT_EXTRA_CUES, parsed from source.
    VALUES only — a group's own key ("safety") is a taxonomy name, not a cue."""
    import ast
    tree = ast.parse(inspect.getsource(lga))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "_MULTI_INTENT_EXTRA_CUES"
                        for t in node.targets)):
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                out[k.value] = {e.value for e in v.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            return out
    raise AssertionError("_MULTI_INTENT_EXTRA_CUES is no longer a module-level dict literal")


def test_the_shared_rows_are_derived_not_declared():
    """A source guard, not a promise: the dimension cues must not appear as LITERALS in the
    intent table's own declaration. If they do, someone has re-copied the vocabulary and the
    containment above will hold only until the next edit."""
    declared = _declared_extra_cue_literals()
    for group, dim in lga._INTENT_GROUP_DIMENSION.items():
        copied = sorted(declared[group] & set(dimensions.cues_for(dim)))
        assert not copied, (
            f"{group}: dimension cues {copied} are written as literals in "
            f"_MULTI_INTENT_EXTRA_CUES. They must come from core.dimensions.cues_for().")


def test_the_product_agrees_with_the_alignment_this_file_asserts():
    """Guards the guard: section 1 uses a local ALIGNED literal so it can run on the old tree.
    If the product's own map ever disagrees with it, section 1 is measuring the wrong pairs."""
    assert lga._INTENT_GROUP_DIMENSION == ALIGNED


def test_the_intent_only_cues_are_exactly_the_declared_extras():
    """The other half: everything in a shared row that is NOT a dimension cue must be declared
    as an intent-only extra. Nothing arrives from nowhere."""
    for group, dim in lga._INTENT_GROUP_DIMENSION.items():
        extra = set(lga._MULTI_INTENT_CUES[group]) - set(dimensions.cues_for(dim))
        assert extra == set(lga._MULTI_INTENT_EXTRA_CUES[group]), group


def test_the_matcher_is_the_shared_one():
    """Both consumers must route through core.dimensions.cues_hit. A private substring loop here
    would reintroduce the ascii/CJK split as a second implementation."""
    for fn in (lga._current_message_has_multi_intent, lga._plannable_intents_in_message):
        src = inspect.getsource(fn)
        assert "cues_hit" in src, fn.__name__
        assert "cue.isascii()" not in src, fn.__name__


# ═══════════════════════════════════════════════════════════════════
# 3. The NON-union direction, pinned — this is the refusal, made unrepealable
# ═══════════════════════════════════════════════════════════════════

# Intent-only, and it must stay that way. Substring matching is why: `park` is inside
# "Finsbury Park" and "parking", `gym` is inside "gymnasium" but also any building-facilities
# blurb, and the product's own _FILLER_TOKENS comment already calls `park` an address word.
INTENT_ONLY_CUES = {"gym", "park", "健身", "多久", "多远", "距离"}


@pytest.mark.parametrize("cue", sorted(INTENT_ONLY_CUES))
def test_router_only_cues_never_become_dimension_cues(cue):
    """The guard against "completing the union" later. Promoting one of these widens the cue set
    of BOTH arches — including fc, which holds the public edge — and makes a fetcher fire and an
    apology print for a dimension nobody asked about."""
    for dim, cues, _tools in dimensions.DIMENSION_CUES:
        assert cue not in cues, (
            f"{cue!r} was promoted into the {dim} dimension. It is a ROUTER cue: as a dimension "
            f"cue it fires the fan-out wave and fc's 'not looked up yet' line on ordinary "
            f"messages (measured: 'a 1-bed in Finsbury Park', 'does it have parking?').")


@pytest.mark.parametrize("message", [
    "Find me a 1-bed in Finsbury Park under £1600.",
    "Does the second one have parking?",
    "Any studios near Queen's Park with a gym in the building?",
])
def test_place_names_and_facilities_do_not_cue_a_dimension(message):
    """The refusal expressed as behaviour rather than as table membership, so it survives a
    rewrite of the tables. None of these messages asks about nearby amenities; none may produce
    a fan-out task on legacy or an apology line on fc."""
    assert "nearby" not in dimensions.cued_dimensions(message), message
    assert "nearby" not in _fanout_dims(message), message
    assert agent_loop._missing_requested_dimension_lines(message, set(), "en") == [], message


def test_the_taxonomies_genuinely_differ():
    """The justification for keeping two tables, asserted so the day it stops being true is the
    day this test goes red and a human merges them. Five intent groups name no dimension."""
    non_dimension = set(lga._MULTI_INTENT_CUES) - set(lga._INTENT_GROUP_DIMENSION)
    assert non_dimension == {"cost", "transport", "weather", "details", "web"}
    assert set(lga._INTENT_GROUP_DIMENSION.values()) <= set(dimensions.DIMENSIONS)
    # ...and the router carries vocabulary of its own, so it is not a mere superset either.
    assert INTENT_ONLY_CUES == {c for g in lga._INTENT_GROUP_DIMENSION
                                for c in lga._MULTI_INTENT_EXTRA_CUES[g]}


def test_an_aligned_group_dispatches_the_dimensions_canonical_tool():
    """The half of the fourth table that had NOT drifted, pinned so it cannot start: a group
    that is a dimension must route to that dimension's canonical read, derived from the same
    ordered tuple the fan-out uses."""
    for group, dim in lga._INTENT_GROUP_DIMENSION.items():
        assert lga._INTENT_GROUP_TO_TOOL[group] == dimensions.canonical_tool(dim), group


def test_the_group_keys_line_up_across_all_three_tables():
    """_plannable_intents_in_message indexes _MULTI_INTENT_CUES by _INTENT_GROUP_TO_TOOL's keys
    on the routing hot path; a key present in one and absent from the other is a KeyError inside
    decide_tool_node. Unguarded before this file."""
    assert set(lga._MULTI_INTENT_CUES) == set(lga._INTENT_GROUP_TO_TOOL)
    assert set(lga._MULTI_INTENT_CUES) == set(lga._MULTI_INTENT_EXTRA_CUES)
    for group, tool in lga._INTENT_GROUP_TO_TOOL.items():
        assert lga._MULTI_INTENT_CUES[group], f"{group} has no cues at all"
        assert tool in lga.LOOPABLE_TOOLS, f"{group} -> {tool} is not loopable"


# ═══════════════════════════════════════════════════════════════════
# 4. Blast radius: this table is read by the LEGACY arch ONLY
# ═══════════════════════════════════════════════════════════════════
#
# The whole reason this change is shippable is that it cannot touch fc. That is a structural
# fact today; it must not become false quietly, because then a "legacy-only" cue edit would
# silently move the public arch.

def test_the_fc_arch_has_no_consumer_of_the_multi_intent_table():
    src = inspect.getsource(agent_loop)
    for name in ("_MULTI_INTENT_CUES", "_MULTI_INTENT_EXTRA_CUES",
                 "_current_message_has_multi_intent", "_plannable_intents_in_message",
                 "_INTENT_GROUP_TO_TOOL", "_INTENT_GROUP_DIMENSION"):
        assert name not in src, (
            f"core.agent_loop now references {name}. The multi-intent table used to be "
            "legacy-only, which is what bounded its blast radius to one arch; re-measure the "
            "fc arm before changing a cue.")


def test_the_fc_graph_has_neither_reflect_nor_decide_tool():
    """The two consumers live in the legacy graph's reflect and decide_tool nodes. The fc graph
    must not contain a node by either name."""
    src = inspect.getsource(agent_loop.build_fc_graph)
    nodes = [ln for ln in src.splitlines() if "add_node(" in ln]
    assert nodes, "build_fc_graph declares no nodes — parsing assumption broke"
    joined = " ".join(nodes)
    for banned in ('"reflect"', '"decide_tool"', "'reflect'", "'decide_tool'"):
        assert banned not in joined, f"fc graph gained a {banned} node"


def test_both_consumers_are_still_the_only_two():
    """If a third caller appears, the measured routing effect above no longer covers this table.
    Two call sites: the build_execution_plan trigger, and reflect's one-shot short-circuit."""
    src = inspect.getsource(lga)
    assert src.count("_current_message_has_multi_intent(") == 3      # 1 def + 2 calls
    assert src.count("_plannable_intents_in_message(") == 2          # 1 def + 1 call
