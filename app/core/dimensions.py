"""THE user-visible "dimension" vocabulary, shared by BOTH architectures.

One question — *which user-visible dimension is this message asking about, and which tools
satisfy it?* — used to be answered by three separate tables:

  * ``core.agent_loop._DIMENSION_CUES``          (fc arch; cues + satisfying tools + apology)
  * ``core.langgraph_agent._SEARCH_DIMENSION_CUES`` (legacy arch; added later, deliberately
                                                    "mirroring" the fc table)
  * ``evaluation.metrics.graders._DIMENSION_TOOLS`` (the evaluator's copy)

The first two are now THIS module; each arch keeps only its own CONSUMER, because the
behaviours genuinely differ — fc *apologises* about a dimension it never fetched, legacy
*dispatches a follow-up wave* to go and fetch it. Different verbs over the same nouns.

The grader's copy stays separate on purpose (evaluation must not import product code, so that
a grader cannot be "fixed" by editing the thing it grades). It is held to this table by the
source guard ``test_canonical_tool_agrees_with_the_graders_dimension_table`` in
tests/test_dimension_fanout.py — allowed to exist, not allowed to disagree.

────────────────────────────────────────────────────────────────────────────────────────────
DRIFT RECORD — the two product tables were NOT identical when they were merged (2026-07-27).
────────────────────────────────────────────────────────────────────────────────────────────
The legacy table was documented as "mirrors agent_loop._DIMENSION_CUES". It did not. The
satisfying-tool tuples agreed exactly, and legacy's fetch tool was always fc's ``tools[0]``,
but the CUE VOCABULARY had diverged in six places, legacy being a strict superset:

    safety   + `safe`
    commute  + `travel time`, `how long`, `how far`
    nearby   + `药店`, `pharmacy`

(Backticks, not quotes, on purpose: the source guard counts quoted cue literals and this
paragraph must not read as a second copy of the table.)

So "is it safe around there?" cued a safety fetch on legacy and cued NOTHING on fc — the two
arches disagreed about what the user had asked for, which is precisely the failure the second
table was written to avoid. That is why the merge below takes the UNION rather than either
side:

  * it is the only direction that keeps every existing assertion true (e.g.
    tests/test_execution_plan.py::test_commute_dimension_dropped_when_no_destination
    depends on "is it safe …" cueing safety);
  * it fails SAFE. A cue that fires produces a fetch (legacy) or an honest "not done yet"
    line (fc). A cue that misses produces silence, and silence about a dimension the user
    asked about is the shape that lets an answer free-associate (HANDOFF §0: E11's invented
    "about 15-20 min to Canary Wharf").

The union WIDENS fc's cue set by those six cues; that is a real behaviour change on the arch
currently holding the public edge and is flagged for the owner, not hidden here.
``tests/test_dimension_table_is_shared.py`` pins the merged vocabulary literally, so a future
narrowing has to be a deliberate act with a failing test in front of it.

────────────────────────────────────────────────────────────────────────────────────────────
THE FOURTH TABLE — measured 2026-07-27, and deliberately NOT merged into this one.
────────────────────────────────────────────────────────────────────────────────────────────
``langgraph_agent._MULTI_INTENT_CUES`` was flagged above as "the closest thing to a fourth
copy". Measured, it is not a copy: it is keyed by the eight LOOPABLE follow-up INTENTS
(safety/commute/cost/transport/weather/poi/details/web), five of which name no dimension at
all, and its question is "does this ONE message pack two distinct asks" — whether to spend a
reflect hop — not "which dimension is owed". Overlap matrix and per-arm routing effect are in
the PR for this note.

The three rows that DO share a taxonomy row with this table (safety, commute, poi≡nearby) are
now DERIVED from it via ``cues_for``, so that table is a guaranteed SUPERSET of this one. That
direction is one-way ON PURPOSE — a deliberate non-union:

  * the router must never know LESS than the fetcher. Before this, legacy's own post-search
    fan-out read `pharmacy`/`药店`/`便利店`/`amenit` as "the user asked about nearby" while
    legacy's own reflect/plan router did not count a poi intent at all, so "is there a pharmacy
    within walking distance, and is the area safe?" short-circuited after check_safety and
    dropped the pharmacy ask. One arch, one sentence, two answers to "what was asked".
  * the reverse direction is REFUSED. The intent-only cues (`gym`, `park`, `健身`, `多久`,
    `多远`, `距离`) must NOT become dimension cues. Cue matching is substring-based, and this
    product already declares elsewhere that `park` is an address word that "can be part of a
    real name" (tools/get_property_details.py ``_FILLER_TOKENS``). Measured: with `park` in the
    nearby row, a plain listings search in an area whose NAME ends in Park — Finsbury Park,
    Queen's Park — and a question about a flat's `parking` both make fc emit its nearby-amenities
    apology line and make legacy dispatch a POI fan-out wave, on BOTH arches, fc holding the
    public edge, for a dimension the user never mentioned. A cue that fires wrongly is cheap for
    a ROUTER (one extra reflect hop) and expensive for a FETCHER (a wave, plus a false line in
    the answer); that asymmetry is why the containment is one-way and not a union.
    (The apology text is deliberately NOT quoted here — see
    ``test_the_apology_lines_stay_with_fc_only``, which caught exactly that on the first draft.)

Pinned by ``tests/test_multi_intent_table_is_derived.py``.

No imports from either arch: this module must stay importable by both without a cycle
(``core.agent_loop`` already imports ``core.langgraph_agent`` at module level).
"""
from __future__ import annotations

from typing import Optional

# dimension -> (cue words, tools that SATISFY the dimension).
#
# The tools tuple is ORDERED: ``tools[0]`` is the CANONICAL read — the one a harness may
# dispatch on its own (fc's plan-time fan-out, legacy's post-search wave). The rest are
# alternates that also satisfy the dimension when the MODEL chooses them, but that no harness
# picks by itself. A separate dimension->tool mapping is exactly the divergence this file
# exists to end, so ``canonical_tool`` is derived rather than declared.
#
# The `listings` dimension (search_properties) is intentionally absent: fc's degraded answer
# already names it via its dedicated recommendations / no-results block, and legacy's fan-out
# is triggered BY a listings search, so it can never be one of the dimensions fanned out.
DIMENSION_CUES = (
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

DIMENSIONS = tuple(d for d, _c, _t in DIMENSION_CUES)


def cues_hit(cues, message: str) -> bool:
    """THE cue matcher. Deterministic and bilingual: CJK cues match the raw text, ascii cues
    the lowercased text (a CJK cue lowercases to itself, so the split only matters for ascii
    substrings embedded in CJK text).

    This must exist exactly once in the product. Both arches route through it, so a cue can
    never mean one thing to a fetcher and another to an apology — nor one thing to fc and
    another to legacy, which is the drift recorded in the module docstring.
    """
    msg = message or ""
    low = msg.lower()
    return any((cue in low) if cue.isascii() else (cue in msg) for cue in cues)


def cued_dimensions(message: str) -> list:
    """The dimensions THIS message explicitly asks about, in DIMENSION_CUES order."""
    return [dim for dim, cues, _tools in DIMENSION_CUES if cues_hit(cues, message)]


def cues_for(dim: str) -> tuple:
    """The cue vocabulary of `dim`. Exists so a consumer that needs to CONTAIN this vocabulary
    (``langgraph_agent._MULTI_INTENT_CUES``) can derive it instead of copying it. ``()`` for an
    unknown dimension."""
    for d, cues, _tools in DIMENSION_CUES:
        if d == dim:
            return tuple(cues)
    return ()


def satisfying_tools(dim: str) -> tuple:
    """Every tool whose completed result SATISFIES `dim` (the model may pick any of them)."""
    for d, _cues, tools in DIMENSION_CUES:
        if d == dim:
            return tuple(tools)
    return ()


def canonical_tool(dim: str) -> Optional[str]:
    """The ONE read a harness may dispatch for `dim` — ``tools[0]`` of its row, derived, never
    declared separately. ``None`` for an unknown dimension."""
    tools = satisfying_tools(dim)
    return tools[0] if tools else None
