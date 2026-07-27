"""The monitor that guards production must be the monitor that is in git — asserted, not promised.

THE DEFECT. `rentcompass-monitor.sh` exists in three places, and on 2026-07-27 all three
were different builds:

    A  deploy/monitoring/rentcompass-monitor.sh   git — nominally the source of truth
    B  /usr/local/bin/rentcompass-monitor.sh      the stable copy the systemd timer runs
    C  /home/shuhan/uk_rent_recommendation/…      the pinned production tree

Measured sha256 prefixes that day: A `43f05af09a29`, B `678073d06356`, C `4a4273ccb9d8`.
B is what actually guarded production, and it was NEITHER of the other two. The A/B split
is deliberate (the tracked unit's ExecStart names C, and an untracked override.conf
redirects it to B, so a frozen deploy pin cannot hold the monitor hostage) — but nothing
compared them, so "which of these three is guarding production" was unanswerable, and the
improvements that stop the monitor screaming a false alarm every five minutes lived only
in an untracked root-owned file. A rebuild would have silently reverted production to C.

The knowledge that this had happened DID already exist — as prose, in the docstring of
tests/test_monitor_legacy_pin_alert.py ("NOTE FOR THE DEPLOYER: the repo copy is NOT what
production runs"). That note is exactly the kind of promise this project distrusts: it was
true, it was accurate, and it changed nothing, because no assertion depended on it.

WHY THE GUARD IS SHAPED LIKE THIS. A single test cannot cover this defect, because the two
copies are never visible from the same place at the same time:

  * CI and the bench container cannot see /usr/local/bin at all, and the file is
    root-owned, so a test can never be the whole answer;
  * the box CAN see both, but nothing there runs pytest on a schedule.

So the guard is three mechanisms, each load-bearing where the others cannot reach:

  1. THE MANIFEST (section 1, always runs). rentcompass-monitor.sha256 records the hash of
     the committed script, and ``test_the_manifest_describes_the_committed_script`` fails
     the moment anyone edits the script without regenerating it. This is what makes the
     expected hash trustworthy enough for the other two to compare against — a guard whose
     reference value can rot silently is not a guard.
  2. SELF-REPORTED PROVENANCE (section 2, always runs). The monitor hashes ITSELF and leads
     every status line with `src=<12 hex>`. /var/log/rentcompass/monitor.log therefore
     records which build produced each line, with no root, no network and no CI involved.
     This is the mechanism that works when nobody is looking, which is the condition under
     which the original drift survived. Section 2 pins that the token is emitted, that it
     is derived from the file itself, and that computing it can never kill a run.
  3. THE COMPARISON (sections 3-4). Section 3 pins the alert that fires when an installer
     declares a build and the running copy is not it. Section 4 is the direct file
     comparison, and it runs ONLY on the box — skipped in CI, honestly, rather than
     faked.

``test_the_installed_copy_matches_the_committed_script`` IS RED ON THIS BOX TODAY, by
design: production is genuinely diverged, and a guard that went green while the defect it
names is still present would be worse than no guard. It turns green when the operator runs
the one install command in deploy/monitoring/README.md.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MON_DIR = _ROOT / "deploy" / "monitoring"
_MONITOR = _MON_DIR / "rentcompass-monitor.sh"
_MANIFEST = _MON_DIR / "rentcompass-monitor.sha256"
_CHECKER = _MON_DIR / "check_install_drift.sh"
_README = _MON_DIR / "README.md"
_UNIT = _MON_DIR / "rentcompass-monitor.service"

_SRC = _MONITOR.read_text(encoding="utf-8")

# The installed copy is world-readable (mode 0755 root:root), so a non-root test can hash
# it. It simply does not exist off the box.
_INSTALLED = Path(os.environ.get("MON_INSTALLED_PATH", "/usr/local/bin/rentcompass-monitor.sh"))
_on_the_box = pytest.mark.skipif(
    not _INSTALLED.is_file(),
    reason=f"{_INSTALLED} not present — not the production box (CI/bench container)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_hash() -> str:
    """The expected hash, parsed the way check_install_drift.sh parses it."""
    for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith("rentcompass-monitor.sh"):
            return parts[0]
    raise AssertionError(f"{_MANIFEST} names no hash for rentcompass-monitor.sh")


def _extract_block(anchor: str) -> str:
    """The `if … fi` block containing `anchor`, verbatim from the shipped script.

    Same idiom as tests/test_monitor_legacy_pin_alert.py, and for the same reason: the
    script cannot be executed here (check 10 makes a paid provider completion, checks 1-9
    curl production), and a transcribed copy of the condition would pass no matter what
    the script said — which is the defect this whole file is about.
    """
    lines = _SRC.splitlines()
    idx = next(i for i, ln in enumerate(lines) if anchor in ln)
    start = next(i for i in range(idx, -1, -1) if lines[i].startswith("if "))
    end = next(i for i in range(idx, len(lines)) if lines[i].rstrip() == "fi")
    return "\n".join(lines[start:end + 1])


def _extract_lines(pattern: str, count: int) -> str:
    """`count` consecutive shipped lines starting at the one matching `pattern`."""
    lines = _SRC.splitlines()
    idx = next(i for i, ln in enumerate(lines) if re.search(pattern, ln))
    return "\n".join(lines[idx:idx + count])


# ═══════════════════════════════════════════════════════════════════
# 1. The manifest must be true of the committed script
# ═══════════════════════════════════════════════════════════════════
#
# Everything else compares against this number, so if it can go stale unnoticed the rest
# of the guard is theatre.

def test_the_manifest_describes_the_committed_script():
    """FAILS the moment the script is edited without regenerating the manifest.

    This is the test that makes the expected hash citable. Regenerate with:
        bash deploy/monitoring/check_install_drift.sh --write-manifest
    """
    actual = _sha256(_MONITOR)
    assert _manifest_hash() == actual, (
        f"{_MANIFEST.name} is stale: it says {_manifest_hash()[:12]} but the committed "
        f"script hashes to {actual[:12]}. Run:\n"
        "    bash deploy/monitoring/check_install_drift.sh --write-manifest\n"
        "and commit the result. Until then every drift comparison on the box is "
        "meaningless, because it compares against a hash of code that no longer exists.")


def test_the_manifest_is_verifiable_with_stock_tooling():
    """`sha256sum -c` must accept it, so the manifest is checkable by someone who has
    never heard of check_install_drift.sh (a rebuilt box, a different operator)."""
    assert shutil.which("sha256sum"), "sha256sum missing from the test environment"
    r = subprocess.run(["sha256sum", "-c", _MANIFEST.name],
                       cwd=_MON_DIR, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"sha256sum -c rejected the manifest:\n{r.stdout}{r.stderr}"


def test_the_manifest_pins_one_file_and_names_it_relatively():
    """A relative name keeps `sha256sum -c` working from deploy/monitoring on any box; an
    absolute path baked at generation time would only verify on the machine that wrote it."""
    lines = [ln for ln in _MANIFEST.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one entry, got {lines}"
    digest, name = lines[0].split()
    assert name == "rentcompass-monitor.sh", name
    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest


# ═══════════════════════════════════════════════════════════════════
# 2. The monitor must report its own provenance, always, and safely
# ═══════════════════════════════════════════════════════════════════

def test_the_status_line_leads_with_the_self_hash():
    """Every status line must start with src=, including on runs that alert.

    FAILS ON THE OLD BEHAVIOUR: before this change `summary` was initialised to "", so
    monitor.log recorded what the monitor observed but never which build observed it.
    """
    assert 'summary="src=$SELF_SHA "' in _SRC, (
        "the summary no longer leads with src=$SELF_SHA; monitor.log would stop recording "
        "which build produced each line, which is the only evidence of install drift that "
        "survives without anyone running a check")
    # and the summary must still reach the log line that is written every run
    assert re.search(r'printf .*"\$ts" "\$status" "\$summary" >> "\$LOG_FILE"', _SRC), (
        "the always-appended status line no longer prints $summary")


def test_the_self_hash_is_computed_from_the_running_file():
    """It must hash `$0` — the file actually executing — not a repo path.

    Hashing a repo path would reproduce the original defect exactly: the installed copy
    would report the hash of a file it is not, which is worse than reporting nothing.
    """
    line = _extract_lines(r"^SELF_SHA=", 1)
    assert 'sha256sum "$0"' in line, (
        f"SELF_SHA is not derived from $0: {line!r} — it must hash the running file")


def test_the_self_hash_really_equals_the_file_it_runs_from(tmp_path):
    """Behavioural, not textual: run the SHIPPED lines with $0 pointing at a known file and
    check the token equals that file's real sha256 prefix. Pins the awk/substr arithmetic,
    which a text assertion would happily let drift to the wrong width."""
    target = tmp_path / "victim.sh"
    target.write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()[:12]

    block = _extract_lines(r"^SELF_SHA=", 2)
    # `bash -c <script> <name>` sets $0 to <name>, which is precisely how systemd's
    # ExecStart=/usr/local/bin/rentcompass-monitor.sh presents the path to the script.
    r = subprocess.run(["bash", "-c", block + '\nprintf %s "$SELF_SHA"', str(target)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, f"got {r.stdout!r}, expected {expected!r}"
    assert len(r.stdout) == 12, f"token width changed to {len(r.stdout)}"


def test_computing_the_provenance_can_never_fail_a_run(tmp_path):
    """A monitor that dies while introspecting itself is strictly worse than one that
    cannot name its own build. With $0 unreadable the block must yield `unknown` and
    succeed — under `set -u` an unset SELF_SHA would abort the whole run."""
    block = _extract_lines(r"^SELF_SHA=", 2)
    missing = tmp_path / "does-not-exist.sh"
    r = subprocess.run(["bash", "-c", "set -u\n" + block + '\nprintf %s "$SELF_SHA"',
                        str(missing)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"the provenance block failed the run: {r.stderr}"
    assert r.stdout == "unknown", r.stdout


# ═══════════════════════════════════════════════════════════════════
# 3. The declared-build alert: inert by default, real when declared
# ═══════════════════════════════════════════════════════════════════
#
# The constraint from b67f1fa still holds: today's steady state must stay at EXACTLY ONE
# genuine alert (`canary-legacy.jsonl missing`). An alert that is always on is an alert
# nobody reads — this file's own MON_EXPECTED_PUBLIC_ARCH comment records that happening,
# and 365 false alarms were fired after the 2026-07-26 cutover before it was caught.

_DRIFT_ANCHOR = "monitor build drift"
_GOOD = "7c7e7f9217ef" + "0" * 52
_BAD = "deadbeefcafe" + "0" * 52


@pytest.fixture()
def drift_block():
    return _extract_block(_DRIFT_ANCHOR)


def _run_drift(block: str, *, self_sha: str, expected: str, prev: str = "") -> str:
    preamble = textwrap.dedent("""
        set -u
        emit_alert() { shift; printf 'ALERT %s\\n' "$*"; }
        declare -A PREV
        declare -A NOW
    """)
    if prev:
        preamble += f'PREV[src_sha]="{prev}"\n'
    script = (f'{preamble}\nSELF_SHA="{self_sha}"\nEXPECTED_SRC_SHA="{expected}"\n'
              f'{block}\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_stays_silent_when_no_build_is_declared(drift_block):
    """TODAY'S STATE. MON_EXPECTED_SRC_SHA is unset, so this must add nothing to the
    steady state — the `src=` token in the log carries the information instead."""
    assert _run_drift(drift_block, self_sha="7c7e7f9217ef", expected="") == ""


def test_stays_silent_when_the_declared_build_is_the_running_build(drift_block):
    assert _run_drift(drift_block, self_sha="7c7e7f9217ef", expected=_GOOD) == ""


def test_stays_silent_when_the_hash_could_not_be_computed(drift_block):
    """`unknown` means sha256sum failed, not that the build is wrong. Paging there would
    turn an unreadable $0 into a false 'production is running the wrong monitor'."""
    assert _run_drift(drift_block, self_sha="unknown", expected=_GOOD) == ""


def test_fires_when_the_running_build_is_not_the_declared_build(drift_block):
    """Guards the guard: a check that never fires is not a check."""
    out = _run_drift(drift_block, self_sha="7c7e7f9217ef", expected=_BAD)
    assert out.startswith("ALERT monitor build drift:"), out
    assert "7c7e7f9217ef" in out and "deadbeefcafe" in out
    assert "check_install_drift.sh" in out, "the alert must name the tool that diagnoses it"


def test_reports_once_per_state_change_not_every_five_minutes(drift_block):
    """A wrong build is a standing condition. Re-reporting it every 5 min is the
    always-on-alert failure this codebase has already paid for once."""
    assert _run_drift(drift_block, self_sha="7c7e7f9217ef", expected=_BAD,
                      prev="7c7e7f9217ef") == ""


def test_the_alert_is_an_error_not_a_warning(drift_block):
    """An unexpected monitor build makes every other check in the file of unknown
    vintage, so it is sev3."""
    assert "emit_alert 3" in drift_block, drift_block


def test_the_declared_build_is_compared_only_against_an_explicit_declaration():
    """THE SUBTLE CORRECTNESS POINT. The check must NOT key on
    $REPO/deploy/monitoring/rentcompass-monitor.sh. The pinned production tree is
    deliberately older than the installed copy — that asymmetry is why override.conf
    exists — so comparing against it would page sev3 every five minutes about the
    intended arrangement, reproducing the 2026-07-26 false alarm in a new place."""
    block = _extract_block(_DRIFT_ANCHOR)
    assert "$REPO" not in block, (
        "the build-drift check reads $REPO; the pinned tree is intentionally stale, so "
        "this would be an always-on alert")
    assert 'EXPECTED_SRC_SHA="${MON_EXPECTED_SRC_SHA:-}"' in _SRC, (
        "the declaration must default to empty, i.e. inert unless an installer sets it")


# ═══════════════════════════════════════════════════════════════════
# 4. The comparison that only the box can make
# ═══════════════════════════════════════════════════════════════════

def test_the_drift_checker_ships_and_is_executable():
    assert _CHECKER.is_file(), f"{_CHECKER} missing"
    assert os.access(_CHECKER, os.X_OK), f"{_CHECKER} is not executable"
    r = subprocess.run(["bash", "-n", str(_CHECKER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_drift_checker_compares_all_three_copies_and_the_execstart():
    """The checker is the only mechanism that sees both files at once, so its coverage is
    pinned here: manifest, installed copy, and the ExecStart systemd actually resolved.
    Dropping the ExecStart comparison would let the tracked unit be re-copied without the
    override.conf and silently revert the timer to the pinned tree's old monitor."""
    src = _CHECKER.read_text(encoding="utf-8")
    assert "MON_INSTALLED_PATH" in src
    assert "ExecStart" in src and "systemctl show" in src
    assert "--write-manifest" in src


@_on_the_box
def test_the_installed_copy_matches_the_committed_script():
    """THE GUARD THAT WAS MISSING. Red on this box today (installed 678073d06356 vs
    committed) and green after the documented install command — which is the correct
    behaviour for a guard whose subject is genuinely broken."""
    installed = _sha256(_INSTALLED)
    committed = _sha256(_MONITOR)
    assert installed == committed, (
        f"INSTALL DRIFT: {_INSTALLED} is src={installed[:12]} but the committed monitor is "
        f"src={committed[:12]}. Production is not running the monitor in git. Install with:\n"
        f"    sudo install -m 0755 deploy/monitoring/rentcompass-monitor.sh {_INSTALLED}\n"
        "then re-run: bash deploy/monitoring/check_install_drift.sh")


@_on_the_box
def test_systemd_runs_the_copy_this_test_just_hashed():
    """Hashing the right file proves nothing if the timer executes a different one. The
    tracked unit's ExecStart names the pinned tree; only an untracked override.conf points
    it at the installed copy, so this is the check that the redirect is still in place."""
    if not shutil.which("systemctl"):
        pytest.skip("no systemctl")
    r = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value",
                        "rentcompass-monitor.service"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("rentcompass-monitor.service not loaded on this host")
    m = re.search(r"path=([^ ;]+)", r.stdout)
    assert m, r.stdout
    assert m.group(1) == str(_INSTALLED), (
        f"the timer runs {m.group(1)}, not {_INSTALLED}. If that path is inside a pinned "
        "deploy tree, production is running whatever monitor that commit carried — "
        "restore the drop-in: systemctl cat rentcompass-monitor.service")


# ═══════════════════════════════════════════════════════════════════
# 5. The install procedure must stay documented
# ═══════════════════════════════════════════════════════════════════
#
# The original drift was not created by a bad install — it was created by a good install
# whose steps existed only in someone's shell history.

def test_the_readme_documents_installing_the_stable_copy():
    doc = _README.read_text(encoding="utf-8")
    assert "/usr/local/bin/rentcompass-monitor.sh" in doc, (
        "the README does not mention the copy that actually guards production")
    assert "install -m 0755" in doc, "no copy-pasteable install command"
    assert "check_install_drift.sh" in doc, (
        "the README does not tell the operator how to verify the install took")


def test_the_readme_explains_why_the_tracked_unit_is_overridden():
    """The trap that cost three divergent copies: the tracked ExecStart is not the path
    that runs. If the README ever stops saying so, the next operator re-copies the unit,
    loses the drop-in, and production reverts to the pinned tree's monitor."""
    doc = _README.read_text(encoding="utf-8")
    assert "override.conf" in doc
    assert "ExecStart" in doc
    unit = _UNIT.read_text(encoding="utf-8")
    assert "/home/shuhan/uk_rent_recommendation/deploy/monitoring/rentcompass-monitor.sh" in unit, (
        "the tracked unit's ExecStart changed; the README's explanation of the override "
        "now describes something that is no longer true")


def test_the_readme_documents_the_provenance_token():
    """`src=` is only useful if a reader knows what it is."""
    doc = _README.read_text(encoding="utf-8")
    assert "src=" in doc, "the README does not explain the src= token in the status line"
