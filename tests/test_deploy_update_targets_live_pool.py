"""`deploy/update.sh` must deploy the pool the PUBLIC upstream actually serves.

The old script hardcoded `docker compose up -d --build app` and health-checked
:5001. That was right only while nginx pointed at the legacy pool. Once the public
upstream moved to fc (:5002) the script rebuilt a pool nobody was served by and
still printed "Healthy ✅  Live at https://rentcompass.co.uk:8443" — a green deploy
that changed nothing the public could see, while silently recreating `app`, the
standing rollback escape hatch, on every run.

The proof lives in ``deploy/test_update_assertions.sh``, which runs the REAL script
against injected fakes (no docker, no root, no nginx, no network) — the same
rehearsal pattern as ``deploy/switch_pool_rehearse.sh``. This module runs it under
pytest so CI cannot go green while that rehearsal is failing, and surfaces the
failing assertion lines in the pytest report.
"""

import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REHEARSAL = os.path.join(_ROOT, "deploy", "test_update_assertions.sh")


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                    reason="the rehearsal builds a throwaway git repo; needs bash + git")
def test_update_sh_rehearsal_passes():
    proc = subprocess.run(
        ["bash", _REHEARSAL],
        cwd=_ROOT, capture_output=True, text=True, timeout=300,
    )
    failed = [ln for ln in proc.stdout.splitlines() if "FAIL" in ln]
    assert proc.returncode == 0, (
        "deploy/update.sh rehearsal failed:\n"
        + "\n".join(failed or proc.stdout.splitlines()[-40:])
        + ("\n--- stderr ---\n" + proc.stderr if proc.stderr.strip() else "")
    )
    # A rehearsal that silently stopped asserting would also "pass".
    assert "passed 4" in proc.stdout, f"unexpectedly few assertions ran:\n{proc.stdout[-2000:]}"


def test_pin_gate_survived_the_rewrite():
    """The pin gate is the only thing standing between production and an arbitrary
    commit. It is quoted here by its markers so a future rewrite of update.sh cannot
    drop it without a test failing."""
    src = open(os.path.join(_ROOT, "deploy", "update.sh"), encoding="utf-8").read()
    assert "# >>> PIN GATE START" in src and "# <<< PIN GATE END" in src
    for required in (
        "DEPLOY_PINNED_SHA",
        "is not the pinned release",
        "tracked working tree is DIRTY",
    ):
        assert required in src, f"pin gate lost its {required!r} check"


def test_fc_pool_is_never_built_from_the_working_tree():
    """compose gives the fc pool no `build:` on purpose — "the working tree can never
    silently become what canary traffic executes". The deploy path must honour that by
    building from an isolated worktree at the pin, not from `.`."""
    src = open(os.path.join(_ROOT, "deploy", "update.sh"), encoding="utf-8").read()
    assert "worktree add --detach" in src
    assert '$DOCKER_CMD build -t "$FC_IMAGE" "$tree"' in src
    # compose must never be asked to --build the fc service.
    assert "--build app-fc" not in src

    compose = open(os.path.join(_ROOT, "docker-compose.yml"), encoding="utf-8").read()
    fc_block = compose.split("app-fc:", 1)[1].split("\n  volumes:", 1)[0]
    assert "build:" not in fc_block, "app-fc gained a build: — the immutability invariant is gone"
