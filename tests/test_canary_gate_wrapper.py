from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy" / "run_canary_gate.sh"


def _fake(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run(tmp_path: Path, report_rc: int, *, rollback_rc: int = 0,
         report_stdout: str = ""):
    report_args = tmp_path / "report.args"
    weight_args = tmp_path / "weight.args"
    _echo = f"printf '%s\\n' {report_stdout!r}\n" if report_stdout else ""
    report = _fake(
        tmp_path,
        "report",
        f"printf '%s\\n' \"$@\" > \"$REPORT_ARGS\"\n{_echo}exit {report_rc}\n",
    )
    weight = _fake(
        tmp_path,
        "weight",
        f"printf '%s\\n' \"$@\" > \"$WEIGHT_ARGS\"\nexit {rollback_rc}\n",
    )
    result = subprocess.run(
        ["bash", str(GATE), "--input", "a.jsonl", "--stage", "c1"],
        cwd=ROOT,
        env={
            **os.environ,
            "CANARY_GATE_REPORT_CMD": str(report),
            "CANARY_GATE_WEIGHT_SCRIPT": str(weight),
            "REPORT_ARGS": str(report_args),
            "WEIGHT_ARGS": str(weight_args),
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result, report_args, weight_args


@pytest.mark.parametrize("report_rc", [0, 1, 2, 64])
def test_non_breach_status_never_changes_weight_and_is_preserved(tmp_path, report_rc):
    result, report_args, weight_args = _run(tmp_path, report_rc)
    assert result.returncode == report_rc
    assert report_args.read_text(encoding="utf-8").splitlines() == [
        "--input", "a.jsonl", "--stage", "c1"
    ]
    assert not weight_args.exists()
    assert "traffic weight is unchanged" in (result.stdout + result.stderr)


def test_exit_three_rolls_back_to_zero_and_preserves_breach_status(tmp_path):
    result, _, weight_args = _run(tmp_path, 3)
    assert result.returncode == 3
    assert weight_args.read_text(encoding="utf-8").splitlines() == ["--weight", "0"]
    assert "ROLLBACK_COMPLETE" in result.stderr


def test_rollback_failure_has_distinct_loud_exit(tmp_path):
    result, _, weight_args = _run(tmp_path, 3, rollback_rc=1)
    assert weight_args.exists()
    assert result.returncode == 70
    assert "ROLLBACK_FAILED" in result.stderr


def test_exit_three_on_an_invalid_measurement_refuses_the_rollback(tmp_path):
    """The second gate on the rollback verb.

    `canary_report.build_verdict` already refuses to return 3 for a window it has
    called unreadable, so this path should be unreachable. It is wired anyway
    because exit 3 here is not a report, it is `set_canary_weight.sh --weight 0`:
    a future regression in the report's precedence rule must not be able to move
    production traffic off a measurement the report itself disowned.
    """
    result, _, weight_args = _run(
        tmp_path, 3,
        report_stdout="[INSTRUMENTATION] measurement_valid=False",
    )

    assert result.returncode == 2
    assert not weight_args.exists(), "no rollback may be fired on an invalid window"
    assert "ROLLBACK_REFUSED" in result.stderr


def test_report_stdout_still_reaches_the_operator(tmp_path):
    """The verdict text is teed, not swallowed, and the report's rc is preserved."""
    result, _, _ = _run(tmp_path, 2, report_stdout="CANARY REPORT LINE")

    assert result.returncode == 2
    assert "CANARY REPORT LINE" in result.stdout
