"""Unit tests for the ZERO-workflow-prior / no-commute routing changes (Agent 2).

These cover the pure functions and the decide_tool fast-path owned by
``core.langgraph_agent``:

A  ``update_search_criteria`` merges the new area/commute_destination/no_commute/
   bedrooms/budget_period keys, mirrors commute_destination<->destination, and keeps
   ``no_commute`` sticky-True across a merge;
B  ``_apply_explicit_criteria_updates`` sets ``no_commute`` (and drops any stale
   travel-time limit) on an explicit "I don't commute", and RESETS it when the turn
   states a fresh commute limit or names a known commute destination;
C  ``_compute_decision`` routes an explicit "I don't commute" straight to
   ``search_properties`` (no LLM vote, no commute clarification loop);
E  ``_resolve_destination_address`` reads ``commute_destination`` (falling back to the
   legacy ``destination``) and returns None under a no-commute profile with no
   destination named this turn.

Agent 1 owns ``_extract_no_commute`` in ``core.tools.search_properties`` and is
editing it in parallel. To keep these tests independent of that work, every test that
reaches the ``_extract_no_commute`` import boundary monkeypatches it onto the module
(``raising=False`` installs it even before Agent 1 lands the real detector). The
deterministic ``_extract_budget`` / ``_extract_commute_minutes`` extractors already
exist and are used for real.
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


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib

    return importlib.import_module("core.langgraph_agent")


@pytest.fixture
def stub_no_commute(monkeypatch):
    """Install a fake ``_extract_no_commute`` on the (real) search_properties module.

    Returns a setter so each test can pick the detector's verdict. ``raising=False``
    lets this work even in the parallel window before Agent 1 adds the real symbol.
    """
    import core.tools.search_properties as sp

    def _set(verdict):
        monkeypatch.setattr(sp, "_extract_no_commute", lambda _text: bool(verdict), raising=False)

    return _set


# ── A: update_search_criteria — new keys, mirroring, sticky no_commute ───────
def test_update_criteria_folds_new_keys(lga):
    out = lga.update_search_criteria({}, {
        "area": "Camden", "commute_destination": "UCL", "no_commute": False,
        "bedrooms": 2, "budget_period": "week",
    })
    assert out["area"] == "Camden"
    assert out["commute_destination"] == "UCL"
    assert out["destination"] == "UCL"           # mirrored from commute_destination
    assert out["bedrooms"] == 2
    assert out["budget_period"] == "week"
    assert out["no_commute"] is False


def test_update_criteria_legacy_destination_mirrors_into_commute_destination(lga):
    out = lga.update_search_criteria({}, {"destination": "UCL"})
    assert out["destination"] == "UCL"
    assert out["commute_destination"] == "UCL"


def test_update_criteria_no_commute_is_sticky_true_across_merge(lga):
    # Once True, a later merge (empty, or even carrying no_commute=False) never downgrades.
    acc = lga.update_search_criteria({}, {"no_commute": True})
    assert acc["no_commute"] is True
    assert lga.update_search_criteria(acc, {})["no_commute"] is True
    assert lga.update_search_criteria(acc, {"no_commute": False})["no_commute"] is True


def test_update_criteria_ignores_bedroom_range_string(lga):
    # The tool emits "0-2" when the user did not pin a bedroom count — not a definite value.
    out = lga.update_search_criteria({}, {"bedrooms": "0-2"})
    assert out.get("bedrooms") is None
    # A clean digit string IS accepted.
    assert lga.update_search_criteria({}, {"bedrooms": "1"})["bedrooms"] == 1


def test_update_criteria_no_commute_result_does_not_leak_area_into_commute_dest(lga):
    # The tool emits commute_destination=None (explicit) under no_commute, with the
    # legacy destination merely mirroring the search area. The merge must not promote
    # that area into commute_destination.
    out = lga.update_search_criteria({}, {
        "area": "manchester", "commute_destination": None, "destination": "manchester",
        "no_commute": True, "bedrooms": 1, "budget_period": "month",
    })
    assert out.get("commute_destination") is None
    assert out["destination"] == "manchester"
    assert out["area"] == "manchester"
    assert out["no_commute"] is True
    assert out["bedrooms"] == 1


def test_update_criteria_preserves_list_merge(lga):
    out = lga.update_search_criteria({"property_features": ["studio"]},
                                     {"property_features": ["en-suite"]})
    assert out["property_features"] == ["studio", "en-suite"]


# ── B: _apply_explicit_criteria_updates — no_commute set / reset ─────────────
def test_apply_sets_no_commute_and_clears_travel_time(lga, stub_no_commute):
    stub_no_commute(True)
    out = lga._apply_explicit_criteria_updates(
        {"max_budget": 1200, "max_travel_time": 30}, "I don't commute, I just live there")
    assert out["no_commute"] is True
    assert out["max_travel_time"] is None
    assert out["max_budget"] == 1200  # untouched


def test_apply_commute_minutes_reset_no_commute(lga, stub_no_commute):
    stub_no_commute(False)  # irrelevant: a stated minute-limit takes the commute branch
    out = lga._apply_explicit_criteria_updates(
        {"no_commute": True}, "actually keep it within 30 minutes")
    assert out["no_commute"] is False
    assert out["max_travel_time"] == 30


def test_apply_named_destination_resets_no_commute(lga, stub_no_commute):
    stub_no_commute(True)  # even if the phrase looks no-commute-ish, naming a dest wins
    out = lga._apply_explicit_criteria_updates(
        {"no_commute": True}, "actually I want to live near UCL")
    assert out["no_commute"] is False


def test_apply_word_boundary_guards_short_slugs(lga, stub_no_commute):
    # "else" must NOT match the 'lse' destination slug (word-boundary guard), so this
    # stays a pure no-op and the sticky no_commute is preserved.
    stub_no_commute(False)
    acc = {"no_commute": True}
    out = lga._apply_explicit_criteria_updates(acc, "just show me anything, nothing else matters")
    assert out is acc
    assert out["no_commute"] is True


def test_apply_no_change_returns_same_object(lga, stub_no_commute):
    stub_no_commute(False)
    acc = {"max_budget": 1200, "max_travel_time": 30}
    assert lga._apply_explicit_criteria_updates(acc, "tell me more about the second one") is acc


# ── C: _compute_decision fast-path (via the real decide_tool node) ───────────
class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info"]

    def get(self, name):
        return None


class _DummyLLM:
    def invoke(self, prompt):  # must never be called on the fast-path
        raise AssertionError("LLM vote must not run for an explicit no-commute message")


def _decide(lga, msg, accumulated=None, extra_ctx=None):
    node = lga._make_decide_tool_node(_DummyRegistry(), _DummyLLM())
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {
        "user_query": msg,
        "extracted_context": ec,
        "accumulated_search_criteria": accumulated or {},
    }
    return node(state)


def test_decide_routes_no_commute_to_search_without_area(lga, stub_no_commute):
    stub_no_commute(True)
    # Defect #2: no area, no housing keyword, no destination — must still go to search
    # (the tool emits its single area-clarification form) and never to the LLM vote.
    cmd = _decide(lga, "我不通勤我单纯住着")
    assert cmd.goto == "execute_tool"
    decision = cmd.update["tool_decision"]
    assert decision["tool"] == "search_properties"
    assert decision["params"] == {"user_query": "我不通勤我单纯住着"}
    assert "commute" in decision["reason"].lower()


def test_decide_no_commute_with_area_still_search(lga, stub_no_commute):
    stub_no_commute(True)
    cmd = _decide(lga, "I work from home, find me a place",
                  accumulated={"area": "Manchester"})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_decide_no_commute_gated_off_lets_transport_through(lga, stub_no_commute):
    # When the detector says False, the fast-path must NOT fire; a real transport
    # question continues to the dedicated TfL route (proves correct gating/ordering).
    stub_no_commute(False)
    cmd = _decide(lga, "how much is the tube fare from Stratford to UCL?")
    assert cmd.update["tool_decision"]["tool"] == "get_transport_info"


def test_decide_greeting_wins_over_no_commute_fastpath(lga, stub_no_commute):
    # Greeting check precedes the no-commute fast-path per the routing order.
    stub_no_commute(True)
    cmd = _decide(lga, "hi")
    assert cmd.update["tool_decision"]["tool"] == "direct_answer"


# ── E: _resolve_destination_address — mirrored key + no_commute guard ────────
def test_resolve_destination_message_named_wins_even_under_no_commute(lga):
    addr = lga._resolve_destination_address(
        "", {"current_message": "how do I get to UCL?"}, {"no_commute": True})
    assert "University College London" in addr


def test_resolve_destination_none_under_no_commute_when_unnamed(lga):
    addr = lga._resolve_destination_address(
        "", {"current_message": "what's the commute cost?"},
        {"no_commute": True, "commute_destination": "UCL"})
    assert addr is None


def test_resolve_destination_reads_commute_destination_key(lga):
    addr = lga._resolve_destination_address(
        "", {"current_message": "what's the commute cost?"},
        {"commute_destination": "Canary Wharf"})
    assert addr == "Canary Wharf, London E14 5AB"


def test_resolve_destination_falls_back_to_legacy_destination(lga):
    addr = lga._resolve_destination_address(
        "", {"current_message": "what's the commute cost?"}, {"destination": "Camden Town"})
    assert addr == "Camden Town"
