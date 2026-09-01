"""The gate has to be able to START, and it has to read only the logs.

R2-8 / R3-M1.  ``deploy/run_canary_gate.sh`` defaulted its interpreter to a bare
``python``. There is no ``python`` on this host — PEP 394 leaves the unversioned
name optional and the distro ships ``python3`` — so the wrapper exited 127 before
the report ran: no verdict, and, on a zero-tolerance breach, no automatic
weight-0 rollback. A driver that branches on "non-zero is bad" reads that 127 as a
HOLD, which is the most dangerous possible reading: the gate looks like it spoke.

R2-6.  ``.runtime/logs/canary-legacy.jsonl.bak-20260831`` is a 2 973-record
pre-cleanup backup of the log sitting NEXT TO the log, inside the directory the
runbook tells operators to pass as ``--input``. It escapes only because its suffix
does not match. Pulling it in would double every record it copies: duplicate
request_ids — a HOLD whose stated reason is a duplicate-emission bug that is not
happening — and, for that file specifically, the mixed-schema HOLD the 08-31
cleanup existed to remove. The directory walk excluded it by accident; a glob did
not.

Every subprocess here runs against fakes in tmp_path with every path injected. No
deploy verb is executed: the weight controller is only ever reached on report exit
3, and no report in this file exits 3.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
GATE = _ROOT / "deploy" / "run_canary_gate.sh"


def _load_report():
    spec = importlib.util.spec_from_file_location(
        "r3b_inputs_canary_report", _ROOT / "scripts" / "canary_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_report()


# --------------------------------------------------------------------------- #
# R2-6 — input selection                                                      #
# --------------------------------------------------------------------------- #

def _log_line(request_id: str) -> str:
    return json.dumps({"event": "canary.turn", "telemetry_schema_version": 2,
                       "agent_arch": "legacy", "request_id": request_id,
                       "endpoint": "alex"}) + "\n"


@pytest.fixture
def logdir(tmp_path):
    """A log directory shaped like the real one, backup file and all."""
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "canary-legacy.jsonl").write_text(_log_line("r1") + _log_line("r2"),
                                                   encoding="utf-8")
    (directory / "canary-fc_loop.ndjson").write_text(_log_line("r3"), encoding="utf-8")
    (directory / "canary-old.log").write_text(_log_line("r4"), encoding="utf-8")
    # The hazards: a pre-cleanup backup of the log next to it, and an operator note.
    (directory / "canary-legacy.jsonl.bak-20260831").write_text(
        _log_line("r1") + _log_line("r2"), encoding="utf-8")
    (directory / "README.txt").write_text("not a log\n", encoding="utf-8")
    return directory


def test_a_directory_input_ignores_the_backup_next_to_the_log(logdir):
    names = {Path(p).name for p in cr.resolve_inputs([str(logdir)])}
    assert names == {"canary-legacy.jsonl", "canary-fc_loop.ndjson", "canary-old.log"}


def test_a_glob_input_ignores_it_too(logdir):
    """The half that was missing: the walk filtered by suffix, the glob did not."""
    names = {Path(p).name for p in cr.resolve_inputs([str(logdir / "*")])}
    assert "canary-legacy.jsonl.bak-20260831" not in names
    assert "README.txt" not in names
    assert names == {"canary-legacy.jsonl", "canary-fc_loop.ndjson", "canary-old.log"}


def test_the_backup_cannot_double_the_record_population(logdir):
    """The consequence the filter prevents, stated as the numbers the gate reads."""
    records, _ = cr.load_records([str(logdir / "*")])
    assert len(records) == 4
    violations = cr.validate_records(records, candidate_arch="fc_loop")["violations"]
    assert not any("duplicate records" in v for v in violations), violations


def test_an_explicitly_named_file_is_still_honoured(logdir):
    """The filter applies to DISCOVERY. An operator naming a file has stated an
    intent, and refusing it would break every ad-hoc investigation."""
    named = logdir / "README.txt"
    assert cr.resolve_inputs([str(named)]) == [str(named.resolve())]
    # ...including the backup itself, when that is deliberately what is being read.
    backup = logdir / "canary-legacy.jsonl.bak-20260831"
    assert cr.resolve_inputs([str(backup)]) == [str(backup.resolve())]


# --------------------------------------------------------------------------- #
# R2-8 — the wrapper's interpreter                                            #
# --------------------------------------------------------------------------- #

def _fake_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _sandbox(tmp_path: Path, *, with_python3: bool):
    """A gate sandbox: fake report, fake weight controller, controlled PATH."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    report = repo / "scripts" / "canary_report.py"
    marker = tmp_path / "report.ran"
    report.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "sys.exit(0)\n",
        encoding="utf-8")
    weight = _fake_script(tmp_path / "weight.sh",
                          f"printf 'called' > {str(tmp_path / 'weight.ran')!r}\nexit 0\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "printf", "cd", "dirname", "pwd", "uname"):
        real = subprocess.run(["bash", "-lc", f"command -v {tool} || true"],
                              text=True, capture_output=True).stdout.strip()
        if real and os.path.isfile(real):
            os.symlink(real, bindir / tool)
    if with_python3:
        os.symlink(sys.executable, bindir / "python3")

    env = {
        "PATH": str(bindir),
        "CANARY_GATE_REPO": str(repo),
        "CANARY_GATE_REPORT_SCRIPT": str(report),
        "CANARY_GATE_WEIGHT_SCRIPT": str(weight),
        # Deliberately absent: CANARY_GATE_PYTHON, PYTHON, CANARY_GATE_REPORT_CMD.
        "HOME": str(tmp_path),
        "LANG": "C",
    }
    return env, marker, tmp_path / "weight.ran"


def test_the_gate_runs_on_a_host_that_has_only_python3(tmp_path):
    env, marker, weight_ran = _sandbox(tmp_path, with_python3=True)
    result = subprocess.run(["bash", str(GATE), "--input", "logs/", "--stage", "c1"],
                            env=env, text=True, capture_output=True, timeout=60)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "--input logs/ --stage c1", (
        "the report must actually have been executed, with its arguments unchanged")
    assert not weight_ran.exists()


def test_an_explicit_interpreter_still_wins(tmp_path):
    env, marker, _ = _sandbox(tmp_path, with_python3=False)
    env["CANARY_GATE_PYTHON"] = sys.executable
    result = subprocess.run(["bash", str(GATE), "--input", "logs/"],
                            env=env, text=True, capture_output=True, timeout=60)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists()


def test_the_ambient_python_env_var_is_used_when_nothing_else_is_set(tmp_path):
    env, marker, _ = _sandbox(tmp_path, with_python3=False)
    env["PYTHON"] = sys.executable
    result = subprocess.run(["bash", str(GATE), "--input", "logs/"],
                            env=env, text=True, capture_output=True, timeout=60)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists()


def test_no_interpreter_at_all_is_loud_and_is_not_a_verdict(tmp_path):
    """The failure that used to be exit 127. It must not collide with a gate
    verdict code (0/2/3), and it must not silently skip the rollback path."""
    env, marker, weight_ran = _sandbox(tmp_path, with_python3=False)
    result = subprocess.run(["bash", str(GATE), "--input", "logs/"],
                            env=env, text=True, capture_output=True, timeout=60)

    assert result.returncode == 69
    assert result.returncode not in (0, 2, 3)
    assert "CANARY_GATE_UNRUNNABLE" in result.stderr
    assert "NO verdict" in result.stderr
    assert not marker.exists() and not weight_ran.exists()


def test_the_wrapper_never_defaults_to_a_bare_python():
    """Source guard: the one-character fix is also the one-character regression."""
    source = GATE.read_text(encoding="utf-8")
    assert 'CANARY_GATE_PYTHON:-python}' not in source
    assert "python3" in source
