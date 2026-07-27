"""Multi-intent EXECUTION PLAN + unified wave executor (app/core/langgraph_agent.py).

The single-tool router + reflect loop grew a concurrent multi-tool plan: a CURRENT message
that packs >= 2 distinct PLANNABLE intents is routed to build_execution_plan, which turns it
into a small set of tool tasks that run through ONE engine (dispatch_tasks -> task_worker x N
-> gather_wave), shared with the degenerate multi_search fan-out. These tests pin, WITHOUT any
live LLM / network:

1. The plan TRIGGER — multi-intent routes to build_execution_plan; single-intent does not.
2. build_execution_plan — planner fallback, build-time param resolution (ordinals), drop-with-
   note, all-drop -> clarification, digest dedup, MAX_PLAN_TASKS / MAX_PLAN_WAVES clamps.
3. The wave executor — depends_on ordering, cycle -> failed obs (never deadlock), worker
   timeout / one-failure-doesn't-kill-siblings, web-vs-structured taint.
4. Reflect integration — a whole plan is ONE loop step; reflect can chain one more serial tool
   after a plan; a degenerate multi_search still ends at generate_response (no reflect).
5. HITL — the confirm payload carries the task list; reject -> format_output.

Harness mirrors test_agent_loop.py: stubbed classification / planning / reflect / generation
LLMs, a canned tool registry — no real DeepSeek call.
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
        return ["get_weather", "web_search", "check_safety", "calculate_commute_cost"]

    def get(self, name):
        return types.SimpleNamespace(version="1", side_effect="none")


class _JsonLLM:
    """Returns the given intent as strict JSON (mimics the DeepSeek classifier)."""

    def __init__(self, intent):
        self.intent = intent

    def invoke(self, prompt):
        return types.SimpleNamespace(content=json.dumps({"intent": self.intent}))


class _NoVoteLLM:
    def invoke(self, prompt):
        raise AssertionError("deterministic guard must route before the LLM vote")


class _PlanLLM:
    """Returns a scripted plan. Pass a list of task dicts (wrapped as {"tasks": [...]}) or a
    raw string to simulate unparseable output."""

    def __init__(self, payload):
        self.payload = payload
        self.seen = None

    def invoke(self, prompt):
        self.seen = prompt
        content = self.payload if isinstance(self.payload, str) else json.dumps({"tasks": self.payload})
        return types.SimpleNamespace(content=content)


def _mp_planner(lga, monkeypatch, payload):
    from core import llm_config
    monkeypatch.setattr(llm_config, "get_planning_llm", lambda: _PlanLLM(payload))


def _run_plan(lga, monkeypatch, payload, base_decision=None, ec=None, accumulated=None,
              search_entry="dispatch_tasks"):
    _mp_planner(lga, monkeypatch, payload)
    node = lga._make_build_execution_plan_node(_DummyRegistry(), search_entry)
    state = {
        "tool_decision": base_decision or {"tool": "get_weather", "params": {"location": "London"}},
        "extracted_context": ec or {"current_message": "x"},
        "accumulated_search_criteria": accumulated or {},
        "observations": [],
        "user_query": (ec or {}).get("current_message", "x"),
    }
    return node(state)


# ═══════════════════════════════════════════════════════════════════════════
# 1. PLAN TRIGGER
# ═══════════════════════════════════════════════════════════════════════════
def _decide(lga, msg, llm, extra_ctx=None, accumulated=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(lga, "_plan_web_searches",
                            lambda q, reg: {"tool": "multi_search",
                                            "params": {"searches": [{"tool": "web_search",
                                                                     "params": {"query": q}}]},
                                            "reason": "m"})
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {"user_query": msg, "extracted_context": ec,
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


def test_multi_intent_zh_routes_to_build_execution_plan(lga, monkeypatch):
    # The contract example: research + safety + commute-cost, joined by 以及.
    msg = "调研一下 UCL 附近的租金趋势，同时看看第二套房源是否安全，以及通勤费用"
    # market-research fires deterministically (step 1.7) before any vote, so _NoVoteLLM proves
    # the plan trigger is reached without a classifier call.
    cmd = _decide(lga, msg, _NoVoteLLM(), monkeypatch=monkeypatch)
    assert cmd.goto == "build_execution_plan"


def test_single_intent_does_not_plan(lga, monkeypatch):
    cmd = _decide(lga, "what is the weather in London", _JsonLLM("get_weather"),
                  monkeypatch=monkeypatch)
    assert cmd.goto == "execute_tool"          # today's single-tool path, unchanged
    assert cmd.goto != "build_execution_plan"


def test_search_properties_stays_primary_even_when_multi_intent(lga, monkeypatch):
    # A listings ask joined with a safety ask must NOT be hijacked into a plan (the engine
    # excludes search_properties, which would drop the user's listings).
    #
    # SCOPE NOTE (2026-07-26). This assertion is still exactly right and is deliberately left
    # as-is, but it is NARROWER than it used to read. For a long time it was the only test
    # covering this shape, and "search_properties stays primary" was taken to mean the turn was
    # DONE after one tool — which is how legacy came to fetch nothing for the commute / nearby /
    # safety dimensions of the same message (measured: E1/E5/E6/E11, §6 below). Staying primary
    # is about the ROUTE at decide_tool; it says nothing about what must happen after the search
    # returns. The dimensions are now fanned out post-execution instead, so both hold: the
    # listings search is never pre-empted, AND the other dimensions are actually fetched.
    cmd = _decide(lga, "帮我找房 in Camden and is it safe", _JsonLLM("search_properties"),
                  monkeypatch=monkeypatch)
    assert cmd.goto == "execute_tool"
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_plannable_intents_counts_distinct(lga):
    got = lga._plannable_intents_in_message(
        "调研一下 UCL 附近的租金趋势，同时看看第二套房源是否安全，以及通勤费用")
    assert "check_safety" in got and "market_research" in got
    assert len(got) >= 2
    assert len(lga._plannable_intents_in_message("is London safe")) < 2


# ═══════════════════════════════════════════════════════════════════════════
# 2. build_execution_plan
# ═══════════════════════════════════════════════════════════════════════════
def test_planner_unparseable_falls_back_to_single_tool(lga, monkeypatch):
    base = {"tool": "get_weather", "params": {"location": "London"}}
    cmd = _run_plan(lga, monkeypatch, "this is not json at all", base_decision=base)
    assert cmd.goto == "execute_tool"                     # fell closed to the base decision
    assert cmd.update["tool_decision"] == base


def test_param_resolution_ordinal_at_build_time(lga, monkeypatch):
    tasks = [
        {"id": "t1", "tool": "check_safety",
         "params_hint": {"query": "第二套房源是否安全"}, "depends_on": []},
        {"id": "t2", "tool": "get_weather",
         "params_hint": {"query": "weather in London"}, "depends_on": []},
    ]
    ec = {"current_message": "…", "last_results": [
        {"address": "A Street, London E1"}, {"address": "B Road, London E2"}]}
    cmd = _run_plan(lga, monkeypatch, tasks, ec=ec)
    assert cmd.goto == "dispatch_tasks"
    assert cmd.update["plan_origin"] == "plan"
    plan = cmd.update["task_plan"]
    safety = next(t for t in plan if t["tool"] == "check_safety")
    # "第二套" resolved deterministically to the SECOND previous result — at BUILD time.
    assert "B Road" in (safety["params"].get("address") or "")


def test_unresolvable_task_drops_with_note_plan_proceeds(lga, monkeypatch):
    tasks = [
        {"id": "t1", "tool": "check_safety", "params_hint": {"query": "安全吗"}, "depends_on": []},
        {"id": "t2", "tool": "get_weather", "params_hint": {"query": "weather London"}, "depends_on": []},
        {"id": "t3", "tool": "web_search", "params_hint": {"query": "uk visa"}, "depends_on": []},
    ]
    cmd = _run_plan(lga, monkeypatch, tasks, ec={"current_message": "x"})  # no last_results/address
    assert cmd.goto == "dispatch_tasks"
    plan = cmd.update["task_plan"]
    assert {t["tool"] for t in plan} == {"get_weather", "web_search"}       # safety dropped
    assert len(cmd.update["plan_notes"]) == 1                                # with a synthetic note
    assert "check_safety" in cmd.update["plan_notes"][0]


def test_all_tasks_drop_yields_clarification(lga, monkeypatch):
    tasks = [
        {"id": "t1", "tool": "check_safety", "params_hint": {"query": "安全吗"}, "depends_on": []},
        {"id": "t2", "tool": "calculate_commute_cost",
         "params_hint": {"query": "通勤费用"}, "depends_on": []},
    ]
    cmd = _run_plan(lga, monkeypatch, tasks, ec={"current_message": "x"})
    assert cmd.goto == "format_output"
    assert cmd.update["tool_decision"]["tool"] == "clarification"
    assert cmd.update["tool_decision"].get("clarification_message")


def test_dedup_duplicate_digest_dropped(lga, monkeypatch):
    tasks = [
        {"id": "t1", "tool": "get_weather", "params_hint": {"query": "weather London"}, "depends_on": []},
        {"id": "t2", "tool": "get_weather", "params_hint": {"query": "weather London"}, "depends_on": []},
        {"id": "t3", "tool": "web_search", "params_hint": {"query": "uk visa"}, "depends_on": []},
    ]
    cmd = _run_plan(lga, monkeypatch, tasks)
    plan = cmd.update["task_plan"]
    assert len(plan) == 2                                                    # one get_weather dropped
    digests = [lga._params_digest(t["tool"], t["params"]) for t in plan]
    assert len(set(digests)) == len(digests)                                # all unique


def test_max_plan_tasks_clamp(lga, monkeypatch):
    tasks = [{"id": f"t{i}", "tool": "web_search",
              "params_hint": {"query": f"topic number {i}"}, "depends_on": []}
             for i in range(lga.MAX_PLAN_TASKS + 4)]
    cmd = _run_plan(lga, monkeypatch, tasks)
    assert len(cmd.update["task_plan"]) == lga.MAX_PLAN_TASKS


def test_max_plan_waves_drops_deep_task(lga, monkeypatch):
    # A chain t0<-t1<-t2<-t3: depth 3 (t3) hits MAX_PLAN_WAVES=3 and is dropped; t0..t2 stay.
    tasks = [{"id": "t0", "tool": "web_search", "params_hint": {"query": "q0"}, "depends_on": []},
             {"id": "t1", "tool": "web_search", "params_hint": {"query": "q1"}, "depends_on": ["t0"]},
             {"id": "t2", "tool": "web_search", "params_hint": {"query": "q2"}, "depends_on": ["t1"]},
             {"id": "t3", "tool": "web_search", "params_hint": {"query": "q3"}, "depends_on": ["t2"]}]
    cmd = _run_plan(lga, monkeypatch, tasks)
    ids = {t["id"] for t in cmd.update["task_plan"]}
    assert ids == {"t0", "t1", "t2"}


def test_plan_wave_depths_and_cycle(lga):
    tasks = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]},
             {"id": "c", "depends_on": ["b"]}]
    d = lga._plan_wave_depths(tasks)
    assert d == {"a": 0, "b": 1, "c": 2}
    cyc = [{"id": "x", "depends_on": ["y"]}, {"id": "y", "depends_on": ["x"]}]
    assert lga._plan_wave_depths(cyc) == {"x": None, "y": None}     # cycle -> None, no hang


# ═══════════════════════════════════════════════════════════════════════════
# 3. WAVE EXECUTOR — fan-out / gather
# ═══════════════════════════════════════════════════════════════════════════
def _t(tid, tool="web_search", deps=None, index=0):
    return {"id": tid, "index": index, "tool": tool, "params": {"query": tid},
            "depends_on": deps or []}


def test_fan_out_respects_depends_on(lga):
    plan = [_t("a", index=0), _t("b", deps=["a"], index=1)]
    st = {"task_plan": plan, "task_results": [], "run_id": "r1"}
    sends = lga.fan_out_tasks(st)
    assert isinstance(sends, list) and len(sends) == 1
    assert sends[0].arg["task"]["id"] == "a"                    # only 'a' is ready
    # After 'a' completes, 'b' becomes ready.
    st["task_results"] = [{"id": "a", "run_id": "r1"}]
    sends2 = lga.fan_out_tasks(st)
    assert [s.arg["task"]["id"] for s in sends2] == ["b"]


def test_fan_out_empty_ready_goes_to_gather(lga):
    st = {"task_plan": [], "task_results": [], "run_id": "r1"}
    assert lga.fan_out_tasks(st) == "gather_wave"


def test_gather_wave_loops_then_finalizes(lga):
    node = lga._make_gather_wave_node()
    plan = [_t("a", index=0), _t("b", deps=["a"], index=1)]
    # 'a' done, 'b' pending & ready -> another wave.
    st = {"task_plan": plan, "run_id": "r1", "plan_origin": "multi_search", "observations": [],
          "task_results": [{"id": "a", "index": 0, "tool": "web_search", "params": {},
                            "obs": "RA", "raw": {"results": "RA"}, "run_id": "r1"}]}
    cmd = node(st)
    assert cmd.goto == "dispatch_tasks"
    # both done -> finalize to generate_response (multi_search origin).
    st["task_results"].append({"id": "b", "index": 1, "tool": "web_search", "params": {},
                               "obs": "RB", "raw": {"results": "RB"}, "run_id": "r1"})
    cmd2 = node(st)
    assert cmd2.goto == "generate_response"
    assert "RA" in cmd2.update["tool_observation"] and "RB" in cmd2.update["tool_observation"]


def test_gather_wave_cycle_fails_obs_not_deadlock(lga):
    node = lga._make_gather_wave_node()
    plan = [_t("x", deps=["y"], index=0), _t("y", deps=["x"], index=1)]
    st = {"task_plan": plan, "run_id": "r1", "plan_origin": "multi_search",
          "observations": [], "task_results": []}
    cmd = node(st)
    assert cmd.goto == "generate_response"                       # finalized, not looped
    assert "could not be satisfied" in cmd.update["tool_observation"]


def test_gather_wave_taint_web_vs_structured(lga):
    node = lga._make_gather_wave_node()
    done = lambda tid, tool: {"id": tid, "index": 0, "tool": tool, "params": {},
                              "obs": "R", "raw": {"results": "R"}, "run_id": "r1"}
    web = {"task_plan": [_t("w", tool="web_search")], "run_id": "r1",
           "plan_origin": "multi_search", "observations": [],
           "task_results": [done("w", "web_search")]}
    assert node(web).update["context_tainted"] is True
    structured = {"task_plan": [_t("g", tool="get_weather")], "run_id": "r1",
                  "plan_origin": "multi_search", "observations": [], "context_tainted": False,
                  "task_results": [done("g", "get_weather")]}
    assert node(structured).update["context_tainted"] is False


def test_gather_wave_plan_origin_appends_observations_and_one_loop_step(lga):
    node = lga._make_gather_wave_node()
    plan = [_t("a", tool="get_weather", index=0), _t("b", tool="web_search", index=1)]
    st = {"task_plan": plan, "run_id": "r1", "plan_origin": "plan", "observations": [],
          "loop_turn": 0, "plan_notes": [],
          "task_results": [
              {"id": "a", "index": 0, "tool": "get_weather", "params": {}, "obs": "WX", "raw": None, "run_id": "r1"},
              {"id": "b", "index": 1, "tool": "web_search", "params": {}, "obs": "WB", "raw": None, "run_id": "r1"}]}
    cmd = node(st)
    assert cmd.goto == "reflect"
    assert cmd.update["loop_turn"] == 1                          # WHOLE plan == one loop step
    assert cmd.update["plan_just_completed"] is True
    obs = cmd.update["observations"]
    assert [e["tool"] for e in obs] == ["get_weather", "web_search"]
    assert all(e["turn"] == 0 for e in obs)                     # one step -> same turn index


# ═══════════════════════════════════════════════════════════════════════════
# 4. task_worker — timeout / failure isolation
# ═══════════════════════════════════════════════════════════════════════════
def test_task_worker_timeout_error_obs(lga, monkeypatch):
    monkeypatch.setattr(lga, "TOOL_TIMEOUT_DEFAULT", 0.05)
    monkeypatch.setattr(lga, "TOOL_TIMEOUTS", {})

    class _SlowReg:
        async def execute_tool(self, name, **kw):
            await asyncio.sleep(1.0)
            from core.tool_system import ToolResult
            return ToolResult(success=True, data={"x": 1}, tool_name=name)

    node = lga._make_task_worker_node(_SlowReg())
    out = asyncio.run(node({"task": _t("t", tool="get_weather"), "run_id": "r1"}))
    obs = out["task_results"][0]["obs"]
    assert "timed out" in obs and out["task_results"][0]["raw"] is None


def test_task_worker_failure_isolated_from_sibling(lga):
    from core.tool_system import ToolResult

    class _MixedReg:
        async def execute_tool(self, name, **kw):
            if name == "bad":
                raise RuntimeError("boom")
            return ToolResult(success=True, data={"results": "OK"}, tool_name=name)

    node = lga._make_task_worker_node(_MixedReg())
    bad = asyncio.run(node({"task": _t("b", tool="bad"), "run_id": "r1"}))
    good = asyncio.run(node({"task": _t("g", tool="web_search"), "run_id": "r1"}))
    assert bad["task_results"][0]["obs"].startswith("Error")
    assert "OK" in good["task_results"][0]["obs"]               # sibling unaffected


# ═══════════════════════════════════════════════════════════════════════════
# 5. INTEGRATION — full compiled graph, all LLMs stubbed
# ═══════════════════════════════════════════════════════════════════════════
class _CountingRegistry:
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
        return ToolResult(success=True, data={"results": obs}, tool_name=name)


class _GenLLM:
    def __init__(self):
        self.prompts = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return types.SimpleNamespace(content="synthesised answer over OBS_WEATHER OBS_WEB")


class _ScriptedReflect:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, _prompt):
        self.calls += 1
        v = self.script.pop(0) if self.script else {"action": "answer"}
        return types.SimpleNamespace(content=json.dumps(v))


class _NoCallReflect:
    def invoke(self, _prompt):
        raise AssertionError("reflect must not run on a degenerate multi_search")


_PLAN_TASKS = [
    {"id": "t1", "tool": "get_weather", "params_hint": {"query": "weather in London"}, "depends_on": []},
    {"id": "t2", "tool": "web_search", "params_hint": {"query": "uk student visa news"}, "depends_on": []},
]


def _build_graph(lga, monkeypatch, reflect_llm, registry, intent, plan_payload=_PLAN_TASKS,
                 **kw):
    from core import llm_config
    monkeypatch.setattr(llm_config, "get_classification_llm", lambda: _JsonLLM(intent))
    monkeypatch.setattr(llm_config, "get_planning_llm", lambda: _PlanLLM(plan_payload))
    gen = _GenLLM()
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: gen)
    graph = lga.build_agent_graph(registry, reflect_llm=reflect_llm, **kw)
    return graph, gen


def _run(lga, graph, msg, config=None):
    from uk_rent_agent.agent.state import create_initial_state
    st = create_initial_state(msg, extracted_context={"current_message": msg},
                              user_id="u", session_id="c")
    cfg = config or {"recursion_limit": lga.GRAPH_RECURSION_LIMIT}
    return asyncio.run(graph.ainvoke(st, config=cfg))


_MULTI = "what is the weather and any visa news"          # weather + web, joined by "and"


def test_plan_end_to_end_one_loop_step(lga, monkeypatch):
    reflect = _ScriptedReflect([{"action": "answer"}])
    reg = _CountingRegistry()
    graph, gen = _build_graph(lga, monkeypatch, reflect, reg, intent="get_weather")
    out = _run(lga, graph, _MULTI)

    assert reg.calls == 2                                  # both plan tasks ran concurrently
    obs = out["observations"]
    assert {e["tool"] for e in obs} == {"get_weather", "web_search"}
    assert all(e["turn"] == 0 for e in obs)                # the whole plan is ONE step
    assert out["loop_turn"] == 1
    assert "OBS_WEATHER" in gen.prompts[0] and "OBS_WEB" in gen.prompts[0]
    assert out["final_response"]


def test_reflect_chains_serial_tool_after_plan(lga, monkeypatch):
    # After the plan, reflect asks for ONE more serial web_search (different query -> passes the
    # no-progress guard), then answers.
    reflect = _ScriptedReflect([
        {"action": "continue", "next_intent": "web_search", "next_query": "extra topic",
         "reason": "one more"},
        {"action": "answer"},
    ])
    reg = _CountingRegistry()
    graph, gen = _build_graph(lga, monkeypatch, reflect, reg, intent="get_weather")
    out = _run(lga, graph, _MULTI)

    assert reg.calls == 3                                  # 2 plan tasks + 1 serial continuation
    assert len(out["observations"]) == 3
    assert out["loop_turn"] == 2


def test_degenerate_multi_search_ends_at_generate_response_no_reflect(lga, monkeypatch):
    monkeypatch.setattr(lga, "_plan_web_searches",
                        lambda q, reg: {"tool": "multi_search",
                                        "params": {"searches": [{"tool": "web_search",
                                                                 "params": {"query": q}}]},
                                        "reason": "m"})
    reg = _CountingRegistry()
    graph, gen = _build_graph(lga, monkeypatch, _NoCallReflect(), reg, intent="market_info")
    out = _run(lga, graph, "what's the average rent in Shoreditch")

    assert reg.calls == 1                                  # one web_search fan-out task
    assert not out.get("observations")                    # multi_search does not enter reflect
    assert out.get("loop_turn", 0) == 0
    assert out["final_response"]


# ═══════════════════════════════════════════════════════════════════════════
# 6. HITL — plan payload carries the task list; reject -> format_output
# ═══════════════════════════════════════════════════════════════════════════
def test_hitl_plan_payload_has_task_list_and_reject(lga, monkeypatch):
    # Sync invoke (like test_langgraph_advanced): interrupt() in a sync node needs a runnable
    # context, which the async path doesn't provide on Python 3.10. Only sync nodes run before
    # the pause (extract -> decide -> build_execution_plan -> confirm_search) and after a reject
    # (format_output), so sync invoke exercises the whole HITL path here.
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from uk_rent_agent.agent.state import create_initial_state

    reg = _CountingRegistry()
    graph, gen = _build_graph(lga, monkeypatch, _ScriptedReflect([{"action": "answer"}]), reg,
                              intent="get_weather", checkpointer=InMemorySaver(), enable_hitl=True)
    cfg = {"configurable": {"thread_id": "u:c"}, "recursion_limit": lga.GRAPH_RECURSION_LIMIT}
    st = create_initial_state(_MULTI, extracted_context={"current_message": _MULTI},
                              user_id="u", session_id="c")
    paused = graph.invoke(st, config=cfg)

    intr = paused["__interrupt__"][0].value
    assert intr["type"] == "confirm_search"
    assert len(intr["task_list"]) == 2
    for t in intr["task_list"]:
        assert {"id", "tool", "params", "depends_on"} <= set(t)
    assert reg.calls == 0                                  # nothing ran before approval

    resumed = graph.invoke(Command(resume={"action": "cancel"}), config=cfg)
    assert "held off" in resumed["final_response"]
    assert reg.calls == 0                                  # reject -> no tasks executed


# ═══════════════════════════════════════════════════════════════════════════
# 6. POST-SEARCH DIMENSION FAN-OUT
#
# The plan trigger excludes search_properties (§1 above), and search_properties is not in
# LOOPABLE_TOOLS, so a listings turn that ALSO asked about commute / safety / nearby used to
# execute exactly ONE tool and then apologise for the dimensions it never fetched.
#
# This is measured, not hypothesised. Retained legacy arm of the 2026-07-25 internal round
# (.runtime/round-8793c0b-internal-2026-07-25/eval/sweep-legacy/per_case.csv):
#   E1, E5, E6, E11 -> tool_batches=1, tools_executed=[search_properties]
#   E3, E9  (base decision NOT search_properties) -> 2 and 3 batches, dimensions fetched
# which isolates the cause to the search_properties exclusion, not to cue detection or the
# wave engine. The user-visible cost, from that round's E1 final_answer, is pinned below.
# ═══════════════════════════════════════════════════════════════════════════

# Verbatim from the retained legacy E1 answer: legacy told the user to go and find the crime
# and supermarket data themselves while check_safety / search_nearby_pois sat unused.
_E1_QUERY = ("Find me a 1-bed in Islington under £1600/month with a commute to UCL under "
             "40 minutes, a supermarket nearby, and avoid high-crime areas — go ahead and search.")
_E1_REGRESSION_STRINGS = (
    "The search results do not provide crime statistics.",
    "The search results do not include specific supermarket proximity data.",
)

_FOUND = {"status": "found", "summary": "found 1",
          "recommendations": [{"name": "Riversdale Road", "address": "Riversdale Road, London, N5",
                               "price": "£1,795"}],
          "search_criteria": {"area": "islington"}}


class _SearchOnlyReg:
    """A registry whose only tool result is the search_properties payload under test."""

    def __init__(self, data=None):
        self.data = _FOUND if data is None else data

    def get(self, _name):
        return types.SimpleNamespace(version="1", side_effect="none")

    def list_tool_names(self):
        return ["search_properties", "check_safety", "search_nearby_pois", "calculate_commute"]

    async def execute_tool(self, name, **_kw):
        from core.tool_system import ToolResult
        return ToolResult(success=True, tool_name=name, data=self.data)


def _exec_search(lga, msg, data=None):
    from uk_rent_agent.agent.state import create_initial_state
    state = create_initial_state(msg, extracted_context={"current_message": msg})
    state["tool_decision"] = {"tool": "search_properties", "params": {"user_query": msg}}
    return asyncio.run(lga._make_execute_tool_node(_SearchOnlyReg(data))(state))


def test_multi_dimension_search_fans_out_missing_dimensions(lga):
    # THE REGRESSION. On the pre-fix source this turn ends at format_output/generate_response
    # with no task_plan at all — one tool for a four-dimension ask.
    cmd = _exec_search(lga, _E1_QUERY)
    assert cmd.goto == "dispatch_tasks"
    assert cmd.update["plan_origin"] == lga._PLAN_ORIGIN_DIMENSIONS
    tools = [t["tool"] for t in cmd.update["task_plan"]]
    assert set(tools) == {"check_safety", "calculate_commute", "search_nearby_pois"}
    # Every dimension task is anchored on the REAL top listing, never on a guess.
    for t in cmd.update["task_plan"]:
        assert "Riversdale Road, London, N5" in json.dumps(t["params"], ensure_ascii=False)
    # The commute destination came from the message ("UCL"), resolved deterministically.
    commute = [t for t in cmd.update["task_plan"] if t["tool"] == "calculate_commute"][0]
    assert "University College London" in commute["params"]["to_address"]
    # The search observation is seeded first so the synthesis sees listings + dimensions.
    assert [e["tool"] for e in cmd.update["observations"]] == ["search_properties"]


def test_zh_multi_dimension_search_fans_out(lga):
    # E5's production query (zh). CJK cues match the raw text, not the lowercased copy.
    msg = ("帮我找伦敦月租不超过1400镑的单间，通勤到帝国理工不超过35分钟，"
           "附近要有超市，尽量避开治安差的区域。")
    cmd = _exec_search(lga, msg)
    assert cmd.goto == "dispatch_tasks"
    tools = {t["tool"] for t in cmd.update["task_plan"]}
    assert {"check_safety", "search_nearby_pois"} <= tools
    # DOCUMENTED SEPARATE GAP (not this change's to fix): the commute dimension IS cued here
    # ("通勤"), but _KNOWN_DESTINATIONS is ascii-only — it has 'imperial', not '帝国理工' — so
    # _resolve_destination_address cannot resolve a Chinese destination and the commute task is
    # correctly DROPPED rather than guessed. Adding CJK aliases would change calculate_commute /
    # calculate_commute_cost routing for every zh turn on the single-tool path too, so it needs
    # its own change. This asserts the drop is deliberate, not silent breakage.
    assert "calculate_commute" not in tools
    assert lga._resolve_destination_address(msg, {"current_message": msg}, {}) is None
    assert "commute" in dict(
        (d, t) for d, _c, _s, t in lga._SEARCH_DIMENSION_CUES)   # the cue itself does fire


def test_plain_listings_search_is_untouched(lga):
    # PR #29 guard: a turn that cued NO extra dimension must not pay for a wave.
    cmd = _exec_search(lga, "find me a flat in Camden")
    assert cmd.goto == "format_output"                 # byte-for-byte today's route
    assert "task_plan" not in cmd.update
    assert "plan_origin" not in cmd.update


def test_single_dimension_fans_out_only_that_dimension(lga):
    # One cued dimension -> exactly one task; the other two are never fetched.
    cmd = _exec_search(lga, "find me a flat in Camden with a supermarket nearby")
    assert cmd.goto == "dispatch_tasks"
    assert [t["tool"] for t in cmd.update["task_plan"]] == ["search_nearby_pois"]


def test_greeting_never_reaches_the_fanout(lga, monkeypatch):
    # A greeting routes to direct_answer at decide_tool and never executes a tool at all,
    # so the fan-out is unreachable for it by construction.
    cmd = _decide(lga, "hello there", _JsonLLM("direct_answer"), monkeypatch=monkeypatch)
    assert cmd.goto != "build_execution_plan"
    assert cmd.update["tool_decision"]["tool"] in ("direct_answer", "clarification")


def test_empty_search_never_fans_out_a_dimension(lga):
    # A fetched-but-unanchorable dimension is how a fabricated walk time is born (C1/C2 in
    # that same round invented walk times against empty evidence). No listing -> no fan-out.
    for data in ({"status": "found", "recommendations": []},
                 {"status": "need_clarification", "question": "which area?"},
                 {"status": "found", "recommendations": [{"name": None, "address": ""}]}):
        cmd = _exec_search(lga, _E1_QUERY, data=data)
        assert cmd.goto != "dispatch_tasks", data
        assert "task_plan" not in cmd.update, data


def test_dimension_already_satisfied_is_not_refetched(lga):
    assert lga._cued_search_dimensions(_E1_QUERY, {"search_properties"}) != []
    got = lga._cued_search_dimensions(
        _E1_QUERY, {"search_properties", "check_safety", "search_nearby_pois",
                    "calculate_commute"})
    assert got == []                                    # nothing left to fetch


def test_commute_dimension_dropped_when_no_destination(lga):
    # No resolvable destination -> the commute task is DROPPED, not guessed. Safety/nearby,
    # which need only the anchor, still run.
    cmd = _exec_search(lga, "find me a flat in Camden, is it safe and how long is the commute")
    tools = {t["tool"] for t in cmd.update["task_plan"]}
    assert "calculate_commute" not in tools
    assert tools == {"check_safety"}


def test_only_read_tools_are_ever_fanned_out(lga):
    # `remember` is the only write tool and drives the taint gate / zero-tolerance records.
    fanned = {tool for _dim, _cues, _sat, tool in lga._SEARCH_DIMENSION_CUES}
    assert "remember" not in fanned
    assert fanned <= set(lga.PLANNABLE_TOOLS)            # read, loopable, non-write


def test_gather_wave_dimension_origin_keeps_listings_and_answers(lga):
    node = lga._make_gather_wave_node()
    st = {"task_plan": [_t("dim_safety", tool="check_safety")], "run_id": "r1",
          "plan_origin": lga._PLAN_ORIGIN_DIMENSIONS, "loop_turn": 0, "plan_notes": [],
          "observations": [{"turn": 0, "tool": "search_properties",
                            "observation": "SEARCH_OBS", "params_digest": "d0"}],
          "tool_raw_data": dict(_FOUND),
          "task_results": [{"id": "dim_safety", "index": 0, "tool": "check_safety",
                            "params": {"address": "Riversdale Road, London, N5"},
                            "obs": "SAFETY_OBS", "raw": {"safety_score": 7.1}, "run_id": "r1"}]}
    cmd = node(st)
    # Straight to the answer — no reflect LLM hop for a deterministically chosen wave.
    assert cmd.goto == "generate_response"
    # The LISTINGS payload survives the reduce (format_output reads status/recommendations
    # off the top level to build the frontend card).
    assert cmd.update["tool_raw_data"]["status"] == "found"
    assert cmd.update["tool_raw_data"]["recommendations"] == _FOUND["recommendations"]
    assert cmd.update["tool_raw_data"]["check_safety_1"] == {"safety_score": 7.1}
    # Both evidence sources reach the synthesis (>1 observation switches it to combined).
    assert [e["tool"] for e in cmd.update["observations"]] == ["search_properties", "check_safety"]
    assert "SAFETY_OBS" in cmd.update["tool_observation"]


def test_format_output_dimension_turn_still_emits_listings(lga):
    # Without this, observations>1 made format_output return tool_data={} and the frontend
    # panel never repainted — i.e. it would DROP the user's listings, the exact failure the
    # plan-trigger exclusion exists to prevent.
    node = lga._make_format_output_node()
    out = node({
        "tool_decision": {"tool": "search_properties"},
        "tool_raw_data": dict(_FOUND),
        "final_response": "SYNTHESIS over listings + safety + commute + nearby",
        "user_preferences": {}, "plan_origin": lga._PLAN_ORIGIN_DIMENSIONS,
        "observations": [{"tool": "search_properties"}, {"tool": "check_safety"}],
        "extracted_context": {}, "accumulated_search_criteria": {},
    })
    assert out["final_response"].startswith("SYNTHESIS")        # prose is the synthesis
    assert out["tool_data"]["recommendations"] == _FOUND["recommendations"]
    assert out["tool_data"]["search_criteria"] == {"area": "islington"}
    # A NON-dimension loop synthesis keeps the old behaviour (no listings card).
    out2 = node({
        "tool_decision": {"tool": "search_properties"}, "tool_raw_data": dict(_FOUND),
        "final_response": "S", "user_preferences": {}, "plan_origin": "plan",
        "observations": [{"tool": "a"}, {"tool": "b"}],
        "extracted_context": {}, "accumulated_search_criteria": {},
    })
    assert out2["tool_data"] == {}


class _DimensionRegistry:
    """search_properties returns listings; each dimension tool returns its own marker. Records
    concurrency by tracking how many calls are in flight at once."""

    def __init__(self):
        self.calls, self.in_flight, self.max_in_flight, self.order = 0, 0, 0, []

    def list_tool_names(self):
        return ["search_properties", "check_safety", "search_nearby_pois", "calculate_commute"]

    def get(self, _name):
        return types.SimpleNamespace(version="1", side_effect="none")

    async def execute_tool(self, name, **_kw):
        from core.tool_system import ToolResult
        self.calls += 1
        self.order.append(name)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)          # hold the slot so a serial path cannot overlap
            if name == "search_properties":
                return ToolResult(success=True, tool_name=name, data=dict(_FOUND))
            payload = {"check_safety": {"safety_score": 7.1, "results": "OBS_SAFETY"},
                       "search_nearby_pois": {"pois": [{"name": "Tesco"}], "results": "OBS_POI"},
                       "calculate_commute": {"duration": "28 min", "results": "OBS_COMMUTE"}}[name]
            return ToolResult(success=True, tool_name=name, data=payload)
        finally:
            self.in_flight -= 1


def test_dimension_fanout_end_to_end_concurrent_and_keeps_listings(lga, monkeypatch):
    # Whole graph: decide_tool -> execute_tool(search) -> dispatch_tasks -> task_worker x3
    # -> gather_wave -> generate_response -> critic -> format_output.
    reg = _DimensionRegistry()
    graph, gen = _build_graph(lga, monkeypatch, _ScriptedReflect([]), reg,
                              intent="search_properties")
    out = _run(lga, graph, _E1_QUERY)

    assert reg.calls == 4                                   # search + 3 dimensions
    assert reg.order[0] == "search_properties"              # listings first, then the wave
    # The dimension wave is genuinely CONCURRENT (LangGraph Send fan-out), matching the fc
    # batch — perf/parallel-tool-batch established fc's batch was already parallel, so this
    # pins that legacy's is too rather than assuming it.
    assert reg.max_in_flight == 3
    # All four evidence sources reached the synthesis prompt. Order is DETERMINISTIC despite
    # the concurrent dispatch: gather_wave sorts task_results by index, so the wave always
    # reduces in _SEARCH_DIMENSION_CUES table order (safety, commute, nearby) regardless of
    # which worker finished first.
    assert [e["tool"] for e in out["observations"]] == [
        "search_properties", "check_safety", "calculate_commute", "search_nearby_pois"]
    prompt = gen.prompts[-1]
    for marker in ("OBS_SAFETY", "OBS_POI", "OBS_COMMUTE"):
        assert marker in prompt, marker
    # The user still gets their listings back in the frontend payload.
    assert out["tool_data"]["recommendations"] == _FOUND["recommendations"]
    assert out["final_response"]


def test_plain_listings_search_end_to_end_runs_exactly_one_tool(lga, monkeypatch):
    # PR #29 invariance, end to end: no cued dimension -> one tool, no wave, and the normal
    # search card (NOT a synthesis) is what the user gets.
    reg = _DimensionRegistry()
    graph, gen = _build_graph(lga, monkeypatch, _ScriptedReflect([]), reg,
                              intent="search_properties")
    out = _run(lga, graph, "find me a 1-bed in Islington under £1600/month")

    assert reg.calls == 1
    assert reg.order == ["search_properties"]
    assert gen.prompts == []                                # no generation LLM call at all
    assert out["tool_data"]["recommendations"] == _FOUND["recommendations"]
    assert out["final_response"] == _FOUND["summary"]        # the structured card, unchanged
