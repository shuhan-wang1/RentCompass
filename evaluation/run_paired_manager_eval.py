"""Run the offline fc_loop vs manager_v1+specialists promotion evaluation.

Each arm runs in a fresh Python process to prevent module singletons, ContextVars,
checkpointers, caches or environment switches leaking across arms.  The command exposes
no ``--live`` mode: it is deterministic, unbilled mechanics validation by construction.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from evaluation.paired_gate import (
    BLOCK,
    HOLD_REGRESSION,
    GateThresholds,
    evaluate_result_dirs,
    exit_code,
    write_report,
)
from evaluation.run_benchmark import REPO_ROOT, guard_output_dir


def _common_benchmark_args(args, *, out: Path) -> List[str]:
    command = [
        sys.executable,
        "-m", "evaluation.run_benchmark",
        "--offline",
        "--config", args.config,
        "--repeat", str(args.repeat),
        "--out", str(out),
        "--timestamp", args.timestamp,
        "--max-cost-usd", "0",
    ]
    if args.cases:
        command.extend(("--cases", args.cases))
    if args.fixtures_dir:
        command.extend(("--fixtures-dir", args.fixtures_dir))
    if args.case_schema:
        command.extend(("--case-schema", args.case_schema))
    if args.smoke:
        command.append("--smoke")
    if args.limit is not None:
        command.extend(("--limit", str(args.limit)))
    if args.category:
        command.extend(("--category", args.category))
    return command


def build_arm_commands(args, out: Path) -> tuple[List[str], List[str]]:
    """Build the two commands from one selector set; only architecture differs."""
    baseline = _common_benchmark_args(args, out=out / "fc_loop")
    baseline.extend(("--arch", "fc_loop"))
    candidate = _common_benchmark_args(args, out=out / "manager_v1")
    candidate.extend(("--arch", "manager_v1", "--manager-v1-specialists"))
    return baseline, candidate


def resolved_arm_flags(command: Sequence[str]) -> str:
    """Echo exactly which architecture/specialist flags an arm command resolved to.

    The two arms are only comparable if they differ in the architecture and nothing
    else; printing the resolved flags makes a mis-built arm visible in the log
    instead of only in the report's ``arm_contract`` row.
    """
    command = list(command)
    arch = (command[command.index("--arch") + 1] if "--arch" in command else "<unset>")
    specialists = "--manager-v1-specialists" in command
    return f"--arch {arch} --manager-v1-specialists={'on' if specialists else 'off'}"


def _prepare_output_dir(out: Path) -> None:
    """Create ``out``; refuse an existing directory with an actionable message.

    ``guard_output_dir`` already refuses a NON-EMPTY dir.  An existing but EMPTY dir
    used to fall through to ``mkdir(exist_ok=False)`` and surface as a bare
    ``FileExistsError`` traceback, which reads like a harness crash rather than the
    deliberate "one round, one fresh directory" rule that it is.
    """
    guard_output_dir(out)
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(
            f"refusing to write this paired round into the existing directory {out}. "
            f"A paired round owns its output directory: reusing one mixes two rounds' "
            f"arm artifacts and makes the report's identity binding unverifiable. "
            f"Pass a --out path that does not exist yet (e.g. {out}-2)."
        ) from None


def _offline_env(arch: str) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONHASHSEED": "0",
        "USE_MCP_TOOLS": "0",
        "AGENT_ARCH": arch,
        "MANAGER_V1_SPECIALISTS": "1" if arch == "manager_v1" else "0",
        # Prevent app/.env from installing a real credential in an offline process.
        "DEEPSEEK_API_KEY": "offline-eval-placeholder",
    })
    return env


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run_paired_manager_eval",
        description=("Offline, unbilled paired fc_loop vs manager_v1+specialists "
                     "mechanics evaluation."),
    )
    parser.add_argument("--config", default="routed_models")
    parser.add_argument("--cases", default=None)
    parser.add_argument("--fixtures-dir", default=None)
    parser.add_argument("--case-schema", default=None)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--min-pairs", type=int, default=GateThresholds.min_pairs)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument(
        "--out", default="evaluation/results/manager_v1_paired_offline",
        help="new, empty parent output directory",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.min_pairs < 1:
        raise SystemExit("--min-pairs must be >= 1")
    args.timestamp = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")

    out = Path(args.out).resolve()
    _prepare_output_dir(out)
    baseline_cmd, candidate_cmd = build_arm_commands(args, out)

    print(f"[paired] baseline=fc_loop (offline) {resolved_arm_flags(baseline_cmd)}",
          flush=True)
    baseline_rc = subprocess.run(
        baseline_cmd,
        cwd=REPO_ROOT,
        env=_offline_env("fc_loop"),
        check=False,
    ).returncode
    print(f"[paired] candidate=manager_v1+specialists (offline) "
          f"{resolved_arm_flags(candidate_cmd)}", flush=True)
    candidate_rc = subprocess.run(
        candidate_cmd,
        cwd=REPO_ROOT,
        env=_offline_env("manager_v1"),
        check=False,
    ).returncode

    report, pairs = evaluate_result_dirs(
        out / "fc_loop",
        out / "manager_v1",
        thresholds=GateThresholds(min_pairs=args.min_pairs),
        # This command has no --live mode by construction, so the arms are offline
        # regardless of what the arm summaries claim.
        offline_execution=True,
    )
    report["execution"] = {
        "baseline_return_code": baseline_rc,
        "candidate_return_code": candidate_rc,
        "pythonhashseed": "0",
        "network_or_model_calls_authorized": False,
        "selectors": {
            "config": args.config, "cases": args.cases, "smoke": args.smoke,
            "limit": args.limit, "category": args.category, "repeat": args.repeat,
        },
    }
    if baseline_rc != 0 or candidate_rc != 0:
        # A subprocess failure is missing measurement, never evidence of safety.  Preserve
        # a possible BLOCK already found in partial candidate artifacts; otherwise this is
        # a MEASURED failure of the round itself, so it is HOLD_REGRESSION (exit 4) rather
        # than the "offline cannot prove it" code -- a crashed arm is something to act on.
        if report["outcome"] != BLOCK:
            report["outcome"] = HOLD_REGRESSION
        report["promotable_modulo_offline_limits"] = False
        report["checks"].append({
            "name": "arm_processes",
            "outcome": "HOLD",
            "baseline": baseline_rc,
            "candidate": candidate_rc,
            "threshold": 0,
            "detail": "both arm processes must complete successfully",
        })
    write_report(out, report, pairs)
    # Small immutable command record; values contain paths/selectors only, never keys.
    (out / "paired_commands.json").write_text(
        json.dumps({"baseline": baseline_cmd, "candidate": candidate_cmd}, indent=2),
        encoding="utf-8",
    )
    # ``evaluate_result_dirs`` already printed the distinctiveness headline (it is the
    # gate's single source for that line); do not echo it a second time.
    print(
        f"[paired] outcome={report['outcome']} "
        f"exit={exit_code(report['outcome'])} paired_runs={report['paired_runs']} "
        f"promotable_modulo_offline_limits="
        f"{report.get('promotable_modulo_offline_limits')} "
        f"hold_reasons={report.get('hold_reasons')} "
        f"unsatisfied_prerequisites={report.get('unsatisfied_promotion_prerequisites')} "
        f"report={out / 'paired_report.json'}",
        flush=True,
    )
    # PROMOTE is structurally unreachable here and that is deliberate: this command
    # has no --live mode, so three security prerequisites can never be satisfied and
    # an exit 0 would read as "cleared for release". The distinction automation
    # needs is instead exit 2 (nothing measurable regressed; live evidence still
    # owed) vs exit 4 (something measurable regressed; act on it) vs exit 3 (BLOCK).
    return exit_code(report["outcome"])


if __name__ == "__main__":
    raise SystemExit(main())
