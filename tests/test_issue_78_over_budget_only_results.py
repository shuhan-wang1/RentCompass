"""ISSUE #78 (B) — an over-budget-ONLY search result must still repaint the panel.

Reported 2026-08-03: "the LLM returns text messages mentioning certain property details but
does not appear in the list on the right."

search_properties reports `status: found, recommendations: [], over_budget_alternatives:
[...]` when nothing lands inside budget but near-misses exist. Both formatters required a
non-empty `recommendations`, so the whole artifact was dropped, tool_data stayed empty, and
/api/alex shipped a `chat` payload the frontend never paints — while the model, which sees
the alternatives in the tool message, described them in the reply.
"""
from __future__ import annotations

import os
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from core.agent_loop import build_fc_nodes  # noqa: E402


# The real payload shape from the reported turn (conversation d28de8f7, 06:06:29): status
# "found" with total_found 0, no in-budget rows, one over-budget near-miss.
OVER_BUDGET_ONLY = {
    "success": True,
    "status": "found",
    "total_found": 0,
    "recommendations": [],
    "over_budget_alternatives": [{
        "rank": 1,
        "address": "Marchmont Street, London WC1N",
        "price": "£1650/month",
        "budget_status": "⚠️ 超预算 £150",
        "match_type": "soft_violation",
        "property_type": "Studio",
        "alternative": True,
    }],
    "search_criteria": {"area": "bloomsbury", "max_budget": 1500},
    "area_recommendations": [],
}


class _NoTools:
    """build_fc_nodes only needs a provider shape; format_output_fc never calls one."""

    def list_specs(self):
        return []

    def get(self, name):
        return None


def _format_output(raw, *, final_response="Here is a near-miss."):
    """Drive format_output_fc_node over a single executed search_properties artifact."""
    nodes = build_fc_nodes(_NoTools())
    state = {
        "tool_artifacts": [{"tool": "search_properties", "raw_data": raw}],
        "user_preferences": {},
        "accumulated_search_criteria": {},
        "final_response": final_response,
        "response_type": "answer",
        "extracted_context": {},
    }
    return nodes["format_output_fc"](state)


def test_over_budget_only_result_repaints_the_panel():
    out = _format_output(OVER_BUDGET_ONLY)
    assert out["response_type"] == "search", "an over-budget-only result is still a result"
    recs = out["tool_data"]["recommendations"]
    assert [r["address"] for r in recs] == ["Marchmont Street, London WC1N"]


def test_over_budget_rows_stay_tagged_so_the_card_reads_as_over_budget():
    """The frontend renders the amber badge off match_type / budget_status. Stripping
    either would turn a near-miss into a card that reads as an in-budget match."""
    rec = _format_output(OVER_BUDGET_ONLY)["tool_data"]["recommendations"][0]
    assert rec["match_type"] == "soft_violation"
    assert "超预算" in rec["budget_status"]


def test_in_budget_results_do_not_gain_the_alternatives():
    """Scoped fix: when real matches exist the alternatives stay OUT of the panel, exactly
    as before — otherwise every budget-constrained search would grow near-misses."""
    raw = dict(OVER_BUDGET_ONLY,
               recommendations=[{"address": "Woburn Place, London WC1H",
                                 "price": "£1400/month", "match_type": "perfect"}])
    recs = _format_output(raw)["tool_data"]["recommendations"]
    assert [r["address"] for r in recs] == ["Woburn Place, London WC1H"]


def test_a_genuinely_empty_result_still_is_not_a_search_payload():
    """0 in budget AND 0 alternatives must stay an answer — the relaxed guard must not
    start emitting empty search payloads."""
    raw = dict(OVER_BUDGET_ONLY, recommendations=[], over_budget_alternatives=[])
    out = _format_output(raw)
    assert out["response_type"] != "search"
    assert not out["tool_data"].get("recommendations")


def test_legacy_arch_agrees_with_fc_on_over_budget_only():
    """Both arches are A/B-compared; a formatter fixed on one side only would show up as an
    architecture difference rather than the bug it is."""
    import inspect

    from core.langgraph_agent import (
        _make_format_output_node,
        _search_payload_has_candidates,
    )

    src = inspect.getsource(_make_format_output_node)
    candidate_predicate_src = inspect.getsource(_search_payload_has_candidates)
    # All three legacy search-result seams (artifact-ledger recovery, plan-dimension
    # fan-out, and the plain tool branch) share one candidate predicate. The old
    # contract counted direct field accesses inside the formatter, but that became
    # stale when final-evidence preservation centralized the predicate. Assert the
    # actual invariant: every seam consults the shared predicate, and that predicate
    # explicitly includes the over-budget channel.
    assert src.count("_search_payload_has_candidates") >= 3, \
        "a legacy search formatting seam bypasses the shared candidate predicate"
    assert "over_budget_alternatives" in candidate_predicate_src, \
        "the shared candidate predicate drops over-budget-only results (issue #78 B)"
