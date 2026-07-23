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
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"

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


@pytest.mark.parametrize("case_id", ["G2", "G3", "E11"])
def test_amended_cases_are_in_sync_across_every_shard(case_id):
    """The three cases this branch amends, pinned explicitly: they are the ones that
    actually went out of sync."""
    shards = _load_all()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    first = next(iter(shards.values()))
    for name, case in shards.items():
        assert case == first, f"{case_id} differs in {name} from the Base98 definition"
