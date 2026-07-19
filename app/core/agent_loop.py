"""Native function-calling agent loop (design §2.3 / Phase 1, AGENT_ARCH=fc_loop).

Replaces the classify-then-execute routing layer with a bounded LangGraph tool loop:

    START -> extract_preferences -> guard -> agent <-> execute_tools
                                     guard -> format_output_fc            (refuse / greet)
                                     agent -> critic -> format_output_fc  (final text)
                                     agent -> format_output_fc            (ask_user)

`agent` makes EXACTLY ONE bound-tools LLM call per super-step; `execute_tools` runs the
trailing tool_calls batch (asyncio.gather + per-tool timeout + idempotency + taint/HITL
gate) and writes ToolMessages back to state.messages. Both are real graph nodes so the
whole loop state lives in the checkpointed AgentState.messages/tool_artifacts channels —
that is what makes HITL interrupt() a true zero-replay resume (design §2.3).

This module imports langgraph_agent helpers at MODULE level; langgraph_agent must therefore
import THIS module only lazily/function-locally (build_agent_graph) to avoid a cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from uk_rent_agent.agent.state import AgentState
from uk_rent_agent.agent.contracts import ToolInvocation

# Loop mechanics + user-facing helpers reused verbatim from the legacy engine. A top-level
# import here is intentional and safe (langgraph_agent imports agent_loop only lazily).
from core.langgraph_agent import (
    MAX_AGENT_TURNS,
    TOOL_TIMEOUTS,
    TOOL_TIMEOUT_DEFAULT,
    SECURITY_DIRECTIVE,
    _params_digest,
    _fair_housing_violation,
    _reply_language_from_ctx,
    _current_message,
    _language_directive,
    _sanitize_final_response,
    clean_response,
    apply_preference_filter,
    _format_safety,
    _format_pois,
    _format_commute_cost,
    _FAIR_HOUSING_REFUSAL_EN,
    _FAIR_HOUSING_REFUSAL_ZH,
)
from uk_rent_agent.agent.guardrails import sanitize_untrusted

logger = logging.getLogger(__name__)

# Untrusted-source tools whose returned data may carry injected instructions: their
# model-facing ToolMessage is sanitized + tainting (design §2.3 dual-channel). Mirrors the
# legacy taint set (execute_tool :2717) plus get_property_details' external description page.
_UNTRUSTED_TOOLS = frozenset({
    "web_search", "search_properties", "reasoning_property", "multi_search",
    "get_property_details",
})

# Per-tool length cap for the model-facing derived view (chars). Raw ToolResult.data is
# ALWAYS preserved untouched in tool_artifacts; only the model channel is capped.
_TOOLMSG_CAPS = {
    "web_search": 8000,
    "search_properties": 6000,
    "get_property_details": 4000,
}
_TOOLMSG_CAP_DEFAULT = 4000

# Kinds whose latest artifact drives a structured card in format_output_fc.
_CARD_FORMATTERS = {
    "check_safety": _format_safety,
    "search_nearby_pois": _format_pois,
    "calculate_commute_cost": _format_commute_cost,
}


# ─── ToolSpec (contract D fallback) ─────────────────────────────────
# Agent T owns the canonical ToolSpec + tool_provider.list_specs(). We prefer the shared
# definition when present and fall back to this identically-shaped one so the loop (and its
# tests / wiring) work before that lands. Only attribute access is used anywhere below, so a
# fake spec object in tests satisfies the same duck-typed contract.
try:  # pragma: no cover - import shape depends on Agent T merge order
    from core.tool_system import ToolSpec  # type: ignore
except Exception:  # pragma: no cover
    @dataclass(frozen=True)
    class ToolSpec:  # type: ignore[no-redef]
        name: str
        description: str
        input_schema: dict
        side_effect: str = "none"
        retry_safe: bool = True
        version: str = "1"
        terminal: bool = False


class _RegistryToolProvider:
    """Adapter exposing list_specs()/execute_tool()/get() over a ToolRegistry that does not
    yet ship Agent T's list_specs(). Single source of truth is the registry's Tool objects."""

    def __init__(self, registry):
        self._registry = registry

    def list_specs(self):
        specs = []
        for tool in getattr(self._registry, "tools", {}).values():
            specs.append(ToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=getattr(tool, "parameters", {}) or {"type": "object", "properties": {}},
                side_effect=getattr(tool, "side_effect", "none"),
                retry_safe=getattr(tool, "retry_safe", True),
                version=getattr(tool, "version", "1"),
                terminal=bool(getattr(tool, "terminal", False)),
            ))
        return specs

    async def execute_tool(self, name, **params):
        return await self._registry.execute_tool(name, **params)

    def get(self, name):
        return self._registry.get(name)


def _as_provider(tool_provider):
    """Accept either a real tool_provider (has list_specs) or a bare ToolRegistry."""
    if hasattr(tool_provider, "list_specs"):
        return tool_provider
    return _RegistryToolProvider(tool_provider)


# ─── memory gate (contract B, imported defensively) ─────────────────
def _load_memory_gate():
    """Return Agent G's memory_gate module, or None when it is not present yet. Indirected so
    tests can monkeypatch agent_loop._load_memory_gate to inject a stub."""
    try:
        from core import memory_gate  # type: ignore
        return memory_gate
    except Exception:
        return None


# ─── message assembly (contract C, imported defensively) ────────────
def _behaviour_directive(reply_language: str) -> str:
    return (
        SECURITY_DIRECTIVE + "\n\n" + _language_directive(reply_language) + "\n\n"
        "=== BEHAVIOUR ===\n"
        "You are Alex, a UK student-housing assistant. Decide ONE thing per step: call a "
        "tool (independent asks may be batched in a single step; dependent asks run in later "
        "steps once you see the results), answer directly, or call ask_user to ask a single "
        "clarifying question. Fill tool parameters from the context block; never invent "
        "listings, addresses, or prices. If a tool reports missing info, correct the call or "
        "ask_user — do not loop the same call. Do not use emoji.\n"
        "=== END BEHAVIOUR ==="
    )


def _build_messages(state: AgentState) -> list:
    """First-entry message construction. Prefers Agent C's assemble_messages (contract C);
    falls back to a minimal system+context+user triple so the loop runs before that lands."""
    ec = state.get("extracted_context") or {}
    reply_language = _reply_language_from_ctx(
        ec, ec.get("current_message") or _current_message(state.get("user_query") or ""))
    user_message = ec.get("current_message") or _current_message(state.get("user_query") or "")

    context_block = {
        "accumulated_criteria": state.get("accumulated_search_criteria") or {},
        "focused_property": ec.get("focused_property") or (
            {"property_address": ec.get("property_address")} if ec.get("property_address") else None),
        "last_results": ec.get("last_results") or [],
        "recommendations_index": ec.get("recommendations_index") or [],
    }
    try:
        from core.context_assembler import assemble_messages  # contract C
        return assemble_messages(
            user_message=user_message,
            history=ec.get("history") or [],
            memory_block=state.get("memory_context") or "",
            context_block=context_block,
            reply_language=reply_language,
            token_budget=6000,
        )
    except Exception:
        # Minimal, self-sufficient fallback: security/behaviour system row + a context row +
        # the raw current message. Grounds the model even without the assembler.
        msgs = [SystemMessage(content=_behaviour_directive(reply_language))]
        ctx_lines = []
        acc = context_block["accumulated_criteria"]
        if acc:
            ctx_lines.append("Accumulated criteria: "
                             + json.dumps(acc, ensure_ascii=False, default=str))
        if context_block["focused_property"]:
            ctx_lines.append("Focused property: "
                             + json.dumps(context_block["focused_property"], ensure_ascii=False, default=str))
        if context_block["last_results"]:
            ctx_lines.append(f"Last results: {len(context_block['last_results'])} listings in context.")
        if state.get("memory_context"):
            ctx_lines.append("What I remember about this user:\n" + str(state.get("memory_context")))
        if ctx_lines:
            msgs.append(SystemMessage(content="\n".join(ctx_lines)))
        msgs.append(HumanMessage(content=user_message))
        return msgs


def _specs_to_openai(specs) -> list:
    """ToolSpec list -> OpenAI-FC tool dicts for ChatModel.bind_tools (design §2.3)."""
    tools = []
    for s in specs:
        tools.append({
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.input_schema or {"type": "object", "properties": {}},
            },
        })
    return tools


def _default_agent_llm():
    from uk_rent_agent.llm.router import ModelRouter
    # Loop driver: v4-flash, thinking DISABLED (responder+low_latency => reasoning=False), so
    # no reasoning_content must be echoed on later tool rounds (design §2.9).
    return ModelRouter().create("responder", low_latency=True)


def _artifact(turn: int, tool: str, raw_data: Any, params_digest: str = "") -> dict:
    return {"turn": turn, "tool": tool, "raw_data": raw_data, "params_digest": params_digest}


# ═══════════════════════════════════════════════════════════════════
# NODE FACTORY
# ═══════════════════════════════════════════════════════════════════

def build_fc_nodes(tool_provider, *, enable_hitl=False, checkpointer=None, agent_llm=None):
    """Produce the fc_loop graph nodes.

    Args:
        tool_provider: object exposing list_specs()/execute_tool()/get(), or a bare
            ToolRegistry (auto-wrapped).
        enable_hitl: gate a search_properties batch behind interrupt() (needs a checkpointer).
        checkpointer: required for HITL to persist the interrupted state.
        agent_llm: injectable base chat model (tests). Defaults to ModelRouter responder.

    Returns dict of {guard, agent, execute_tools, format_output_fc} node callables.
    """
    provider = _as_provider(tool_provider)
    hitl_on = bool(enable_hitl and checkpointer is not None)
    _llm_holder = {"llm": agent_llm}

    def _llm():
        if _llm_holder["llm"] is None:
            _llm_holder["llm"] = _default_agent_llm()
        return _llm_holder["llm"]

    # ── guard ──────────────────────────────────────────────────────
    def guard_node(state: AgentState) -> Command[Literal["agent", "format_output_fc"]]:
        ec = state.get("extracted_context") or {}
        cm = ec.get("current_message") or _current_message(state.get("user_query") or "")
        lang = _reply_language_from_ctx(ec, cm)
        # 1) Fair-housing refusal (Equality Act 2010) — deterministic, short-circuits.
        if _fair_housing_violation(cm):
            refusal = _FAIR_HOUSING_REFUSAL_ZH if lang == "zh" else _FAIR_HOUSING_REFUSAL_EN
            return Command(update={
                "final_response": refusal, "response_type": "clarification",
            }, goto="format_output_fc")
        # 2) Greeting fast path — skip the bound-tools call for a bare hello/thanks.
        greetings = ["hi", "hello", "你好", "您好", "hey", "thanks", "谢谢"]
        ql = (state.get("user_query") or "").lower()
        if any(g == ql.strip() for g in greetings) or (
                len(state.get("user_query") or "") < 10 and any(g in ql for g in greetings)):
            if lang == "zh":
                msg = "你好！我是 Alex，帮你在英国找学生房。告诉我你的预算、想住的区域或通勤目的地就可以开始。"
            else:
                msg = ("Hi! I'm Alex, your UK student-housing assistant. Tell me your budget, "
                       "the area you'd like to live in, or where you commute to and we'll start.")
            return Command(update={"final_response": msg, "response_type": "answer"},
                           goto="format_output_fc")
        return Command(goto="agent")

    # ── agent ──────────────────────────────────────────────────────
    async def _resolve_pending_memory(state: AgentState):
        """A+ rule-4 consumer (design §2.8c): after a deny froze a candidate, the NEXT
        user turn decides its fate — 'yes'/explicit re-authorization replays the FROZEN
        content verbatim (never the model's args), 'no' discards it; both consume the
        ledger entry exactly once. An unrelated message leaves it frozen. Returns a
        system note for the model, or None."""
        gate = _load_memory_gate()
        if gate is None or not hasattr(gate, "latest_pending_digest"):
            return None
        session_id = state.get("session_id", "default")
        try:
            digest = gate.latest_pending_digest(session_id)
        except Exception:
            return None
        if not digest:
            return None
        ec = state.get("extracted_context") or {}
        cur = ec.get("current_message") or _current_message(state.get("user_query") or "")
        intent = gate.confirmation_intent(cur) if hasattr(gate, "confirmation_intent") else "none"
        if intent == "none" and not gate.user_authorizes_memory(cur):
            return None
        frozen = gate.consume_pending_write(session_id, digest)
        if not frozen:
            return None
        if intent == "no":
            return ("[memory] The user declined saving the pending memory candidate; it "
                    "was discarded. Acknowledge briefly and continue with their request.")
        kind = frozen.get("kind")
        if kind not in ("semantic", "episodic", "reflection"):
            kind = "semantic"
        try:
            result = await asyncio.wait_for(
                provider.execute_tool(
                    "remember",
                    content=frozen.get("content") or "",
                    kind=kind,
                    user_id=state.get("user_id") or "",
                    session_id=session_id,
                    idempotency_key=f"memgate:{session_id}:{digest}",
                ),
                TOOL_TIMEOUTS.get("remember", TOOL_TIMEOUT_DEFAULT))
            saved = bool(getattr(result, "success", False))
        except Exception:
            saved = False
        if saved:
            return ("[memory] The user confirmed; the frozen candidate was saved verbatim: "
                    + json.dumps(frozen.get("content") or "", ensure_ascii=False)
                    + ". Tell the user it has been saved.")
        return ("[memory] The user confirmed, but saving the frozen candidate failed; "
                "apologize briefly and offer to retry.")

    async def agent_node(state: AgentState) -> Command[Literal["execute_tools", "critic", "format_output_fc"]]:
        messages = list(state.get("messages") or [])
        if not messages:
            replay_note = await _resolve_pending_memory(state)
            messages = _build_messages(state)
            if replay_note:
                messages.append(SystemMessage(content=replay_note))

        loop_turn = int(state.get("loop_turn", 0)) + 1
        degraded = loop_turn > MAX_AGENT_TURNS

        specs = list(provider.list_specs())
        if degraded:
            # Loop cap: one last no-tools call to answer from the observations gathered.
            llm = _llm()
            prompt_msgs = messages + [SystemMessage(content=(
                "You have reached the tool-call limit. Answer the user now using ONLY the tool "
                "results already gathered above. Do not request more tools."))]
            resp = await llm.ainvoke(prompt_msgs)
            text = clean_response(resp.content if hasattr(resp, "content") else str(resp))
            return Command(update={
                "messages": messages + [resp], "loop_turn": loop_turn,
                "final_response": text,
            }, goto="critic")

        llm = _llm().bind_tools(_specs_to_openai(specs))
        resp = await llm.ainvoke(messages)
        tool_calls = list(getattr(resp, "tool_calls", None) or [])

        if tool_calls:
            terminal_names = {s.name for s in specs if getattr(s, "terminal", False)}
            terminal_names.add("ask_user")  # contract A: ask_user is always terminal
            ask = next((tc for tc in tool_calls if tc.get("name") in terminal_names), None)
            if ask is not None:
                # ask_user (contract A): terminal. Record its model-provided fields as an
                # artifact; format_output_fc derives known_criteria deterministically.
                args = ask.get("args") or {}
                payload = {
                    "status": "ask_user",
                    "question": args.get("question", ""),
                    "clarification_kind": args.get("clarification_kind", "other"),
                    "missing_fields": args.get("missing_fields", []) or [],
                    "missing_optional_fields": args.get("missing_optional_fields", []) or [],
                }
                artifacts = list(state.get("tool_artifacts") or [])
                artifacts.append(_artifact(loop_turn - 1, "ask_user", payload))
                return Command(update={
                    "messages": messages + [resp], "loop_turn": loop_turn,
                    "tool_artifacts": artifacts,
                }, goto="format_output_fc")
            # Normal tool batch: append the assistant message; execute_tools reads it back.
            return Command(update={
                "messages": messages + [resp], "loop_turn": loop_turn,
            }, goto="execute_tools")

        # Plain text -> final answer through the legacy critic.
        text = clean_response(resp.content if hasattr(resp, "content") else str(resp))
        return Command(update={
            "messages": messages + [resp], "loop_turn": loop_turn, "final_response": text,
        }, goto="critic")

    # ── execute_tools ──────────────────────────────────────────────
    def _spec_map():
        return {s.name: s for s in provider.list_specs()}

    def _inject_search_params(params: dict, state: AgentState) -> dict:
        """Executor re-injection for search_properties (mirror langgraph_agent :2600-2660):
        criteria_gate_shown / reply_language / accumulated criteria are set by the harness,
        never the model. A city switch stated THIS turn still wins."""
        p = dict(params or {})
        ec = state.get("extracted_context") or {}
        acc = state.get("accumulated_search_criteria") or {}
        if not p.get("current_message"):
            p["current_message"] = ec.get("current_message", "")
        try:
            from core.tools.search_properties import _extract_area
            switched = _extract_area(p.get("current_message") or "")
        except Exception:
            switched = None
        if switched:
            p["area"] = switched
        if not p.get("area") and acc.get("area"):
            p["area"] = acc["area"]
        if not switched and not p.get("areas") and acc.get("areas"):
            p["areas"] = acc["areas"]
        cd = acc.get("commute_destination") or acc.get("destination")
        if not p.get("commute_destination") and cd and not acc.get("no_commute"):
            p["commute_destination"] = cd
        if not p.get("max_budget") and acc.get("max_budget"):
            p["max_budget"] = acc["max_budget"]
        if not p.get("max_commute_time") and acc.get("max_travel_time") and not acc.get("no_commute"):
            p["max_commute_time"] = acc["max_travel_time"]
        if acc.get("no_commute"):
            p["no_commute"] = True
        if acc.get("bedrooms") is not None and not p.get("bedrooms"):
            p["bedrooms"] = acc["bedrooms"]
        if acc.get("room_type") and not p.get("room_type"):
            p["room_type"] = acc["room_type"]
        if acc.get("move_in_date") and not p.get("move_in_date"):
            p["move_in_date"] = acc["move_in_date"]
        if acc.get("criteria_gate_shown"):
            p["criteria_gate_shown"] = True
        if not p.get("area") and not p.get("commute_destination") and acc.get("destination"):
            p["location"] = acc["destination"]
        if acc.get("property_features"):
            p["property_features"] = acc["property_features"]
        if acc.get("soft_preferences"):
            p["accumulated_preferences"] = acc["soft_preferences"]
        rl = ec.get("reply_language")
        if rl and not p.get("reply_language"):
            p["reply_language"] = rl
        return p

    def _derived_toolmsg(tool: str, result) -> tuple[str, bool]:
        """Dual-channel model-facing view (design §2.3): {"success","data","error"} JSON,
        untrusted data sanitized, length-capped. Returns (content, tainted)."""
        raw = getattr(result, "data", None)
        tainted = False
        if tool in _UNTRUSTED_TOOLS and raw is not None:
            data_view = sanitize_untrusted(
                json.dumps(raw, ensure_ascii=False, default=str)).text
            tainted = True
        else:
            data_view = raw
        content = json.dumps(
            {"success": getattr(result, "success", False), "data": data_view,
             "error": getattr(result, "error", None)},
            ensure_ascii=False, default=str)
        cap = _TOOLMSG_CAPS.get(tool, _TOOLMSG_CAP_DEFAULT)
        if len(content) > cap:
            logger.info("fc_loop.toolmsg_truncated tool=%s len=%d cap=%d", tool, len(content), cap)
            content = content[:cap] + "\n...[truncated]"
        return content, tainted

    async def execute_tools_node(state: AgentState) -> Command[Literal["agent"]]:
        messages = list(state.get("messages") or [])
        ai = messages[-1] if messages else None
        batch = list(getattr(ai, "tool_calls", None) or [])
        specs = _spec_map()

        # HITL: whole search_properties batch gated BEFORE any execution. On resume the node
        # reruns from the top; nothing executed pre-interrupt, so zero replay (design §2.3).
        if hitl_on and any(tc.get("name") == "search_properties" for tc in batch):
            interrupt({
                "action": "confirm_search",
                "tools": [tc.get("name") for tc in batch],
            })

        artifacts = list(state.get("tool_artifacts") or [])
        # No-progress guard: (tool, digest) already run this turn (any earlier batch OR earlier
        # in THIS batch) is not re-run; a "already ran" ToolMessage is injected instead.
        seen = {(a.get("tool"), a.get("params_digest")) for a in artifacts if a.get("params_digest")}
        turn = int(state.get("loop_turn", 0))
        mem_gate = _load_memory_gate()
        ec = state.get("extracted_context") or {}
        session_id = state.get("session_id", "default")
        cur_msg = ec.get("current_message") or _current_message(state.get("user_query") or "")

        plan = []  # (tool_call, digest, mode, params) ; mode in {run, skip_dup, deny}
        for tc in batch:
            name = tc.get("name")
            args = dict(tc.get("args") or {})
            if name == "search_properties":
                args = _inject_search_params(args, state)
            elif name in ("recall_memory", "remember"):
                # PRIVACY (mirror legacy execute_tool_node): namespace from state, and
                # fail closed on a missing user_id rather than falling into the shared
                # 'default' memory bucket.
                args["user_id"] = state.get("user_id") or ""
                args["session_id"] = state.get("session_id", "default")
            digest = _params_digest(name, args)
            if (name, digest) in seen:
                plan.append((tc, digest, "skip_dup", args))
                continue
            seen.add((name, digest))
            spec = specs.get(name)
            side_effect = getattr(spec, "side_effect", "none") if spec else "none"
            if side_effect == "write":
                if mem_gate is not None:
                    user_authorized = bool(mem_gate.user_authorizes_memory(cur_msg))
                    allowed = bool(mem_gate.memory_write_allowed(
                        context_tainted=state.get("context_tainted", False),
                        user_authorized=user_authorized))
                    if not allowed:
                        content = str(args.get("content") or args.get("fact") or json.dumps(
                            args, ensure_ascii=False, default=str))
                        kind = str(args.get("kind") or name)
                        try:
                            frozen = mem_gate.freeze_pending_write(session_id, content, kind)
                        except Exception:
                            frozen = ""
                        plan.append((tc, digest, ("deny", frozen), args))
                        continue
                else:
                    # No gate module yet: fall back to the legacy taint rule (deny writes in a
                    # tainted turn) so the safety property holds before Agent G lands.
                    from uk_rent_agent.agent.guardrails import tool_allowed
                    if not tool_allowed(side_effect="write",
                                        context_tainted=state.get("context_tainted", False),
                                        tool_name=name):
                        plan.append((tc, digest, ("deny", ""), args))
                        continue
            plan.append((tc, digest, "run", args))

        async def _run(name, args, digest):
            tool = provider.get(name) if hasattr(provider, "get") else None
            version = getattr(tool, "version", "1") if tool else "1"
            inv = ToolInvocation.create(run_id=state.get("run_id", "fc"), node_id="execute_tools",
                                        tool=name, params=args, version=version)
            call_args = dict(args)
            call_args["idempotency_key"] = inv.idempotency_key
            timeout = TOOL_TIMEOUTS.get(name, TOOL_TIMEOUT_DEFAULT)
            try:
                return await asyncio.wait_for(provider.execute_tool(name, **call_args), timeout)
            except asyncio.TimeoutError:
                from core.tool_system import ToolResult
                return ToolResult(False, error=f"{name} timed out after {timeout}s", tool_name=name)
            except Exception as e:  # degrade-don't-crash: one failed tool never kills the batch
                from core.tool_system import ToolResult
                return ToolResult(False, error=str(e), tool_name=name)

        run_idx = [i for i, (_tc, _d, mode, _a) in enumerate(plan) if mode == "run"]
        results = await asyncio.gather(*[
            _run(plan[i][0].get("name"), plan[i][3], plan[i][1]) for i in run_idx
        ]) if run_idx else []
        result_by_idx = dict(zip(run_idx, results))

        tainted_any = False
        for i, (tc, digest, mode, args) in enumerate(plan):
            name = tc.get("name")
            tcid = tc.get("id") or tc.get("tool_call_id") or f"call_{i}"
            if mode == "skip_dup":
                messages.append(ToolMessage(
                    content=json.dumps({"success": False, "data": None,
                                        "error": "already ran; see the earlier result above"},
                                       ensure_ascii=False),
                    tool_call_id=tcid, name=name))
                continue
            if isinstance(mode, tuple) and mode[0] == "deny":
                frozen = mode[1]
                hint = (" A confirmation is required before saving; the exact content has been "
                        f"frozen (digest {frozen}) and will be saved only on explicit user "
                        "confirmation." if frozen else "")
                messages.append(ToolMessage(
                    content=json.dumps({
                        "success": False, "data": None,
                        "error": ("write blocked: this turn contains untrusted content and the "
                                  "user has not authorized saving to memory." + hint),
                    }, ensure_ascii=False),
                    tool_call_id=tcid, name=name))
                continue
            result = result_by_idx[i]
            artifacts.append(_artifact(turn, name, getattr(result, "data", None), digest))
            content, tainted = _derived_toolmsg(name, result)
            tainted_any = tainted_any or tainted
            messages.append(ToolMessage(content=content, tool_call_id=tcid, name=name))

        update = {
            "messages": messages,
            "tool_artifacts": artifacts,
            "context_tainted": state.get("context_tainted", False) or tainted_any,
        }
        return Command(update=update, goto="agent")

    # ── format_output_fc ───────────────────────────────────────────
    def format_output_fc_node(state: AgentState) -> dict:
        artifacts = list(state.get("tool_artifacts") or [])
        prefs = state.get("user_preferences") or {}
        acc = state.get("accumulated_search_criteria") or {}
        final_response = state.get("final_response", "") or ""
        response_type = state.get("response_type", "answer") or "answer"
        tool_data: dict = {}

        def _last(tool_name):
            for a in reversed(artifacts):
                if a.get("tool") == tool_name:
                    return a
            return None

        # ask_user (contract A / §2.5a): clarification payload + deterministic known_criteria.
        ask = _last("ask_user")
        if ask is not None:
            data = ask.get("raw_data") or {}
            tool_data = {
                "missing_fields": data.get("missing_fields", []),
                "missing_optional_fields": data.get("missing_optional_fields", []),
                "clarification_kind": data.get("clarification_kind", "other"),
                "known_criteria": _derive_known_criteria(acc),
            }
            response = _sanitize_final_response(data.get("question", "") or final_response)
            return {"final_response": response, "response_type": "clarification",
                    "tool_data": tool_data}

        # search_properties: last successful "found" artifact drives the search card.
        search_found = None
        search_clarify = None
        for a in reversed(artifacts):
            if a.get("tool") != "search_properties":
                continue
            raw = a.get("raw_data")
            if not isinstance(raw, dict):
                continue
            if raw.get("status") == "found" and raw.get("recommendations") and search_found is None:
                search_found = raw
            if raw.get("status") == "need_clarification" and search_clarify is None:
                search_clarify = raw
            if search_found is not None:
                break

        if search_found is not None:
            recs = apply_preference_filter(search_found["recommendations"], prefs)
            tool_data = {
                "recommendations": recs,
                "search_criteria": search_found.get("search_criteria", {}),
                "area_recommendations": search_found.get("area_recommendations", []),
            }
            response = final_response or search_found.get("summary") or f"I found {len(recs)} properties."
            response_type = "search"
            # Structured cards (safety/POI/commute) also present this turn ride along in tool_data.
            _merge_cards(artifacts, tool_data)
            return {"final_response": _sanitize_final_response(response),
                    "response_type": response_type, "tool_data": tool_data}

        # A dangling search clarification with no final answer -> surface it.
        if search_clarify is not None and not final_response:
            tool_data = {
                "missing_fields": search_clarify.get("missing_fields", []),
                "known_criteria": search_clarify.get("known_criteria") or _derive_known_criteria(acc),
                "clarification_kind": search_clarify.get("clarification_kind", "missing_area"),
            }
            if search_clarify.get("missing_optional_fields") is not None:
                tool_data["missing_optional_fields"] = search_clarify.get("missing_optional_fields")
            response = _sanitize_final_response(search_clarify.get("question", ""))
            return {"final_response": response, "response_type": "clarification",
                    "tool_data": tool_data}

        # Structured cards (safety/POI/commute): latest of each kind, all downshipped (§2.8b).
        card_response = _merge_cards(artifacts, tool_data)
        if tool_data and not final_response:
            response = card_response or final_response
            return {"final_response": _sanitize_final_response(response),
                    "response_type": "answer", "tool_data": tool_data}

        # Plain answer.
        response = _sanitize_final_response(final_response)
        return {"final_response": response, "response_type": response_type, "tool_data": tool_data}

    return {
        "guard": guard_node,
        "agent": agent_node,
        "execute_tools": execute_tools_node,
        "format_output_fc": format_output_fc_node,
    }


def _merge_cards(artifacts: list, tool_data: dict) -> str:
    """Merge the latest safety/POI/commute card of each kind into tool_data (keys don't
    collide). Returns the formatted text of the single-card case for use as the response."""
    last_text = ""
    for tool_name, formatter in _CARD_FORMATTERS.items():
        latest = None
        for a in reversed(artifacts):
            if a.get("tool") == tool_name and isinstance(a.get("raw_data"), dict):
                latest = a["raw_data"]
                break
        if latest is None:
            continue
        if tool_name == "check_safety" and latest.get("safety_score") is None:
            continue
        if tool_name == "search_nearby_pois" and not latest.get("pois"):
            continue
        if tool_name == "calculate_commute_cost" and not latest.get("success"):
            continue
        text, td = formatter(latest)
        tool_data.update(td)
        last_text = text
    return last_text


def _derive_known_criteria(acc: dict) -> dict:
    """Deterministic known_criteria from accumulated criteria — mirrors search_properties'
    _known_criteria() shape so the frontend form highlight stays identical (§2.5a). The model
    never supplies this; the harness derives it."""
    acc = acc or {}
    area = acc.get("area")
    areas = acc.get("areas") or ([area] if area else [])
    return {
        "area": area,
        "areas": list(areas),
        "commute_destination": acc.get("commute_destination") or acc.get("destination"),
        "max_budget": acc.get("max_budget"),
        "max_travel_time": acc.get("max_travel_time"),
        "no_commute": acc.get("no_commute"),
        "bedrooms": acc.get("bedrooms"),
        "budget_period": acc.get("budget_period"),
        "room_type": acc.get("room_type"),
        "move_in_date": acc.get("move_in_date"),
        "property_features": acc.get("property_features") or [],
        "soft_preferences": acc.get("soft_preferences") or [],
    }


# ═══════════════════════════════════════════════════════════════════
# GRAPH WIRING (consumed lazily by langgraph_agent.build_agent_graph)
# ═══════════════════════════════════════════════════════════════════

def build_fc_graph(tool_registry, *, extract_preferences_node, critic_node,
                   checkpointer=None, store=None, enable_hitl=False,
                   hydrate_prefs_node=None, persist_prefs_node=None, instrument=None,
                   agent_llm=None):
    """Assemble the fc_loop StateGraph, reusing the legacy extract_preferences + critic nodes.

    langgraph_agent.build_agent_graph passes the already-constructed legacy nodes so this
    module needs no back-import of them. `instrument` is the legacy _n(name, fn) eval wrapper
    (identity when None).
    """
    nodes = build_fc_nodes(tool_registry, enable_hitl=enable_hitl, checkpointer=checkpointer,
                           agent_llm=agent_llm)
    ident = instrument or (lambda name, fn: fn)
    use_store = store is not None

    graph = StateGraph(AgentState)
    graph.add_node("extract_preferences", ident("extract_preferences", extract_preferences_node))
    graph.add_node("guard", ident("guard", nodes["guard"]))
    graph.add_node("agent", ident("agent", nodes["agent"]))
    graph.add_node("execute_tools", ident("execute_tools", nodes["execute_tools"]))
    graph.add_node("critic", ident("critic", critic_node))
    graph.add_node("format_output_fc", ident("format_output_fc", nodes["format_output_fc"]))
    if use_store and hydrate_prefs_node is not None:
        graph.add_node("hydrate_prefs", ident("hydrate_prefs", hydrate_prefs_node))
    if use_store and persist_prefs_node is not None:
        graph.add_node("persist_prefs", ident("persist_prefs", persist_prefs_node))

    if use_store and hydrate_prefs_node is not None:
        graph.add_edge(START, "hydrate_prefs")
        graph.add_edge("hydrate_prefs", "extract_preferences")
    else:
        graph.add_edge(START, "extract_preferences")
    graph.add_edge("extract_preferences", "guard")
    # guard/agent/execute_tools route via Command(goto=...); only critic needs a static edge.
    graph.add_edge("critic", "format_output_fc")
    if use_store and persist_prefs_node is not None:
        graph.add_edge("format_output_fc", "persist_prefs")
        graph.add_edge("persist_prefs", END)
    else:
        graph.add_edge("format_output_fc", END)

    compile_options = {}
    if checkpointer is not None:
        compile_options["checkpointer"] = checkpointer
    if store is not None:
        compile_options["store"] = store
    return graph.compile(**compile_options)
