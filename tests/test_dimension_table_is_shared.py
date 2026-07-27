"""ONE dimension table, read by BOTH arches — and the drift that proves why.

FAILS ON THE OLD BEHAVIOUR. Every test in section 1 fails on mainline 4f410ab, where the
knowledge "which user-visible dimension does this cue mean, and which tools satisfy it" lived
in two product copies:

    app/core/agent_loop.py        _DIMENSION_CUES         (fc arch)
    app/core/langgraph_agent.py   _SEARCH_DIMENSION_CUES  (legacy arch, added later)

The legacy copy carried the comment "Mirrors agent_loop._DIMENSION_CUES so the two arches
recognise the same user-visible dimensions". IT DID NOT. Measured on 4f410ab, the satisfying-
tool tuples matched exactly and legacy's fetch tool was always fc's ``tools[0]``, but the cue
vocabulary had drifted in SIX places, all of them legacy-only additions:

    safety   + safe
    commute  + travel time, how long, how far
    nearby   + 药店, pharmacy

So "Find me a flat in Camden, is it safe?" cued a safety fetch on legacy and cued NOTHING on
fc. Two arches, one user sentence, two different understandings of what was asked — which is
the precise failure the second copy was written to prevent, and it shipped inside the copy's
own "mirrors" comment. A comment is not a guard.

``test_the_two_arches_recognise_the_same_cues`` is the one to run against the old tree: it is
red there because of REAL drift, not because of a refactor.

WHAT THE MERGE CHOSE, and why it is an owner-visible decision. The union. It is the only
direction under which every pre-existing assertion stays true (see
tests/test_execution_plan.py::test_commute_dimension_dropped_when_no_destination, which
depends on "is it safe …" cueing safety), and it fails safe: a cue that fires yields a fetch
or an honest "not done yet" line, while a cue that misses yields silence, and silence about a
dimension the user asked about is the shape that lets an answer free-associate. But it does
WIDEN fc's cue set by those six cues, and fc holds the public edge, so section 3 pins the
merged vocabulary literally — narrowing it later has to be deliberate, with this test red in
front of it.
"""
from __future__ import annotations

import inspect

import pytest

import core.agent_loop as agent_loop
import core.langgraph_agent as lga
from core import dimensions

# The six cues that had drifted onto the legacy side only, with the dimension each belongs to
# and a sentence that contains it and no other cue for that dimension. On mainline 4f410ab
# every one of these sentences produced a DIFFERENT answer from the two arches.
DRIFTED_CUES = [
    ("safety", "safe", "Find me a flat in Camden, is it safe?"),
    ("commute", "travel time", "Find me a flat in Camden, what is the travel time to UCL?"),
    ("commute", "how long", "Find me a flat in Camden, how long to UCL?"),
    ("commute", "how far", "Find me a flat in Camden, how far is UCL?"),
    ("nearby", "药店", "帮我找一个卡姆登的房子，附要有药店"),
    ("nearby", "pharmacy", "Find me a flat in Camden with a pharmacy on the street"),
]

PRODUCT_MODULES = (agent_loop, lga, dimensions)


def _legacy_cued(message):
    """The dimensions LEGACY thinks this message asks about, with nothing executed yet."""
    return [dim for dim, _tool in lga._cued_search_dimensions(message, set())]


# ═══════════════════════════════════════════════════════════════════
# 1. The two arches must agree — the test that is RED on 4f410ab
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("dim,cue,message", DRIFTED_CUES,
                         ids=[c for _d, c, _m in DRIFTED_CUES])
def test_the_two_arches_recognise_the_same_cues(dim, cue, message):
    """THE regression. On 4f410ab fc returns [] for each of these and legacy returns [dim]:
    the fc user is told nothing and gets no fetch, the legacy user gets a fetch, from the same
    sentence. Both must now name the same dimension, because there is only one table left."""
    fc = agent_loop._cued_dimensions(message)
    legacy = _legacy_cued(message)
    assert fc == legacy, (
        f"cue {cue!r}: fc sees {fc}, legacy sees {legacy} — the arches disagree about what "
        "the user asked for")
    assert dim in fc, f"cue {cue!r} no longer cues {dim} in either arch"


def test_the_two_arches_agree_on_every_message_in_the_corpus():
    """Broader form: not just the six drifted cues, but every message either arch's test
    corpus exercises. Cue matching is substring-based, so agreement has to hold on real
    sentences, not only on the cue words in isolation."""
    corpus = [m for _d, _c, m in DRIFTED_CUES] + [
        "hi", "hello there", "帮我查治安",
        "Find me a 2-bed flat in Camden under £1500 a month. No commute to worry about, "
        "so just go ahead and search.",
        "Find me a 1-bed in Islington under £1600/month with a commute to UCL under 40 "
        "minutes, a supermarket nearby, and avoid high-crime areas — go ahead and search.",
        "帮我找伦敦月租不超过1400镑的单间，通勤到帝国理工不超过35分钟，"
        "附近要有超市，尽量避开治安差的区域。",
        "Find a studio in Stratford under £1300/month, with a commute to Canary Wharf "
        "under 25 minutes, a pharmacy nearby, and steer clear of high-crime spots — "
        "go ahead and search.",
        "find me a flat in Camden",
        "find me a flat in Camden with a supermarket nearby",
        "find me a flat in Camden, is it safe and how long is the commute",
    ]
    for msg in corpus:
        assert agent_loop._cued_dimensions(msg) == _legacy_cued(msg), msg


def test_the_arches_agree_on_which_tool_satisfies_each_dimension():
    """The half that had NOT drifted, pinned so it cannot start. Legacy's fetch tool was a
    separate fourth column; it is now derived from the same ordered tuple fc reads."""
    for dim in dimensions.DIMENSIONS:
        satisfying = dimensions.satisfying_tools(dim)
        assert agent_loop._dimension_satisfying_tools(dim) == satisfying
        assert agent_loop._canonical_dimension_tool(dim) == satisfying[0]
        # legacy names exactly that tool when it decides to fetch the dimension
        msg = next(m for _d, _c, m in DRIFTED_CUES if _d == dim) if any(
            d == dim for d, _c, _m in DRIFTED_CUES) else ""
        if msg:
            assert dict(lga._cued_search_dimensions(msg, set()))[dim] == satisfying[0]


# ═══════════════════════════════════════════════════════════════════
# 2. Source guard: there is exactly ONE table and ONE matcher, product-wide
# ═══════════════════════════════════════════════════════════════════
#
# Practice 3: a guard, not a promise. The pre-existing guards in tests/test_dimension_fanout.py
# were the right idea scoped to the wrong unit — they counted cue literals inside agent_loop.py
# only, so the copy that actually shipped, in the OTHER arch's module, was invisible to them.

# A second cue table is not "a cue word appears twice" — words like `safe` and `restaurant`
# have plenty of unrelated uses. It is a LITERAL GROUP OF STRINGS that substantially
# reproduces one dimension's cue row. So: parse each arch module, collect every string
# tuple/list/set/dict-key group, and flag any that overlaps a dimension's cues by >= 3.
_OVERLAP_THRESHOLD = 3

# The overlapping groups that ALREADY existed on 4f410ab. Each is a real, separate question —
# not a copy of the dimension table — and each is recorded here rather than merged, because
# merging any of them would change routing behaviour that is not this change's to touch:
#
#   POI_TYPES (~:182)             display icons/labels per POI type. Not cues at all; the
#                                 overlap is just that a supermarket is called a supermarket.
#   safety_kws (~:216)            memory extraction: "did the user voice a safety CONCERN
#                                 about a named area", which is a different judgement from
#                                 "did the user ask about safety".
#   _LOCATION_INTENT_KWS (~:1970) an EXCLUSION list for the comparative/detail interception —
#                                 location asks that must not be swallowed. Spans dimensions
#                                 the table does not have (stations, gyms, transport cost).
#   _heuristic_fallback (~:2399)  last-resort routing when every vote failed.
#   _MULTI_INTENT_CUES (~:3437)   ***THE CLOSEST THING TO A FOURTH COPY.*** Keyed
#                                 safety/commute/cost/transport/weather/poi/details/web, it
#                                 answers "does this ONE message pack two distinct asks"
#                                 (whether to spend a reflect hop), not "which dimension is
#                                 owed". It has ALSO drifted: commute carries 多久/多远/距离
#                                 which the dimension table lacks, safety lacks `unsafe` and
#                                 `police`, and its `poi` key is the table's `nearby`.
#                                 Reported, deliberately NOT merged — unifying it changes
#                                 reflect-hop routing for every legacy turn and needs its own
#                                 measured change. See the return notes for 2026-07-27.
#
# Fingerprint is (module, dimension, the overlapping cues) so it survives line moves but NOT a
# widening of one of these groups toward the cue table — which is exactly when a human should
# look again.
KNOWN_CUE_OVERLAPS = {
    ("core.langgraph_agent", "nearby", frozenset({"pharmacy", "restaurant", "supermarket"})),
    ("core.langgraph_agent", "safety",
     frozenset({"crime", "safe", "safety", "unsafe", "安全", "治安"})),
    ("core.langgraph_agent", "nearby",
     frozenset({"nearby", "pharmacy", "restaurant", "supermarket", "超市", "附近"})),
    ("core.langgraph_agent", "safety",
     frozenset({"crime", "safe", "safety", "安全", "治安", "犯罪"})),
    ("core.langgraph_agent", "commute",
     frozenset({"commute", "how far", "how long", "travel time", "通勤"})),
    ("core.langgraph_agent", "nearby",
     frozenset({"nearby", "poi", "restaurant", "supermarket", "超市", "附近"})),
    ("core.langgraph_agent", "safety", frozenset({"crime", "safe", "safety", "unsafe"})),
    ("core.langgraph_agent", "nearby",
     frozenset({"nearby", "restaurant", "supermarket", "超市", "附近"})),
    ("core.langgraph_agent", "safety", frozenset({"crime", "safe", "安全", "犯罪"})),
}


def _string_groups(module):
    """Every literal group of strings in `module`, as (lineno, {values}). Dict keys and dict
    values-that-are-sequences both count: a cue table is just as much a cue table when it is
    written as ``{"safety": ["safe", ...]}``."""
    import ast
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            vals = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            for v in node.values:
                if isinstance(v, (ast.List, ast.Tuple, ast.Set)):
                    vals |= {e.value for e in v.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if len(vals) >= 2:
                yield node.lineno, vals
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            vals = {e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if len(vals) >= 2:
                yield node.lineno, vals


@pytest.mark.parametrize("module", [agent_loop, lga], ids=["fc", "legacy"])
def test_no_arch_module_contains_a_second_dimension_cue_table(module):
    """THE structural half of the regression. On 4f410ab this fails for the legacy arch on a
    fingerprint that is NOT in the allowlist — the full ``_SEARCH_DIMENSION_CUES`` rows, i.e.
    the second copy itself."""
    found = set()
    for lineno, vals in _string_groups(module):
        for dim, cues, _tools in dimensions.DIMENSION_CUES:
            overlap = vals & set(cues)
            if len(overlap) >= _OVERLAP_THRESHOLD:
                fp = (module.__name__, dim, frozenset(overlap))
                if fp not in KNOWN_CUE_OVERLAPS:
                    found.add((lineno, dim, tuple(sorted(overlap))))
    assert not found, (
        f"{module.__name__} contains string group(s) that reproduce a dimension's cue row: "
        f"{sorted(found)}. The cue vocabulary belongs to core.dimensions.DIMENSION_CUES. If "
        "this is a genuinely different question (see KNOWN_CUE_OVERLAPS), document it there.")


def test_the_shared_module_is_where_the_cues_actually_live():
    """Guards the guard: if DIMENSION_CUES ever stopped holding real cue words the scan above
    would pass vacuously."""
    for dim, cues, _tools in dimensions.DIMENSION_CUES:
        assert len(cues) >= _OVERLAP_THRESHOLD, dim
        assert all(isinstance(c, str) and c for c in cues), dim


def test_the_ascii_cjk_matcher_exists_exactly_once():
    """The ascii/CJK split is the subtle half of the cue contract: a CJK cue must be matched
    against the RAW text and an ascii cue against the lowercased copy. Two implementations of
    that rule is two chances to get it wrong."""
    counts = {m.__name__: inspect.getsource(m).count("cue.isascii()") for m in PRODUCT_MODULES}
    assert sum(counts.values()) == 1, f"cue matching is duplicated: {counts}"
    assert counts["core.dimensions"] == 1


def test_neither_arch_declares_a_dimension_table_of_its_own():
    """Names, not just literals: the two module-level tables must be gone, not shadowed."""
    assert not hasattr(agent_loop, "_DIMENSION_CUES"), (
        "agent_loop re-declared _DIMENSION_CUES; it must read core.dimensions")
    assert not hasattr(lga, "_SEARCH_DIMENSION_CUES"), (
        "langgraph_agent re-declared _SEARCH_DIMENSION_CUES; it must read core.dimensions")


def test_the_shared_module_imports_neither_arch():
    """core.dimensions is imported at module level by BOTH arches, and agent_loop already
    imports langgraph_agent at module level. Any import back into an arch is a cycle."""
    src = inspect.getsource(dimensions)
    for banned in ("core.agent_loop", "core.langgraph_agent"):
        assert f"import {banned}" not in src and f"from {banned}" not in src, banned


# ═══════════════════════════════════════════════════════════════════
# 3. The merged vocabulary, pinned literally
# ═══════════════════════════════════════════════════════════════════

EXPECTED_TABLE = (
    ("safety",
     ("治安", "安全", "犯罪", "crime", "safety", "safe", "unsafe", "police"),
     ("check_safety",)),
    ("commute",
     ("通勤", "commute", "travel time", "how long", "how far"),
     ("calculate_commute", "calculate_commute_cost", "check_transport_cost",
      "get_transport_info")),
    ("nearby",
     ("超市", "便利店", "餐厅", "药店", "附近", "周边", "设施",
      "supermarket", "grocery", "nearby", "amenit", "restaurant", "pharmacy", "poi"),
     ("search_nearby_pois",)),
)


def test_the_merged_table_is_the_union_and_stays_the_union():
    """The resolution of the drift, written down. This is the UNION of the two 4f410ab tables:
    fc's set plus the six legacy-only cues. Changing it is a product decision about what a
    user is understood to have asked for, in BOTH arches at once — not a refactor."""
    assert dimensions.DIMENSION_CUES == EXPECTED_TABLE


def test_the_union_lost_no_cue_from_either_arch():
    """Stated as the property rather than the literal: nothing either arch understood on
    4f410ab may have been dropped by the merge."""
    fc_4f410ab = {
        "safety": ("治安", "安全", "犯罪", "crime", "safety", "unsafe", "police"),
        "commute": ("通勤", "commute"),
        "nearby": ("超市", "便利店", "餐厅", "附近", "周边", "设施",
                   "supermarket", "grocery", "nearby", "amenit", "restaurant", "poi"),
    }
    legacy_4f410ab = {
        "safety": ("治安", "安全", "犯罪", "crime", "safety", "safe", "unsafe", "police"),
        "commute": ("通勤", "commute", "travel time", "how long", "how far"),
        "nearby": ("超市", "便利店", "餐厅", "药店", "附近", "周边", "设施",
                   "supermarket", "grocery", "nearby", "amenit", "restaurant",
                   "pharmacy", "poi"),
    }
    for dim, cues, _tools in dimensions.DIMENSION_CUES:
        merged = set(cues)
        assert set(fc_4f410ab[dim]) <= merged, f"{dim}: merge dropped an fc cue"
        assert set(legacy_4f410ab[dim]) <= merged, f"{dim}: merge dropped a legacy cue"
        assert merged == set(fc_4f410ab[dim]) | set(legacy_4f410ab[dim]), (
            f"{dim}: merged table is not the union — a cue was invented, not merged")


# ═══════════════════════════════════════════════════════════════════
# 4. The consumers stay SEPARATE (same table, different behaviour)
# ═══════════════════════════════════════════════════════════════════

def test_fc_apologises_where_legacy_fetches():
    """Deduplicating the table must not deduplicate the behaviour. fc turns an unserved cued
    dimension into an honest 'not done yet' line; legacy turns the same fact into a task for
    the wave engine. Both remain true of the same message."""
    msg = "Find me a flat in Camden, is it safe?"
    assert agent_loop._missing_requested_dimension_lines(msg, set(), "en") == [
        "Safety has not been verified yet (crime data was not retrieved)."]
    assert lga._cued_search_dimensions(msg, set()) == [("safety", "check_safety")]


def test_the_apology_lines_stay_with_fc_only():
    """The apology text is fc's own presentation, not shared vocabulary: legacy has no use for
    it and the shared module must not grow a presentation column."""
    assert set(agent_loop._DIMENSION_APOLOGY_LINES) == set(dimensions.DIMENSIONS)
    shared_src = inspect.getsource(dimensions)
    for zh, en in agent_loop._DIMENSION_APOLOGY_LINES.values():
        assert zh not in shared_src and en not in shared_src


@pytest.mark.parametrize("lang,idx", [("zh", 0), ("en", 1)])
def test_every_dimension_still_has_both_apology_languages(lang, idx):
    """A dimension added to the shared table with no apology line would KeyError inside the
    degraded answer builder — the one path that only runs when the turn is already in trouble."""
    for dim in dimensions.DIMENSIONS:
        line = agent_loop._DIMENSION_APOLOGY_LINES[dim][idx]
        assert line and isinstance(line, str)
