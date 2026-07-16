"""Runnable, offline demo of the four advanced LangGraph capabilities added on this branch.

    python examples/langgraph_advanced_demo.py

No LLM, no network, no API keys. It builds a MINIMAL "mini rent agent" graph that mirrors the
real project's patterns (hydrate -> plan -> confirm -> Send fan-out -> gather -> persist) and
reuses the SHIPPED helpers from ``core.graph_advanced`` so what you see running here is the
same code the real graph runs. Each section prints what happened and why it matters.

Concepts demonstrated:
  1. HITL — interrupt() pauses before the fan-out; Command(resume=...) approves / edits / cancels.
  2. Store — durable criteria saved under one thread are visible from a DIFFERENT thread.
  3. Time-travel — update_state rewinds a thread and forks an alternate branch.
  4. Durability — the invoke-time durability= knob ("exit" | "async" | "sync").
"""
from __future__ import annotations

import operator
import sys
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

# Force UTF-8 stdout so the narration renders on a Windows GBK console too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make ``core`` importable when run as a script from the repo root.
_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Send
from typing_extensions import TypedDict

from core.graph_advanced import (  # the REAL shipped helpers
    fork_from_checkpoint,
    make_confirm_search_node,
    make_hydrate_prefs_node,
    make_persist_prefs_node,
)


class MiniState(TypedDict, total=False):
    user_id: str
    accumulated_search_criteria: Dict[str, Any]
    tool_decision: Dict[str, Any]
    tool_observation: str
    search_results: Annotated[List[dict], operator.add]
    final_response: str
    response_type: str


def _plan_node(state: MiniState) -> dict:
    """Turn the current criteria into a multi_search plan (one sub-search per area)."""
    crit = state.get("accumulated_search_criteria") or {}
    budget = crit.get("max_budget", "any")
    areas = crit.get("property_features") or ["Zone 2"]
    searches = [{"tool": "search_properties", "params": {"area": a, "max_budget": budget}} for a in areas]
    return {"tool_decision": {"tool": "multi_search", "params": {"searches": searches}}}


def _fan_out(state: MiniState):
    searches = (state["tool_decision"].get("params") or {}).get("searches") or []
    if not searches:
        return "gather_searches"
    return [Send("search_worker", {"search": s, "i": i}) for i, s in enumerate(searches)]


def _worker_node(state) -> dict:
    s = state["search"]
    area = s.get("params", {}).get("area")
    return {"search_results": [{"area": area, "hits": 3}]}  # pretend we scraped 3 listings


def _gather_node(state: MiniState) -> dict:
    results = state.get("search_results", [])
    areas = ", ".join(r["area"] for r in results)
    total = sum(r["hits"] for r in results)
    return {"tool_observation": f"Found {total} listings across: {areas}."}


def _generate_node(state: MiniState) -> dict:
    # Mirrors the REAL generate_response: rebuilds final_response from the observation,
    # OVERWRITING whatever was there — which is exactly why the HITL cancel branch must
    # route to format_output instead of here.
    return {"final_response": state.get("tool_observation") or "no data", "response_type": "answer"}


def _format_node(state: MiniState) -> dict:
    # Mirrors the real format_output default path: passes an existing final_response through.
    return {}


def build_mini_graph(*, checkpointer=None, store=None, enable_hitl=False):
    """Compile the mini graph. When enable_hitl, plan routes into the real confirm_search node."""
    g = StateGraph(MiniState)
    g.add_node("hydrate_prefs", make_hydrate_prefs_node())
    g.add_node("plan", _plan_node)
    g.add_node("dispatch_searches", lambda s: {})
    g.add_node("search_worker", _worker_node)
    g.add_node("gather_searches", _gather_node)
    g.add_node("generate_response", _generate_node)
    g.add_node("format_output", _format_node)
    g.add_node("persist_prefs", make_persist_prefs_node())
    if enable_hitl:
        g.add_node("confirm_search", make_confirm_search_node())

    g.add_edge(START, "hydrate_prefs")
    g.add_edge("hydrate_prefs", "plan")
    if enable_hitl:
        g.add_edge("plan", "confirm_search")  # confirm_search Command-routes onward
    else:
        g.add_edge("plan", "dispatch_searches")
    g.add_conditional_edges("dispatch_searches", _fan_out, ["search_worker", "gather_searches"])
    g.add_edge("search_worker", "gather_searches")
    g.add_edge("gather_searches", "generate_response")
    g.add_edge("generate_response", "format_output")
    g.add_edge("format_output", "persist_prefs")
    g.add_edge("persist_prefs", END)

    opts = {}
    if checkpointer is not None:
        opts["checkpointer"] = checkpointer
    if store is not None:
        opts["store"] = store
    return g.compile(**opts)


def _cfg(user_id: str, conv_id: str) -> dict:
    return {"configurable": {"thread_id": f"{user_id}:{conv_id}"}}


def _rule(title: str) -> None:
    print("\n" + "=" * 72 + f"\n> {title}\n" + "=" * 72)


def demo_hitl() -> None:
    _rule("1. HITL — interrupt() before the expensive search fan-out")
    graph = build_mini_graph(checkpointer=InMemorySaver(), enable_hitl=True)
    initial = {"user_id": "u1", "accumulated_search_criteria": {"max_budget": 1200, "property_features": ["Camden", "Islington"]}}

    for label, resume in [
        ("APPROVE", True),
        ("EDIT (drop Islington)", {"action": "edit", "searches": [{"tool": "search_properties", "params": {"area": "Camden"}}]}),
        ("CANCEL", {"action": "cancel"}),
    ]:
        cfg = _cfg("u1", f"conv-{label}")
        paused = graph.invoke(initial, cfg)
        intr = paused.get("__interrupt__")
        planned = intr[0].value["planned_searches"]
        print(f"\n  [pause]  paused — graph asked to confirm {len(planned)} searches: {[s['params']['area'] for s in planned]}")
        resumed = graph.invoke(Command(resume=resume), cfg)
        print(f"  >  resume={label}: {resumed['final_response']}")


def demo_store() -> None:
    _rule("2. Store — durable criteria saved in conv-A, reused in conv-B (cross-thread)")
    store = InMemoryStore()
    graph = build_mini_graph(checkpointer=InMemorySaver(), store=store)

    # Conversation A: the user has a budget + target areas; persist_prefs writes them to the Store.
    graph.invoke(
        {"user_id": "u1", "accumulated_search_criteria": {"max_budget": 1000, "property_features": ["Shoreditch"]}},
        _cfg("u1", "conv-A"),
    )
    from core.graph_advanced import load_persisted_prefs
    print(f"\n  conv-A saved to Store: {load_persisted_prefs(store, 'u1')}")

    # Conversation B: a BRAND NEW thread with no criteria — hydrate_prefs fills them from the Store.
    out_b = graph.invoke({"user_id": "u1", "accumulated_search_criteria": {}}, _cfg("u1", "conv-B"))
    print(f"  conv-B (empty input) hydrated from Store -> {out_b['final_response']}")

    # A different user sees NOTHING (namespace isolation).
    out_c = graph.invoke({"user_id": "u2", "accumulated_search_criteria": {}}, _cfg("u2", "conv-A"))
    print(f"  user u2 (isolated) -> {out_c['final_response']}")


def demo_time_travel() -> None:
    _rule("3. Time-travel — update_state rewinds a thread and forks an alternate budget")
    graph = build_mini_graph(checkpointer=InMemorySaver())
    cfg = _cfg("u1", "conv-tt")
    first = graph.invoke({"user_id": "u1", "accumulated_search_criteria": {"max_budget": 1200, "property_features": ["Camden"]}}, cfg)
    print(f"\n  original run: {first['final_response']}")

    # Find the ORIGINAL run's checkpoint right before 'plan' executed (search_results still
    # empty), and fork a fresh branch from THERE with alternate criteria — so the forked run
    # is clean, not appended to the first run's results.
    history = list(graph.get_state_history(cfg))
    pre_plan = next(s for s in history if "plan" in (s.next or ()))
    # pre_plan.config carries the full {thread_id, checkpoint_ns, checkpoint_id} — fork from it.
    forked_cfg = fork_from_checkpoint(
        graph, pre_plan.config,
        {"accumulated_search_criteria": {"max_budget": 800, "property_features": ["Peckham", "Lewisham"]}},
    )
    forked = graph.invoke(None, forked_cfg)
    print(f"  forked run (budget->800, areas->Peckham/Lewisham): {forked['final_response']}")
    print(f"  checkpoints recorded on this thread: {len(history)} (time-travel can target any of them)")


def demo_durability() -> None:
    _rule("4. Durability modes — invoke-time durability= knob")
    graph = build_mini_graph(checkpointer=InMemorySaver())
    for mode in ("exit", "async", "sync"):
        out = graph.invoke(
            {"user_id": "u1", "accumulated_search_criteria": {"property_features": ["Bow"]}},
            _cfg("u1", f"conv-dur-{mode}"),
            durability=mode,
        )
        print(f"  durability={mode:5s} -> {out['final_response']}")
    print("\n  exit = persist once at the end (fastest) | async = per-step in background (default) |"
          "\n  sync = block until each step is durably written (safest, e.g. before a paid side effect).")


def main() -> None:
    demo_hitl()
    demo_store()
    demo_time_travel()
    demo_durability()
    print("\n[ok] all four capabilities exercised against an in-memory checkpointer + store.\n")


if __name__ == "__main__":
    main()
