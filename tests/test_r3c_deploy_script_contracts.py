"""Contracts the deploy scripts must hold that no single harness scenario shows.

R4-5. `deploy/set_canary_weight.sh` hardened `CANARY_ANSWER_PROBE_CMD` to
`${VAR-default}` after an injected-but-empty override drove real billed turns at
the live pools. The five URL variables beside it kept `${VAR:-default}`, so an
explicitly EMPTY override still resolved to the REAL `127.0.0.1:5001` /
`:5002` — the same shape as the incident that motivated the fix. Two assertions:
the expansion form (cheap, total) and the runtime refusal (proves the form is
load-bearing).

Nothing here is allowed to reach a real pool: `CANARY_CURL_CMD` is `/bin/false`
in every subprocess, so even a regression that skipped the refusal opens no
socket.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEIGHT_SCRIPT = REPO / "deploy" / "set_canary_weight.sh"
URL_VARS = (
    "CANARY_PUBLIC_URL",
    "CANARY_LEGACY_URL",
    "CANARY_CANDIDATE_URL",
    "CANARY_LEGACY_ANSWER_URL",
    "CANARY_CANDIDATE_ANSWER_URL",
)


def test_no_url_default_uses_the_colon_form():
    text = WEIGHT_SCRIPT.read_text()
    offenders = [name for name in URL_VARS if f"${{{name}:-" in text]
    assert offenders == [], (
        f"{offenders} still use ${{VAR:-default}}: an explicitly empty override "
        "would silently resolve to the real live pools"
    )
    for name in URL_VARS:
        assert f"${{{name}-" in text, f"{name} lost its default entirely"


@pytest.mark.parametrize("name", URL_VARS)
def test_an_explicitly_empty_url_is_refused_and_never_targets_a_real_port(name):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        # Belt AND braces: even if the refusal regressed, no socket can open.
        "CANARY_CURL_CMD": "/bin/false",
        "CANARY_ANSWER_PROBE_CMD": "/bin/true",
        "CANARY_ROUTE_CONF": "/nonexistent/rentcompass-canary-routing.conf",
        name: "",
    }
    result = subprocess.run(
        ["bash", str(WEIGHT_SCRIPT), "--status"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"an empty {name} must be refused"
    assert f"{name} is set but EMPTY" in combined
    assert "127.0.0.1:5001" not in combined and "127.0.0.1:5002" not in combined, (
        "the refusal must not have resolved the real pool addresses"
    )


def test_the_answer_probe_command_keeps_its_own_refusal():
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/tmp",
        "CANARY_CURL_CMD": "/bin/false",
        "CANARY_ANSWER_PROBE_CMD": "",
        "CANARY_ROUTE_CONF": "/nonexistent/rentcompass-canary-routing.conf",
    }
    result = subprocess.run(
        ["bash", str(WEIGHT_SCRIPT), "--status"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode != 0
    assert "CANARY_ANSWER_PROBE_CMD is set but empty" in result.stdout + result.stderr


def test_the_answer_probe_opt_out_reaches_every_layer():
    """R3-M2: only set_canary_weight.sh parsed it, so no caller could use it."""
    for name in ("update.sh", "release.sh", "switch_pool.sh", "set_canary_weight.sh"):
        text = (REPO / "deploy" / name).read_text()
        assert "--skip-answer-probe" in text, f"deploy/{name} cannot forward the opt-out"


def test_the_restore_leg_never_drives_a_billed_turn():
    text = (REPO / "deploy" / "update.sh").read_text()
    restore = text[text.index("restore_pre_drain_route() {"):]
    assert '--to "$DRAINED_FROM" --skip-answer-probe' in restore, (
        "returning traffic to the pool that was already serving it is a restore, "
        "not a new exposure decision, and a probe failure must never strand "
        "production on the drain target"
    )


def test_the_drain_unwind_is_armed_before_the_switch_runs():
    """R3-M5: a signal in the window used to leave production on the drain target."""
    text = (REPO / "deploy" / "update.sh").read_text()
    armed = text.index("""      DRAINED_FROM="$target"
      DRAIN_ACTIVE=1
      trap '_on_exit "$?"' EXIT""")
    switched = text.index('$SWITCH_CMD --to "$standby"')
    assert armed < switched, "the EXIT trap must be installed before the switch is invoked"


def test_every_deploy_script_parses():
    scripts = sorted((REPO / "deploy").rglob("*.sh"))
    assert scripts, "no deploy scripts found"
    for script in scripts:
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.relative_to(REPO)}: {result.stderr}"


def test_the_runbook_does_not_hardcode_one_operators_home_directory():
    text = (REPO / "docs" / "canary_runbook.md").read_text()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"/home/[a-z]+/uk_rent_recommendation", line)
    ]
    assert offenders == [], (
        "the runbook must be usable from a checkout that is not this box's: "
        f"{offenders}"
    )


def test_the_monitor_manifest_matches_the_committed_monitor():
    """The reinstall step in the runbook is worthless if the manifest has rotted."""
    import hashlib

    monitor = REPO / "deploy" / "monitoring" / "rentcompass-monitor.sh"
    manifest = (REPO / "deploy" / "monitoring" / "rentcompass-monitor.sha256").read_text()
    digest = hashlib.sha256(monitor.read_bytes()).hexdigest()
    assert manifest.split()[0] == digest, (
        "regenerate with: (cd deploy/monitoring && sha256sum rentcompass-monitor.sh "
        "> rentcompass-monitor.sha256)"
    )
