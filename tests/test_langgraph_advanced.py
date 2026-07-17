"""Tests for the advanced LangGraph capabilities (HITL / Store / time-travel / durability).

Layers:
  * Topology guards — the DEFAULT graph must be unchanged; flags weave in exactly the
    expected nodes. This is the regression net protecting the production path.
  * Behaviour — the real confirm_search node + Store helpers + time-travel exercised through
    a minimal offline graph (no LLM, no network) that MIRRORS the production topology.
    Crucially, its generate_response node OVERWRITES final_response the way the real one
    does — an earlier version stubbed it as a no-op, which masked a real bug where the
    HITL cancel message was routed into generate_response and destroyed.
  * Regression tests for every adversarial-review finding (resume wiring, cancel routing,
    checkpoint_ns, store race, cleared-criteria resurrection, list removal, sentinel ids,
    malformed edit payloads).
"""
import operator
import os
import threading
from typing import Annotated, Any, Dict, List

import pytest

# get_classification_llm() constructs a lazy client; a dummy key keeps it importable offline.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")
os.environ.setdefault("LLM_PROVIDER", "deepseek")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Send
from typing_extensions import TypedDict

from core import graph_advanced as ga
from core.langgraph_agent import build_agent_graph


class _FakeRegistry:
    def get_all_tools(self):
        return []


def _nodes(graph):
    return {n for n in graph.get_graph().nodes if not n.startswith("__")}


# ── Topology guards ──────────────────────────────────────────────────────────────────
def test_default_topology_unchanged():
    """The production default must not gain any advanced nodes."""
    g = build_agent_graph(_FakeRegistry())
    nodes = _nodes(g)
    assert "confirm_search" not in nodes
    assert "hydrate_prefs" not in nodes
    assert "persist_prefs" not in nodes


def test_advanced_flags_weave_in_nodes():
    g = build_agent_graph(
        _FakeRegistry(), checkpointer=InMemorySaver(), store=InMemoryStore(), enable_hitl=True
    )
    nodes = _nodes(g)
    assert {"confirm_search", "hydrate_prefs", "persist_prefs"} <= nodes


def test_hitl_degrades_without_checkpointer():
    """interrupt() needs a checkpointer — without one, HITL must silently no-op."""
    g = build_agent_graph(_FakeRegistry(), enable_hitl=True)  # no checkpointer
    assert "confirm_search" not in _nodes(g)


# ── Store helpers (pure) ─────────────────────────────────────────────────────────────
def test_store_roundtrip_and_cross_thread_isolation():
    store = InMemoryStore()
    ga.save_persisted_prefs(store, "u1", {"max_budget": 1200, "destination": "UCL"})
    got = ga.load_persisted_prefs(store, "u1")
    assert got["max_budget"] == 1200 and got["destination"] == "UCL"
    # Same user, any thread → same profile; different user → empty.
    assert ga.load_persisted_prefs(store, "u1") == got
    assert ga.load_persisted_prefs(store, "u2") == {}


def test_store_save_is_authoritative_clear_propagates():
    """Fixed resurrection bug: an empty field in the post-hydrate criteria means the user
    cleared it this turn — the save must DROP it, not keep the stale value forever."""
    store = InMemoryStore()
    ga.save_persisted_prefs(store, "u1", {"max_budget": 1200, "destination": "LSE"})
    ga.save_persisted_prefs(store, "u1", {"max_budget": None, "destination": "LSE"})
    got = ga.load_persisted_prefs(store, "u1")
    assert "max_budget" not in got  # cleared value dropped, not resurrected
    assert got["destination"] == "LSE"


def test_store_list_removal_sticks():
    """Fixed one-way-ratchet: lists are replaced (union happens at hydrate), so removing a
    soft preference actually stays removed."""
    store = InMemoryStore()
    ga.save_persisted_prefs(store, "u1", {"soft_preferences": ["pet friendly", "quiet"]})
    ga.save_persisted_prefs(store, "u1", {"soft_preferences": ["quiet"]})
    assert ga.load_persisted_prefs(store, "u1")["soft_preferences"] == ["quiet"]


def test_store_falsy_meaningful_values_survive():
    store = InMemoryStore()
    ga.save_persisted_prefs(store, "u1", {"max_budget": 0})
    assert ga.load_persisted_prefs(store, "u1")["max_budget"] == 0


def test_store_sentinel_user_ids_fail_closed():
    """'default' / 'anonymous' are shared sentinels — the Store must fail closed on them,
    matching memory_tools' no-default-fallback rule."""
    store = InMemoryStore()
    for uid in ("default", "anonymous", "", None):
        ga.save_persisted_prefs(store, uid, {"max_budget": 999})
        assert ga.load_persisted_prefs(store, uid) == {}


def test_store_concurrent_saves_do_not_lose_updates():
    """Fixed lost-update race: the read-modify-write is now serialised by _PREFS_LOCK.
    Interleave saves of DISJOINT fields from many threads; every final field must be one
    of the values some thread wrote last — and with authoritative semantics + the lock,
    the final state must exactly equal the last writer's full payload."""
    store = InMemoryStore()
    errors = []

    def writer(i):
        try:
            for _ in range(50):
                ga.save_persisted_prefs(
                    store, "u1", {"max_budget": 1000 + i, "destination": f"city-{i}"}
                )
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    got = ga.load_persisted_prefs(store, "u1")
    # Atomic RMW → the profile is one writer's coherent payload, never a torn mix.
    assert got["max_budget"] - 1000 == int(got["destination"].split("-")[1])


def test_merge_this_turn_wins_and_unions_lists():
    persisted = {"max_budget": 1200, "property_features": ["ensuite"]}
    merged = ga._merge_into_accumulated({"max_budget": 900, "property_features": ["garden"]}, persisted)
    assert merged["max_budget"] == 900  # this-turn scalar wins
    assert set(merged["property_features"]) == {"garden", "ensuite"}  # lists unioned


@pytest.mark.parametrize("value,expected", [
    (True, ("proceed", None)),
    ("proceed", ("proceed", None)),
    (None, ("cancel", None)),
    ("cancel", ("cancel", None)),
    ({"action": "cancel"}, ("cancel", None)),
    ({"action": "edit", "searches": [{"tool": "t", "params": {}}]},
     ("edit", [{"tool": "t", "params": {}}])),
    # Fail-closed hardening: garbage resumes cancel instead of launching the fan-out.
    (42, ("cancel", None)),
    ([1, 2], ("cancel", None)),
    ({"action": "launch_missiles"}, ("cancel", None)),
    # An edit whose entries are all malformed cancels; valid entries are kept, junk dropped.
    ({"action": "edit", "searches": []}, ("cancel", None)),
    ({"action": "edit", "searches": ["junk", 42, {"no_tool": 1}]}, ("cancel", None)),
    ({"action": "edit", "searches": ["junk", {"tool": "t"}]}, ("edit", [{"tool": "t"}])),
])
def test_parse_resume(value, expected):
    assert ga._parse_resume(value) == expected


@pytest.mark.parametrize("text,expected", [
    ("yes", "proceed"), ("OK!", "proceed"), ("好的", "proceed"), ("确认", "proceed"),
    ("no", "cancel"), ("取消", "cancel"), ("算了", "cancel"),
    ("actually search Leeds instead", None), ("", None),
])
def test_parse_confirmation_reply(text, expected):
    assert ga.parse_confirmation_reply(text) == expected


# ── Behaviour via a minimal offline graph MIRRORING production topology ──────────────
class _MiniState(TypedDict, total=False):
    user_id: str
    accumulated_search_criteria: Dict[str, Any]
    clears: List[str]  # fields the user explicitly cleared THIS turn (mirrors extract's job)
    tool_decision: Dict[str, Any]
    task_plan: List[dict]
    tool_observation: str
    search_results: Annotated[List[dict], operator.add]
    final_response: str
    response_type: str


def _build_mini(*, checkpointer=None, store=None, enable_hitl=False):
    """Mirror the production shape: gather sets tool_observation; generate_response
    UNCONDITIONALLY rebuilds final_response from it (like the real LLM node does);
    format_output passes an existing final_response through. The HITL cancel message
    only survives because confirm_search routes to format_output."""

    def extract(state):
        # Mirrors production ordering: hydrate runs FIRST, then this turn's explicit
        # changes (including clears) are applied on top — so a clear beats the profile.
        clears = state.get("clears") or []
        if not clears:
            return {}
        crit = dict(state.get("accumulated_search_criteria") or {})
        for field in clears:
            crit[field] = None
        return {"accumulated_search_criteria": crit}

    def plan(state):
        crit = state.get("accumulated_search_criteria") or {}
        areas = crit.get("property_features") or ["Zone 2"]
        return {"task_plan": [{"id": f"t{i}", "index": i, "tool": "search_properties",
                               "params": {"area": a}, "depends_on": []}
                              for i, a in enumerate(areas)]}

    def fan_out(state):
        tasks = state.get("task_plan") or []
        return [Send("task_worker", {"task": t}) for t in tasks] or "gather_wave"

    def worker(state):
        return {"search_results": [{"area": state["task"]["params"]["area"]}]}

    def gather(state):
        areas = ", ".join(r["area"] for r in state.get("search_results", []))
        return {"tool_observation": f"searched: {areas}"}

    def generate_response(state):
        # Like the real node: rebuilds from observation, OVERWRITING any prior value.
        return {"final_response": f"LLM says: {state.get('tool_observation') or 'no data'}"}

    def format_output(state):
        return {}  # passthrough — preserves final_response, like the real node's default path

    g = StateGraph(_MiniState)
    g.add_node("hydrate_prefs", ga.make_hydrate_prefs_node())
    g.add_node("extract", extract)
    g.add_node("plan", plan)
    g.add_node("dispatch_tasks", lambda s: {})
    g.add_node("task_worker", worker)
    g.add_node("gather_wave", gather)
    g.add_node("generate_response", generate_response)
    g.add_node("format_output", format_output)
    g.add_node("persist_prefs", ga.make_persist_prefs_node())
    if enable_hitl:
        g.add_node("confirm_search", ga.make_confirm_search_node())
    g.add_edge(START, "hydrate_prefs")
    g.add_edge("hydrate_prefs", "extract")
    g.add_edge("extract", "plan")
    g.add_edge("plan", "confirm_search" if enable_hitl else "dispatch_tasks")
    g.add_conditional_edges("dispatch_tasks", fan_out, ["task_worker", "gather_wave"])
    g.add_edge("task_worker", "gather_wave")
    g.add_edge("gather_wave", "generate_response")
    g.add_edge("generate_response", "format_output")
    g.add_edge("format_output", "persist_prefs")
    g.add_edge("persist_prefs", END)
    opts = {}
    if checkpointer is not None:
        opts["checkpointer"] = checkpointer
    if store is not None:
        opts["store"] = store
    return g.compile(**opts)


def _cfg(user, conv):
    return {"configurable": {"thread_id": f"{user}:{conv}"}}


def test_hitl_interrupt_then_approve():
    g = _build_mini(checkpointer=InMemorySaver(), enable_hitl=True)
    cfg = _cfg("u1", "c1")
    initial = {"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden", "Islington"]}}
    paused = g.invoke(initial, cfg)
    assert paused.get("__interrupt__"), "graph should pause at confirm_search"
    assert len(paused["__interrupt__"][0].value["task_list"]) == 2
    resumed = g.invoke(Command(resume=True), cfg)
    assert "Camden" in resumed["final_response"] and "Islington" in resumed["final_response"]


def test_hitl_edit_before_execute():
    g = _build_mini(checkpointer=InMemorySaver(), enable_hitl=True)
    cfg = _cfg("u1", "c2")
    g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden", "Islington"]}}, cfg)
    edited = {"action": "edit", "searches": [{"tool": "search_properties", "params": {"area": "Camden"}}]}
    resumed = g.invoke(Command(resume=edited), cfg)
    assert "Camden" in resumed["final_response"]
    assert "Islington" not in resumed["final_response"]


def test_hitl_cancel_message_survives_real_generate_response():
    """Regression for the cancel-overwrite bug: the cancel message must survive a
    generate_response node that rebuilds final_response (route = format_output)."""
    g = _build_mini(checkpointer=InMemorySaver(), enable_hitl=True)
    cfg = _cfg("u1", "c3")
    g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden"]}}, cfg)
    resumed = g.invoke(Command(resume={"action": "cancel"}), cfg)
    assert "held off" in resumed["final_response"]
    assert "LLM says" not in resumed["final_response"]  # generate_response did NOT overwrite
    assert not resumed.get("search_results")  # search never ran


def test_hitl_abandon_with_fresh_input_restarts_cleanly():
    """Documented behavior the app relies on: fresh state input on an interrupted thread
    restarts from START (abandoning the pending confirmation) rather than erroring or
    mis-consuming the input as a resume value."""
    g = _build_mini(checkpointer=InMemorySaver(), enable_hitl=True)
    cfg = _cfg("u1", "c4")
    g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden"]}}, cfg)
    second = g.invoke(
        {"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Soho"]}}, cfg
    )
    # The new turn pauses again, now proposing the NEW plan — the old one was abandoned.
    planned = second["__interrupt__"][0].value["task_list"]
    assert [t["params"]["area"] for t in planned] == ["Soho"]


def test_store_hydrates_new_thread_cross_conversation():
    store = InMemoryStore()
    g = _build_mini(checkpointer=InMemorySaver(), store=store)
    # conv-A persists criteria for u1
    g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Shoreditch"]}}, _cfg("u1", "A"))
    assert ga.load_persisted_prefs(store, "u1")["property_features"] == ["Shoreditch"]
    # conv-B (new thread, empty input) is hydrated from the Store
    out_b = g.invoke({"user_id": "u1", "accumulated_search_criteria": {}}, _cfg("u1", "B"))
    assert "Shoreditch" in out_b["final_response"]
    # a different user is isolated
    out_c = g.invoke({"user_id": "u2", "accumulated_search_criteria": {}}, _cfg("u2", "A"))
    assert "Shoreditch" not in out_c["final_response"]


def test_store_cleared_criterion_does_not_resurrect_across_turns():
    """Full-cycle regression for the resurrection bug: after a turn whose criteria cleared
    a field (post-hydrate), the NEXT turn must not refill the stale value."""
    store = InMemoryStore()
    g = _build_mini(checkpointer=InMemorySaver(), store=store)
    # conv-A: user sets a budget → persisted.
    g.invoke({"user_id": "u1",
              "accumulated_search_criteria": {"max_budget": 1200, "property_features": ["Camden"]}},
             _cfg("u1", "A"))
    assert ga.load_persisted_prefs(store, "u1")["max_budget"] == 1200
    # conv-B turn 1: the user explicitly clears the budget. Production ordering applies:
    # hydrate refills 1200 from the Store FIRST, then extract's clear wins the turn —
    # and authoritative persist propagates the clear to the Store.
    g.invoke({"user_id": "u1",
              "accumulated_search_criteria": {"property_features": ["Camden"]},
              "clears": ["max_budget"]},
             _cfg("u1", "B"))
    assert "max_budget" not in ga.load_persisted_prefs(store, "u1")
    # conv-B turn 2: an empty-budget turn must NOT be refilled with the stale 1200.
    out = g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden"]}},
                   _cfg("u1", "B2"))
    assert not out["accumulated_search_criteria"].get("max_budget")


def test_time_travel_fork_alternate_branch():
    g = _build_mini(checkpointer=InMemorySaver())
    cfg = _cfg("u1", "tt")
    first = g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden"]}}, cfg)
    assert "Camden" in first["final_response"]
    # Rewind to the pre-plan checkpoint of the original run and fork with new criteria.
    pre_plan = next(s for s in g.get_state_history(cfg) if "plan" in (s.next or ()))
    forked_cfg = ga.fork_from_checkpoint(
        g, pre_plan.config, {"accumulated_search_criteria": {"property_features": ["Peckham", "Lewisham"]}}
    )
    forked = g.invoke(None, forked_cfg)
    assert "Peckham" in forked["final_response"] and "Lewisham" in forked["final_response"]
    assert "Camden" not in forked["final_response"]  # clean fork, not appended


def test_time_travel_checkpoint_id_param_path():
    """Regression for the KeyError('checkpoint_ns') bug: forking via the checkpoint_id=
    parameter with a hand-built config (thread_id only) must work on both savers' API."""
    g = _build_mini(checkpointer=InMemorySaver())
    cfg = _cfg("u1", "tt2")
    g.invoke({"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Camden"]}}, cfg)
    pre_plan = next(s for s in g.get_state_history(cfg) if "plan" in (s.next or ()))
    ckpt_id = pre_plan.config["configurable"]["checkpoint_id"]
    forked_cfg = ga.fork_from_checkpoint(
        g, _cfg("u1", "tt2"),  # bare config WITHOUT checkpoint_ns — used to KeyError
        {"accumulated_search_criteria": {"property_features": ["York"]}},
        checkpoint_id=ckpt_id,
    )
    forked = g.invoke(None, forked_cfg)
    assert "York" in forked["final_response"]


@pytest.mark.parametrize("mode", ["exit", "async", "sync"])
def test_durability_modes_all_run(mode):
    g = _build_mini(checkpointer=InMemorySaver())
    out = g.invoke(
        {"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Bow"]}},
        _cfg("u1", f"dur-{mode}"),
        durability=mode,
    )
    assert "Bow" in out["final_response"]
