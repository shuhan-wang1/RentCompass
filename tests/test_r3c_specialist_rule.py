"""One rule for `X-Agent-Specialists`, in five files, that today's images can pass.

THE DEFECT (R3-H1). `X-Agent-Specialists` does not exist in `origin/main`, so
NEITHER container running on this box emits it. The PR added an equality check to
`deploy/switch_pool.sh`'s single-upstream path without the exemption
`set_canary_weight.sh::verify_local` and `probe_pool_answer.py` already carried,
so `switch_pool.sh --to legacy` — the documented emergency rollback — and the
drain leg of `update.sh --pool fc` both refused against the very images they exist
to protect. 221 harness assertions were green because every fake pool in the
harnesses emitted the header.

The rule is now one rule: the header is REQUIRED only when the EXPECTED identity
has specialists=1. When 0 is expected an absent header (`''` or `none`) counts as
0, for any architecture — legacy and every pre-2026-08-31 candidate image alike.

Four shell copies must stay byte-identical; a fifth implementation is the Python
one in the answer probe. The copies are compared here because they cannot be
shared: the monitor is installed standalone into /usr/local/bin, and the deploy
scripts are run under sudo from arbitrary directories.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
COPIES = (
    REPO / "deploy" / "update.sh",
    REPO / "deploy" / "switch_pool.sh",
    REPO / "deploy" / "set_canary_weight.sh",
    REPO / "deploy" / "monitoring" / "rentcompass-monitor.sh",
)
BLOCK = re.compile(
    r"# --- CANONICAL specialist-header rule.*?\nspecialists_shown\(\).*?\n\}\n",
    re.DOTALL,
)


def _block(path: Path) -> str:
    match = BLOCK.search(path.read_text())
    assert match is not None, f"{path} has no canonical specialist-rule block"
    return match.group(0)


def test_every_shell_copy_of_the_rule_is_byte_identical():
    blocks = {path: _block(path) for path in COPIES}
    reference = blocks[COPIES[0]]
    for path, text in blocks.items():
        assert text == reference, (
            f"{path.relative_to(REPO)} has drifted from the canonical rule in "
            f"{COPIES[0].relative_to(REPO)}; the copies exist because the monitor "
            "is installed standalone, not because they may differ"
        )


@pytest.mark.parametrize(
    ("observed", "expected", "accepted"),
    [
        ("0", "0", True),
        ("1", "1", True),
        # TODAY'S IMAGES: no header at all.
        ("none", "0", True),
        ("", "0", True),
        # The bit the manager_v1 rollout is gated on may never be inferred.
        ("none", "1", False),
        ("", "1", False),
        ("0", "1", False),
        ("1", "0", False),
        # `_expected_specialists` is literally `none` when the monitor cannot
        # resolve what the edge should be running; that stays a failure.
        ("0", "none", False),
    ],
)
def test_the_shell_rule_behaves_the_same_in_every_copy(observed, expected, accepted):
    for path in COPIES:
        script = _block(path) + f'\nspecialists_ok "{observed}" "{expected}"\n'
        result = subprocess.run(["bash", "-c", script], capture_output=True)
        assert (result.returncode == 0) is accepted, (
            f"{path.relative_to(REPO)}: specialists_ok({observed!r}, {expected!r}) "
            f"returned {result.returncode}"
        )


def test_the_python_twin_agrees_with_the_shell_rule():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "probe_pool_answer", REPO / "deploy" / "probe_pool_answer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # An absent header is excused for ANY arch whose expected bit is 0 — the fc
    # pool's image predates the header exactly as the legacy pool's does.
    assert module.specialists_match("", "0", "fc_loop") is True
    assert module.specialists_match("", "0", "legacy") is True
    assert module.specialists_match("", "none", "manager_v1") is True
    # ...and never excused when 1 is expected.
    assert module.specialists_match("", "1", "manager_v1") is False
    assert module.specialists_match("0", "1", "manager_v1") is False
    assert module.specialists_match("1", "0", "fc_loop") is False
    # An empty EXPECTATION means "the caller does not care", unchanged.
    assert module.specialists_match("1", "", "fc_loop") is True


def test_no_deploy_script_recommends_demoting_production_to_legacy():
    """R3-H3: the flip gate's remedy used to say `switch_pool.sh --to legacy`.

    That is a production DOWNGRADE (this box has served the fc_loop architecture
    since 07-27), and it was itself broken by R3-H1. A remedy that makes things
    worse is worse than no remedy.
    """
    release = (REPO / "deploy" / "release.sh").read_text()
    gate = release[release.index("4a. Rollout preflight"):]
    assert "switch_pool.sh --to legacy" not in gate
    assert "--weight 0" not in gate
