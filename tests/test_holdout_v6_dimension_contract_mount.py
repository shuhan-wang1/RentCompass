"""Held-out v6 regression: HO6-198 / HO6-208 / HO6-238 lost the structured contract.

Those three ``E_multi_constraint`` turns answered CORRECTLY — HO6-198 ran
``calculate_commute`` eight times, named the compliant listing and quoted its commute
minutes — yet scored zero on all three structured-contract metrics. The cause was not
the model skipping tools: legacy ``format_output``'s post-search dimension fan-out
branch hand-built a three-key ``tool_data``
(``recommendations`` / ``search_criteria`` / ``area_recommendations``) instead of
mounting the eight-key listings contract that the 57 passing cases in the same category
received, and it did so AFTER the artifact-ledger recovery had already built the correct
eight-key payload — overwriting it.

Every input here comes from a primary source, never from a summary or an earlier report:

* symptom + contract shape -- ``evaluation/results/holdout_v6_live/raw_runs.jsonl``
* run identity (arch)      -- ``evaluation/results/holdout_v6_live/manifest.json``
* frozen tool evidence     -- ``evaluation/benchmark/holdout_v6/fixtures/ho6_<n>_search.json``

No live model call is needed; the defect is entirely inside the formatter.
"""

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RUNS = _REPO / "evaluation/results/holdout_v6_live/raw_runs.jsonl"
_MANIFEST = _REPO / "evaluation/results/holdout_v6_live/manifest.json"
_FIXTURES = _REPO / "evaluation/benchmark/holdout_v6/fixtures"

# The three cases whose tool_data came back as the three-key subset.
_REGRESSED = ("HO6-198", "HO6-208", "HO6-238")
# HO6-238's user query carries no commute constraint (hard_constraint_slots has no
# "commute" and its fixture holds a search record only), so an EMPTY commute_evidence is
# the correct outcome there. Only these two must come back with evidence attached.
_COMMUTE_CASES = ("HO6-198", "HO6-208")

_SUBSET_KEYS = {"recommendations", "search_criteria", "area_recommendations"}


def _archive():
    return {r["case_id"]: r for r in
            (json.loads(line) for line in _RUNS.read_text().splitlines() if line.strip())}


@pytest.fixture(scope="module")
def runs():
    if not _RUNS.exists():                                   # pragma: no cover - archive gone
        pytest.skip(f"held-out v6 archive missing: {_RUNS}")
    return _archive()


@pytest.fixture(scope="module")
def contract_keys(runs):
    """The eight-key contract, read off the runs that GOT it rather than hard-coded here.

    Anchoring the expectation in the archive is what makes this test a regression against
    the observed defect instead of against one author's memory of the schema.
    """
    shapes = {frozenset(r["tool_data"]) for r in runs.values()
              if len(r.get("tool_data") or {}) == 8}
    assert len(shapes) == 1, f"passing runs disagree on the contract shape: {shapes}"
    keys = set(next(iter(shapes)))
    assert _SUBSET_KEYS < keys                               # the subset really is a subset
    return keys


def test_archive_is_the_legacy_arch_run():
    # format_output_node in langgraph_agent.py is the LEGACY formatter. If the archive were
    # an fc_loop run, this whole file would be pointed at the wrong module.
    assert json.loads(_MANIFEST.read_text())["arch"] == "legacy"


def test_archived_symptom_is_exactly_these_three_cases(runs, contract_keys):
    subset_cases = sorted(cid for cid, r in runs.items()
                          if set(r.get("tool_data") or {}) == _SUBSET_KEYS)
    assert subset_cases == sorted(_REGRESSED)
    # ...and they are not "the model skipped the tools" cases: HO6-198 and HO6-208 each
    # executed a long run of commute calls and still lost the evidence at format time.
    assert runs["HO6-198"]["tools_called"].count("calculate_commute") == 8
    assert runs["HO6-208"]["tools_called"].count("calculate_commute") == 7


def _frozen(case_id):
    """``(search_payload, commute_evidence)`` rebuilt from the case's frozen fixture.

    The commute records are shaped exactly as ``validate_search_payload_with_provider``
    emits them — the shape observed in the archive's passing runs — so the formatter sees
    what it saw live.
    """
    results = json.loads(
        (_FIXTURES / f"ho6_{case_id.split('-')[1]}_search.json").read_text())["results"]
    search = next(r["data"] for r in results if r["tool_name"] == "search_properties")
    evidence = []
    for rec in results:
        if rec["tool_name"] != "calculate_commute":
            continue
        raw = rec["data"]
        evidence.append({
            "candidate_key": raw["candidate_key"],
            "from_address": raw["from_address"],
            "to_address": raw["to_address"],
            "mode": "transit",
            "success": True,
            "evidence_status": "success",
            "duration_minutes": raw["duration_minutes"],
            "raw_data": raw,
        })
    return dict(search), evidence


def _dimension_fanout_state(case_id):
    """The state legacy hands to format_output on a post-search dimension fan-out turn.

    Mirrors what ``execute_tool_node`` and ``gather_wave_node`` actually write: the search
    artifact in ``tool_artifacts``, the search payload still on ``tool_raw_data`` with the
    wave's per-task raws merged over it, more than one ``observations`` entry (that is what
    flips ``is_loop_synthesis``), and ``plan_origin == _PLAN_ORIGIN_DIMENSIONS``.
    """
    from core import langgraph_agent as lga

    search, evidence = _frozen(case_id)
    merged = dict(search)
    for i, ev in enumerate(evidence, start=1):
        merged[f"calculate_commute_{i}"] = ev["raw_data"]
    observations = [{"turn": 0, "tool": "search_properties", "observation": "SEARCH_OBS",
                     "params_digest": "d0"}]
    observations += [{"turn": 1, "tool": "calculate_commute", "observation": "COMMUTE_OBS",
                      "params_digest": f"d{i}"} for i in range(1, len(evidence) + 1)]
    if len(observations) < 2:
        # HO6-238 fanned out on a non-commute dimension; one extra observation is all it
        # takes to be a loop synthesis, and the branch under test is the same one.
        observations.append({"turn": 1, "tool": "search_nearby_pois",
                             "observation": "POI_OBS", "params_digest": "d1"})
    return lga, {
        "tool_decision": {"tool": "search_properties"},
        "tool_raw_data": merged,
        "tool_artifacts": [{"turn": 0, "tool": "search_properties", "raw_data": dict(search),
                            "success": True, "params_digest": "d0"}],
        "observations": observations,
        "plan_origin": lga._PLAN_ORIGIN_DIMENSIONS,
        "commute_evidence": evidence,
        "final_response": "SYNTHESIS over listings + commute evidence",
        "user_preferences": {},
        "extracted_context": {},
        "accumulated_search_criteria": {},
    }


@pytest.mark.parametrize("case_id", _REGRESSED)
def test_dimension_fanout_mounts_the_full_contract(case_id, contract_keys):
    lga, state = _dimension_fanout_state(case_id)
    out = lga._make_format_output_node()(state)

    # The prose is still the model's multi-tool synthesis — the fix touches tool_data only.
    assert out["final_response"].startswith("SYNTHESIS")
    got = set(out["tool_data"])
    assert got == contract_keys, (
        f"{case_id}: dimension fan-out mounted {sorted(got)}, "
        f"missing {sorted(contract_keys - got)}")
    # Not merely present-and-empty: the deterministic candidate ledger has to be populated,
    # which is what the structured-contract metrics actually read.
    assert out["tool_data"]["candidate_states"], f"{case_id}: empty candidate_states"
    assert out["tool_data"]["eligible_recommendations"], f"{case_id}: no eligible listings"


@pytest.mark.parametrize("case_id", _COMMUTE_CASES)
def test_commute_evidence_survives_the_dimension_fanout(case_id):
    lga, state = _dimension_fanout_state(case_id)
    out = lga._make_format_output_node()(state)
    evidence = out["tool_data"]["commute_evidence"]
    assert evidence, f"{case_id}: commute evidence was dropped by the formatter"
    assert len(evidence) == len(state["commute_evidence"])
    assert all(e["evidence_status"] == "success" for e in evidence)


def test_non_dimension_loop_synthesis_is_unchanged():
    # Guard the blast radius: a plain multi-tool plan with no search artifact still returns
    # no listings card, exactly as before.
    from core import langgraph_agent as lga
    out = lga._make_format_output_node()({
        "tool_decision": {"tool": "search_properties"},
        "tool_raw_data": {"status": "found", "recommendations": [{"name": "X"}]},
        "final_response": "S", "user_preferences": {}, "plan_origin": "plan",
        "observations": [{"tool": "a"}, {"tool": "b"}],
        "extracted_context": {}, "accumulated_search_criteria": {},
    })
    assert out["tool_data"] == {}
