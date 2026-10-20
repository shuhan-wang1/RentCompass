"""Area coverage is not a source outage (R2-07, R2-23)."""
from __future__ import annotations

from core.source_status import source_failure_summary
from core.tools.search_properties import canonical_requested_area


def _result(status, complete=True, rows=1):
    return {"rows": [{}] * rows, "meta": {"source_status": status, "retrieval_complete": complete}}


def test_r2_07_one_uncovered_area_is_named_not_reported_as_an_outage():
    summary = source_failure_summary({"Canary Wharf": _result("cached"),
                                      "King's Cross": _result("ok"),
                                      "Liverpool": _result("unavailable", False, 0)})
    assert summary["source_status"] == "partial"
    assert "temporarily unavailable" not in summary["source_message"]
    assert "Liverpool" in summary["source_message"] and "Canary Wharf" in summary["source_message"]
    zh = source_failure_summary({"Camden": _result("ok"), "Liverpool": _result("unavailable", False, 0)},
                                language="zh")
    assert "Liverpool" in zh["source_message"] and "暂时不可用，本次" not in zh["source_message"]


def test_r2_07_a_real_outage_is_still_an_outage():
    summary = source_failure_summary({"Camden": _result("rate_limited", False, 0)})
    assert summary["source_status"] == "rate_limited"
    assert "OnTheMarket listing source" in summary["source_message"]


def test_r2_23_a_borough_qualified_area_is_canonicalised():
    assert canonical_requested_area("Angel, Islington") == "Angel"
    assert canonical_requested_area("Hackney, London") == "Hackney"
    assert canonical_requested_area("Camden") == "Camden"
    assert canonical_requested_area("Gloucester Avenue, London NW1") == "Gloucester Avenue, London NW1"
    assert canonical_requested_area("  ") is None
