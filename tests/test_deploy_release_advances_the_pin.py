"""`deploy/release.sh` is the scripted form of the re-pin procedure — and it must
never become a way to ship something the pin gate would have refused.

The gate in `deploy/update.sh` refuses to deploy anything but the exact sha named
in an untracked, server-local file, so that no commit to this repo can change what
production runs. On 2026-07-28 the *procedure* around that gate — three manual
steps in deploy/monitoring/README.md — was skipped, and a merged search fix sat
unshipped while the same bug was re-reported. release.sh automates the procedure
WITHOUT touching the gate: update.sh still enforces `HEAD == DEPLOY_PINNED_SHA`.

The behavioural proof lives in ``deploy/test_release_assertions.sh``, which runs
the REAL script against injected fakes (no docker, no root, no network) and
asserts mostly on what must NOT happen — no re-pin on a dirty tree, on red CI, or
without confirmation. This module runs it under pytest so CI cannot go green while
that rehearsal fails, and pins the structural invariants a later edit could quietly
drop.
"""

import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REHEARSAL = os.path.join(_ROOT, "deploy", "test_release_assertions.sh")
_RELEASE = os.path.join(_ROOT, "deploy", "release.sh")


def _src(path):
    return open(path, encoding="utf-8").read()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                    reason="the rehearsal builds a throwaway git repo; needs bash + git")
def test_release_sh_rehearsal_passes():
    proc = subprocess.run(
        ["bash", _REHEARSAL], cwd=_ROOT, capture_output=True, text=True, timeout=300,
    )
    failed = [ln for ln in proc.stdout.splitlines() if "FAIL" in ln]
    assert proc.returncode == 0, (
        "deploy/release.sh rehearsal failed:\n"
        + "\n".join(failed or proc.stdout.splitlines()[-40:])
        + ("\n--- stderr ---\n" + proc.stderr if proc.stderr.strip() else "")
    )
    summary = re.search(r"passed (\d+), failed 0", proc.stdout)
    assert summary and int(summary.group(1)) >= 46, (
        f"unexpectedly few assertions ran:\n{proc.stdout[-2000:]}")


def test_release_does_not_weaken_the_pin_gate():
    """release.sh may ADVANCE the pin; it may not bypass it. It must hand off to
    update.sh (which enforces the gate) rather than driving compose itself."""
    src = _src(_RELEASE)
    assert "deploy/update.sh" in src, "release.sh must delegate the deploy to update.sh"
    for forbidden in ("docker compose", "docker build", "--profile canary"):
        assert forbidden not in src, (
            f"release.sh drives {forbidden!r} directly — it would then be deploying "
            f"without the gate update.sh applies")

    gate = _src(os.path.join(_ROOT, "deploy", "update.sh"))
    assert "# >>> PIN GATE START" in gate and "# <<< PIN GATE END" in gate
    for required in ("DEPLOY_PINNED_SHA", "is not the pinned release",
                     "working tree is DIRTY", "tracked or untracked"):
        assert required in gate, f"the pin gate lost its {required!r} check"


def test_release_target_comes_from_a_remote_ref():
    """The releasable commit must come from a REMOTE tracking ref. A local branch
    can hold commits that never went through a PR, which is the whole reason the
    gate is allowed to be advanced automatically at all."""
    src = _src(_RELEASE)
    assert 'RELEASE_TRACK_REF:-origin/' in src, "the default track ref must be a remote ref"


def test_release_never_invokes_sudo_outside_the_pin_write():
    """One privileged action, and it is the pin file. A deploy script that
    escalates anywhere else is a much larger blast radius than it looks."""
    src = _src(_RELEASE)
    sudo_uses = [ln.strip() for ln in src.splitlines()
                 if "$SUDO_CMD" in ln and not ln.strip().startswith("#")]
    assert len(sudo_uses) == 1, f"expected exactly one privileged call, got: {sudo_uses}"
    assert "tee" in sudo_uses[0] and "PIN_ENV_FILE" in sudo_uses[0], sudo_uses[0]
