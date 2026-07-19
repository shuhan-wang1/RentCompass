"""A+ rule-4 confirmation-replay path (design §2.8c): the frozen memory candidate is
replayed verbatim exactly once on user confirmation, discarded on decline, and left
frozen on an unrelated message. Coordinator integration coverage on top of
tests/test_taint_aplus.py (freeze/consume primitives) and tests/test_fc_loop.py
(loop mechanics)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

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
    assert params["idempotency_key"] == f"memgate:s1:{digest}"
    # consumed exactly once
    assert gate.latest_pending_digest("s1") is None
    # the model was told about the replay
    note = "\n".join(getattr(m, "content", "") for m in state["messages"]
                     if isinstance(getattr(m, "content", ""), str))
    assert "saved verbatim" in note


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
