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
import time
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, ToolMessage

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
