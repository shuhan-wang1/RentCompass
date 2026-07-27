"""The monitor's legacy-pool checks: the corrected comment, and the new pin-mismatch alert.

WHY THIS IS TESTED AT ALL, AND WHY NOT BY RUNNING THE SCRIPT. `rentcompass-monitor.sh` cannot
be executed here: check 10 makes an out-of-band PROVIDER COMPLETION (a paid call), and checks
1-9 curl production and shell out to `docker inspect` / `docker logs`. So the alert logic is
extracted FROM THE SHIPPED FILE by text and evaluated in isolation under `bash`. It is the
real block, not a transcription — a copy of the condition would pass no matter what the script
said, which is the same defect the rest of this changeset is about.

TWO CHANGES UNDER TEST (2026-07-27).

1. A CORRECTED COMMENT, no behaviour change. The "legacy pool cannot state its commit" alert
   was explained as "APP_CANDIDATE_SHA is unset for the 'app' service". That became false when
   compose wired `APP_CANDIDATE_SHA: "${LEGACY_APP_SHA:-}"` onto `app`. The knob exists; it is
   inert because LEGACY_APP_SHA is unset in the root .env AND the running container predates
   the wiring. The alert itself keys on the OBSERVED header ('unknown'/'none'), which is still
   the right trigger, so only the prose moved.

2. A NEW ALERT, symmetric with the fc pool's FC_CANARY_SHA pin-mismatch check, which had no
   legacy counterpart. The constraint on adding it was that today's steady state must stay at
   EXACTLY ONE genuine alert (`canary-legacy.jsonl missing` — the legacy pool serves nothing,
   so it writes no telemetry). ``test_stays_silent_in_todays_steady_state`` is that
   constraint, and ``test_stays_silent_while_the_pin_is_set_but_the_container_is_not_recreated``
   is the one that would have made this unsafe: setting LEGACY_APP_SHA without recreating
   `app` is the DOCUMENTED intermediate state, l_ver stays 'unknown', and a naive symmetric
   check would page sev3 every five minutes about an intended condition. That is the exact
   failure the MON_EXPECTED_PUBLIC_ARCH comment in this same file records as already having
   happened once.

NOTE FOR THE DEPLOYER: the repo copy is NOT what production runs. Production runs a stable
copy at /usr/local/bin/rentcompass-monitor.sh installed via a systemd override.conf, because
the unit's own ExecStart points at a deploy tree pinned to an old commit. These changes take
effect only after that copy is re-installed.

That note used to be the ONLY record of that fact, which made it a promise rather than a
guard — and on 2026-07-27 all three copies were measured to be three different builds while
this docstring sat here being accurate and changing nothing. The assertion now exists:
tests/test_monitor_install_provenance.py compares the installed copy against the committed
one (and against what systemd actually resolved), the monitor leads every status line with
`src=<hash>` so the running build is readable from monitor.log, and
deploy/monitoring/check_install_drift.sh is the one command that checks the lot.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MONITOR = _ROOT / "deploy" / "monitoring" / "rentcompass-monitor.sh"
_COMPOSE = _ROOT / "docker-compose.yml"

_SRC = _MONITOR.read_text(encoding="utf-8")


def _extract_block(anchor: str) -> str:
    """The `if … fi` block containing `anchor`, taken verbatim from the shipped script."""
    lines = _SRC.splitlines()
    idx = next(i for i, ln in enumerate(lines) if anchor in ln)
    start = next(i for i in range(idx, -1, -1) if lines[i].startswith("if "))
    end = next(i for i in range(idx, len(lines)) if lines[i].rstrip() == "fi")
    return "\n".join(lines[start:end + 1])


def _run_block(block: str, *, env_text: str, tmp_path: Path, **vars_) -> str:
    """Evaluate `block` with a stub emit_alert; return whatever it alerted."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".env").write_text(env_text, encoding="utf-8")
    preamble = textwrap.dedent(f"""
        set -u
        REPO={repo!s}
        emit_alert() {{ shift; printf 'ALERT %s\\n' "$*"; }}
        declare -A PREV
    """)
    assigns = "\n".join(f'{k}="{v}"' for k, v in vars_.items())
    script = f"{preamble}\n{assigns}\n{block}\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


_PIN_BLOCK_ANCHOR = 'l_pinned="$(sed -n'
_SHA_A = "c9e60c2d1ba3fadf41c731f094abdc94ba712bfd"
_SHA_B = "0000000111112222233333444445555566666777"


@pytest.fixture()
def pin_block():
    return _extract_block(_PIN_BLOCK_ANCHOR)


# ═══════════════════════════════════════════════════════════════════
# 1. The new alert must be INERT in today's steady state
# ═══════════════════════════════════════════════════════════════════

def test_stays_silent_in_todays_steady_state(pin_block, tmp_path):
    """TODAY: LEGACY_APP_SHA absent from the root .env, legacy answers 'unknown'. Two
    independent reasons this cannot fire — the steady state stays at its one genuine alert."""
    out = _run_block(pin_block, env_text="FC_CANARY_SHA=%s\n" % _SHA_A, tmp_path=tmp_path,
                     l_code="200", l_ver="unknown")
    assert out == "", f"the new check fires in the CURRENT steady state: {out!r}"


def test_stays_silent_while_the_pin_is_set_but_the_container_is_not_recreated(pin_block,
                                                                             tmp_path):
    """The documented intermediate state, and the reason this check excludes 'unknown'.
    docker-compose.yml says LEGACY_APP_SHA is set at the next PLANNED rebuild and that `app`
    must NOT be recreated meanwhile — so there is a window where the pin exists and the header
    still says 'unknown'. Alerting there would page every five minutes about an intended
    condition, and would also double-report the 'cannot state its commit' alert that already
    covers it."""
    out = _run_block(pin_block, env_text=f"LEGACY_APP_SHA={_SHA_A}\n", tmp_path=tmp_path,
                     l_code="200", l_ver="unknown")
    assert out == "", out


@pytest.mark.parametrize("l_ver", ["none", "unknown"])
def test_stays_silent_for_every_cannot_state_its_commit_value(pin_block, tmp_path, l_ver):
    """Both sentinel values the probe can produce are owned by the OTHER alert."""
    assert _run_block(pin_block, env_text=f"LEGACY_APP_SHA={_SHA_A}\n", tmp_path=tmp_path,
                      l_code="200", l_ver=l_ver) == ""


def test_stays_silent_when_the_pool_is_down(pin_block, tmp_path):
    """A pool that is not answering has its own sev3; a pin mismatch on top is noise."""
    assert _run_block(pin_block, env_text=f"LEGACY_APP_SHA={_SHA_A}\n", tmp_path=tmp_path,
                      l_code="000", l_ver="none") == ""


def test_stays_silent_when_the_pin_matches(pin_block, tmp_path):
    assert _run_block(pin_block, env_text=f"LEGACY_APP_SHA={_SHA_A}\n", tmp_path=tmp_path,
                      l_code="200", l_ver=_SHA_A) == ""


# ═══════════════════════════════════════════════════════════════════
# 2. …and must still FIRE on the failure it exists for
# ═══════════════════════════════════════════════════════════════════

def test_fires_on_a_real_pin_mismatch(pin_block, tmp_path):
    """Guards the guard: a check that never fires is not a check. Once legacy can name its
    commit, serving something other than the pinned image means the rollback target is not the
    image anyone believes it is."""
    out = _run_block(pin_block, env_text=f"LEGACY_APP_SHA={_SHA_A}\n", tmp_path=tmp_path,
                     l_code="200", l_ver=_SHA_B)
    assert out.startswith("ALERT legacy pool serves ")
    assert _SHA_B in out and _SHA_A in out


def test_the_legacy_check_mirrors_the_fc_check(pin_block):
    """Symmetry is the point: same trigger, same severity, same .env-derived pin. The only
    intended difference is the extra 'unknown' exclusion, which fc does not need because
    app-fc's APP_CANDIDATE_SHA is `:?`-required and so can never be unset."""
    fc_block = _extract_block('pinned="$(sed -n')
    assert 'sed -n \'s/^FC_CANARY_SHA=//p\'' in fc_block
    assert 'sed -n \'s/^LEGACY_APP_SHA=//p\'' in pin_block
    for frag in ('[ -r "$REPO/.env" ]', 'emit_alert 3', '| head -1'):
        assert frag in fc_block and frag in pin_block, frag
    assert '"$l_ver" != "unknown"' in pin_block, (
        "the legacy check must exclude 'unknown', or it fires during the documented "
        "pin-set-but-not-yet-recreated window")


def test_the_pinned_variable_is_the_one_compose_actually_reads():
    """Cross-file anti-drift: the monitor greps the root-.env variable that compose
    substitutes into the `app` service. A rename on either side makes this check inert."""
    assert 'APP_CANDIDATE_SHA: "${LEGACY_APP_SHA:-}"' in _COMPOSE.read_text(encoding="utf-8")
    assert "LEGACY_APP_SHA" in _SRC


# ═══════════════════════════════════════════════════════════════════
# 3. The corrected comment (item 3: a stale record that asserted a false thing)
# ═══════════════════════════════════════════════════════════════════

def test_the_false_unset_claim_is_gone():
    """It read "APP_CANDIDATE_SHA is unset for the 'app' service". compose sets it."""
    assert "APP_CANDIDATE_SHA is unset for the 'app' service" not in _SRC, (
        "the monitor still claims the `app` service has no APP_CANDIDATE_SHA; compose wires "
        'it as ${LEGACY_APP_SHA:-} (see tests/test_pool_identity_wiring.py)')


def test_the_cannot_state_its_commit_alert_still_fires_on_the_same_condition():
    """Behaviour must NOT have changed: the alert keys on the observed header value, which is
    what actually blocks switch_pool.sh, not on the configuration that produces it."""
    block = _extract_block("legacy pool cannot state its commit")
    assert '[ "$l_ver" = "unknown" ]' in block and '[ "$l_ver" = "none" ]' in block
    assert '[ "$l_code" = "200" ]' in block
    assert 'emit_alert 4' in block, "severity changed; it was a warning, not an error"
    assert '"${PREV[ver_legacy]:-}" != "$l_ver"' in block, (
        "the once-per-state-change guard is gone; this would alert every five minutes")


def test_the_replacement_explanation_names_the_real_blocker():
    """A corrected record has to be actionable, not merely not-wrong: the reader needs to know
    that BOTH setting LEGACY_APP_SHA and recreating the container are required."""
    idx = _SRC.index("legacy pool cannot state its commit")
    comment = _SRC[max(0, idx - 1600):idx]
    assert "LEGACY_APP_SHA" in comment
    assert re.search(r"recreat", comment), "the comment must say the container must be recreated"
    assert "escape hatch" in comment, "…and why it is not recreated now"
