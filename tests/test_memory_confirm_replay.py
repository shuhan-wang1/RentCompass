"""A+ rule-4 confirmation-replay path (design §2.8c): the frozen memory candidate is
replayed verbatim exactly once on user confirmation, discarded on decline, and left
frozen on an unrelated message. Coordinator integration coverage on top of
tests/test_taint_aplus.py (freeze/consume primitives) and tests/test_fc_loop.py
(loop mechanics)."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

import core.agent_loop as agent_loop
from core.agent_loop import build_fc_nodes


# ─── fakes (mirrors tests/test_fc_loop.py) ──────────────────────────
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


class FakeProvider:
    def __init__(self, specs):
        self._specs = list(specs)
        self.calls = []

    def list_specs(self):
        return list(self._specs)

    def get(self, name):
        return None

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        return FakeResult(True, {"ok": True})


class SequencedProvider(FakeProvider):
    def __init__(self, specs, outcomes):
        super().__init__(specs)
        self._outcomes = list(outcomes)

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        success = self._outcomes.pop(0)
        return FakeResult(success, {"ok": success}, None if success else "temporary failure")


class FreezeNewThenFailProvider(FakeProvider):
    def __init__(self, specs, gate, clock):
        super().__init__(specs)
        self._gate = gate
        self._clock = clock
        self.new_digest = None

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        self._clock[0] = 2000.0
        self.new_digest = self._gate.freeze_pending_write(
            "s1", "新的待保存内容", "semantic")
        self._clock[0] = 3000.0
        return FakeResult(False, {"ok": False}, "temporary failure")


class RecordingProvider:
    def __init__(self, registry):
        self._registry = registry
        self.calls = []

    def list_specs(self):
        return self._registry.list_specs()

    def get(self, name):
        return self._registry.get(name)

    async def execute_tool(self, name, **params):
        self.calls.append((name, dict(params)))
        return await self._registry.execute_tool(name, **params)


class FakeChat:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return self._scripted.pop(0)


def _state(current_message, **over):
    st = {
        "user_query": current_message,
        "extracted_context": {"current_message": current_message, "reply_language": "zh"},
        "accumulated_search_criteria": {},
        "user_preferences": {"hard_preferences": [], "soft_preferences": [], "excluded_areas": [],
                             "required_amenities": [], "safety_concerns": []},
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "r1",
        "loop_turn": 0,
        "messages": [],
        "tool_artifacts": [],
        "context_tainted": False,
        "final_response": "",
        "response_type": "answer",
    }
    st.update(over)
    return st


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    import core.memory_gate as mg
    monkeypatch.setenv("MEMORY_GATE_DB", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setattr(mg, "_STORE", None)
    yield mg
    mg._STORE = None


def _run_agent_once(provider, current_message):
    from langchain_core.messages import AIMessage
    nodes = build_fc_nodes(provider, agent_llm=FakeChat([AIMessage(content="好的，已处理。")]))
    state = _state(current_message)
    cmd = asyncio.run(nodes["agent"](state))
    state.update(cmd.update or {})
    return state, cmd


# ─── confirmation_intent ────────────────────────────────────────────
@pytest.mark.parametrize("msg,expected", [
    ("好的", "yes"),
    ("是的", "yes"),
    ("ok", "yes"),
    ("save it", "yes"),
    ("不用", "no"),
    ("算了", "no"),
    ("no", "no"),
    ("继续搜索吧", "none"),
    ("帮我找一下国王十字附近的房子", "none"),
    ("好的，另外帮我看看这个区域的安全情况怎么样呢", "none"),  # long → none
    ("", "none"),
])
def test_confirmation_intent(gate, msg, expected):
    assert gate.confirmation_intent(msg) == expected


# ─── latest_pending_digest ──────────────────────────────────────────
def test_latest_pending_digest_empty(gate):
    assert gate.latest_pending_digest("s1") is None


def test_latest_pending_digest_orders_by_created(gate, monkeypatch):
    import core.memory_gate as mg
    t = [1000.0]
    monkeypatch.setattr(mg.time, "time", lambda: t[0])
    d1 = gate.freeze_pending_write("s1", "first", "semantic")
    t[0] = 2000.0
    d2 = gate.freeze_pending_write("s1", "second", "semantic")
    assert d1 != d2
    assert gate.latest_pending_digest("s1") == d2
    assert gate.latest_pending_digest("other") is None


def test_pending_store_migrates_legacy_row_and_persists_retry_attempt(tmp_path):
    import core.memory_gate as mg

    path = tmp_path / "legacy-gate.sqlite3"
    content = "legacy pending fact"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE pending_memory_writes ("
            "session_id TEXT NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL, "
            "kind TEXT NOT NULL, created REAL NOT NULL, "
            "PRIMARY KEY(session_id, digest))"
        )
        db.execute(
            "INSERT INTO pending_memory_writes VALUES (?, ?, ?, ?, ?)",
            ("s1", digest, content, "semantic", 123.0),
        )

    store = mg._PendingWriteStore(path)
    claimed = store.consume("s1", digest, include_created=True)

    assert claimed["created"] == 123.0
    assert len(claimed["operation_id"]) == 32
    assert claimed["attempt"] == 0
    claimed["attempt"] = 1
    assert store.restore("s1", digest, claimed) is True

    reopened = mg._PendingWriteStore(path)
    retried = reopened.consume("s1", digest, include_created=True)
    assert retried["created"] == 123.0
    assert retried["operation_id"] == claimed["operation_id"]
    assert retried["attempt"] == 1


# ─── replay flow through agent_node ─────────────────────────────────
def test_confirm_replays_frozen_content_verbatim(gate):
    digest = gate.freeze_pending_write("s1", "预算上限 £1400/月", "semantic")
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    state, _cmd = _run_agent_once(provider, "好的")

    assert len(provider.calls) == 1
    name, params = provider.calls[0]
    assert name == "remember"
    assert params["content"] == "预算上限 £1400/月"      # frozen content, never model args
    assert params["kind"] == "semantic"
    assert params["user_id"] == "u1"
    assert params["session_id"] == "s1"
    assert params["idempotency_key"].startswith(f"memgate:s1:{digest}:")
    assert params["idempotency_key"].endswith(":0")
    # consumed exactly once
    assert gate.latest_pending_digest("s1") is None
    # the model was told about the replay
    note = "\n".join(getattr(m, "content", "") for m in state["messages"]
                     if isinstance(getattr(m, "content", ""), str))
    assert "saved verbatim" in note
    system_blob = "\n".join(
        m.content for m in state["messages"] if isinstance(m, SystemMessage))
    assert "预算上限 £1400/月" not in system_blob
    assert any(
        isinstance(m, HumanMessage)
        and agent_loop._LOW_PRIVILEGE_DATA_HEADER in m.content
        and "saved verbatim" in m.content
        for m in state["messages"]
    )


def test_failed_confirmation_keeps_candidate_for_idempotent_retry(gate):
    digest = gate.freeze_pending_write("s1", "预算上限 £1400/月", "semantic")
    provider = SequencedProvider(
        [FakeSpec("remember", side_effect="write", retry_safe=False)],
        outcomes=[False, True],
    )

    failed_state, _cmd = _run_agent_once(provider, "好的")

    assert gate.latest_pending_digest("s1") == digest
    failed_note = "\n".join(
        getattr(m, "content", "") for m in failed_state["messages"]
        if isinstance(getattr(m, "content", ""), str)
    )
    assert "offer to retry" in failed_note

    saved_state, _cmd = _run_agent_once(provider, "好的")

    assert len(provider.calls) == 2
    first_key = provider.calls[0][1]["idempotency_key"]
    second_key = provider.calls[1][1]["idempotency_key"]
    assert first_key.startswith(f"memgate:s1:{digest}:") and first_key.endswith(":0")
    assert second_key.startswith(f"memgate:s1:{digest}:") and second_key.endswith(":1")
    assert second_key != first_key
    assert gate.latest_pending_digest("s1") is None
    saved_note = "\n".join(
        getattr(m, "content", "") for m in saved_state["messages"]
        if isinstance(getattr(m, "content", ""), str)
    )
    assert "saved verbatim" in saved_note


def test_failed_restore_does_not_promote_old_candidate_over_newer_one(
        gate, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    old_digest = gate.freeze_pending_write("s1", "旧的待保存内容", "semantic")
    provider = FreezeNewThenFailProvider(
        [FakeSpec("remember", side_effect="write", retry_safe=False)], gate, clock)

    _state_, _cmd = _run_agent_once(provider, "好的")

    assert provider.new_digest and provider.new_digest != old_digest
    assert gate.latest_pending_digest("s1") == provider.new_digest
    assert gate.consume_pending_write("s1", provider.new_digest) == {
        "content": "新的待保存内容", "kind": "semantic",
    }
    assert gate.latest_pending_digest("s1") == old_digest


def test_real_registry_executes_again_after_explicit_retry(gate, tmp_path):
    from core.tool_system import Tool, ToolRegistry
    from uk_rent_agent.tools.idempotency import IdempotencyStore

    outcomes = [False, True]
    implementation_calls = []

    def _remember(content, kind="semantic", session_id="default", user_id=None):
        implementation_calls.append(content)
        success = outcomes.pop(0)
        return {
            "success": success,
            "stored": content if success else None,
            "error": None if success else "temporary failure",
        }

    registry = ToolRegistry(
        idempotency_store=IdempotencyStore(tmp_path / "idempotency.sqlite3"))
    registry.register(Tool(
        name="remember",
        description="test remember",
        func=_remember,
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {"type": "string", "default": "semantic"},
                "session_id": {"type": "string", "default": "default"},
                "user_id": {"type": "string"},
            },
            "required": ["content"],
        },
        side_effect="write",
        retry_safe=False,
    ))
    provider = RecordingProvider(registry)
    digest = gate.freeze_pending_write("s1", "预算上限 £1400/月", "semantic")

    _run_agent_once(provider, "好的")

    assert implementation_calls == ["预算上限 £1400/月"]
    assert gate.latest_pending_digest("s1") == digest
    first_key = provider.calls[0][1]["idempotency_key"]
    assert registry._idempotency_store.get_record(first_key).status == "failed"

    _run_agent_once(provider, "好的")

    assert implementation_calls == ["预算上限 £1400/月", "预算上限 £1400/月"]
    second_key = provider.calls[1][1]["idempotency_key"]
    assert second_key != first_key
    assert registry._idempotency_store.get_record(second_key).status == "complete"
    assert gate.latest_pending_digest("s1") is None


def test_decline_discards_without_executing(gate):
    gate.freeze_pending_write("s1", "预算上限 £1400/月", "semantic")
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    state, _cmd = _run_agent_once(provider, "不用")

    assert provider.calls == []
    assert gate.latest_pending_digest("s1") is None      # consumed (discarded)
    note = "\n".join(getattr(m, "content", "") for m in state["messages"]
                     if isinstance(getattr(m, "content", ""), str))
    assert "declined" in note


def test_unrelated_message_leaves_candidate_frozen(gate):
    digest = gate.freeze_pending_write("s1", "预算上限 £1400/月", "semantic")
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    _state_, _cmd = _run_agent_once(provider, "帮我找一下国王十字附近的一居室")

    assert provider.calls == []
    assert gate.latest_pending_digest("s1") == digest    # still pending


def test_bad_frozen_kind_falls_back_to_semantic(gate):
    gate.freeze_pending_write("s1", "some fact", "remember")  # executor froze tool name as kind
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    _state_, _cmd = _run_agent_once(provider, "yes")

    assert len(provider.calls) == 1
    assert provider.calls[0][1]["kind"] == "semantic"


# ─── write-gate under the rule-2 refinement (H13) ───────────────────
def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _drive_write(gate, current_message, tool_args, *, context_tainted=True):
    """Run agent → execute_tools once for a model-initiated remember, so the write-gate
    (write_authorization = cue AND user-stated content) actually runs."""
    from langchain_core.messages import AIMessage
    from core.agent_loop import build_fc_nodes
    provider = FakeProvider([FakeSpec("remember", side_effect="write", retry_safe=False)])
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("remember", dict(tool_args), "c1")]),
        AIMessage(content="ok"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = _state(current_message, context_tainted=context_tainted)
    cmd = asyncio.run(nodes["agent"](state))
    state.update(cmd.update or {})
    assert cmd.goto == "execute_tools"
    cmd = asyncio.run(nodes["execute_tools"](state))
    state.update(cmd.update or {})
    return provider, state


_H13_MSG = "搜下 Camden 的房子，顺便把你找到的最便宜那套的价格记住"


def test_h13_tool_derived_content_denied_and_frozen(gate):
    # Cue present ("记住") but the number is a scraped price never in the user message →
    # not authorized → denied even though context is tainted, and content is frozen.
    provider, state = _drive_write(
        gate, _H13_MSG,
        {"content": "£950 (cheapest flat found in Camden)", "kind": "semantic"})

    assert provider.calls == []                       # write never executed
    assert gate.latest_pending_digest("s1") is not None  # exact content frozen for replay
    from langchain_core.messages import ToolMessage
    blocked = [m for m in state["messages"]
               if isinstance(m, ToolMessage) and "write blocked" in m.content]
    assert len(blocked) == 1


def test_user_stated_content_allowed_under_taint(gate):
    # Cue present AND the saved content is derivable from the user message → authorized
    # even though the turn is tainted; the write executes, nothing is frozen.
    provider, _state_ = _drive_write(
        gate, "记住我预算1400", {"content": "budget £1400/month", "kind": "semantic"})

    assert len(provider.calls) == 1
    assert provider.calls[0][0] == "remember"
    assert gate.latest_pending_digest("s1") is None


def test_cue_but_unrelated_number_matches_denied(gate):
    # numbers-match-but-text-unrelated: the figure 1400 is in the message but the
    # distinguishing phrase is not → not user-stated → denied.
    provider, _state_ = _drive_write(
        gate, "记住我预算1400", {"content": "flat viewing at 1400 Camden Road", "kind": "semantic"})

    assert provider.calls == []
    assert gate.latest_pending_digest("s1") is not None
