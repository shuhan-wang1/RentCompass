from __future__ import annotations

from pathlib import Path

import pytest

import scripts.audit_installed_dependencies as audit
from scripts.audit_installed_dependencies import (
    filter_frozen_requirements,
    validate_constraints,
)


def test_audit_filter_removes_only_non_editable_local_product():
    frozen = """
    certifi==2026.1.1
    pip==26.2.1
    uk-rent-agent @ file:///workspace/uk_rent_recommendation
    urllib3==2.6.3
    """

    assert filter_frozen_requirements(frozen) == [
        "certifi==2026.1.1",
        "pip==26.2.1",
        "urllib3==2.6.3",
    ]


def test_audit_filter_rejects_any_editable_distribution():
    with pytest.raises(RuntimeError, match="editable distribution"):
        filter_frozen_requirements(
            "certifi==2026.1.1\n-e file:///workspace#egg=uk_rent_agent\n"
        )


def test_audit_filter_rejects_unpinned_or_unexpected_local_dependency():
    with pytest.raises(RuntimeError, match="direct dependency"):
        filter_frozen_requirements(
            "uk-rent-agent==0.1.0\nother @ file:///tmp/other\n"
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        filter_frozen_requirements("certifi==2026.1.1\n")


def test_audit_uses_osv_for_local_versioned_cpu_wheels():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_installed_dependencies.py"
    ).read_text(encoding="utf-8")
    assert '"--vulnerability-service=osv"' in source
    assert '"--environment-python"' in source
    assert '"--audit-python"' in source
    assert '"--freeze-file"' in source


def test_audit_requires_installed_closure_to_equal_constraints():
    frozen = ["certifi==2026.1.1", "pip==26.2.1"]
    validate_constraints(frozen, "certifi==2026.1.1\npip==26.2.1\n")

    with pytest.raises(RuntimeError, match="unexpected=urllib3"):
        validate_constraints(
            frozen + ["urllib3==2.6.3"],
            "certifi==2026.1.1\npip==26.2.1\n",
        )
    with pytest.raises(RuntimeError, match="mismatched=certifi"):
        validate_constraints(
            frozen,
            "certifi==2026.1.2\npip==26.2.1\n",
        )


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _scripted(*results):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return results[len(calls) - 1]

    return run, calls


def test_audit_retries_a_transient_osv_outage_then_passes():
    from scripts.audit_installed_dependencies import run_audit_with_retry

    run, calls = _scripted(
        _Completed(1, stderr="500 Server Error: Internal Server Error for url: "
                             "https://api.osv.dev/v1/query"),
        _Completed(0, stdout="No known vulnerabilities found"),
    )
    sleeps = []
    assert run_audit_with_retry(["pip-audit"], run=run, sleep=sleeps.append) == 0
    assert len(calls) == 2 and len(sleeps) == 1


def test_audit_never_retries_real_findings():
    from scripts.audit_installed_dependencies import run_audit_with_retry

    run, calls = _scripted(_Completed(1, stdout="Found 2 known vulnerabilities in 1 package"))
    assert run_audit_with_retry(["pip-audit"], run=run, sleep=lambda _: None) == 1
    assert len(calls) == 1


def test_audit_gives_up_after_the_last_attempt():
    from scripts.audit_installed_dependencies import run_audit_with_retry

    outage = _Completed(1, stderr="requests.exceptions.ConnectionError: Max retries exceeded")
    run, calls = _scripted(*[outage] * 4)
    assert run_audit_with_retry(["pip-audit"], run=run, sleep=lambda _: None) == 1
    assert len(calls) == 4


def test_audit_does_not_retry_an_unrelated_failure():
    from scripts.audit_installed_dependencies import run_audit_with_retry

    run, calls = _scripted(_Completed(2, stderr="usage: pip-audit: error: bad flag"))
    assert run_audit_with_retry(["pip-audit"], run=run, sleep=lambda _: None) == 2
    assert len(calls) == 1


def test_the_tooling_audit_covers_the_tool_environment_with_the_same_retry(monkeypatch):
    commands = []
    monkeypatch.setattr(audit, "run_audit_with_retry", lambda c: commands.append(c) or 0)

    assert audit.main(["--tooling", "--audit-python", "/tools/python"]) == 0

    assert commands[0][:3] == ["/tools/python", "-m", "pip_audit"]
    assert "--strict" in commands[0] and "--local" in commands[0]


def test_ci_runs_both_audits_whichever_fails():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    install = workflow.split("name: Install isolated pinned gate tooling", 1)[1].split("- name:", 1)[0]
    gate = workflow.split("name: Dependency vulnerability gate", 1)[1].split("- name:", 1)[0]

    assert "pip_audit" not in install
    assert gate.count("|| status=1") == 2
    assert '--audit-python "$audit_python" --tooling' in gate
    assert 'exit "$status"' in gate
