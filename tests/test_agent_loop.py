"""Bounded agent LOOP + routing / language / emoji fixes (app/core/langgraph_agent.py).

The DAG became a bounded loop: decide -> tool -> reflect -> (decide again), capped at
MAX_AGENT_TURNS. These tests pin, WITHOUT any live LLM / network:

1. market_info negative guard — the reported regression 「…先不要搜索房源」 routes to the
   web-research path deterministically (vote must NOT run), and its precision.
2. The loop mechanics — continue-once-then-answer synthesises over BOTH observations;
   the turn cap forces an answer after exactly MAX_AGENT_TURNS tool executions; the
   no-progress guard breaks a repeating loop.
3. search_properties is NOT loopable (goes straight to format_output; reflect untouched).
4. reply_language hard directive (no zh/en mixing) + _has_cjk fallback.
5. Emoji stripping at the evidence + final-output layers (CJK preserved).
6. The widened property-context escape (a safety question about a focused listing does
   not get answered from the static record).

Harness mirrors test_intent_router.py / test_listing_advice.py: stubbed classification
LLM, stubbed reflect LLM, stubbed generation LLM — no real DeepSeek call.
"""

import json
import os
import sys
import types

# Pin the real source roots ahead of tests/ (stale shadow `core` copies live under
# tests/ and would otherwise shadow the app packages under prepend mode).
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


class _NoVoteLLM:
    """Fails if the LLM vote is ever reached — proves the deterministic guard fired."""

    def invoke(self, prompt):
        raise AssertionError("market_info guard must route before the LLM vote")


def _decide(lga, msg, llm, extra_ctx=None, accumulated=None, monkeypatch=None):
    if monkeypatch is not None:
        # No real network from web-search planning (market_info path).
        monkeypatch.setattr(lga, "_plan_web_searches",
                            lambda q, reg: {"tool": "multi_search",
                                            "params": {"searches": [{"tool": "web_search",
                                                                     "params": {"query": q}}]},
                                            "reason": "planned"})
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {"user_query": msg, "extracted_context": ec,
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE REGRESSION — explicit research + do-not-search -> market_info, not search
# ═══════════════════════════════════════════════════════════════════════════
def test_regression_research_do_not_search_routes_to_market_info(lga, monkeypatch):
    msg = "请你帮我做一下调研，UCL附近房源的价格大概是多少？先不要搜索房源"
    # _NoVoteLLM proves the DETERMINISTIC guard fired before any LLM vote.
    cmd = _decide(lga, msg, _NoVoteLLM(), monkeypatch=monkeypatch)
    d = cmd.update["tool_decision"]
    assert d["tool"] != "search_properties"
    assert d["tool"] == "multi_search"        # market_info -> web synthesis path
    assert "market_info" in d["reason"]


def test_guard_pure_predicate(lga):
    # The deterministic predicate itself: fires on the regression, not on a listings ask.
    assert lga._is_market_research_request(
        "请你帮我做一下调研，UCL附近房源的价格大概是多少？先不要搜索房源") is True
    assert lga._is_market_research_request("帮我找UCL附近的房子") is False
    # research verb + price noun, no listings ask -> fires
    assert lga._is_market_research_request("了解一下曼城的租金行情") is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. NEGATIVE-GUARD PRECISION — listings ask still searches; plain avg-rent still votes
# ═══════════════════════════════════════════════════════════════════════════
def test_find_flat_still_routes_to_search(lga, monkeypatch):
    cmd = _decide(lga, "帮我找UCL附近的房子", _JsonLLM("search_properties"), monkeypatch=monkeypatch)
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_plain_average_rent_reaches_the_vote(lga, monkeypatch):
    # No do-not-search phrase and no research verb -> the guard MUST NOT fire; the LLM
    # vote (market_info) owns it.
    cmd = _decide(lga, "what's the average rent in Zone 2", _JsonLLM("market_info"),
                  monkeypatch=monkeypatch)
    d = cmd.update["tool_decision"]
    assert d["tool"] == "multi_search"        # market_info via the vote
    assert lga._is_market_research_request("what's the average rent in Zone 2") is False


# ═══════════════════════════════════════════════════════════════════════════
# Loop harness — full compiled graph, all LLMs stubbed
# ═══════════════════════════════════════════════════════════════════════════
class _CountingRegistry:
    """Executes any tool, returning a canned per-tool observation; counts executions."""

    def __init__(self):
        self.calls = 0

    def list_tool_names(self):
        return ["get_weather", "web_search"]

    def get(self, _name):
        return types.SimpleNamespace(version="1", side_effect="none")

    async def execute_tool(self, name, **_kw):
        from core.tool_system import ToolResult
        self.calls += 1
        obs = "OBS_WEATHER" if name == "get_weather" else "OBS_WEB"
        return ToolResult(success=True, data={"obs": obs}, tool_name=name)


class _GenLLM:
    """Records the generation prompts it is asked to synthesise; echoes a grounded reply."""

    def __init__(self):
        self.prompts = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return types.SimpleNamespace(content="OBS_WEATHER OBS_WEB synthesised answer")


class _ScriptedReflect:
    """Returns a scripted list of reflect verdicts (dicts), one per invoke call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, _prompt):
        self.calls += 1
        verdict = self.script.pop(0) if self.script else {"action": "answer"}
        return types.SimpleNamespace(content=json.dumps(verdict))


def _build_loop_graph(lga, monkeypatch, reflect_llm, registry, intent="get_weather"):
    from core import llm_config
    monkeypatch.setattr(llm_config, "get_classification_llm", lambda: _JsonLLM(intent))
    gen = _GenLLM()
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: gen)
    graph = lga.build_agent_graph(registry, reflect_llm=reflect_llm)
    return graph, gen


def _run_turn(lga, graph, msg):
    from uk_rent_agent.agent.state import create_initial_state
    st = create_initial_state(msg, extracted_context={"current_message": msg})
    return asyncio.run(graph.ainvoke(st, config={"recursion_limit": lga.GRAPH_RECURSION_LIMIT}))


# ═══════════════════════════════════════════════════════════════════════════
# 3. LOOP — continue once, then answer -> two executions, both obs synthesised
# ═══════════════════════════════════════════════════════════════════════════
def test_loop_continue_once_then_answer(lga, monkeypatch):
    reflect = _ScriptedReflect([
        {"action": "continue", "next_intent": "web_search", "next_query": "extra detail",
         "reason": "need web"},
        {"action": "answer"},
    ])
    reg = _CountingRegistry()
    graph, gen = _build_loop_graph(lga, monkeypatch, reflect, reg)
    # Multi-intent message (safety + weather, joined by "and") so the reflect controller
    # is genuinely consulted on turn 1 — a single-intent message would hit the one-shot
    # short-circuit and answer after the first tool without ever calling reflect.
    out = _run_turn(lga, graph, "is London safe and what is the weather there")

    assert reg.calls == 2                                   # get_weather + one web_search
    obs = out["observations"]
    assert [(e["turn"], e["tool"]) for e in obs] == [(0, "get_weather"), (1, "web_search")]
    # The final synthesis evidence contains BOTH observations' text.
    assert "OBS_WEATHER" in gen.prompts[0] and "OBS_WEB" in gen.prompts[0]


# ═══════════════════════════════════════════════════════════════════════════
# 4. CAP — reflect always continues -> exactly MAX_AGENT_TURNS executions then answer
# ═══════════════════════════════════════════════════════════════════════════
def test_loop_cap_forces_answer(lga, monkeypatch):
    class _AlwaysContinueUnique:
        def __init__(self):
            self.calls = 0

        def invoke(self, _prompt):
            self.calls += 1
            return types.SimpleNamespace(content=json.dumps(
                {"action": "continue", "next_intent": "web_search",
                 "next_query": f"q{self.calls}", "reason": "more"}))

    reg = _CountingRegistry()
    graph, gen = _build_loop_graph(lga, monkeypatch, _AlwaysContinueUnique(), reg)
    # Multi-intent message (safety + weather, joined by "and") so the reflect controller
    # is genuinely consulted on turn 1 — a single-intent message would hit the one-shot
    # short-circuit and answer after the first tool without ever calling reflect.
    out = _run_turn(lga, graph, "is London safe and what is the weather there")

    assert reg.calls == lga.MAX_AGENT_TURNS
    assert len(out["observations"]) == lga.MAX_AGENT_TURNS
    assert out["final_response"]                            # an answer was still produced


# ═══════════════════════════════════════════════════════════════════════════
# 5. NO-PROGRESS GUARD — same next_intent+query repeated -> loop breaks to answer
# ═══════════════════════════════════════════════════════════════════════════
def test_loop_no_progress_guard_breaks(lga, monkeypatch):
    class _AlwaysSame:
        def invoke(self, _prompt):
            return types.SimpleNamespace(content=json.dumps(
                {"action": "continue", "next_intent": "web_search",
                 "next_query": "identical", "reason": "loop"}))

    reg = _CountingRegistry()
    graph, gen = _build_loop_graph(lga, monkeypatch, _AlwaysSame(), reg)
    # Multi-intent message (safety + weather, joined by "and") so the reflect controller
    # is genuinely consulted on turn 1 — a single-intent message would hit the one-shot
    # short-circuit and answer after the first tool without ever calling reflect.
    out = _run_turn(lga, graph, "is London safe and what is the weather there")

    # get_weather (turn 0) -> web_search 'identical' (turn 1) -> the SAME proposal is
    # caught by the no-progress guard -> answer. Well short of the cap.
    assert reg.calls == 2
    assert reg.calls < lga.MAX_AGENT_TURNS
    assert [e["tool"] for e in out["observations"]] == ["get_weather", "web_search"]


# ═══════════════════════════════════════════════════════════════════════════
# 6. search_properties does NOT enter reflect (behaviour identical to today)
# ═══════════════════════════════════════════════════════════════════════════
def test_search_properties_never_reflects(lga, monkeypatch):
    from core.tool_system import ToolResult

    class _SearchReg:
        def get(self, _name):
            return types.SimpleNamespace(version="1", side_effect="none")

        async def execute_tool(self, _name, **_kw):
            return ToolResult(success=True, tool_name="search_properties",
                              data={"status": "found", "summary": "found 1",
                                    "recommendations": [{"name": "Maple", "address": "Maple, London"}]})

    from uk_rent_agent.agent.state import create_initial_state
    state = create_initial_state("find me a flat in Camden")
    state["tool_decision"] = {"tool": "search_properties",
                              "params": {"user_query": "find me a flat in Camden"}}
    cmd = asyncio.run(lga._make_execute_tool_node(_SearchReg())(state))
    assert cmd.goto == "format_output"          # today's terminal, NOT reflect
    assert cmd.goto != "reflect"


def test_loopable_tool_routes_to_reflect(lga):
    # Foil to the above: a loopable tool (non-error) DOES hand off to reflect.
    from core.tool_system import ToolResult

    class _WeatherReg:
        def get(self, _name):
            return types.SimpleNamespace(version="1", side_effect="none")

        async def execute_tool(self, _name, **_kw):
            return ToolResult(success=True, tool_name="get_weather", data={"temp": "12C"})

    from uk_rent_agent.agent.state import create_initial_state
    state = create_initial_state("weather in London")
    state["tool_decision"] = {"tool": "get_weather", "params": {"location": "London"}}
    cmd = asyncio.run(lga._make_execute_tool_node(_WeatherReg())(state))
    assert cmd.goto == "reflect"


# ═══════════════════════════════════════════════════════════════════════════
# 7. reply_language — hard directive, no mixing; _has_cjk fallback when absent
# ═══════════════════════════════════════════════════════════════════════════
def _gen_state(msg, **ctx):
    return {"user_query": msg, "tool_observation": None, "tool_decision": {"tool": "direct_answer"},
            "extracted_context": {"current_message": msg, **ctx}, "user_preferences": {},
            "context_tainted": False}


def test_reply_language_zh_on_english_message(lga):
    # reply_language='zh' forces an all-Chinese reply even for an English prompt.
    p = lga._build_generation_prompt(_gen_state("what is the average rent", reply_language="zh"))
    assert "Write the ENTIRE reply in Chinese" in p
    assert "Do NOT mix English" in p


def test_reply_language_en_directive(lga):
    p = lga._build_generation_prompt(_gen_state("你好", reply_language="en"))
    assert "Write the ENTIRE reply in English" in p


def test_reply_language_absent_falls_back_to_has_cjk(lga):
    zh = lga._build_generation_prompt(_gen_state("请问租金水平如何"))     # CJK -> zh
    assert "Write the ENTIRE reply in Chinese" in zh
    en = lga._build_generation_prompt(_gen_state("what can you do"))       # ascii -> en
    assert "Write the ENTIRE reply in English" in en


# ═══════════════════════════════════════════════════════════════════════════
# 8. EMOJI — stripped at evidence + final-output layers, CJK preserved
# ═══════════════════════════════════════════════════════════════════════════
def test_strip_emoji_removes_symbols_keeps_cjk(lga):
    assert lga._strip_emoji("✅⚠️🔍") == ""
    assert lga._strip_emoji("✅ 在预算内") == "在预算内"
    assert "中文" in lga._strip_emoji("🔍中文📊")
    assert "£1200" in lga._strip_emoji("£1200 ✅")


def test_sanitize_final_response_strips_emoji(lga):
    out = lga._sanitize_final_response("Great choice ✅ near UCL 🎉")
    assert "✅" not in out and "🎉" not in out
    assert "near UCL" in out


def test_evidence_line_has_no_emoji_sentinel(lga):
    rec = {"name": "Maple", "address": "Maple, London", "budget_status": "✅ 在预算内"}
    line = lga._format_result_line(1, rec)
    assert "在预算内" in line
    assert "✅" not in line
    # and the single-result surface too
    single = lga._format_single_result({"address": "A", "budget_status": "✅ within budget"})
    assert "within budget" in single and "✅" not in single


# ═══════════════════════════════════════════════════════════════════════════
# 9. Property-context escape — a safety question about a focused listing escapes
# ═══════════════════════════════════════════════════════════════════════════
def test_focused_listing_safety_question_escapes_reasoning_property(lga, monkeypatch):
    cmd = _decide(lga, "这个房源附近安全吗", _JsonLLM("check_safety"),
                  extra_ctx={"property_address": "40 Merchant St, London E3"},
                  monkeypatch=monkeypatch)
    d = cmd.update["tool_decision"]
    assert d["tool"] != "reasoning_property"        # widened _LOCATION_INTENT_KWS escape
    assert d["tool"] == "check_safety"
    # the focused property is the resolved target (final fallback in _resolve_target_address)
    assert "Merchant St" in (d["params"].get("address") or "")


def test_focused_listing_plain_question_still_reasons_from_record(lga):
    # A NON-location question about the focused listing still answers from the record.
    cmd = _decide(lga, "这个房源值得租吗", _NoVoteLLM(),
                  extra_ctx={"property_address": "40 Merchant St, London E3"})
    assert cmd.update["tool_decision"]["tool"] == "reasoning_property"
