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


def _run(tmp_path: Path, report_rc: int, *, rollback_rc: int = 0):
    report_args = tmp_path / "report.args"
    weight_args = tmp_path / "weight.args"
    report = _fake(
        tmp_path,
        "report",
        f"printf '%s\\n' \"$@\" > \"$REPORT_ARGS\"\nexit {report_rc}\n",
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
