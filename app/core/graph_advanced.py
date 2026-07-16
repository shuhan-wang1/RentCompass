"""Advanced LangGraph capabilities layered onto the rent-agent StateGraph.

Everything here is ADDITIVE and OFF BY DEFAULT — when the corresponding flags are not
enabled the main graph topology and behaviour are byte-for-byte unchanged. The four
capabilities, and the interview concept each one demonstrates:

  1. HITL (human-in-the-loop) — ``confirm_search`` pauses the graph with ``interrupt()``
     right before the expensive multi-search fan-out, so a human can approve / edit / cancel
     the planned scrapes. Resumed with ``Command(resume=...)``. Requires a checkpointer.

  2. Store (cross-thread memory) — ``hydrate_prefs`` / ``persist_prefs`` read & write the
     user's DURABLE structured criteria (budget, destination, must-haves) to a LangGraph
     ``BaseStore``, namespaced by ``user_id``. Unlike the checkpointer (per-thread, keyed by
     ``thread_id``) the Store is SHARED ACROSS a user's conversations — that is the whole
     checkpointer-vs-store distinction, made concrete.

  3. Time-travel — ``fork_from_checkpoint`` wraps ``graph.update_state`` to rewind a thread
     to an earlier checkpoint, patch its state, and resume down an alternate branch.

  4. Durability modes — ``DURABILITY`` documents the invoke-time ``durability=`` knob
     ("exit" | "async" | "sync") that trades checkpoint-write latency against crash safety.

The confirm node and the store helpers are exercised by ``examples/langgraph_advanced_demo.py``
and ``tests/test_langgraph_advanced.py`` against an in-memory checkpointer + store, so this is
real, tested code rather than a throwaway snippet.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Literal, Optional

from langgraph.types import Command, interrupt

# ── Durability: invoke-time knob, not a compile option (LangGraph >= 1.x) ────────────
# graph.ainvoke(state, config, durability=...) — trade write latency vs crash safety:
#   "exit"  — persist only when the graph finishes/interrupts. Fastest; a crash mid-run
#             loses the in-flight turn (fine for cheap, repl-restartable turns).
#   "async" — persist each super-step in the background while the next step runs. Default.
#   "sync"  — block each super-step until its checkpoint is durably written. Safest;
#             use when a replay would double-charge a paid side effect and idempotency
#             alone is not enough.
DURABILITY = {"exit": "exit", "async": "async", "sync": "sync"}


# ── Store namespacing (cross-thread, per-user) ───────────────────────────────────────
# The checkpointer is keyed by thread_id = f"{user_id}:{conversation_id}"; the Store is
# keyed by (namespace, key) where namespace is per-USER, so a fact saved in conversation A
# is visible in conversation B. That is the point of the Store.
def prefs_namespace(user_id: str) -> tuple[str, str]:
    """Per-user Store namespace for durable structured criteria."""
    return ("user_prefs", user_id or "anonymous")


PREFS_KEY = "search_criteria"

# Fail CLOSED on shared sentinel ids (matching memory_tools' no-'default'-fallback rule):
# two different people asserting "default" must never share a durable prefs bucket.
_SENTINEL_USER_IDS = frozenset({"default", "anonymous"})

# Serialises the load->merge->put read-modify-write below. The app's turn_lock is keyed by
# (user_id, conversation_id), so two conversations of the SAME user (two browser tabs) run
# persist_prefs concurrently — without this lock one turn's write silently clobbers the other.
_PREFS_LOCK = threading.Lock()


def _prefs_user_ok(user_id: str) -> bool:
    return bool(user_id) and user_id not in _SENTINEL_USER_IDS

# Only these structured, stable fields are persisted cross-thread. Free-text / per-turn
# channels stay in the checkpointer; the Store holds the "profile", not the transcript.
_PERSISTED_FIELDS = (
    "destination",
    "max_budget",
    "max_travel_time",
    "property_features",
    "soft_preferences",
    "amenities_of_interest",
)


def load_persisted_prefs(store, user_id: str) -> Dict[str, Any]:
    """Read the user's durable criteria from the Store (empty dict if none / no store)."""
    if store is None or not _prefs_user_ok(user_id):
        return {}
    try:
        item = store.get(prefs_namespace(user_id), PREFS_KEY)
    except Exception:
        return {}
    if item is None:
        return {}
    value = getattr(item, "value", item)
    return dict(value) if isinstance(value, dict) else {}


def save_persisted_prefs(store, user_id: str, criteria: Dict[str, Any]) -> None:
    """Write this turn's structured criteria to the Store as the AUTHORITATIVE profile.

    ``criteria`` is expected to be the post-turn accumulated criteria of a graph run whose
    entry node (hydrate_prefs) already merged the stored profile in. That invariant makes
    emptiness meaningful: a field that is empty NOW was either cleared by the user this turn
    (hydrate had re-filled it at the start of the turn) or never existed at all — in both
    cases dropping it from the profile is correct. Lists are likewise replaced, not unioned:
    the union already happened at hydrate time, so a removal this turn actually sticks
    instead of ratcheting back forever.

    The whole read-compare-write runs under _PREFS_LOCK so concurrent turns from the same
    user's other conversations can't interleave and drop each other's fields.
    """
    if store is None or not _prefs_user_ok(user_id) or not criteria:
        return
    with _PREFS_LOCK:
        existing = load_persisted_prefs(store, user_id)
        merged = {}
        for field in _PERSISTED_FIELDS:
            val = criteria.get(field)
            if val not in (None, "", [], {}):
                merged[field] = val
        if merged != existing:
            try:
                store.put(prefs_namespace(user_id), PREFS_KEY, merged)
            except Exception:
                pass


def _merge_into_accumulated(accumulated: Dict[str, Any], persisted: Dict[str, Any]) -> Dict[str, Any]:
    """Fill ONLY the gaps in this turn's accumulated criteria from persisted prefs.

    This-turn / this-thread values always win — the Store is a fallback profile, not an
    override — so a user who says "budget £900 today" is never silently reverted to a saved
    £1200. Lists are unioned (order-preserving); scalars fill only when currently empty.
    """
    if not persisted:
        return accumulated
    merged = dict(accumulated or {})
    for field in _PERSISTED_FIELDS:
        saved = persisted.get(field)
        if saved in (None, "", [], {}):
            continue
        current = merged.get(field)
        if isinstance(saved, list):
            base = list(current or [])
            for item in saved:
                if item not in base:
                    base.append(item)
            merged[field] = base
        elif current in (None, "", [], {}):
            merged[field] = saved
    return merged


# ── HITL: confirm the expensive multi-search fan-out ─────────────────────────────────
def make_confirm_search_node():
    """Node inserted between decide_tool and dispatch_searches when HITL is enabled.

    It pauses with ``interrupt(payload)`` where payload lists the planned sub-searches, and
    blocks until the caller resumes with ``Command(resume=decision)``:

      * truthy / "proceed" / {"action": "proceed"}      -> run the searches as planned
      * {"action": "edit", "searches": [...]}           -> run the EDITED search plan
      * falsey / "cancel" / {"action": "cancel"}        -> skip searching, answer politely

    re-execution gotcha (the interview point): when the graph resumes, the ENTIRE
    confirm_search node re-runs from the top — ``interrupt()`` replays and returns the resume
    value instead of pausing again. So keep everything above the interrupt() call pure /
    idempotent; never do a side effect before it.
    """

    def confirm_search_node(state) -> Command[Literal["dispatch_searches", "format_output"]]:
        decision = state.get("tool_decision") or {}
        searches = (decision.get("params") or {}).get("searches") or []

        # Nothing to fan out — behave exactly like the no-HITL path.
        if not searches:
            return Command(goto="dispatch_searches")

        # PAUSE. On resume this returns the value passed to Command(resume=...).
        decision_in = interrupt(
            {
                "type": "confirm_search",
                "question": "About to run these property searches — proceed?",
                "planned_searches": [
                    {"tool": s.get("tool"), "params": s.get("params", {})} for s in searches
                ],
            }
        )

        action, edited = _parse_resume(decision_in)

        if action == "cancel":
            # Route to format_output, NOT generate_response: the real generate_response node
            # rebuilds final_response from the LLM (with tool_observation=None it would answer
            # the original query with no data), overwriting this message. format_output passes
            # an already-set final_response through untouched.
            return Command(
                update={
                    "final_response": "No problem — I've held off on running those searches. "
                    "Tell me what to change and I'll try again.",
                    "response_type": "answer",
                },
                goto="format_output",
            )

        if action == "edit" and edited:
            new_decision = dict(decision)
            new_params = dict(new_decision.get("params") or {})
            new_params["searches"] = edited
            new_decision["params"] = new_params
            return Command(update={"tool_decision": new_decision}, goto="dispatch_searches")

        return Command(goto="dispatch_searches")

    return confirm_search_node


def _parse_resume(value: Any) -> tuple[str, Optional[list]]:
    """Normalise a Command(resume=...) payload to (action, edited_searches).

    Fail CLOSED: anything unrecognised cancels rather than silently launching the expensive
    fan-out. An edit whose searches are all malformed likewise cancels — running the ORIGINAL
    plan against an explicit (if broken) edit request would defy the user's intent, and the
    malformed entries themselves must never reach search_worker.
    """
    if value in (None, False, "", "cancel", "no", "abort"):
        return "cancel", None
    if value in (True, "proceed", "yes", "ok", "approve"):
        return "proceed", None
    if isinstance(value, dict):
        action = str(value.get("action", "proceed")).lower()
        if action == "cancel":
            return "cancel", None
        if action == "edit":
            raw = value.get("searches") or []
            valid = [s for s in raw if isinstance(s, dict) and s.get("tool")]
            return ("edit", valid) if valid else ("cancel", None)
        if action == "proceed":
            return "proceed", None
        return "cancel", None
    return "cancel", None


def parse_confirmation_reply(text: str) -> Optional[str]:
    """Map a user's free-text reply to a pending confirm_search into a resume action.

    Returns "proceed" / "cancel" for a clear yes/no, or None when the reply is neither —
    the caller should then treat the message as a NEW turn (fresh graph input cleanly
    restarts from START, deliberately abandoning the pending confirmation).
    """
    t = (text or "").strip().lower().rstrip("!.。！")
    if t in {"yes", "y", "ok", "okay", "sure", "proceed", "confirm", "go", "go ahead",
             "do it", "yes please", "是", "好", "好的", "可以", "确认", "继续", "搜吧", "搜"}:
        return "proceed"
    if t in {"no", "n", "stop", "cancel", "abort", "don't", "dont", "no thanks",
             "不", "不要", "取消", "算了", "先别", "停"}:
        return "cancel"
    return None


# ── Store hydrate / persist nodes (added only when a Store is compiled in) ────────────
def make_hydrate_prefs_node():
    """Entry node: fill this turn's accumulated criteria from the user's Store profile."""

    def hydrate_prefs_node(state) -> dict:
        from langgraph.config import get_store  # injected only when compiled with a store

        try:
            store = get_store()
        except Exception:
            store = None
        if store is None:
            return {}
        persisted = load_persisted_prefs(store, state.get("user_id", ""))
        if not persisted:
            return {}
        merged = _merge_into_accumulated(state.get("accumulated_search_criteria") or {}, persisted)
        return {"accumulated_search_criteria": merged}

    return hydrate_prefs_node


def make_persist_prefs_node():
    """Exit node: write this turn's structured criteria back to the user's Store profile."""

    def persist_prefs_node(state) -> dict:
        from langgraph.config import get_store

        try:
            store = get_store()
        except Exception:
            store = None
        if store is not None:
            save_persisted_prefs(
                store, state.get("user_id", ""), state.get("accumulated_search_criteria") or {}
            )
        return {}

    return persist_prefs_node


# ── Time-travel: rewind a thread and fork an alternate branch ────────────────────────
def fork_from_checkpoint(
    graph,
    thread_config: dict,
    patch: dict,
    *,
    checkpoint_id: Optional[str] = None,
    as_node: Optional[str] = None,
) -> dict:
    """Rewind ``thread_config`` and apply ``patch``, returning a config to resume from.

    ``update_state`` writes a NEW checkpoint carrying ``patch``; ``invoke(None, forked_cfg)``
    then continues the graph from that point down an alternate branch:

        forked = fork_from_checkpoint(graph, cfg, {"accumulated_search_criteria": {...}},
                                      as_node="hydrate_prefs")
        result = graph.invoke(None, forked)

    ``as_node`` positions the write as if that node just produced it, so the graph resumes at
    whatever follows the node — the clean way to say "change my budget and re-run from the
    search step" without replaying the whole conversation. ``checkpoint_id`` instead forks
    from one specific historical checkpoint (see ``graph.get_state_history``).
    """
    cfg = dict(thread_config)
    if checkpoint_id is not None:
        configurable = dict(cfg.get("configurable") or {})
        configurable["checkpoint_id"] = checkpoint_id
        # The checkpointer's put() requires checkpoint_ns; a hand-built config usually has
        # only thread_id, and both InMemorySaver and SqliteSaver KeyError without this.
        configurable.setdefault("checkpoint_ns", "")
        cfg["configurable"] = configurable
    kwargs = {"as_node": as_node} if as_node is not None else {}
    return graph.update_state(cfg, patch, **kwargs)
