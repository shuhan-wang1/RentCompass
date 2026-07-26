"""The cut-short fallback must not narrate our internals, and must not miscount.

Both defects were observed in the owner's manual test on 2026-07-26, in one answer:

    抱歉，本轮处理耗时较长，我先根据已经拿到的结果给你一个简要回答（可能不完整）：
    已完成的查询：calculate_commute、get_property_details、search_properties、web_search。
    已找到 6 个房源（数据来自 OnTheMarket）：
      ... five listings ...

  1. Internal tool identifiers were printed verbatim. They expose the architecture and
     mean nothing to a renter.
  2. The sentence said six and the list had five, because the count came from `len(recs)`
     while the body was sliced `recs[:5]`. Nothing explained the gap.
"""
from __future__ import annotations

import inspect

from core import agent_loop


TOOL_IDENTIFIERS = (
    "search_properties", "calculate_commute", "get_property_details",
    "web_search", "check_safety", "search_nearby_pois", "get_weather",
    "check_transport_cost", "remember", "ask_user",
)


def _fallback_source() -> str:
    return inspect.getsource(agent_loop._artifact_grounded_fallback_answer)


def test_fallback_never_prints_internal_tool_identifiers():
    src = _fallback_source()
    # The rendered lines are what reaches the user. Any f-string/join that pours tool names
    # into them is the defect, in either language.
    assert '"、".join(str(t) for t in tool_names)' not in src
    assert '", ".join(str(t) for t in tool_names)' not in src
    assert "已完成的查询" not in src, "leaks internal tool identifiers to the user"
    assert "Completed lookups" not in src, "leaks internal tool identifiers to the user"


def test_the_removed_line_is_not_reintroduced_under_another_name():
    """Guard the intent, not one spelling: no user-facing line may be built from the set of
    executed tool names."""
    src = _fallback_source()
    for ident in TOOL_IDENTIFIERS:
        assert f'"{ident}"' not in src.split("lines = [opener]", 1)[-1], (
            f"{ident} must not appear in the rendered answer body")


def test_the_stated_listing_count_matches_what_is_rendered():
    """'Found 6' followed by five rows is the exact defect. Either the numbers agree, or the
    sentence says how many of the total are being shown."""
    src = _fallback_source()
    assert "_MAX_FALLBACK_RECS" in src, "the cap must be named, not a bare literal"
    assert "recs[:5]" not in src, (
        "a hard-coded slice next to len(recs) is how the count and the body drifted apart")
    # both language branches must derive the rendered rows from the same cap
    assert src.count("recs[:_MAX_FALLBACK_RECS]") == 2
    # and both must disclose truncation rather than silently dropping rows
    assert "先列出其中" in src
    assert "here are" in src


def test_cap_is_a_module_constant():
    assert isinstance(agent_loop._MAX_FALLBACK_RECS, int)
    assert agent_loop._MAX_FALLBACK_RECS > 0
