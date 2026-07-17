"""Core LLM-routing fixes in app/core (langgraph_agent.py, llm_config.py, router.py).

Pins four production-grade fixes, WITHOUT any live LLM / network:

1. Response-model selection — greetings / direct answers / single-observation answers
   use the cheap chat model (low_latency=True); genuine multi-observation synthesis uses
   the reasoner (low_latency=False). (_synthesis_needs_reasoner + get_react_llm plumbing.)
2. Reflect one-shot short-circuit — the FIRST loopable tool on a single-intent message
   answers WITHOUT the controller LLM round-trip; a multi-intent message (or loop_turn>=1)
   still runs full reflection.
3. JSON mode — the intent classifier and the reflect controller bind DeepSeek's
   response_format json_object, while the existing parse ladder still recovers malformed
   output as a defensive fallback.

Harness mirrors test_agent_loop.py: the source roots are pinned ahead of any stale
tests/ shadow copies, and every LLM is stubbed.
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

import asyncio

import pytest


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


# ── stubs ────────────────────────────────────────────────────────────────────
class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_weather", "check_safety"]

    def get(self, _name):
        return None


class _AsyncGen:
    """Async generation stub (mimics ChatOpenAI.ainvoke)."""

    async def ainvoke(self, messages):
        return types.SimpleNamespace(content="grounded synthesised answer")


class _CountingReflect:
    """Reflect controller stub that counts invocations and returns a scripted verdict.
    Has no .bind, so _bind_json_mode falls back to it and .invoke still counts."""

    def __init__(self, verdict=None):
        self.calls = 0
        self.verdict = verdict or {"action": "answer"}

    def invoke(self, _prompt):
        self.calls += 1
        return types.SimpleNamespace(content=json.dumps(self.verdict))


def _reflect_state(msg, *, loop_turn=0, observations=None):
    return {
        "user_query": msg,
        "tool_decision": {"tool": "get_weather", "params": {"location": "London"}},
        "tool_observation": "OBS_WEATHER",
        "tool_raw_data": {"temp": "12C"},
        "extracted_context": {"current_message": msg},
        "accumulated_search_criteria": {},
        "loop_turn": loop_turn,
        "observations": observations or [],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. MODEL SELECTION — chat model for cheap paths, reasoner for real synthesis
# ═══════════════════════════════════════════════════════════════════════════
def test_synthesis_needs_reasoner_gate(lga):
    f = lga._synthesis_needs_reasoner
    assert f({}) is False
    assert f({"observations": []}) is False
    assert f({"observations": [{"turn": 0}]}) is False                       # single obs -> chat
    assert f({"observations": [{"turn": 0}, {"turn": 1}]}) is True           # multi obs -> reasoner


def test_router_responder_low_latency_selects_chat_model():
    from uk_rent_agent.llm.router import ModelRouter
    r = ModelRouter()
    fast = r.route("responder", low_latency=True)
    slow = r.route("responder", low_latency=False)
    assert fast.model == r.chat_model and fast.reasoning is False
    assert slow.model == r.reasoner_model and slow.reasoning is True


def _gen_state(observations, *, tool_observation=None, tool="direct_answer"):
    return {"user_query": "hi", "tool_decision": {"tool": tool},
            "extracted_context": {"current_message": "hi"}, "user_preferences": {},
            "context_tainted": False, "observations": observations,
            "tool_observation": tool_observation}


def _patch_capture_react_llm(monkeypatch, captured):
    from core import llm_config
    monkeypatch.setattr(
        llm_config, "get_react_llm",
        lambda low_latency=False: captured.__setitem__("low_latency", low_latency) or _AsyncGen(),
    )


def test_generate_response_greeting_uses_chat_model(lga, monkeypatch):
    captured = {}
    _patch_capture_react_llm(monkeypatch, captured)
    asyncio.run(lga._make_generate_response_node()(_gen_state([])))
    assert captured["low_latency"] is True


def test_generate_response_multi_observation_uses_reasoner(lga, monkeypatch):
    captured = {}
    _patch_capture_react_llm(monkeypatch, captured)
    obs = [{"turn": 0, "tool": "get_weather", "observation": "OBS_A"},
           {"turn": 1, "tool": "web_search", "observation": "OBS_B"}]
    asyncio.run(lga._make_generate_response_node()(
        _gen_state(obs, tool_observation="OBS_B", tool="web_search")))
    assert captured["low_latency"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 2. REFLECT ONE-SHOT SHORT-CIRCUIT
# ═══════════════════════════════════════════════════════════════════════════
def test_multi_intent_detector(lga):
    f = lga._current_message_has_multi_intent
    assert f("") is False
    assert f("is the first one safe") is False                 # single intent
    assert f("what is the weather in London") is False
    assert f("is it safe and how cheap is it") is False        # 1 recognized group only
    assert f("is it safe and how is the weather there") is True   # safety + weather + "and"
    assert f("is it safe? what is the weather?") is True          # two distinct questions


def test_reflect_single_intent_short_circuits_without_llm(lga):
    # A continue verdict is scripted — but the short-circuit must answer BEFORE consulting it.
    reflect = _CountingReflect({"action": "continue", "next_intent": "web_search",
                                "next_query": "x", "reason": "y"})
    cmd = lga._make_reflect_node(_DummyRegistry(), reflect)(
        _reflect_state("what is the weather in London"))
    assert reflect.calls == 0                     # controller LLM never consulted
    assert cmd.goto != "execute_tool"             # answered, did not loop
    assert cmd.update["loop_turn"] == 1
    assert len(cmd.update["observations"]) == 1


def test_reflect_multi_intent_still_calls_controller(lga):
    reflect = _CountingReflect({"action": "answer"})
    lga._make_reflect_node(_DummyRegistry(), reflect)(
        _reflect_state("is it safe and how is the weather there"))
    assert reflect.calls == 1                      # multi-intent -> full reflection


def test_reflect_second_turn_single_intent_still_reflects(lga):
    reflect = _CountingReflect({"action": "answer"})
    prior = [{"turn": 0, "tool": "get_weather", "observation": "OBS", "params_digest": "abc"}]
    lga._make_reflect_node(_DummyRegistry(), reflect)(
        _reflect_state("what is the weather in London", loop_turn=1, observations=prior))
    assert reflect.calls == 1                      # loop_turn>=1 -> short-circuit does NOT apply


# ═══════════════════════════════════════════════════════════════════════════
# 3. JSON MODE + defensive parse ladder
# ═══════════════════════════════════════════════════════════════════════════
def test_prompts_contain_json_keyword_for_deepseek(lga):
    # DeepSeek's json_object mode requires the literal word "json" in the prompt.
    assert "json" in lga.INTENT_CLASSIFICATION_PROMPT
    assert "json" in lga.REFLECT_PROMPT


def test_bind_json_mode_binds_and_falls_back(lga):
    class _Bindable:
        def __init__(self):
            self.kw = None

        def bind(self, **kw):
            self.kw = kw
            return "BOUND"

    b = _Bindable()
    assert lga._bind_json_mode(b) == "BOUND"
    assert b.kw == {"response_format": {"type": "json_object"}}
    # A client without .bind is returned unchanged (parse ladder remains the safety net).
    obj = object()
    assert lga._bind_json_mode(obj) is obj


def test_parse_intent_ladder_recovers_malformed(lga):
    valid = {n for n, _, _ in lga._INTENT_CATALOG}
    assert lga._parse_intent('{"intent": "web_search"}', valid) == "web_search"
    assert lga._parse_intent('Sure: {"intent": "direct_answer"} !', valid) == "direct_answer"
    assert lga._parse_intent('probably search_properties then', valid) == "search_properties"
    assert lga._parse_intent('@@@ no json @@@', valid) is None


def test_parse_reflect_ladder_recovers_malformed(lga):
    assert lga._parse_reflect_action('{"action": "answer"}')["action"] == "answer"
    v = lga._parse_reflect_action(
        'note {"action":"continue","next_intent":"web_search","next_query":"x"} end')
    assert v["action"] == "continue" and v["next_intent"] == "web_search"
    assert lga._parse_reflect_action('total nonsense')["action"] == "answer"   # fails CLOSED


def test_intent_classifier_applies_json_mode(lga):
    class _BindableJsonLLM:
        def __init__(self, content):
            self.content = content
            self.bound = False

        def bind(self, **kw):
            assert kw.get("response_format") == {"type": "json_object"}
            self.bound = True
            return self

        def invoke(self, _prompt):
            return types.SimpleNamespace(content=self.content)

    llm = _BindableJsonLLM('{"intent": "direct_answer"}')
    res = lga._majority_vote("hello there", {"current_message": "hello there"},
                             llm, _DummyRegistry(), {})
    assert llm.bound is True
    assert res["tool"] == "direct_answer"


def test_reflect_controller_applies_json_mode(lga):
    class _BindableReflect:
        def __init__(self, verdict):
            self.verdict = verdict
            self.bound = False

        def bind(self, **kw):
            assert kw.get("response_format") == {"type": "json_object"}
            self.bound = True
            return self

        def invoke(self, _prompt):
            return types.SimpleNamespace(content=json.dumps(self.verdict))

    r = _BindableReflect({"action": "answer"})
    # Multi-intent so the controller is actually consulted (short-circuit does not fire).
    lga._make_reflect_node(_DummyRegistry(), r)(
        _reflect_state("is it safe and how is the weather there"))
    assert r.bound is True
