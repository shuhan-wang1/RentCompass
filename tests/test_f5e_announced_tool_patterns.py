"""`ANNOUNCED_TOOL_PATTERNS` exists twice and the two copies must not drift.

WHY IT IS DUPLICATED. ``deploy/probe_pool_answer.py`` is stdlib-only on purpose:
it runs inside the deploy path, under `sudo`, from arbitrary directories, where
the application package is not importable. So it cannot import the list from
``app/core/agent_loop.py``, and the list is written out twice.

WHY THAT NEEDS A TEST. The in-process check (agent_loop) stops such a turn being
shipped; the probe (deploy) catches one that was shipped anyway. If the two lists
diverge, the probe stops catching exactly the phrasings the runtime stopped
guarding, and nothing says so. The failure is silent in both directions.

This file imports the app-side constant; the PROBE must stay import-free, which
``test_deploy_answer_probe.py::test_the_probe_is_stdlib_only_so_it_runs_inside_the_deploy_path``
separately enforces.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "deploy" / "probe_pool_answer.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location("probe_pool_answer", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_defines_the_patterns_usably():
    probe = _probe_module()
    patterns = probe.ANNOUNCED_TOOL_PATTERNS
    assert isinstance(patterns, tuple) and patterns
    for pattern in patterns:
        re.compile(pattern)  # an uncompilable entry would break the probe at import
    assert len(set(patterns)) == len(patterns), "duplicate pattern"
    assert any(p.isascii() for p in patterns), "no English pattern"
    assert any(not p.isascii() for p in patterns), "no Chinese pattern"


def test_the_match_is_case_insensitive_and_reports_the_text_that_hit():
    probe = _probe_module()
    # The verdict message quotes what the answer actually said, so the matched
    # text is returned with its original casing, not the pattern source.
    assert probe.announced_tool("Let me search for listings") == "Let me search"
    assert probe.announced_tool("LET ME SEARCH for listings") == "LET ME SEARCH"
    assert probe.announced_tool("让我搜索一下该区域的房源") == "让我搜索"
    # A grounded answer that does not announce anything must not match.
    assert probe.announced_tool(
        "Peckham recorded 412 crimes per data.police.uk.") == ""
    # The auxiliary WITHOUT a lookup verb is deliberately not an announcement.
    assert probe.announced_tool("Let me know if you want the breakdown.") == ""
    assert probe.announced_tool("I'll be honest, that figure is noisy.") == ""


def test_the_stub_markers_are_identical_to_the_runtimes():
    """`agent_loop._mark_announced_stub` appends these sentences; the probe fails
    any answer carrying one. A reworded marker that the probe still looks for is a
    check that silently stops firing."""
    try:
        import core.agent_loop as agent_loop
    except Exception as exc:  # pragma: no cover - environment problem, not drift
        pytest.skip(f"core.agent_loop is not importable here: {exc!r}")
    probe = _probe_module()
    assert probe.ANNOUNCED_TOOL_STUB_MARKER_EN == agent_loop.ANNOUNCED_TOOL_STUB_MARKER_EN
    assert probe.ANNOUNCED_TOOL_STUB_MARKER_ZH == agent_loop.ANNOUNCED_TOOL_STUB_MARKER_ZH
    assert probe.ANNOUNCED_TOOL_STUB_MARKERS == (
        agent_loop.ANNOUNCED_TOOL_STUB_MARKER_EN,
        agent_loop.ANNOUNCED_TOOL_STUB_MARKER_ZH,
    )


def test_the_app_side_twin_is_identical():
    """agent_loop's copy and the probe's copy must be the same list.

    The app-side constant landed with the agent_loop half of P1-2 (the loop uses
    it to force one more bounded iteration before shipping such a turn). This is
    the drift guard for the two copies.
    """
    try:
        import core.agent_loop as agent_loop
    except Exception as exc:  # pragma: no cover - environment problem, not drift
        pytest.skip(f"core.agent_loop is not importable here: {exc!r}")

    patterns = getattr(agent_loop, "ANNOUNCED_TOOL_PATTERNS", None)
    if patterns is None:
        pytest.fail(
            "app/core/agent_loop.py no longer defines ANNOUNCED_TOOL_PATTERNS. It "
            "is the in-process half of P1-2 (a turn whose answer announces a tool "
            "call must not end with tool_batches == 0). If it was removed on "
            "purpose, remove the mirrored list and the check in "
            "deploy/probe_pool_answer.py in the same change — do not leave the "
            "probe enforcing a rule the runtime has dropped."
        )

    probe = _probe_module()
    assert tuple(patterns) == tuple(probe.ANNOUNCED_TOOL_PATTERNS), (
        "ANNOUNCED_TOOL_PATTERNS has drifted between "
        "app/core/agent_loop.py and deploy/probe_pool_answer.py; the probe cannot "
        "import from the app (it runs stdlib-only inside the deploy path), so the "
        "two literals must be kept identical by hand"
    )
