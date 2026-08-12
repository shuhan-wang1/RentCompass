from __future__ import annotations

import pytest

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
