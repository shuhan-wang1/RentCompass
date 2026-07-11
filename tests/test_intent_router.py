"""Deliverable 3 — router rework.

The classifier now reads the CURRENT stripped message (not the memory/history-prefixed
query) and returns strict JSON {"intent": "..."}, parsed via a json -> substring ->
heuristic ladder. The headline fix: an area PRICE-research question must route to
market_info (web_search synthesis), NOT search_properties.

The DeepSeek call is mocked; the deterministic parse ladder and the fallback are also
tested directly.
"""

import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info", "check_safety"]

    def get(self, name):
        return None


class _JsonLLM:
    """Returns the given intent as strict JSON (mimics the DeepSeek classifier)."""

    def __init__(self, intent):
        self.intent = intent
        self.seen = None

    def invoke(self, prompt):
        self.seen = prompt
        return types.SimpleNamespace(content=json.dumps({"intent": self.intent}))


class _BoomLLM:
    def invoke(self, prompt):
        raise RuntimeError("no api key")


def _decide(lga, msg, llm, accumulated=None, monkeypatch=None):
    # Avoid any real network from web-search planning.
    if monkeypatch is not None:
        monkeypatch.setattr(lga, "_plan_web_searches",
                            lambda q, reg: {"tool": "multi_search",
                                            "params": {"searches": [{"tool": "web_search",
                                                                     "params": {"query": q}}]},
                                            "reason": "market"})
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    state = {"user_query": msg, "extracted_context": {"current_message": msg},
             "accumulated_search_criteria": accumulated or {}}
    return node(state).update["tool_decision"]


# ── the headline routing fix ──────────────────────────────────────────────
def test_price_research_routes_to_market_info_not_search(lga, monkeypatch):
    msg = "你能不能先帮我调查一下帝国理工附近的价格"
    d = _decide(lga, msg, _JsonLLM("market_info"), monkeypatch=monkeypatch)
    assert d["tool"] != "search_properties"
    assert d["tool"] == "multi_search"  # market_info -> web_search synthesis path
    assert "market_info" in d["reason"]


def test_find_flat_routes_to_search(lga, monkeypatch):
    msg = "帮我找帝国理工附近的房子"
    d = _decide(lga, msg, _JsonLLM("search_properties"), monkeypatch=monkeypatch)
    assert d["tool"] == "search_properties"


def test_english_average_rent_routes_to_market_info(lga, monkeypatch):
    d = _decide(lga, "what's the average rent in Shoreditch",
                _JsonLLM("market_info"), monkeypatch=monkeypatch)
    assert d["tool"] == "multi_search"


def test_direct_answer_intent(lga, monkeypatch):
    d = _decide(lga, "what can you do", _JsonLLM("direct_answer"), monkeypatch=monkeypatch)
    assert d["tool"] == "direct_answer"


def test_action_keyword_forces_search_over_classifier(lga, monkeypatch):
    # Even if the classifier says market_info, an explicit action verb forces a search.
    d = _decide(lga, "帮我找房 in Camden please", _JsonLLM("market_info"), monkeypatch=monkeypatch)
    assert d["tool"] == "search_properties"


def test_router_sees_only_current_message_not_memory(lga, monkeypatch):
    # The classifier prompt must contain the stripped current message and NOT the
    # injected long-term-memory block (input hygiene — the root cause of the mis-route).
    llm = _JsonLLM("market_info")
    memoryful = ("What I remember about this user: they searched Camden flats before.\n\n"
                 "Previous conversation:\nUser: hi\nAlex: hello\n\n"
                 "Current user message: 调查一下帝国理工附近的租金")
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    monkeypatch.setattr(lga, "_plan_web_searches",
                        lambda q, reg: {"tool": "multi_search", "params": {}, "reason": "m"})
    state = {"user_query": memoryful,
             "extracted_context": {"current_message": "调查一下帝国理工附近的租金"},
             "accumulated_search_criteria": {}}
    node(state)
    assert "What I remember about this user" not in llm.seen
    assert "调查一下帝国理工附近的租金" in llm.seen


# ── deterministic parse ladder ────────────────────────────────────────────
def test_parse_intent_json(lga):
    names = {n for n, _, _ in lga._INTENT_CATALOG}
    assert lga._parse_intent('{"intent": "market_info"}', names) == "market_info"
    assert lga._parse_intent('Sure: {"intent":"check_safety"} done', names) == "check_safety"


def test_parse_intent_substring_fallback(lga):
    names = {n for n, _, _ in lga._INTENT_CATALOG}
    assert lga._parse_intent("Tool: search_properties", names) == "search_properties"
    assert lga._parse_intent("i think market info here", names) == "market_info"


def test_parse_intent_none_on_garbage(lga):
    names = {n for n, _, _ in lga._INTENT_CATALOG}
    assert lga._parse_intent("completely unrelated text", names) is None
    assert lga._parse_intent("", names) is None


# ── heuristic fallback when the LLM call fails ────────────────────────────
def test_fallback_to_search_on_llm_failure(lga, monkeypatch):
    d = _decide(lga, "find me a flat in Camden", _BoomLLM(), monkeypatch=monkeypatch)
    assert d["tool"] == "search_properties"  # heuristic: 'find me'


def test_fallback_to_web_search_on_llm_failure_generic(lga, monkeypatch):
    d = _decide(lga, "tell me about UK guarantors", _BoomLLM(), monkeypatch=monkeypatch)
    assert d["tool"] == "web_search"
