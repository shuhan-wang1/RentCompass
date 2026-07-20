"""Coverage for Phase 2.2 event-loop unblocking + tool time budgets (agent Q1).

Verifies, WITHOUT any live Overpass / network call:

  * batch tool budget cancels an unfinished tool and emits a structured timed_out artifact
    + matching ToolMessage;
  * turn tool budget exhaustion skips further batches with the same structured artifact;
  * denied-write artifact shape, its exclusion from card rendering, and that the no-progress
    guard still suppresses an identical retry;
  * search_nearby_pois' internal monotonic deadline: no per-type request is issued past the
    deadline and the remaining types come back as a partial result with a skipped-types note;
  * THE regression test for the confirmed root cause — search_nearby_pois runs off the event
    loop (registered sync -> executor thread) so a concurrent heartbeat keeps ticking while a
    blocking Overpass/geocode call is in flight.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import core.agent_loop as agent_loop
from core.agent_loop import build_fc_nodes


# ─── fakes (mirror tests/test_fc_loop.py) ───────────────────────────
@dataclass
class FakeSpec:
    name: str
    description: str = "desc"
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    side_effect: str = "none"
    retry_safe: bool = True
    version: str = "1"
    terminal: bool = False


class FakeResult:
    def __init__(self, success=True, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


class _FakeTool:
    def __init__(self, version="1", side_effect="none"):
        self.version = version
        self.side_effect = side_effect


class FakeProvider:
    def __init__(self, specs, results=None):
        self._specs = list(specs)
        self._results = results or {}
        self.calls = []

    def list_specs(self):
        return list(self._specs)

    def get(self, name):
        for s in self._specs:
            if s.name == name:
                return _FakeTool(version=s.version, side_effect=s.side_effect)
        return None

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        r = self._results.get(name)
        if callable(r):
            r = r(**params)
        return r if r is not None else FakeResult(True, {"ok": True})


class SlowProvider(FakeProvider):
    """execute_tool awaits `delay` before returning — used to overrun a budget."""

    def __init__(self, specs, delay):
        super().__init__(specs)
        self.delay = delay

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        await asyncio.sleep(self.delay)
        return FakeResult(True, {"ok": True})


class PerToolDelayProvider(FakeProvider):
    """execute_tool awaits a per-tool `delays[name]` — lets a slow read and a slower/faster
    write share one batch so the read/write partition can be exercised deterministically."""

    def __init__(self, specs, delays):
        super().__init__(specs)
        self.delays = dict(delays)

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        await asyncio.sleep(self.delays.get(name, 0.0))
        return FakeResult(True, {"ok": name})


class FakeChat:
    def __init__(self, scripted=None):
        self._scripted = list(scripted or [])

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return self._scripted.pop(0)


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _state(**over):
    st = {
        "user_query": "what is nearby",
        "extracted_context": {"current_message": "what is nearby", "reply_language": "en"},
        "accumulated_search_criteria": {},
        "user_preferences": {"hard_preferences": [], "soft_preferences": [], "excluded_areas": [],
                             "required_amenities": [], "safety_concerns": []},
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "r1",
        "loop_turn": 1,
        "messages": [],
        "tool_artifacts": [],
        "context_tainted": False,
        "final_response": "",
        "response_type": "answer",
        "turn_tool_budget_used_s": 0.0,
    }
    st.update(over)
    return st


def _exec_once(nodes, state):
    cmd = asyncio.run(nodes["execute_tools"](state))
    state.update(cmd.update or {})
    return state


# ─── batch budget ──────────────────────────────────────────────────
def test_batch_budget_kills_slow_tool(monkeypatch):
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = SlowProvider([FakeSpec("web_search")], delay=1.5)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"query": "x"}, "c1")])]

    state = _exec_once(nodes, state)

    timed = [a for a in state["tool_artifacts"] if a.get("timed_out")]
    assert len(timed) == 1
    assert timed[0]["tool"] == "web_search"
    assert timed[0]["raw_data"] is None
    assert timed[0]["success"] is False
    assert timed[0]["params_digest"]  # kept so no-progress can suppress a retry
    assert "batch budget" in timed[0]["error"]
    # matching ToolMessage so the model sees the tool did not return
    tmsg = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tmsg) == 1 and "batch budget" in tmsg[0].content


# ─── turn budget ───────────────────────────────────────────────────
def test_turn_budget_exhaustion_skips_batch(monkeypatch):
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "10")
    provider = FakeProvider([FakeSpec("web_search")],
                            {"web_search": FakeResult(True, {"results": "x"})})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    # turn already spent its whole budget before this batch.
    state = _state(loop_turn=3, turn_tool_budget_used_s=10.0)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"query": "x"}, "c1")])]

    state = _exec_once(nodes, state)

    assert provider.calls == []  # later batch is not executed
    timed = [a for a in state["tool_artifacts"] if a.get("timed_out")]
    assert len(timed) == 1 and timed[0]["error"] == "turn tool budget exhausted"
    tmsg = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert any("turn tool budget exhausted" in m.content for m in tmsg)


def test_turn_budget_accumulates_across_batches(monkeypatch):
    """turn_tool_budget_used_s grows with each executed batch (so the turn ceiling is real)."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "5")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = SlowProvider([FakeSpec("web_search")], delay=0.2)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"query": "x"}, "c1")])]

    state = _exec_once(nodes, state)
    assert state["turn_tool_budget_used_s"] >= 0.2  # at least the batch we just ran


# ─── denied-write artifact ─────────────────────────────────────────
class _DenyGate:
    frozen = []

    @staticmethod
    def user_authorizes_memory(msg):
        return False

    @staticmethod
    def memory_write_allowed(*, context_tainted, user_authorized):
        return False

    @staticmethod
    def freeze_pending_write(session_id, content, kind):
        _DenyGate.frozen.append((session_id, content, kind))
        return "digestX"


def test_denied_artifact_shape_and_no_progress(monkeypatch):
    _DenyGate.frozen = []
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _DenyGate)
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    state = _state(context_tainted=True)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("remember", {"content": "user likes Camden", "kind": "semantic"}, "c1")])]

    # batch 1: the tainted write is denied.
    state = _exec_once(nodes, state)
    assert provider.calls == []  # never executed
    denied = [a for a in state["tool_artifacts"] if a.get("denied")]
    assert len(denied) == 1
    d = denied[0]
    assert d["tool"] == "remember"
    assert d["raw_data"] is None
    assert d["success"] is False
    assert d["error"] == "denied: tainted write requires confirmation"
    assert d["params_digest"]

    # batch 2: an identical remember call is suppressed by the no-progress guard.
    state["messages"].append(AIMessage(content="", tool_calls=[
        _tc("remember", {"content": "user likes Camden", "kind": "semantic"}, "c2")]))
    state = _exec_once(nodes, state)
    assert provider.calls == []  # still never executed
    assert any(isinstance(m, ToolMessage) and "already ran" in m.content
               for m in state["messages"])


def test_timed_out_and_denied_excluded_from_cards():
    """A card tool that timed out (raw_data=None) must not render a card."""
    provider = FakeProvider([FakeSpec("check_safety")])
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["tool_artifacts"] = [
        {"turn": 0, "tool": "check_safety", "raw_data": None, "params_digest": "d1",
         "success": False, "error": "timeout after 20s (batch budget)", "timed_out": True},
    ]
    out = nodes["format_output_fc"](state)
    # no safety card was assembled from a timed-out artifact
    assert "safety_score" not in out["tool_data"]
    assert out["response_type"] == "answer"


# ─── search_nearby_pois internal deadline ──────────────────────────
def test_poi_internal_deadline_partial(monkeypatch):
    import core.tools.search_nearby_pois as sp

    clock = {"t": 1000.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(sp.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(sp, "POI_SEARCH_BUDGET_S", 5.0)
    monkeypatch.setattr(sp, "geocode_address", lambda addr: (51.5, -0.1))

    calls = []

    def fake_query(lat, lon, ptype, *a, **k):
        calls.append(ptype)
        clock["t"] += 3.0  # each Overpass query costs 3s of the shared budget
        return [{"name": f"{ptype} A", "icon": "X", "distance_display": "10m"}]

    monkeypatch.setattr(sp, "query_osm_pois", fake_query)

    # poi_type="all" -> [restaurant, supermarket, convenience, cafe]; budget 5s:
    # restaurant @1000 -> 1003, pace -> 1003.3, supermarket @1003.3 -> 1006.3,
    # convenience @1006.3 >= deadline(1005) -> skip convenience + cafe.
    res = sp.search_nearby_pois_impl(address="x", poi_type="all")

    assert calls == ["restaurant", "supermarket"]  # nothing issued past the deadline
    assert res["partial"] is True
    assert set(res["skipped_types"]) == {"convenience", "cafe"}
    assert "budget" in res["note"].lower()
    assert "convenience" in res["note"].lower() or "Convenience Store" in res["note"]


def test_poi_no_deadline_when_fast(monkeypatch):
    """Well within budget -> no partial flag, all types queried."""
    import core.tools.search_nearby_pois as sp
    monkeypatch.setattr(sp, "geocode_address", lambda addr: (51.5, -0.1))
    monkeypatch.setattr(sp, "POI_PACING_S", 0.0)
    calls = []

    def fake_query(lat, lon, ptype, *a, **k):
        calls.append(ptype)
        return [{"name": f"{ptype} A", "icon": "X", "distance_display": "10m"}]

    monkeypatch.setattr(sp, "query_osm_pois", fake_query)
    res = sp.search_nearby_pois_impl(address="x", poi_type="restaurant")
    assert calls == ["restaurant"]
    assert res.get("partial") is not True
    assert "skipped_types" not in res


# ─── event-loop non-blocking (THE regression test) ─────────────────
def test_event_loop_not_blocked_by_poi(monkeypatch):
    """search_nearby_pois is registered as a sync function, so Tool.execute offloads it to an
    executor thread; a blocking Overpass/geocode call must therefore NOT freeze the event
    loop. Run it concurrently with a heartbeat coroutine and assert the heartbeat keeps
    ticking while a 0.4s blocking query is in flight. If the impl blocked the loop (the
    original async-def-with-sync-calls bug), the heartbeat could not advance."""
    import core.tools.search_nearby_pois as sp
    monkeypatch.setattr(sp, "geocode_address", lambda addr: (51.5, -0.1))

    def blocking_query(*a, **k):
        time.sleep(0.4)  # REAL blocking call; must run in a thread, not on the loop
        return []

    monkeypatch.setattr(sp, "query_osm_pois", blocking_query)

    async def run():
        ticks = {"n": 0}
        stop = {"v": False}

        async def heartbeat():
            while not stop["v"]:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        hb = asyncio.ensure_future(heartbeat())
        result = await sp.search_nearby_pois_tool.execute(address="x", poi_type="restaurant")
        stop["v"] = True
        await hb
        return ticks["n"], result

    ticks, result = asyncio.run(run())
    assert ticks >= 5, f"event loop appeared blocked (only {ticks} heartbeat ticks)"
    assert result.success is True


# ─── Phase 2.3: per-call cap, write no-abandon, attribution ─────────
def test_per_call_timeout_capped_by_remaining_window(monkeypatch, caplog):
    """A tool whose TOOL_TIMEOUTS entry (25s) exceeds the batch window is clamped to the
    window at dispatch — it does NOT burn 25s. The straggler is abandoned and the artifact
    NAMES it, carries elapsed_ms, and the attribution log record attributes the batch kill."""
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "big_read", 25)
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")  # proxy for the 20s window
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = SlowProvider([FakeSpec("big_read")], delay=1.5)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("big_read", {"q": "x"}, "c1")])]

    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="core.agent_loop"):
        state = _exec_once(nodes, state)
    wall = time.monotonic() - t0

    assert wall < 1.5, f"tool ran to its 25s/1.5s completion instead of the window ({wall:.2f}s)"
    ab = [a for a in state["tool_artifacts"] if a.get("abandoned")]
    assert len(ab) == 1
    a = ab[0]
    assert a["tool"] == "big_read"           # artifact NAMES the abandoned tool
    assert a["outcome_unknown"] is True
    assert a["timed_out"] is True            # kept for the eval three-way split
    assert a["raw_data"] is None
    assert "abandoned" in a["error"] and "batch budget" in a["error"]
    assert isinstance(a["elapsed_ms"], int)  # per-call timing lands on the artifact
    assert a["elapsed_ms"] <= 900             # ~window, nowhere near 25s
    # attribution log record: names the tool, the batch budget, kind=batch, abandoned=True
    rec = [r for r in caplog.records if "fc_loop.tool_budget_timeout" in r.getMessage()]
    assert len(rec) == 1
    msg = rec[0].getMessage()
    assert "tool=big_read" in msg and "kind=batch" in msg and "abandoned=True" in msg


def test_write_runs_past_window_while_reads_abandoned(monkeypatch):
    """side_effect=='write' is excluded from the abandon set: the batch AWAITS the write to
    completion even past the batch window, while a sibling read that overruns is abandoned."""
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: None)  # legacy allow path
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = PerToolDelayProvider(
        [FakeSpec("web_search"), FakeSpec("save_note", side_effect="write")],
        delays={"web_search": 1.5, "save_note": 0.5})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(context_tainted=False)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("web_search", {"q": "x"}, "c1"),
        _tc("save_note", {"content": "note"}, "c2")])]

    state = _exec_once(nodes, state)

    called = sorted(n for n, _ in provider.calls)
    assert called == ["save_note", "web_search"]  # both dispatched
    by_tool = {a["tool"]: a for a in state["tool_artifacts"]}
    # sibling read abandoned
    assert by_tool["web_search"].get("abandoned") is True
    # write completed past the 0.3s window (delay 0.5) — never abandoned / unknown
    w = by_tool["save_note"]
    assert w["success"] is True
    assert w["raw_data"] == {"ok": "save_note"}
    assert "abandoned" not in w and "outcome_unknown" not in w and "timed_out" not in w
    assert isinstance(w["elapsed_ms"], int) and w["elapsed_ms"] >= 400


def test_write_own_timeout_is_outcome_unknown(monkeypatch):
    """If even the write's own wait_for fires, the artifact says outcome UNKNOWN (the write may
    still complete in the background) — never a clean failure, mirroring MCPToolClient."""
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: None)
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "save_note", 0.3)  # tiny write wait_for
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "5")   # window is not the binding cap
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = PerToolDelayProvider(
        [FakeSpec("save_note", side_effect="write")], delays={"save_note": 1.5})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(context_tainted=False)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("save_note", {"content": "note"}, "c1")])]

    state = _exec_once(nodes, state)

    w = next(a for a in state["tool_artifacts"] if a["tool"] == "save_note")
    assert w["success"] is False
    assert w["outcome_unknown"] is True
    assert "abandoned" not in w            # a write is NEVER abandoned
    assert "outcome unknown" in w["error"] and "background" in w["error"]
    assert isinstance(w["elapsed_ms"], int)
    tmsg = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert any("outcome_unknown" in m.content for m in tmsg)


def test_elapsed_ms_on_executed_artifact():
    """Every executed artifact carries elapsed_ms so the eval events show tool timing."""
    provider = FakeProvider([FakeSpec("web_search")],
                            {"web_search": FakeResult(True, {"results": "x"})})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")])]

    state = _exec_once(nodes, state)

    a = next(x for x in state["tool_artifacts"] if x["tool"] == "web_search")
    assert a["success"] is True
    assert isinstance(a["elapsed_ms"], int) and a["elapsed_ms"] >= 0


def test_turn_exhaustion_emits_turn_attribution(monkeypatch, caplog):
    """Turn-budget exhaustion emits a kind='turn', abandoned=False attribution record."""
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "10")
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(loop_turn=3, turn_tool_budget_used_s=10.0)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")])]

    with caplog.at_level(logging.WARNING, logger="core.agent_loop"):
        state = _exec_once(nodes, state)

    rec = [r for r in caplog.records if "fc_loop.tool_budget_timeout" in r.getMessage()]
    assert len(rec) == 1
    msg = rec[0].getMessage()
    assert "kind=turn" in msg and "abandoned=False" in msg
    a = next(x for x in state["tool_artifacts"] if x["tool"] == "web_search")
    assert a["timed_out"] is True and a["elapsed_ms"] == 0


# ═══════════════════════════════════════════════════════════════════
# Latency/observability round: turn-wide soft wrap, eval stream, deadline
# injection, partial surfacing, H12 recall-question gate.
# ═══════════════════════════════════════════════════════════════════

class WrapChat:
    """Records bind_tools(tools, tool_choice=...) and the messages ainvoke saw, so the
    soft-wrap path can be asserted (tools disabled + wrap directive present)."""

    def __init__(self, reply):
        self._reply = reply
        self.bound_tools = "unset"
        self.tool_choice = "unset"
        self.seen_messages = None

    def bind_tools(self, tools, tool_choice=None, **kw):
        self.bound_tools = tools
        self.tool_choice = tool_choice
        return self

    async def ainvoke(self, messages):
        self.seen_messages = list(messages)
        return self._reply


# ─── Task 1: turn-wide soft wrap ────────────────────────────────────
def test_soft_wrap_forces_answer_now(monkeypatch):
    """elapsed > FC_TURN_SOFT_WRAP_S -> the agent node calls the LLM with tools disabled
    (tool_choice='none'), appends the wrap directive, records the soft-wrap event once, and
    routes to critic with a final answer (no new tool batch)."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    events = []
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event",
                        lambda **kw: events.append(kw))
    chat = WrapChat(AIMessage(content="Here is what I found so far."))
    provider = FakeProvider([FakeSpec("web_search"), FakeSpec("search_properties")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _state(
        loop_turn=3,
        messages=[HumanMessage(content="find flats in Camden")],
        turn_start_monotonic=time.monotonic() - 30.0,
        tool_artifacts=[{"turn": 0, "tool": "web_search", "raw_data": {"x": 1},
                         "params_digest": "d0", "success": True}],
    )

    cmd = asyncio.run(nodes["agent"](state))

    # FIX 3: a wrapped turn skips the (LLM/expensive) critic entirely and renders directly.
    assert cmd.goto == "format_output_fc"
    assert cmd.update["final_response"] == "Here is what I found so far."
    # tools disabled for the wrap call
    assert chat.tool_choice == "none"
    # wrap directive present, model-facing only (a SystemMessage on the prompt)
    assert any("TIME BUDGET NEARLY EXHAUSTED" in getattr(m, "content", "")
               for m in chat.seen_messages)
    # ...but NOT persisted into the returned messages channel (user-invisible)
    assert not any("TIME BUDGET NEARLY EXHAUSTED" in getattr(m, "content", "")
                   for m in cmd.update["messages"])
    # soft-wrap event fired exactly once with llm_calls/tool_batches
    assert len(events) == 1
    assert events[0]["llm_calls"] == 4 and events[0]["tool_batches"] == 1
    assert events[0]["elapsed_ms"] >= 25000


def test_no_soft_wrap_under_threshold(monkeypatch):
    """Under the soft-wrap threshold the agent opens tool batches normally (tools bound,
    no wrap event, routes to execute_tools)."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    events = []
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event",
                        lambda **kw: events.append(kw))
    chat = WrapChat(AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")]))
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _state(loop_turn=1, messages=[HumanMessage(content="hi")],
                   turn_start_monotonic=time.monotonic() - 1.0)

    cmd = asyncio.run(nodes["agent"](state))

    assert cmd.goto == "execute_tools"   # normal tool batch, not wrapped
    assert events == []                   # no soft-wrap event
    assert chat.tool_choice is None       # normal bind_tools (tool_choice not forced)


def test_per_call_timeout_respects_soft_wrap_remainder(monkeypatch):
    """The per-call min() folds in the soft-wrap remainder: a batch dispatched just before the
    wrap edge cannot run its full window. Turn started 24.5s ago (remainder ~0.5s) despite a
    20s window and a 30s tool timeout -> the tool is abandoned in well under a second."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    # remainder ~0.5s is still >= FC_MIN_BATCH_S here, so the batch is DISPATCHED (bounded),
    # not skipped — this asserts the fold binds a dispatched batch (the skip path is covered
    # by test_batch_skipped_when_below_min_batch).
    monkeypatch.setenv("FC_MIN_BATCH_S", "0.1")
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "web_search", 30)
    provider = SlowProvider([FakeSpec("web_search")], delay=1.5)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(turn_start_monotonic=time.monotonic() - 24.5)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")])]

    t0 = time.monotonic()
    state = _exec_once(nodes, state)
    wall = time.monotonic() - t0

    assert wall < 1.5, f"soft-wrap remainder did not bind the dispatch ({wall:.2f}s)"
    timed = [a for a in state["tool_artifacts"] if a.get("timed_out")]
    assert len(timed) == 1 and timed[0]["tool"] == "web_search"


# ─── Task 2: tool_budget_timeout into the eval stream ───────────────
def test_batch_abandon_emits_eval_budget_timeout(monkeypatch):
    """The batch-abandon path mirrors its attribution into the eval stream via
    record_tool_budget_timeout with phase='batch', outcome='abandoned'."""
    events = []
    monkeypatch.setattr(agent_loop, "_record_budget_timeout_event",
                        lambda **kw: events.append(kw))
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider = SlowProvider([FakeSpec("web_search")], delay=1.5)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")])]

    state = _exec_once(nodes, state)

    assert len(events) == 1
    e = events[0]
    assert e["tool"] == "web_search"
    assert e["phase"] == "batch"
    assert e["outcome"] == "abandoned"
    assert e["budget_s"] > 0 and e["elapsed_ms"] >= 0


def test_turn_and_write_unknown_emit_eval_outcomes(monkeypatch):
    """Turn exhaustion -> outcome='timed_out'/phase='turn'; write-own-timeout ->
    outcome='outcome_unknown'/phase='per_call'."""
    events = []
    monkeypatch.setattr(agent_loop, "_record_budget_timeout_event",
                        lambda **kw: events.append(kw))
    # turn exhaustion
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "10")
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(loop_turn=3, turn_tool_budget_used_s=10.0)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("web_search", {"q": "x"}, "c1")])]
    _exec_once(nodes, state)
    assert events[-1]["phase"] == "turn" and events[-1]["outcome"] == "timed_out"

    # write own wait_for fires -> outcome unknown
    events.clear()
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: None)
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "save_note", 0.3)
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "5")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    provider2 = PerToolDelayProvider([FakeSpec("save_note", side_effect="write")],
                                     delays={"save_note": 1.5})
    nodes2 = build_fc_nodes(provider2, agent_llm=FakeChat())
    state2 = _state(context_tainted=False)
    state2["messages"] = [AIMessage(content="", tool_calls=[_tc("save_note", {"content": "n"}, "c1")])]
    _exec_once(nodes2, state2)
    assert events[-1]["phase"] == "per_call" and events[-1]["outcome"] == "outcome_unknown"


# ─── Task 3: deadline injection + partial surfacing ─────────────────
def test_search_deadline_injected_and_hidden(monkeypatch):
    """search_properties receives an absolute-monotonic _deadline_monotonic (a future time),
    which is NOT in the model-visible tool schema and (leading underscore) is excluded from
    the digest/idempotency identity."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "5")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    captured = {}

    def sp_result(**params):
        captured.update(params)
        return FakeResult(True, {"status": "found", "recommendations": []})

    provider = FakeProvider([FakeSpec("search_properties")], {"search_properties": sp_result})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(turn_start_monotonic=time.monotonic())
    state["messages"] = [AIMessage(content="",
                                   tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")])]

    now = time.monotonic()
    state = _exec_once(nodes, state)

    assert "_deadline_monotonic" in captured
    assert isinstance(captured["_deadline_monotonic"], float)
    assert now < captured["_deadline_monotonic"] <= now + 6.0  # bounded by the 5s window
    assert "idempotency_key" in captured
    # not in the model-visible schema built for bind_tools
    tools = agent_loop._specs_to_openai(provider.list_specs())
    sp = next(t for t in tools if t["function"]["name"] == "search_properties")
    assert "_deadline_monotonic" not in json.dumps(sp)


def test_partial_note_surfaced_in_toolmsg():
    """A deadline-driven partial search: the model-facing ToolMessage surfaces partial + note
    prominently; the raw artifact keeps every field intact."""
    data = {"status": "found", "recommendations": [{"id": 1}], "partial": True,
            "partial_note": "Only 2 of 5 areas searched before the deadline.",
            "incomplete_areas": ["Shoreditch", "Hackney"]}
    provider = FakeProvider([FakeSpec("search_properties")],
                            {"search_properties": FakeResult(True, data)})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state()
    state["messages"] = [AIMessage(content="",
                                   tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")])]

    state = _exec_once(nodes, state)

    tmsg = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert '"partial": true' in tmsg.content
    assert "Only 2 of 5 areas searched before the deadline." in tmsg.content
    # raw artifact keeps ALL fields untouched
    art = next(a for a in state["tool_artifacts"] if a["tool"] == "search_properties")
    assert art["raw_data"]["incomplete_areas"] == ["Shoreditch", "Hackney"]
    assert art["raw_data"]["partial_note"] == "Only 2 of 5 areas searched before the deadline."


# ─── Task 4: H12 recall-question write gate ─────────────────────────
class _RecallGate:
    pure = True
    authorized = False

    @staticmethod
    def is_pure_recall_question(msg):
        return _RecallGate.pure

    @staticmethod
    def write_authorization(msg, content):
        return _RecallGate.authorized

    @staticmethod
    def user_authorizes_memory(msg):
        return _RecallGate.authorized

    @staticmethod
    def content_is_user_stated(content, msg):
        return False

    @staticmethod
    def memory_write_allowed(*, context_tainted, user_authorized):
        return (not context_tainted) or user_authorized

    @staticmethod
    def freeze_pending_write(session_id, content, kind):
        return "dig"


def test_pure_recall_denies_model_remember(monkeypatch):
    """On a CLEAN (untainted) turn, a model-initiated remember is denied when the current
    message is a pure recall question — regardless of taint — as a distinct denied artifact."""
    _RecallGate.pure = True
    _RecallGate.authorized = False
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _RecallGate)
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(context_tainted=False)  # CLEAN turn
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("remember", {"content": "user budget 1400", "kind": "semantic"}, "c1")])]

    state = _exec_once(nodes, state)

    assert provider.calls == []  # never executed
    denied = [a for a in state["tool_artifacts"] if a.get("denied")]
    assert len(denied) == 1
    assert denied[0]["error"] == "denied: recall-question turn, memory write blocked"
    tmsg = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "recall question" in tmsg.content.lower()


def test_explicit_authorization_bypasses_recall_gate(monkeypatch):
    """Explicit authorization wins over the recall gate (order-of-evaluation): the write runs."""
    _RecallGate.pure = True
    _RecallGate.authorized = True
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _RecallGate)
    provider = FakeProvider([FakeSpec("remember", side_effect="write")],
                            {"remember": FakeResult(True, {"saved": True})})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(context_tainted=False)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("remember", {"content": "user budget 1400", "kind": "semantic"}, "c1")])]

    state = _exec_once(nodes, state)

    assert [c[0] for c in provider.calls] == ["remember"]  # executed
    assert [a for a in state["tool_artifacts"] if a.get("denied")] == []


class _ReplayGate:
    @staticmethod
    def latest_pending_digest(session_id):
        return "digX"

    @staticmethod
    def confirmation_intent(msg):
        return "yes"

    @staticmethod
    def user_authorizes_memory(msg):
        return False

    @staticmethod
    def consume_pending_write(session_id, digest):
        return {"content": "user budget 1400", "kind": "semantic"}

    @staticmethod
    def is_pure_recall_question(msg):
        return True  # even a recall-shaped confirmation must not block the frozen replay


def test_frozen_replay_bypasses_recall_gate(monkeypatch):
    """The frozen pending-confirmation replay (agent node, not the executor gate) saves the
    frozen candidate verbatim and is unaffected by the recall-question gate."""
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _ReplayGate)
    provider = FakeProvider([FakeSpec("remember", side_effect="write")],
                            {"remember": FakeResult(True, {"saved": True})})
    chat = FakeChat([AIMessage(content="Saved it.")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _state(messages=[])  # first entry -> pending-memory replay resolves

    cmd = asyncio.run(nodes["agent"](state))

    assert [c[0] for c in provider.calls] == ["remember"]  # replay saved verbatim
    assert cmd.goto == "critic"


# ═══════════════════════════════════════════════════════════════════
# Latency-leak round: soft-fold must bound EVERY dispatch, bounded wrap
# call + deterministic fallback, wrapped-turn critic bypass, retuned
# defaults, wrap-directive source/figure rules.
# ═══════════════════════════════════════════════════════════════════

class SleepyChat:
    """LLM whose ainvoke hangs `delay`s — used to overrun the bounded wrap-up call so the
    deterministic fallback + cancel-and-abandon path fires."""

    def __init__(self, delay, reply=None):
        self._delay = delay
        self._reply = reply or AIMessage(content="late answer")
        self.bound_tools = "unset"
        self.tool_choice = "unset"

    def bind_tools(self, tools, tool_choice=None, **kw):
        self.bound_tools = tools
        self.tool_choice = tool_choice
        return self

    async def ainvoke(self, messages):
        await asyncio.sleep(self._delay)
        return self._reply


# ─── FIX 1(b): the CR4-shape regression — soft fold bounds a dispatched batch ──
def test_cr4_soft_fold_bounds_read_batch(monkeypatch):
    """CR4 shape (scaled for test speed): a batch dispatched with only a small soft-wrap
    remainder must have its window bounded to that remainder, NOT the full FC_BATCH_TOOL_BUDGET_S
    (20s) / tool timeout (30s). Here soft remaining ~= 1.0s while the batch budget is 20s and the
    tool would run 30s -> the soft remainder is the binding cap and the straggler is abandoned by
    ~1s. (Live CR4: 4.4s remaining, yet the batch got a ~14.4s window — the fold missed.)"""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setenv("FC_MIN_BATCH_S", "0.5")   # 1.0s remaining >= min -> dispatch, not skip
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "search_properties", 30)
    provider = SlowProvider([FakeSpec("search_properties")], delay=30)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(turn_start_monotonic=time.monotonic() - 24.0)  # soft remaining ~1.0s
    state["messages"] = [AIMessage(content="",
                                   tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")])]

    t0 = time.monotonic()
    state = _exec_once(nodes, state)
    wall = time.monotonic() - t0

    # bounded by the ~1s soft remainder, nowhere near the 20s window or 30s tool timeout
    assert wall < 4.0, f"soft fold did not bound the dispatched batch ({wall:.2f}s)"
    ab = [a for a in state["tool_artifacts"] if a.get("abandoned")]
    assert len(ab) == 1 and ab[0]["tool"] == "search_properties"
    assert ab[0]["timed_out"] is True and ab[0]["outcome_unknown"] is True
    assert ab[0]["elapsed_ms"] <= 2500  # ~1s window, not 20s/30s


def test_cr4_soft_fold_bounds_write_wait_for(monkeypatch):
    """The write path was the genuinely-unbounded window: writes ran their full per-tool
    wait_for (up to 30s) past the soft deadline because only reads folded the remainder. A write
    dispatched with ~1s of soft runway must now have its OWN wait_for bounded to the remainder
    (never abandoned, but never past it) -> its wait_for fires and it is outcome_unknown."""
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: None)  # legacy allow path
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "40")
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setenv("FC_MIN_BATCH_S", "0.5")
    monkeypatch.setitem(agent_loop.TOOL_TIMEOUTS, "save_note", 30)  # full write timeout is 30s
    provider = PerToolDelayProvider([FakeSpec("save_note", side_effect="write")],
                                    delays={"save_note": 30})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(context_tainted=False, turn_start_monotonic=time.monotonic() - 24.0)
    state["messages"] = [AIMessage(content="", tool_calls=[_tc("save_note", {"content": "n"}, "c1")])]

    t0 = time.monotonic()
    state = _exec_once(nodes, state)
    wall = time.monotonic() - t0

    assert wall < 4.0, f"write wait_for was not folded with the soft remainder ({wall:.2f}s)"
    w = next(a for a in state["tool_artifacts"] if a["tool"] == "save_note")
    assert w["success"] is False and w["outcome_unknown"] is True
    assert "abandoned" not in w  # a write is never abandoned, only bounded


# ─── FIX 1(a): batch skipped below FC_MIN_BATCH_S -> denied, then wrap ──
def test_batch_skipped_when_below_min_batch(monkeypatch):
    """soft_remaining < FC_MIN_BATCH_S -> the whole batch is NOT dispatched (no executor thread
    leaked): every call becomes a denied/not-executed artifact with a clear error, and the loop
    routes back to the agent."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setenv("FC_MIN_BATCH_S", "2.0")
    provider = FakeProvider([FakeSpec("search_properties")],
                            {"search_properties": FakeResult(True, {"status": "found"})})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    state = _state(turn_start_monotonic=time.monotonic() - 24.0)  # soft remaining ~1.0 < 2.0
    state["messages"] = [AIMessage(content="",
                                   tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")])]

    cmd = asyncio.run(nodes["execute_tools"](state))
    state.update(cmd.update or {})

    assert cmd.goto == "agent"
    assert provider.calls == []  # nothing dispatched -> no thread leaked
    denied = [a for a in state["tool_artifacts"] if a.get("denied")]
    assert len(denied) == 1
    assert denied[0]["error"] == "denied: turn time budget exhausted"
    assert not agent_loop._is_executed(denied[0])  # must NOT count as executed
    tmsg = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert any("turn time budget exhausted" in m.content for m in tmsg)


def test_skipped_batch_leads_to_exactly_one_wrap_no_loop(monkeypatch):
    """A skipped batch routes to the agent, which — past the wrap edge — takes the wrap branch
    exactly once and terminates at format_output_fc (no re-plan, no infinite loop)."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setenv("FC_MIN_BATCH_S", "2.0")
    events = []
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event",
                        lambda **kw: events.append(kw))
    # A chat that would keep planning tools forever if it were ever consulted past the wrap edge.
    chat = WrapChat(AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "x"}, "cN")]))
    provider = FakeProvider([FakeSpec("search_properties")])
    nodes = build_fc_nodes(provider, agent_llm=chat)

    turn_start = time.monotonic() - 24.0  # past the wrap edge (25 - 2 = 23)
    # 1) execute_tools skips the straddling batch (soft remaining ~1.0 < 2.0) -> back to agent
    st = _state(loop_turn=4, turn_start_monotonic=turn_start,
                messages=[AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "x"}, "c1")])])
    cmd1 = asyncio.run(nodes["execute_tools"](st))
    st.update(cmd1.update or {})
    assert cmd1.goto == "agent"

    # 2) the next agent entry wraps ONCE and terminates (never dispatches the planned batch).
    chat._reply = AIMessage(content="Best-effort answer from what I have.")
    cmd2 = asyncio.run(nodes["agent"](st))
    assert cmd2.goto == "format_output_fc"
    assert len(events) == 1          # exactly one wrap
    assert provider.calls == []      # the planned batch was never dispatched


# ─── FIX 2: bounded wrap call + deterministic fallback ──────────────
def test_wrap_call_timeout_falls_back_to_deterministic(monkeypatch):
    """When the wrap-up LLM call overruns its bounded window it is cancelled-and-abandoned (the
    call is NOT awaited to completion) and a DETERMINISTIC answer is synthesized from the
    gathered artifacts: it names the tools that ran, renders the artifact's recommendations, and
    states plainly that it was cut short — never fabricating numbers."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setenv("FC_MIN_BATCH_S", "2.0")
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event", lambda **kw: None)
    chat = SleepyChat(delay=5.0)  # far longer than the bounded wrap window (floored at 2.0s)
    provider = FakeProvider([FakeSpec("search_properties")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    art = {"turn": 0, "tool": "search_properties", "params_digest": "d0", "success": True,
           "raw_data": {"status": "found",
                        "recommendations": [{"title": "Studio in Camden", "price_display": "£1,400 pcm"}]}}
    state = _state(loop_turn=4,
                   extracted_context={"current_message": "find flats", "reply_language": "en"},
                   messages=[HumanMessage(content="find flats in Camden")],
                   turn_start_monotonic=time.monotonic() - 29.0,  # -> wrap_timeout floored at 2.0s
                   tool_artifacts=[art])

    t0 = time.monotonic()
    cmd = asyncio.run(nodes["agent"](state))
    wall = time.monotonic() - t0

    # did not await the 5s call to completion — bounded to ~the 2s wrap window
    assert wall < 4.0, f"wrap call was awaited past its bound ({wall:.2f}s)"
    assert cmd.goto == "format_output_fc"
    resp = cmd.update["final_response"]
    assert "search_properties" in resp            # names the tool that ran
    assert "Studio in Camden" in resp             # artifact-derived content, rendered plainly
    assert "£1,400 pcm" in resp                   # only a figure PRESENT in the artifact
    assert "cut short" in resp.lower()            # honest time-budget note
    assert "late answer" not in resp              # the LLM's late reply was discarded


def test_wrap_deterministic_fallback_never_claims_no_listings(monkeypatch):
    """A partial/timed-out search with no clean recs must NOT be reported as 'no listings' by the
    deterministic fallback (zero-tolerance rule)."""
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event", lambda **kw: None)
    state = _state(
        extracted_context={"current_message": "find flats", "reply_language": "en"},
        tool_artifacts=[{"turn": 0, "tool": "search_properties", "params_digest": "d",
                         "raw_data": None, "success": False, "timed_out": True, "abandoned": True,
                         "outcome_unknown": True, "error": "abandoned"}])
    ans = agent_loop._deterministic_wrap_answer(state)
    assert "cut short" in ans.lower()
    assert "no listings" not in ans.lower() and "no results" not in ans.lower()


# ─── FIX 3: wrapped-turn critic fast-path (<0.5s, bypass critic) ────
def test_wrapped_turn_bypasses_critic_fast(monkeypatch):
    """A wrapped turn with a fast LLM renders in well under 0.5s and routes to format_output_fc
    (NOT the critic) — the wrapped-turn critic tail is deterministic and cheap."""
    monkeypatch.setenv("FC_TURN_SOFT_WRAP_S", "25")
    monkeypatch.setattr(agent_loop, "_record_turn_soft_wrap_event", lambda **kw: None)
    chat = WrapChat(AIMessage(content="Here is my best-effort answer."))
    provider = FakeProvider([FakeSpec("search_properties")])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _state(loop_turn=3, messages=[HumanMessage(content="find flats")],
                   turn_start_monotonic=time.monotonic() - 26.0)

    t0 = time.monotonic()
    cmd = asyncio.run(nodes["agent"](state))
    wall = time.monotonic() - t0

    assert cmd.goto == "format_output_fc"      # critic bypassed
    assert wall < 0.5                          # wrapped-turn tail is fast


# ─── FIX 4: retuned defaults ────────────────────────────────────────
def test_retuned_defaults(monkeypatch):
    for k in ("FC_TURN_SOFT_WRAP_S", "FC_FINAL_RESERVE_S", "FC_MIN_BATCH_S",
              "FC_WRAP_CRITIC_RESERVE_S"):
        monkeypatch.delenv(k, raising=False)
    assert agent_loop._turn_soft_wrap_s() == 23.0
    assert agent_loop._final_reserve_s() == 5.0
    assert agent_loop._min_batch_s() == 2.0
    assert agent_loop._wrap_critic_reserve_s() == 1.0


# ─── FIX 5: wrap directive additions ────────────────────────────────
def test_wrap_directive_has_source_and_figure_rules():
    d = agent_loop._WRAP_DIRECTIVE.lower()
    assert "cite" in d and "source" in d                       # source-citation instruction
    assert "only numbers" in d and ("appear" in d or "present" in d)  # no-invented-figures rule
    assert "onthemarket" in d                                  # concrete source example
