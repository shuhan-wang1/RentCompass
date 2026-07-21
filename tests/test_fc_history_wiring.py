"""Phase 2.1 P1 — fc_loop production context wiring.

Covers the confirmed production gaps that only bite under AGENT_ARCH=fc_loop:

  1. ``extracted_context["history"]`` reaches the assembled message array as
     alternating Human/AI turns (the fc loop consumes it; legacy ignores it).
  2. ``discussed_areas`` — curated area names from recent turns + last_results are
     surfaced in the fc context block so 「那个区域」 resolves to a concrete area (H6).
  3. The ``_artifact`` contract carries ``success`` / ``error`` (agreed with P2).
  4. app.py wires ``extracted_context["history"] = history_snapshot`` unconditionally.

No Flask, no live API: the loop's ``_build_messages`` is driven directly with the real
``assemble_messages`` / ``loop_prompts``; the app.py wiring is guarded at source level.
"""
from __future__ import annotations

import asyncio
import os
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import core.agent_loop as agent_loop
from core.agent_loop import _artifact, build_fc_nodes
from core import loop_prompts
from core.context_assembler import assemble_messages
from core.loop_prompts import (
    DISCUSSED_AREAS_MARKER,
    extract_discussed_areas,
    render_discussed_areas,
)


# ── minimal fakes (mirror test_fc_loop) ─────────────────────────────
class _FakeResult:
    def __init__(self, success=True, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


class _FakeTool:
    def __init__(self, version="1", side_effect="none"):
        self.version = version
        self.side_effect = side_effect


class _FakeSpec:
    def __init__(self, name, side_effect="none", terminal=False):
        self.name = name
        self.description = "desc"
        self.input_schema = {"type": "object", "properties": {}}
        self.side_effect = side_effect
        self.retry_safe = True
        self.version = "1"
        self.terminal = terminal


class _FakeProvider:
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
        return r if r is not None else _FakeResult(True, {"ok": True})


class _FakeChat:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.bound = None

    def bind_tools(self, tools):
        self.bound = tools
        return self

    async def ainvoke(self, messages):
        return self._scripted.pop(0)


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _run(coro):
    # asyncio.run (matches test_fc_loop): get_event_loop().run_until_complete breaks under
    # pytest's loop lifecycle on 3.12.
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# 1. history reaches the assembled message array
# ═══════════════════════════════════════════════════════════════════

def test_history_from_extracted_context_becomes_alternating_turns():
    history = [
        {"user": "I'm looking near UCL", "assistant": "Bloomsbury and Camden are good options."},
        {"user": "how about the budget", "assistant": "What is your maximum monthly budget?"},
    ]
    state = {
        "user_query": "about 1200 pcm",
        "extracted_context": {
            "current_message": "about 1200 pcm",
            "reply_language": "en",
            "history": history,
        },
        "accumulated_search_criteria": {},
        "memory_context": "",
    }
    msgs = agent_loop._build_messages(state)

    # Real assemble_messages path (not the fallback): message #1 is the system directive.
    assert isinstance(msgs[0], SystemMessage)
    humans = [m.content for m in msgs if isinstance(m, HumanMessage)]
    ais = [m.content for m in msgs if isinstance(m, AIMessage)]
    assert "I'm looking near UCL" in humans
    assert "Bloomsbury and Camden are good options." in ais
    assert "What is your maximum monthly budget?" in ais
    # The current message is the LAST human turn, verbatim.
    assert isinstance(msgs[-1], HumanMessage) and msgs[-1].content == "about 1200 pcm"


def test_no_history_key_means_no_prior_turns():
    """Guards the production gap: without extracted_context['history'] the model gets
    no conversation history (only the system directive + current message)."""
    state = {
        "user_query": "is that area safe",
        "extracted_context": {"current_message": "is that area safe", "reply_language": "en"},
        "accumulated_search_criteria": {},
    }
    msgs = agent_loop._build_messages(state)
    ais = [m for m in msgs if isinstance(m, AIMessage)]
    assert ais == []
    assert isinstance(msgs[-1], HumanMessage) and msgs[-1].content == "is that area safe"


# ═══════════════════════════════════════════════════════════════════
# 2. discussed_areas extraction + rendering
# ═══════════════════════════════════════════════════════════════════

def test_extract_discussed_areas_zh_and_en():
    history = [
        {"user": "找 UCL 附近的房子", "assistant": "Camden 和 Islington 都不错。"},
        {"user": "曼彻斯特呢", "assistant": "曼彻斯特也有很多选择。"},
    ]
    areas = extract_discussed_areas(history, last_results=[])
    assert "Camden" in areas
    assert "Islington" in areas
    assert "Manchester" in areas  # 曼彻斯特 via the zh table


def test_extract_discussed_areas_includes_last_results_area():
    areas = extract_discussed_areas(
        history=[], last_results=[{"area": "Shoreditch"}, {"area": "Shoreditch"}, {"area": None}])
    assert areas == ["Shoreditch"]  # deduped, None dropped


def test_extract_discussed_areas_empty_and_nonsense():
    assert extract_discussed_areas([], []) == []
    assert extract_discussed_areas(
        [{"user": "Wakanda 火星", "assistant": "no such place"}], []) == []


def test_render_discussed_areas_line_and_marker():
    line = render_discussed_areas(["Camden", "Manchester"])
    assert DISCUSSED_AREAS_MARKER in line
    assert "Camden, Manchester" in line
    assert "那个区域" in line
    assert render_discussed_areas([]) == ""


def test_discussed_areas_rendered_into_assembled_context_message():
    msgs = assemble_messages(
        user_message="那个区域安全吗",
        history=[{"user": "找 Camden 的房子", "assistant": "Camden 有几个选择。"}],
        context_block={"discussed_areas": ["Camden"]},
        reply_language="zh",
    )
    # Message #2 is the context block; it must carry the discussed-areas anchor.
    ctx = "\n".join(m.content for m in msgs if isinstance(m, SystemMessage))
    assert DISCUSSED_AREAS_MARKER in ctx
    assert "Camden" in ctx


def test_build_messages_surfaces_discussed_areas_for_h6():
    """End-to-end via the loop entry point: a safety follow-up after Camden was discussed
    surfaces Camden in the context so the model can call check_safety instead of asking."""
    state = {
        "user_query": "那个区域安全吗",
        "extracted_context": {
            "current_message": "那个区域安全吗",
            "reply_language": "zh",
            "history": [{"user": "找 Camden 的房子", "assistant": "Camden 有几个选择。"}],
            "last_results": [{"area": "Camden"}],
        },
        "accumulated_search_criteria": {},
    }
    blob = "\n".join(m.content for m in agent_loop._build_messages(state)
                     if isinstance(m, SystemMessage))
    assert DISCUSSED_AREAS_MARKER in blob
    assert "Camden" in blob


# ═══════════════════════════════════════════════════════════════════
# 3. _artifact contract carries success / error
# ═══════════════════════════════════════════════════════════════════

def test_artifact_default_success_true():
    a = _artifact(0, "ask_user", {"question": "x"})
    assert a["success"] is True
    assert a["error"] is None
    assert set(a) >= {"turn", "tool", "raw_data", "params_digest", "success", "error"}


def test_artifact_records_explicit_failure():
    a = _artifact(2, "web_search", None, "digest123", success=False, error="boom")
    assert a["success"] is False
    assert a["error"] == "boom"
    assert a["params_digest"] == "digest123"


def test_execute_tools_artifact_carries_result_success_and_error():
    specs = [_FakeSpec("check_safety"), _FakeSpec("web_search")]
    provider = _FakeProvider(specs, results={
        "check_safety": _FakeResult(True, {"safety_score": 80}),
        "web_search": _FakeResult(False, None, error="rate limited"),
    })
    nodes = build_fc_nodes(provider, agent_llm=_FakeChat([]))
    ai = AIMessage(content="", tool_calls=[
        _tc("check_safety", {"area": "Camden"}, "c1"),
        _tc("web_search", {"query": "rents"}, "c2"),
    ])
    state = {
        "user_query": "check", "extracted_context": {"current_message": "check"},
        "accumulated_search_criteria": {}, "session_id": "s1", "run_id": "r1",
        "loop_turn": 1, "messages": [ai], "tool_artifacts": [], "context_tainted": False,
    }
    cmd = _run(nodes["execute_tools"](state))
    arts = {a["tool"]: a for a in cmd.update["tool_artifacts"]}
    assert arts["check_safety"]["success"] is True
    assert arts["check_safety"]["error"] is None
    assert arts["web_search"]["success"] is False
    assert arts["web_search"]["error"] == "rate limited"


# ═══════════════════════════════════════════════════════════════════
# 4. app.py wiring guard (import-light; no Flask)
# ═══════════════════════════════════════════════════════════════════

def test_app_wires_history_into_extracted_context():
    app_path = os.path.join(os.path.dirname(__file__), "..", "app", "app.py")
    with open(app_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    # The unconditional assignment that feeds fc's assemble_messages.
    assert re.search(r"extracted_context\[['\"]history['\"]\]\s*=\s*history_snapshot", src), (
        "app.py must set extracted_context['history'] = history_snapshot unconditionally")
