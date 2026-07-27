"""`evaluation/benchmark/validate.py` must reject `expected_tools: ["market_info"]`.

FAILS ON THE OLD BEHAVIOUR. On mainline 4f410ab the validator checked both trace-matched tool
fields against a hand-copied literal:

    REAL_TOOLS = { "search_properties", ... }        # "the 14 real registry tools"
    PSEUDO_ROUTES = { "market_info", ... }
    VALID_TARGETS = REAL_TOOLS | PSEUDO_ROUTES
    ...
    for field in ("expected_tools", "forbidden_tools"):
        if tool not in VALID_TARGETS: problems.append(...)

Two defects in four lines. The list was a COPY of `create_tool_registry()`, so it could go
stale silently; and the union with `PSEUDO_ROUTES` meant a router decision validated clean in
a field the graders match against the EXECUTED TOOL TRACE.

That is how F7 shipped as `expected_tools: ["market_info"]`. `market_info` is a graph route,
never a registry tool, so `graders.route_matches` — which scores `expected_tools` as a subset
of the trace — could never match it, for any run of any architecture. F7 lost a route point in
every round ever run. The CASE was fixed. The validator that should have caught it was not,
which means the next such case is unguarded. `market_info` is pinned below as the literal
regression.

WHICH EXISTING APPROACH WAS REUSED: `tests/test_case_contract_consistency.py`'s
`_registered_tool_names()` — `frozenset(create_tool_registry().tools)`. That sibling test
already derives the real-tool set from the live registry (and its docstring already names
validate.py's `REAL_TOOLS` as the same defect one level up). validate.py now derives it the
same way, from the same call, and `test_validator_and_sibling_test_agree_on_the_tool_set`
below pins the two together so the repo keeps ONE definition of "a real tool".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_VALIDATE = _ROOT / "evaluation" / "benchmark" / "validate.py"

sys.path[:0] = [p for p in (str(_ROOT / "app"), str(_ROOT / "src"), str(_ROOT))
                if p not in sys.path]

from evaluation.benchmark import validate as V  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
# 1. THE regression: a pseudo-route in a trace-matched field is rejected
# ═══════════════════════════════════════════════════════════════════

_MINIMAL = {
    "case_id": "F7",
    "category": "F_grounding",
    "conversation_history": [],
    "expected_constraints": [],
    "failure_conditions": ["fabricates"],
}


def _case(**over):
    c = dict(_MINIMAL)
    c.update(over)
    return c


def _problems(**over):
    """The tool/route problems the REAL validator reports for one case.

    Calls `validate.tool_field_problems`, which is the code path `main()` itself runs — not a
    re-implementation of the rule, which would have passed on the broken version too. Isolated
    from main() because main()'s other checks are corpus-WIDE (category coverage, smoke
    counts) and a one-case fixture trips all of them for reasons unrelated to tool names."""
    return V.tool_field_problems("F7", _case(**over), V.registered_tool_names())


@pytest.mark.parametrize("field", ["expected_tools", "forbidden_tools"])
def test_market_info_is_rejected_in_a_trace_matched_field(field):
    """THE regression, pinned on the literal value that got through: `market_info`.

    On 4f410ab this reports NOTHING, because `market_info` is in PSEUDO_ROUTES and
    PSEUDO_ROUTES was unioned into the very set both trace-matched fields were checked
    against."""
    probs = _problems(**{field: ["market_info"]})
    assert probs, (
        f"validator ACCEPTED {field}=['market_info'] — a pseudo-route can never appear in an "
        "executed tool trace, so the case is unsatisfiable by construction (this is F7)")
    assert "market_info" in probs[0] and "pseudo-route" in probs[0]


def test_market_info_is_rejected_end_to_end_through_main(tmp_path, monkeypatch, capsys):
    """The same regression through `main()` as a human runs it, so the wiring is covered too.
    Asserted on the MESSAGE, not the exit code: a one-case fixture fails main()'s corpus-wide
    coverage checks either way, so only the message distinguishes fixed from broken."""
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(_case(expected_tools=["market_info"])) + "\n",
                          encoding="utf-8")
    monkeypatch.setattr(V, "CASES_PATH", cases_path)
    assert V.main() == 1
    err = capsys.readouterr().err
    assert "F7: expected_tools names the pseudo-route 'market_info'" in err, err


@pytest.mark.parametrize("route", sorted(V.PSEUDO_ROUTES))
def test_every_pseudo_route_is_rejected_in_expected_tools(route):
    """Not just `market_info`: none of the five router decisions can appear in a trace."""
    assert _problems(expected_tools=[route]), route


@pytest.mark.parametrize("route", sorted(V.PSEUDO_ROUTES))
def test_pseudo_routes_stay_legal_in_expected_route(route):
    """The inverse guard. `expected_route` is compared to the ROUTER's decision, not to a
    trace, so a pseudo-route there is correct and must not be condemned by the fix."""
    assert _problems(expected_route=route) == [], route


def test_a_real_registry_tool_is_still_accepted():
    """Guards the guard: a fix that rejected everything would also pass the tests above."""
    assert _problems(expected_tools=["search_properties"], forbidden_tools=["web_search"],
                     expected_route="search_properties") == []


def test_an_invented_tool_is_still_rejected():
    assert _problems(expected_tools=["fetch_the_moon"])


def test_an_invented_route_is_still_rejected():
    assert _problems(expected_route="teleport")


# ═══════════════════════════════════════════════════════════════════
# 2. The list is DERIVED, not copied
# ═══════════════════════════════════════════════════════════════════

def test_the_tool_set_comes_from_the_registry():
    from core.tool_system import create_tool_registry

    assert V.registered_tool_names() == frozenset(create_tool_registry().tools)


def test_validator_and_sibling_test_agree_on_the_tool_set():
    """One definition of "a real tool" in the repo. The sibling guard
    (tests/test_case_contract_consistency.py) got here first; validate.py now uses the same
    derivation rather than a second one."""
    from tests.test_case_contract_consistency import _registered_tool_names

    assert V.registered_tool_names() == _registered_tool_names()


def test_no_hand_copied_tool_list_survives_in_the_validator():
    """A source guard, because the defect was a LITERAL and a literal can come back. The old
    `REAL_TOOLS = {...}` block enumerated registry tool names in the module body; nothing but
    the pseudo-routes may do that now."""
    import re

    src = _VALIDATE.read_text(encoding="utf-8")
    # An ASSIGNMENT, not a mention: the module comment names the old constant on purpose, to
    # explain what was removed and why.
    assert not re.search(r"^REAL_TOOLS\s*=", src, re.MULTILINE), (
        "validate.py re-introduced a hand-copied REAL_TOOLS list; derive from "
        "create_tool_registry() instead")
    from core.tool_system import create_tool_registry

    quoted = [t for t in create_tool_registry().tools if f'"{t}"' in src]
    assert quoted == [], (
        f"validate.py hard-codes registry tool name(s) {quoted}; that is the copy whose "
        "staleness this change removed")


def test_the_pseudo_route_set_matches_the_sibling_guard():
    """PSEUDO_ROUTES is still a literal in both places (they name graph ROUTES, which have no
    registry to derive from), so they are pinned to each other instead."""
    from tests.test_case_contract_consistency import PSEUDO_ROUTES as SIBLING

    assert set(V.PSEUDO_ROUTES) == set(SIBLING)


def test_a_registry_tool_is_never_also_a_pseudo_route():
    """If the two sets ever intersected, the F7 check would reject a real tool."""
    assert not (V.registered_tool_names() & set(V.PSEUDO_ROUTES))


# ═══════════════════════════════════════════════════════════════════
# 3. The real corpus passes, end to end
# ═══════════════════════════════════════════════════════════════════

def test_the_shipped_cases_file_validates():
    """The validator must be runnable exactly as documented, from the repo root, and the
    corpus as shipped must be clean under the STRICTER rule (F7 having been fixed)."""
    r = subprocess.run([sys.executable, "-m", "evaluation.benchmark.validate"],
                       cwd=str(_ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "Registry tools (from create_tool_registry())" in r.stdout
