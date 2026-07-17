"""Deliverable 1 — soft criteria gate.

Two layers:
  * TOOL level (search_properties_impl): the gate fires ONCE when a recommended field
    (budget / room_type / commute) is missing on the chat path, lists exactly the
    missing fields, and is bypassed by confirmed=True (form path), criteria_gate_shown
    (already fired), or a proceed phrase in the message.
  * ROUTING level (_compute_decision): once the gate has been shown this conversation,
    a proceed phrase or a criteria answer routes straight back to search_properties
    with confirmation, BEFORE the LLM vote.

The scraper + RAG coordinator are stubbed (no network, no ML model).
"""

import asyncio
import os
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
from core.tools.search_properties import (
    search_properties_impl,
    set_rag_coordinator,
    _soft_gate_question,
)


# --------------------------------------------------------------------------
# Stubs (same shape as test_search_optional_criteria.py)
# --------------------------------------------------------------------------
def _row(addr, price, geo="51.52,-0.13", rt="1 bed Flat"):
    return {
        "Address": addr, "URL": "https://www.onthemarket.com/details/x/",
        "Price": f"£{price} pcm", "geo_location": geo, "Geo_Location": geo,
        "Room_Type_Category": rt, "Description": "Bright flat near transport. Bus 10 min.",
        "Images": [],
    }


class _FakeStore:
    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows)

    def search(self, query, top_k=10):
        return list(self.rows)


class _FakeCoordinator:
    def __init__(self):
        self.property_store = _FakeStore()

    def enhanced_search(self, query, criteria):
        rows = self.property_store.rows
        for r in rows:
            r.setdefault("similarity_score", 0.6)
        return list(rows), [], []


def _install_listings(monkeypatch, rows):
    m = {"slug": "x", "requested_location": "x", "requested_city": "london",
         "source": "scraped", "stale": False, "count": len(rows), "elapsed_s": 0.01, "message": ""}
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": list(rows), "meta": m})


def _no_scrape(monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate must fire before scraping")))


@pytest.fixture
def stub_env(monkeypatch):
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# ── TOOL level ────────────────────────────────────────────────────────────
def test_gate_triggers_when_all_recommended_missing(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="find me a place in Camden")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert set(res["missing_fields"]) == {"budget", "room_type", "commute"}
    # known_criteria carries room_type (per the contract, even when null)
    assert "room_type" in res["known_criteria"]
    # the "gate already shown" flag is persisted through the merge path
    assert res["extracted_so_far"]["criteria_gate_shown"] is True


def test_gate_never_asks_for_max_commute_time():
    """Bug 6: max commute time is OPTIONAL — the gate must never ask for it. The commute
    clause asks only whether the user commutes and WHERE TO, never for a maximum minutes."""
    for is_cjk in (True, False):
        q = _soft_gate_question(['budget', 'room_type', 'commute'], is_cjk, move_in_missing=True)
        # No max-commute-time solicitation, either language.
        assert '分钟' not in q
        assert 'max minutes' not in q.lower()
        assert 'maximum commute' not in q.lower()
        assert 'commute time' not in q.lower()
        assert '多少分钟' not in q
    # It still asks the destination side of the commute (zh: 通勤到哪里 ; en: where to).
    assert '通勤到哪里' in _soft_gate_question(['commute'], True)
    assert 'where to' in _soft_gate_question(['commute'], False).lower()


def test_gate_missing_fields_never_include_max_commute(stub_env, monkeypatch):
    """The structured missing_fields contract must never surface a max-commute field —
    only budget / room_type / commute(-destination) are recommended."""
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="find me a place in Camden")
    for bad in ('max_commute_time', 'max_travel_time'):
        assert bad not in res['missing_fields']
        assert bad not in res.get('missing_optional_fields', [])


def test_gate_lists_only_missing_fields(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    # budget given + no_commute satisfies commute -> only room_type is missing
    res = _run(area="Camden", max_budget=1500, no_commute=True)
    assert res["status"] == "need_clarification"
    assert res["missing_fields"] == ["room_type"]


def test_gate_commute_satisfied_by_destination_and_limit(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    # commute satisfied (dest + real limit) and budget given -> only room_type missing
    res = _run(area="Camden", max_budget=1500, commute_destination="UCL", max_commute_time=30)
    assert res["missing_fields"] == ["room_type"]


def test_gate_bypassed_by_confirmed(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", confirmed=True)  # form / direct path
    assert res["status"] == "found"


def test_gate_fires_at_most_once(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", criteria_gate_shown=True)  # already shown this conversation
    assert res["status"] == "found"


def test_gate_bypassed_by_proceed_phrase(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", current_message="继续搜索")
    assert res["status"] == "found"


def test_gate_not_triggered_when_all_present(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", max_budget=1500, room_type="ensuite",
               commute_destination="UCL", max_commute_time=30)
    assert res.get("clarification_kind") != "soft_criteria"
    assert res["status"] in ("found", "no_results")


def test_gate_providing_criteria_then_searches(stub_env, monkeypatch):
    # Follow-up turn after the gate: flag already set, user supplies a room type -> search.
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", room_type="studio", criteria_gate_shown=True)
    assert res["status"] in ("found", "no_results")


def test_area_gate_takes_precedence_and_is_tagged(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(current_message="find me somewhere")  # no area at all
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "missing_area"
    assert res["missing_fields"] == ["area"]
    assert "room_type" in res["known_criteria"]


# ── ROUTING level (_compute_decision) ─────────────────────────────────────
@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info"]

    def get(self, name):
        return None


class _NoVoteLLM:
    def invoke(self, prompt):
        raise AssertionError("proceed guard should route before the LLM vote")


def _decide(lga, msg, accumulated=None):
    node = lga._make_decide_tool_node(_DummyRegistry(), _NoVoteLLM())
    state = {"user_query": msg, "extracted_context": {"current_message": msg},
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


def test_proceed_phrase_routes_to_search_with_confirmation(lga):
    cmd = _decide(lga, "继续搜索", accumulated={"criteria_gate_shown": True, "area": "Camden"})
    d = cmd.update["tool_decision"]
    assert d["tool"] == "search_properties"
    assert d["params"].get("confirmed") is True


def test_criteria_answer_after_gate_routes_to_search(lga):
    cmd = _decide(lga, "ensuite的，1000以内", accumulated={"criteria_gate_shown": True})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_proceed_guard_inactive_without_flag(lga):
    # No criteria_gate_shown -> the proceed guard must NOT fire; a bare proceed phrase
    # falls through to the vote (which _NoVoteLLM turns into the heuristic fallback).
    cmd = _decide(lga, "继续搜索", accumulated={})
    assert cmd.update["tool_decision"]["tool"] != "search_properties"
