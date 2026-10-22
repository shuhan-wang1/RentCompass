"""r6-M3: the lock generator refuses a pip-compile that runs with click >= 8.5.

pip-tools 7.5.2 under click 8.5.0 writes a false ``--no-index`` into every lock-file header, so the
generator environment must pin ``click<8.5``. ``scripts/compile_dependency_locks.sh`` now reads the
click version of the interpreter its pip-compile runs under and stops before writing any lock.

Nothing here runs a real pip-compile or touches the repository's locks: the script is copied into
a temporary tree and pointed at a fake pip-compile that only records how it was called, and the
click version is a fake ``click-*.dist-info`` placed first on PYTHONPATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "compile_dependency_locks.sh"
FAKE_BODY = (
    "import os, sys\n"
    "if '--version' in sys.argv:\n"
    "    print('pip-compile, version 7.5.2')\n"
    "    sys.exit(0)\n"
    "with open(os.environ['FAKE_PIP_COMPILE_LOG'], 'a') as log:\n"
    "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
)


def _fake_click(site: Path, version: str) -> None:
    info = site / f"click-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: click\nVersion: {version}\n")


def _fake_pip_compile(path: Path, launcher: str) -> None:
    if launcher == "shebang":
        path.write_text(f"#!{sys.executable}\n" + FAKE_BODY)
    else:   # pip's launcher for an interpreter path that is too long or has spaces
        path.write_text(f"#!/bin/sh\n'''exec' \"{sys.executable}\" \"$0\" \"$@\"\n' '''\n"
                        + FAKE_BODY)
    path.chmod(0o755)


def _run(tmp_path: Path, click: str, launcher: str = "shebang"):
    tree = tmp_path / "repo"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, tree / "scripts" / SCRIPT.name)
    site = tmp_path / "site"
    _fake_click(site, click)
    fake = tmp_path / "bin" / "pip-compile"
    fake.parent.mkdir()
    _fake_pip_compile(fake, launcher)
    log = tmp_path / "calls.log"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(site),
           "RENTCOMPASS_PIP_COMPILE": str(fake), "FAKE_PIP_COMPILE_LOG": str(log)}
    result = subprocess.run(["bash", str(tree / "scripts" / SCRIPT.name)], capture_output=True,
                            text=True, env=env, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, tree


@pytest.mark.parametrize("launcher", ["shebang", "sh-launcher"])
@pytest.mark.parametrize("click", ["8.5.0", "8.10.1", "9.0.0"])
def test_click_8_5_or_newer_stops_before_any_lock_is_written(tmp_path, click, launcher):
    result, calls, tree = _run(tmp_path, click, launcher)
    assert result.returncode == 2, result.stderr
    assert f"click {click}" in result.stderr and "click<8.5" in result.stderr
    assert "--no-index" in result.stderr
    assert calls == []                       # pip-compile was never asked to compile
    assert not list(tree.glob("*.lock"))


@pytest.mark.parametrize("launcher", ["shebang", "sh-launcher"])
@pytest.mark.parametrize("click", ["8.4.2", "8.1.7"])
def test_click_below_8_5_runs_the_generator(tmp_path, click, launcher):
    result, calls, _tree = _run(tmp_path, click, launcher)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 4 and all("--output-file" in call for call in calls)


def test_an_unknown_launcher_is_refused_not_guessed(tmp_path):
    tree = tmp_path / "repo"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, tree / "scripts" / SCRIPT.name)
    fake = tmp_path / "pip-compile"
    fake.write_text("#!/bin/sh\necho 'pip-compile, version 7.5.2'\n")
    fake.chmod(0o755)
    result = subprocess.run(["bash", str(tree / "scripts" / SCRIPT.name)], capture_output=True,
                            text=True, timeout=60,
                            env={"PATH": "/usr/bin:/bin", "RENTCOMPASS_PIP_COMPILE": str(fake)})
    assert result.returncode == 2
    assert "RENTCOMPASS_PIP_COMPILE_PYTHON" in result.stderr


def test_the_generator_pin_is_documented_where_the_generator_is():
    header = SCRIPT.read_text().split("set -euo pipefail", 1)[0]
    assert "pip-tools 7.5.2" in header and "click<8.5" in header
    assert "click<8.5" in (REPO / "README.md").read_text(encoding="utf-8")
