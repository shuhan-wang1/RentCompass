"""Unit tests for the three follow-up defects fixed in the chat agent:

D1  comparative questions over existing results route to a direct answer
    (no clarification loop);
D2  an explicit mid-conversation budget/commute change is merged into the
    accumulated criteria (raise OR lower), and the search tool no longer drops
    ``current_message`` at its pydantic input boundary;
D3  a "tell me more about the second one" follow-up resolves the FULL record
    from the conversation's last results (never the demo CSV).

The graph module imports ``langgraph`` at import time, so those cases are guarded
with ``importorskip``. The tool-schema case only needs ``core.tools`` + pydantic.
"""

import os
import sys

import pytest

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]


# ── D2: the search tool must NOT drop current_message at its input boundary ──
def test_search_tool_declares_current_message_and_retains_it():
    from core.tools.search_properties import search_properties_tool

    props = search_properties_tool.parameters["properties"]
    assert "current_message" in props, "current_message must be declared or pydantic drops it"

    # The pydantic input model (extra='ignore') previously silently discarded
    # current_message; assert it now survives model_validate/model_dump.
    dumped = search_properties_tool.input_model.model_validate(
        {"user_query": "y", "current_message": "Actually my budget is £1000 max now"}
    ).model_dump(exclude_none=True)
    assert dumped.get("current_message") == "Actually my budget is £1000 max now"


# ── graph-dependent helpers ─────────────────────────────────────────────────
@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib

    return importlib.import_module("core.langgraph_agent")


def _ec(current_message, results=None):
    ec = {"current_message": current_message}
    if results is not None:
        ec["last_results"] = results
    return ec


def _results():
    return [
        {"name": "Flat 211, Oldfield Wharf", "address": "Flat 211, Oldfield Wharf, 11 Gaythorn Street",
         "price": "£1150/month", "travel_time": "16 min to University of Manchester", "bedrooms": 1},
        {"name": "at Property Sense", "address": "at Property Sense, Apartment 105, Talbot Road M16",
         "price": "£1200/month", "travel_time": "20 min to University of Manchester", "bedrooms": 1},
        {"name": "Kelso Place", "address": "Kelso Place, Manchester, M15",
         "price": "£1275/month", "travel_time": "12 min to University of Manchester", "bedrooms": 1},
    ]


# ── D2: accumulated criteria merge (raise OR lower, week->month, commute) ────
def test_apply_explicit_criteria_lowers_budget(lga):
    acc = {"max_budget": 1200, "max_travel_time": 30}
    out = lga._apply_explicit_criteria_updates(acc, "Actually my budget is £1000 max now")
    assert out["max_budget"] == 1000
    assert out["max_travel_time"] == 30  # untouched


def test_apply_explicit_criteria_raises_budget(lga):
    acc = {"max_budget": 1200}
    assert lga._apply_explicit_criteria_updates(acc, "my budget is now £1800")["max_budget"] == 1800


def test_apply_explicit_criteria_weekly_is_normalised_to_monthly(lga):
    acc = {"max_budget": 1200}
    out = lga._apply_explicit_criteria_updates(acc, "I can do £250 per week max")
    assert out["max_budget"] == int(round(250 * 4.33))


def test_apply_explicit_criteria_commute_update(lga):
    acc = {"max_budget": 1200, "max_travel_time": 30}
    out = lga._apply_explicit_criteria_updates(acc, "keep it within a 40 minute commute")
    assert out["max_travel_time"] == 40


def test_apply_explicit_criteria_no_change_returns_same_object(lga):
    acc = {"max_budget": 1200, "max_travel_time": 30}
    # No budget/commute stated -> same object (caller skips a redundant write).
    assert lga._apply_explicit_criteria_updates(acc, "tell me more about the second one") is acc
    # Restating the SAME budget is also a no-op.
    assert lga._apply_explicit_criteria_updates(acc, "budget is £1200") is acc


# ── D3: ordinal / deictic / name -> full record resolution ──────────────────
def test_resolve_last_result_by_ordinal(lga):
    r = _results()
    rec = lga._resolve_last_result("", _ec("tell me more about the second one", r))
    assert rec is r[1]
    assert "Property Sense" in rec["address"]


def test_resolve_last_result_by_deictic_and_number(lga):
    r = _results()
    assert lga._resolve_last_result("", _ec("what about the first one", r)) is r[0]
    assert lga._resolve_last_result("", _ec("give me details on #3", r)) is r[2]


def test_resolve_last_result_by_name(lga):
    r = _results()
    assert lga._resolve_last_result("", _ec("tell me about Kelso Place", r)) is r[2]


def test_resolve_last_result_none_without_reference(lga):
    assert lga._resolve_last_result("", _ec("what's the weather", _results())) is None
    assert lga._resolve_last_result("", _ec("the second one", None)) is None  # no results


# ── D1: comparative routing decision ────────────────────────────────────────
def test_is_comparative_followup_true_for_which_is_closest(lga):
    assert lga._is_comparative_followup("", _ec("Which of these is closest to the university?", _results())) is True


def test_is_comparative_followup_true_when_told_to_use_listings(lga):
    assert lga._is_comparative_followup(
        "", _ec("Use the listings — which one is the cheapest?", _results())) is True


def test_is_comparative_followup_false_for_new_search(lga):
    # A genuinely new search must not be hijacked (no set-reference).
    assert lga._is_comparative_followup("", _ec("find me a cheaper 2-bed in Leeds", _results())) is False


def test_is_comparative_followup_false_without_results(lga):
    assert lga._is_comparative_followup("", _ec("which is closest?", None)) is False


# ── D3: detail routing decision ─────────────────────────────────────────────
def test_is_detail_followup_returns_record(lga):
    r = _results()
    assert lga._is_detail_followup("", _ec("Tell me more about the second one", r)) is r[1]


def test_is_detail_followup_skips_safety_and_new_search(lga):
    r = _results()
    assert lga._is_detail_followup("", _ec("is the second one safe?", r)) is None
    assert lga._is_detail_followup("", _ec("find me another like the second one", r)) is None


def test_format_single_result_omits_missing_fields(lga):
    rec = {"address": "at Property Sense, Talbot Road M16", "price": "£1200/month",
           "travel_time": "20 min to University of Manchester"}
    text = lga._format_single_result(rec)
    assert "£1200/month" in text and "Talbot Road" in text
    assert "Bedrooms" not in text  # not present -> not fabricated
