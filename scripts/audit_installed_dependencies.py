#!/usr/bin/env python3
"""Audit the exact installed environment without hiding editable packages.

The supply-chain job installs the product as a normal wheel, freezes every installed
distribution (including pip/setuptools), removes exactly the local product line, and
passes the remaining exact versions to pip-audit with dependency resolution disabled.
This avoids both an editable-package false failure and an accidental audit bypass.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


_PROJECT = "uk-rent-agent"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXACT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:===|==)(?P<version>[^;\s]+)(?:\s*;.*)?$"
)
_DIRECT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s+@\s+(?P<url>\S+)$")
# pip-audit exits 1 both for "vulnerabilities found" and for "could not ask the service".
# Only the second is retried, and only when the output says so: api.osv.dev returning 500
# failed main's CI three times in a row on 2026-09-01 with no code change. There is no
# fallback service on purpose — PyPI's cannot see local versions such as `+cpu` wheels.
_TRANSIENT_SERVICE_ERROR = re.compile(
    r"\b5\d\d Server Error\b|ServiceError|ConnectionError|ConnectTimeout|"
    r"ReadTimeout|Read timed out|Max retries exceeded|Temporary failure in name resolution"
)
_FOUND_VULNERABILITIES = re.compile(r"Found \d+ known vulnerabilit", re.IGNORECASE)
_AUDIT_ATTEMPTS = 4
_RETRY_DELAYS_SECONDS = (15, 45, 90)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def filter_frozen_requirements(text: str) -> list[str]:
    """Return fully pinned third-party lines and reject ambiguous exclusions."""
    kept: list[str] = []
    removed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-e ", "--editable ")):
            raise RuntimeError(
                "editable distribution present; supply-chain audit requires a "
                "non-editable product install"
            )
        direct = _DIRECT.fullmatch(line)
        exact = _EXACT.fullmatch(line)
        name = (direct or exact).group("name") if (direct or exact) else None
        if name and _normalise(name) == _PROJECT:
            removed += 1
            continue
        if direct:
            raise RuntimeError(
                f"unversioned direct dependency cannot be audited reproducibly: {name}"
            )
        if not exact:
            raise RuntimeError(f"unrecognised or unpinned freeze line: {line}")
        kept.append(line)
    if removed != 1:
        raise RuntimeError(
            f"expected exactly one installed {_PROJECT} distribution, found {removed}"
        )
    if not kept:
        raise RuntimeError("frozen audit input is empty")
    return sorted(set(kept), key=str.casefold)


def validate_constraints(requirements: list[str], text: str) -> None:
    """Require the installed closure to equal the reviewed constraints exactly."""
    expected: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT.fullmatch(line)
        if not match:
            raise RuntimeError(f"constraint is not an exact pin: {line}")
        name = _normalise(match.group("name"))
        if name in expected:
            raise RuntimeError(f"duplicate production constraint: {name}")
        expected[name] = match.group("version")

    installed = {
        _normalise(match.group("name")): match.group("version")
        for line in requirements
        if (match := _EXACT.fullmatch(line))
    }
    missing = sorted(expected.keys() - installed.keys())
    unexpected = sorted(installed.keys() - expected.keys())
    mismatched = sorted(
        name
        for name in expected.keys() & installed.keys()
        if expected[name] != installed[name]
    )
    if missing or unexpected or mismatched:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if mismatched:
            details.append(
                "mismatched="
                + ",".join(
                    f"{name}:{installed[name]}!={expected[name]}"
                    for name in mismatched
                )
            )
        raise RuntimeError(
            "installed production closure differs from constraints: "
            + "; ".join(details)
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment-python",
        default=sys.executable,
        help="Python interpreter whose installed production closure is audited",
    )
    parser.add_argument(
        "--audit-python",
        default=sys.executable,
        help="Isolated interpreter containing the pinned pip-audit tool",
    )
    parser.add_argument(
        "--tooling",
        action="store_true",
        help="Audit the isolated gate tooling itself instead of the production closure",
    )
    parser.add_argument(
        "--freeze-file",
        help="Audit a captured pip-freeze file, or '-' for stdin (for image audits)",
    )
    parser.add_argument(
        "--constraints",
        default=str(_REPO_ROOT / "constraints-production.txt"),
        help="Reviewed exact production closure",
    )
    args = parser.parse_args(argv)
    if args.tooling:
        with tempfile.TemporaryDirectory(prefix="rentcompass-audit-") as directory:
            return run_audit_with_retry(
                [
                    args.audit_python,
                    "-m",
                    "pip_audit",
                    "--strict",
                    "--progress-spinner=off",
                    "--local",
                    "--cache-dir",
                    str(Path(directory) / "cache"),
                ]
            )
    if args.freeze_file:
        freeze = (
            sys.stdin.read()
            if args.freeze_file == "-"
            else Path(args.freeze_file).read_text(encoding="utf-8")
        )
    else:
        freeze = subprocess.run(
            [args.environment_python, "-m", "pip", "freeze", "--all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    requirements = filter_frozen_requirements(freeze)
    validate_constraints(
        requirements,
        Path(args.constraints).read_text(encoding="utf-8"),
    )
    with tempfile.TemporaryDirectory(prefix="rentcompass-audit-") as directory:
        root = Path(directory)
        path = root / "requirements.txt"
        path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        command = [
            args.audit_python,
            "-m",
            "pip_audit",
            "--strict",
            "--progress-spinner=off",
            "--cache-dir",
            str(root / "cache"),
            "--vulnerability-service=osv",
            "--no-deps",
            "--disable-pip",
            "--requirement",
            str(path),
        ]
        return run_audit_with_retry(command)


def is_transient_service_failure(returncode: int, output: str) -> bool:
    """True only for a failed run that never got an answer from the service."""
    if returncode == 0 or _FOUND_VULNERABILITIES.search(output):
        return False
    return bool(_TRANSIENT_SERVICE_ERROR.search(output))


def run_audit_with_retry(command, *, run=subprocess.run, sleep=time.sleep,
                         attempts: int = _AUDIT_ATTEMPTS) -> int:
    returncode = 1
    for attempt in range(1, attempts + 1):
        completed = run(command, check=False, capture_output=True, text=True)
        sys.stdout.write(completed.stdout or "")
        sys.stderr.write(completed.stderr or "")
        returncode = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
        if not is_transient_service_failure(returncode, output) or attempt == attempts:
            return returncode
        delay = _RETRY_DELAYS_SECONDS[min(attempt - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
        print(
            f"audit: vulnerability service unavailable (attempt {attempt}/{attempts}); "
            f"retrying in {delay}s",
            file=sys.stderr,
        )
        sleep(delay)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
