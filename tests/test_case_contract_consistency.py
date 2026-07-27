"""A case_id that appears in more than one benchmark shard must mean the SAME thing.

The shards overlap heavily: every one of the 98 Base98 cases also lives in a smaller
shard (`cases_base45`, `cases_ext_CDE`, `cases_ext_FG`, ...). Nothing enforced that the
copies agree, so amending a case in `cases.jsonl` alone silently left the same case_id
being graded under a DIFFERENT contract depending on which shard a round happened to run.

That is not hypothetical. The G2/G3/E11 amendments were originally written into
`cases.jsonl` only, leaving `cases_base45` (G2, G3) and `cases_ext_CDE` (E11) on the
superseded contract. It is the same family as the scar the measurement infrastructure
already encodes — a green run on one shard proves nothing about the others.

`KNOWN_DIVERGENCES` records pre-existing drift that predates this contract work. Those
are real contract questions, not formatting noise (different constraint TYPES), so they
are recorded as debt rather than silently resolved here: picking a winner changes what
"pass" means for those cases and belongs in its own review. Shrinking this set is
progress; adding to it means a new amendment forgot a shard.

SECOND GUARD (2026-07-26): a case definition that is UNSATISFIABLE BY CONSTRUCTION.
`graders.route_matches` scores `expected_tools` as a subset of the EXECUTED tool trace,
and `graders.forbidden_tool_used` matches `forbidden_tools` against that same trace.
Graph PSEUDO-ROUTES (`market_info`, `multi_search`, `clarification`, `direct_answer`,
`reasoning_property`) are router decisions, never registry tools, so they can NEVER
appear in a trace. A pseudo-route in `expected_tools` is therefore a permanent route
MISS whatever the agent does; in `forbidden_tools` it is a guard that can never fire.
Both were legal until now: `schema.json` used to describe `expected_tools` as "real
registry tools OR documented pseudo-routes", and `evaluation/benchmark/validate.py`
checked both fields against `REAL_TOOLS | PSEUDO_ROUTES`. F7 sat on the wrong side
of that for the whole programme (`expected_tools: ["market_info"]`), costing a route
point in every round ever run.

The registered-tool list below is DERIVED from the live registry. A hand-copied literal
would be the same defect one level up — which is exactly what validate.py's `REAL_TOOLS`
was, and why it could not catch F7.

UPDATE 2026-07-27: validate.py no longer has that hole. Its `REAL_TOOLS` literal is gone;
it now calls `create_tool_registry()` the same way `_registered_tool_names()` below does,
and it rejects a pseudo-route in either trace-matched field outright. This test and that
validator are pinned to each other by
tests/test_benchmark_validate_tool_names.py::test_validator_and_sibling_test_agree_on_the_tool_set,
so the repo keeps ONE definition of "a real tool". The sentence above is left in the past
tense as the record of why both guards exist.

STILL HAND-COPIED, and NOT this change's to fix: `schema.json`'s `expected_route` carries
an `enum` of every tool name. That is a third copy of the registry, in a file owned
elsewhere; it is reported rather than edited here.

DELIBERATELY NOT GUARDED: `expected_route: clarification` together with a non-empty
`expected_tools`. That pattern was proposed as a second self-contradiction, but it is
well-formed and measurably satisfiable, so a guard would condemn correct cases:
  * H14 (hard_gate) declares `clarification` + `expected_tools: ["ask_user"]` —
    `ask_user` is the REGISTERED terminal tool through which fc_loop realises a
    clarification. This is the canonical fc encoding, not a defect.
  * A8/A11/A13 declare `clarification` + `["search_properties"]`, expressing "the
    agent calls the search tool and the tool's own missing-field gate returns the
    clarification". All three scored `route_matched=True` in the retained legacy sweep
    (round-8793c0b-internal-2026-07-25), and A11 also scored True under fc_loop.
An architecture-dependent route difference is a product/contract question for the case
owner (the vehicle would be `allowed_tool_paths`, as H14 already uses), NOT a structural
impossibility, and amending it after seeing the round it judges is what rule 3.5 forbids.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"

# Graph-internal router decisions. Documented in app/core/langgraph_agent.py
# (_INTENT_CATALOG: "Covers every routable tool PLUS the pseudo-routes market_info ...
# and direct_answer") and mirrored by validate.py's PSEUDO_ROUTES. Legal in
# `expected_route`; never executable, so never legal in a trace-matched tool list.
PSEUDO_ROUTES = frozenset({
    "market_info", "direct_answer", "multi_search", "reasoning_property", "clarification",
})

# Fields that graders match against the EXECUTED tool trace, and must therefore only ever
# name real registry tools.
TRACE_MATCHED_TOOL_FIELDS = ("expected_tools", "forbidden_tools")

# case_id -> why it differs across shards. Pre-existing on mainline f053508; the Base98
# copy is the newer one in all three (ext_FG's F11 still carries a NEEDS_CHECKER note).
KNOWN_DIVERGENCES = {
    "E8": "Base98 uses must_flag_unrealistic_constraint; ext_CDE still uses must_refuse_fabrication",
    "F11": "Base98 uses must_flag_stale_data; ext_FG still uses must_note_missing_data (NEEDS_CHECKER)",
    "G16": "Base98 uses must_supersede_value; ext_FG still uses must_recall_value",
}


def _load_all():
    by_case = defaultdict(dict)
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                by_case[case["case_id"]][path.name] = case
    return by_case


def _registered_tool_names() -> frozenset:
    """The single source of truth for "is this a real tool", DERIVED from the registry
    the benchmark actually executes against — `create_tool_registry()` keys its `tools`
    dict by `Tool.name`, and `registry.execute_tool(name, ...)` is exactly what a trace
    records. Registering a new tool therefore widens this guard automatically; a
    hand-copied list would silently reject the new tool instead (or, like validate.py's
    `REAL_TOOLS`, silently accept a pseudo-route)."""
    from core.tool_system import create_tool_registry

    return frozenset(create_tool_registry().tools)


def _all_cases():
    """(case_id, shard_name, case) for every row in every shard, duplicates included —
    a defect must be caught in each shard that carries it, not just the first."""
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line), path.name


def test_shards_overlap_at_all():
    """Guards the guard: if the shards stopped sharing case_ids this suite would pass
    vacuously."""
    by_case = _load_all()
    shared = [c for c, shards in by_case.items() if len(shards) > 1]
    assert len(shared) >= 90, f"expected heavy shard overlap, found {len(shared)}"


def test_same_case_id_means_the_same_contract_in_every_shard():
    by_case = _load_all()
    divergent = {}
    for case_id, shards in by_case.items():
        if len(shards) < 2:
            continue
        first = next(iter(shards.values()))
        if any(case != first for case in shards.values()):
            divergent[case_id] = sorted(shards)

    unexpected = {c: s for c, s in divergent.items() if c not in KNOWN_DIVERGENCES}
    assert not unexpected, (
        "case_id defined differently across shards — an amendment probably updated "
        f"cases.jsonl but not its sibling shard: {unexpected}"
    )

    healed = set(KNOWN_DIVERGENCES) - set(divergent)
    if healed:
        pytest.fail(
            f"{sorted(healed)} no longer diverge — remove them from KNOWN_DIVERGENCES "
            "so the guard keeps its teeth."
        )


# --------------------------------------------------------------------------- #
# Unsatisfiable-by-construction guard (2026-07-26)
# --------------------------------------------------------------------------- #
def test_registry_derivation_can_actually_bite():
    """Guards the guard. If `_registered_tool_names()` ever returned {} (an import that
    silently degraded) or somehow contained the pseudo-routes, the two tests below would
    pass vacuously or reject nothing."""
    registered = _registered_tool_names()
    assert len(registered) >= 14, f"registry derivation looks degraded: {sorted(registered)}"
    leaked = registered & PSEUDO_ROUTES
    assert not leaked, (
        f"{sorted(leaked)} is now a REGISTERED tool as well as a documented pseudo-route. "
        "Drop it from PSEUDO_ROUTES here (and in validate.py) before this guard can be "
        "trusted again."
    )
    assert "ask_user" in registered, (
        "ask_user must stay registered: it is the real terminal tool through which a "
        "clarification route is expressed (H14), and the reason a "
        "'clarification implies no tools' guard would be wrong."
    )


@pytest.mark.parametrize("field", TRACE_MATCHED_TOOL_FIELDS)
def test_trace_matched_tool_fields_name_only_real_registry_tools(field):
    """`expected_tools` / `forbidden_tools` are compared against the EXECUTED tool trace,
    so every entry must be a name that can actually appear there. A pseudo-route makes the
    case unsatisfiable (expected) or the guard vacuous (forbidden) forever — F7 carried
    `expected_tools: ["market_info"]` and was scored a route miss in every round ever run,
    regardless of what the agent did."""
    registered = _registered_tool_names()
    offenders = defaultdict(list)
    for case, shard in _all_cases():
        for name in case.get(field) or []:
            if name not in registered:
                kind = "pseudo-route" if name in PSEUDO_ROUTES else "unknown name"
                offenders[f"{case['case_id']} ({shard})"].append(f"{name} [{kind}]")

    assert not offenders, (
        f"{field} entries that can never appear in a tool trace: {dict(offenders)}. "
        f"Registered tools: {sorted(registered)}. A pseudo-route belongs in "
        "expected_route (or allowed_tool_paths for a route shape), never in a "
        "trace-matched tool list."
    )


@pytest.mark.parametrize("case_id", ["G2", "G3", "E11"])
def test_amended_cases_are_in_sync_across_every_shard(case_id):
    """The three cases this branch amends, pinned explicitly: they are the ones that
    actually went out of sync."""
    shards = _load_all()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    first = next(iter(shards.values()))
    for name, case in shards.items():
        assert case == first, f"{case_id} differs in {name} from the Base98 definition"
