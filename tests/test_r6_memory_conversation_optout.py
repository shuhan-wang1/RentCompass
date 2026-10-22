"""r6-M1: an opt-out with forward or conversation scope pauses automatic memory for the rest of
that conversation.

Recorded (r03 inst-A priv1): "OK. Please don't save anything about me from now on. What will you
keep?" was answered "Understood. I won't save anything about you to long-term memory from now
on." On 6cb064d only that one message was withheld (R5 A-08); the next turn of the same
conversation was logged verbatim and its facts were extracted and consolidated again.

Owner decision: a forward/conversation-scoped opt-out sets a durable per-conversation flag; while
it holds nothing from the conversation is logged with content, extracted, consolidated or
reflected on; an explicit "please remember X" still saves X only; the flag is lifted only by an
explicit re-consent; a message-scoped opt-out ("don't remember this") stays per message.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field

import pytest


def _pin_app():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)


_pin_app()
am_mod = importlib.import_module("rag.agent_memory")
memory_gate = importlib.import_module("core.memory_gate")
USER = "user-r6-m1"

# The recorded r03 priv1 conversation, then two turns after the opt-out.
PRIV1_TURN1 = "I'm looking for a flat in Bow, 1 bed, around £1,700."
PRIV1_TURN2 = "Do you store anything about me when I haven't asked you to remember it? Be honest."
PRIV1_OPTOUT = "OK. Please don't save anything about me from now on. What will you keep?"
AFTER_OPTOUT = "Actually make it 2 beds in Mile End, my budget is £2,000 and I have a dog."
R07_OPTOUT = ("Please do not store or remember anything about me in this chat — no memory at all. "
              "I am HIV positive and my wife Sarah is pregnant. I want a 1-bed in Camden under "
              "£2,000.")
R07_MESSAGE_SCOPE = "I also have a criminal record from 2019. Please do not store that."
FACTS = ["Wants 2 beds in Mile End", "Budget is £2,000", "Has a dog"]


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_GATE_DB", str(tmp_path / "memory_gate.sqlite3"))
    monkeypatch.setattr(memory_gate, "_STORE", None)
    yield tmp_path / "memory_gate.sqlite3"
    memory_gate._STORE = None


@pytest.fixture
def harness(tmp_path, monkeypatch, gate):
    prompts: list[str] = []

    def fake_llm(prompt, *a, **k):
        prompts.append(prompt)
        if '"facts"' in prompt:
            return json.dumps({"facts": FACTS})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            return json.dumps({"insights": ["The user may prefer east London."]})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem")), prompts


def _turn(memory, message, conversation="conv-1", turn_id="t", user=USER, via_app=True):
    """One turn as app.py runs it: the decision when the turn starts, the job later."""
    if via_app:
        decision = am_mod.apply_conversation_memory_turn(user, conversation, message)
        return memory.remember_turn(message, "ok", session_id=user, user_id=user,
                                    conversation_id=conversation, turn_id=turn_id,
                                    memory_withheld=decision["withhold"]), decision
    return memory.remember_turn(message, "ok", session_id=user, user_id=user,
                                conversation_id=conversation, turn_id=turn_id), None


def _rows(memory, user=USER):
    got = memory.col.get(where={"user_id": user})
    return [(meta.get("mtype"), meta.get("turn_id"), doc)
            for doc, meta in zip(got["documents"], got["metadatas"])]


def _episodic(memory, turn_id, user=USER):
    return [doc for mtype, tid, doc in _rows(memory, user) if mtype == "episodic" and tid == turn_id]


def _semantic(memory, user=USER):
    return [doc for mtype, _tid, doc in _rows(memory, user) if mtype == "semantic"]


def _kept_nothing(memory, turn_id, message, user=USER):
    """The turn left no content: at most the shared content-free placeholder row (identical
    placeholders are one row, as for a per-message opt-out), and its words appear nowhere."""
    return (_episodic(memory, turn_id, user) in ([], [am_mod._OPTED_OUT_LOG])
            and not any(message in doc for _m, _t, doc in _rows(memory, user)))


# ---------------------------------------------------------------- scope classification
@pytest.mark.parametrize("message", [
    PRIV1_OPTOUT, R07_OPTOUT,
    "Please don't save anything about me from now on",
    "From now on, don't remember anything I tell you.",
    "Don't store anything for the rest of this chat.",
    "For the rest of this conversation, please don't record what I say.",
    "Stop remembering things.",
    "Do not store any of that personal info please. Just search, do not keep anything about me.",
    "Turn off memory for this conversation.",
    "以后都别记", "这次对话都不要记", "从现在开始，不要记录", "什么都别记", "别再记了",
])
def test_a_forward_or_conversation_opt_out_is_conversation_scoped(message):
    assert am_mod.memory_opt_out_scope(message) == am_mod.MEMORY_SCOPE_CONVERSATION
    assert am_mod.do_not_remember_requested(message) is True


@pytest.mark.parametrize("message", [
    R07_MESSAGE_SCOPE, "Don't remember this.", "Please don't remember my budget any more.",
    "Please do not store or remember anything from this message: I'm moving because of my divorce.",
    "Please do not keep any of that personal info about me. Delete it from your memory and do "
    "not store it.",
    "Don't save anything about my health.", "别记这条", "这句不要保存",
])
def test_a_message_scoped_opt_out_stays_per_message(message):
    assert am_mod.memory_opt_out_scope(message) == am_mod.MEMORY_SCOPE_MESSAGE


@pytest.mark.parametrize("message", [
    "You can remember things again.", "OK, start saving again.", "ok start saving again",
    "Feel free to remember things about me again", "Turn memory back on please",
    "可以重新记了", "你可以继续记住了", "恢复记忆",
])
def test_an_explicit_re_consent_is_recognised(message):
    assert am_mod.memory_resume_requested(message) is True


@pytest.mark.parametrize("message", [
    "Can you remember things again?", "Will you start saving again?",
    "Please remember again: my budget is 2000", "You can't remember things again",
    "再记一下我的预算", "可以记了吗", "Remember that I have a cat",
    "You can remember things again, but don't save anything about me from now on",
    PRIV1_TURN2, "from now on I want 2-bed flats only", "Stop saving listings to my favourites",
])
def test_questions_explicit_saves_and_ordinary_messages_are_not_a_re_consent(message):
    assert am_mod.memory_resume_requested(message) is False
    if message in (PRIV1_TURN2, "from now on I want 2-bed flats only",
                   "Stop saving listings to my favourites", "Remember that I have a cat"):
        assert am_mod.memory_opt_out_scope(message) is None


# ---------------------------------------------------------------- behaviour
@pytest.mark.parametrize("via_app", [True, False], ids=["app-decision", "direct-caller"])
def test_after_a_forward_opt_out_later_turns_keep_nothing(harness, via_app):
    memory, prompts = harness
    _turn(memory, PRIV1_TURN1, turn_id="t1", via_app=via_app)
    _turn(memory, PRIV1_TURN2, turn_id="t2", via_app=via_app)
    assert _episodic(memory, "t1") == [f"User asked: {PRIV1_TURN1}"]
    before_semantic = _semantic(memory)
    before_reflections = [d for m, _t, d in _rows(memory) if m == "reflection"]
    prompts.clear()

    _turn(memory, PRIV1_OPTOUT, turn_id="t3", via_app=via_app)
    _turn(memory, AFTER_OPTOUT, turn_id="t4", via_app=via_app)
    _turn(memory, "And somewhere quiet near Victoria Park, please.", turn_id="t5", via_app=via_app)

    assert _episodic(memory, "t3") == [am_mod._OPTED_OUT_LOG]
    assert _kept_nothing(memory, "t4", AFTER_OPTOUT)
    assert _kept_nothing(memory, "t5", "Victoria Park")
    # no extraction, no consolidation, no reflection was asked for on any later turn
    assert not any(marker in p for p in prompts for marker in ('"facts"', '"ops"', '"insights"'))
    assert _semantic(memory) == before_semantic
    assert [d for m, _t, d in _rows(memory) if m == "reflection"] == before_reflections
    assert memory_gate.conversation_memory_paused(USER, "conv-1") is True


def test_a_message_scoped_opt_out_lets_the_next_turn_be_saved(harness):
    memory, prompts = harness
    _turn(memory, R07_MESSAGE_SCOPE, turn_id="t1")
    assert _episodic(memory, "t1") == [am_mod._OPTED_OUT_LOG]
    assert memory_gate.conversation_memory_paused(USER, "conv-1") is False
    _turn(memory, AFTER_OPTOUT, turn_id="t2")
    assert _episodic(memory, "t2") == [f"User asked: {AFTER_OPTOUT}"]
    assert set(FACTS) <= set(_semantic(memory))


def test_re_consent_lifts_the_pause(harness):
    memory, _prompts = harness
    _turn(memory, PRIV1_OPTOUT, turn_id="t1")
    _, decision = _turn(memory, "OK, you can remember things again.", turn_id="t2")
    assert decision["event"] == "resumed" and decision["paused"] is False
    assert memory_gate.conversation_memory_paused(USER, "conv-1") is False
    _turn(memory, AFTER_OPTOUT, turn_id="t3")
    assert _episodic(memory, "t3") == [f"User asked: {AFTER_OPTOUT}"]
    assert set(FACTS) <= set(_semantic(memory))


def test_an_explicit_remember_while_paused_stores_only_that_fact(harness, monkeypatch):
    memory, prompts = harness
    from core.tools import memory_tools
    monkeypatch.setattr(memory_tools, "_mem", lambda: memory)
    _turn(memory, PRIV1_OPTOUT, turn_id="t1")
    message = "Please remember that I have a dog. Also I'd like 2 beds in Mile End under £2,000."
    prompts.clear()
    decision = am_mod.apply_conversation_memory_turn(USER, "conv-1", message)
    assert decision["withhold"] is True and decision["paused"] is True
    # the turn's explicit save (the remember tool, the user's own words) ...
    assert memory_gate.write_authorization(message, "I have a dog")
    saved = memory_tools.remember_impl("I have a dog", session_id=USER, user_id=USER)
    assert saved["success"] is True
    # ... and the same turn's background job
    memory.remember_turn(message, "Saved.", session_id=USER, user_id=USER,
                         conversation_id="conv-1", turn_id="t2",
                         memory_withheld=decision["withhold"])
    assert _semantic(memory) == ["I have a dog"]
    assert _kept_nothing(memory, "t2", "Mile End")
    assert not any('"facts"' in p for p in prompts)
    # the pause still holds for everything else
    _turn(memory, AFTER_OPTOUT, turn_id="t3")
    assert _semantic(memory) == ["I have a dog"]
    assert memory_gate.conversation_memory_paused(USER, "conv-1") is True


def test_the_pause_survives_a_fresh_store_and_process(harness, tmp_path, monkeypatch):
    memory, prompts = harness
    _turn(memory, PRIV1_OPTOUT, turn_id="t1")
    # a restart: new memory_gate ledger handle, new AgentMemory on the same files
    monkeypatch.setattr(memory_gate, "_STORE", None)
    fresh = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    prompts.clear()
    _turn(fresh, AFTER_OPTOUT, turn_id="t2", via_app=False)
    assert _kept_nothing(fresh, "t2", AFTER_OPTOUT)
    assert not any('"facts"' in p for p in prompts)
    # the ledger keeps identifiers only, never what the user said
    with sqlite3.connect(tmp_path / "memory_gate.sqlite3") as db:
        rows = db.execute("SELECT * FROM conversation_memory_pause").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(USER, "conv-1")]
    assert not any("save" in str(value) for row in rows for value in row)


def test_other_conversations_and_users_are_unaffected(harness):
    memory, _prompts = harness
    _turn(memory, PRIV1_OPTOUT, conversation="conv-1", turn_id="t1")
    _turn(memory, AFTER_OPTOUT, conversation="conv-2", turn_id="t2")
    assert _episodic(memory, "t2") == [f"User asked: {AFTER_OPTOUT}"]
    assert set(FACTS) <= set(_semantic(memory))
    other = "user-r6-m1-other"
    _turn(memory, AFTER_OPTOUT, conversation="conv-1", turn_id="t3", user=other)
    assert _episodic(memory, "t3", user=other) == [f"User asked: {AFTER_OPTOUT}"]
    assert memory_gate.conversation_memory_paused(USER, "conv-2") is False
    assert memory_gate.conversation_memory_paused(other, "conv-1") is False


def test_a_branch_made_after_the_pause_stays_paused(harness):
    """Editing a later message forks the conversation; the branch continues the paused stretch.
    A branch made from a point before the opt-out does not inherit it."""
    import datetime
    memory, _prompts = harness
    am_mod.apply_conversation_memory_turn(USER, "conv-1", PRIV1_OPTOUT)
    paused_at = memory_gate.conversation_memory_paused_at(USER, "conv-1")

    def lineage(branch, offset):
        cut = datetime.datetime.fromtimestamp(paused_at + offset, tz=datetime.timezone.utc)
        return [{"conversation_id": branch, "before": None},
                {"conversation_id": "conv-1", "before": cut.isoformat()}]

    later = am_mod.apply_conversation_memory_turn(USER, "branch-after", AFTER_OPTOUT,
                                                  lineage=lineage("branch-after", 30))
    assert later["withhold"] is True and later["paused"] is True
    assert memory_gate.conversation_memory_paused(USER, "branch-after") is True
    earlier = am_mod.apply_conversation_memory_turn(USER, "branch-before", AFTER_OPTOUT,
                                                    lineage=lineage("branch-before", -30))
    assert earlier["withhold"] is False
    assert memory_gate.conversation_memory_paused(USER, "branch-before") is False


def test_a_late_job_keeps_the_decision_its_turn_took(harness):
    """A paused turn's job that runs after a later turn lifted the pause still keeps nothing,
    and it does not pause the conversation again."""
    memory, _prompts = harness
    paused = am_mod.apply_conversation_memory_turn(USER, "conv-1", PRIV1_OPTOUT)
    during = am_mod.apply_conversation_memory_turn(USER, "conv-1", AFTER_OPTOUT)
    am_mod.apply_conversation_memory_turn(USER, "conv-1", "You can remember things again.")
    for turn_id, message, decision in (("t1", PRIV1_OPTOUT, paused), ("t2", AFTER_OPTOUT, during)):
        memory.remember_turn(message, "ok", session_id=USER, user_id=USER,
                             conversation_id="conv-1", turn_id=turn_id,
                             memory_withheld=decision["withhold"])
    assert _kept_nothing(memory, "t2", AFTER_OPTOUT)
    assert _semantic(memory) == []
    assert memory_gate.conversation_memory_paused(USER, "conv-1") is False


def test_an_unreadable_pause_ledger_keeps_nothing_from_the_turn(monkeypatch):
    monkeypatch.setattr(memory_gate, "conversation_memory_paused", lambda u, c: None)
    monkeypatch.setattr(memory_gate, "set_conversation_memory_paused",
                        lambda *a: pytest.fail("the flag must not change on an unknown state"))
    decision = am_mod.apply_conversation_memory_turn(USER, "conv-1", AFTER_OPTOUT)
    assert decision["withhold"] is True and decision["event"] is None


def test_placeholder_rows_never_reach_the_reflection_corpus(harness):
    memory, prompts = harness
    for index in range(5):
        memory.add(f"User asked: 1-bed in Bow under £1,{700 + index}", "episodic", session_id=USER,
                   user_id=USER, role="user", importance=9)
    memory.add(am_mod._OPTED_OUT_LOG, "episodic", session_id=USER, user_id=USER, role="user",
               importance=9)
    memory.maybe_reflect(USER, USER)
    corpus = [p for p in prompts if '"insights"' in p]
    assert corpus and "message not stored" not in corpus[-1]


# ---------------------------------------------------------------- what the user is told
def test_the_reply_states_the_conversation_scope():
    out = memory_gate.acknowledge_memory_pause(
        "Understood. I won't save that.", "paused", "en")
    assert out.endswith(memory_gate.MEMORY_PAUSED_ACK_EN)
    assert "rest of this conversation, not just this message" in out
    zh = memory_gate.acknowledge_memory_pause("好的。", "paused", "zh")
    assert zh.endswith(memory_gate.MEMORY_PAUSED_ACK_ZH) and "本次对话" in zh
    assert memory_gate.acknowledge_memory_pause("ok", "resumed", "en").endswith(
        memory_gate.MEMORY_RESUMED_ACK_EN)
    assert memory_gate.acknowledge_memory_pause("ok", None, "en") == "ok"
    once = memory_gate.acknowledge_memory_pause("ok", "paused", "en")
    assert memory_gate.acknowledge_memory_pause(once, "paused", "en") == once


def test_the_storage_disclosure_is_the_paused_one_while_paused():
    from core.agent_loop import AUTOMATIC_MEMORY_DISCLOSURE_EN, disclose_automatic_memory
    paused = disclose_automatic_memory("No.", PRIV1_TURN2, "en", memory_paused=True)
    assert memory_gate.MEMORY_PAUSED_DISCLOSURE_EN in paused
    assert AUTOMATIC_MEMORY_DISCLOSURE_EN not in paused
    assert disclose_automatic_memory("No.", PRIV1_TURN2, "en").endswith(
        AUTOMATIC_MEMORY_DISCLOSURE_EN)


def test_the_model_is_told_the_pause_covers_the_conversation():
    note = memory_gate.MEMORY_PAUSE_NOTE
    assert "PAUSED for this conversation" in note and "not only one message" in note
    assert not note.startswith("[")       # never read as a saved [FACT]/[PAST] line


def test_the_memory_job_forwards_the_turns_decision(monkeypatch):
    import app as appmod
    seen = {}

    class _Memory:
        def remember_turn(self, *args, **kwargs):
            seen.update(kwargs)
            return True

    # By module object, not by dotted path: another test module evicts the ``rag`` namespace
    # package, after which ``rag.agent_memory`` is no attribute of it.
    monkeypatch.setattr(importlib.import_module("rag.agent_memory"), "get_agent_memory",
                        lambda: _Memory())
    monkeypatch.setattr(appmod.conversation_store, "save_background_job_result",
                        lambda *a, **k: None)
    appmod._apply_background_job({"id": 1, "kind": "memory_turn", "user_id": USER,
                                  "conversation_id": "conv-1", "turn_id": "t1",
                                  "payload": {"user_message": AFTER_OPTOUT,
                                              "memory_withheld": True}}, "w1")
    assert seen["memory_withheld"] is True
    seen.clear()
    appmod._apply_background_job({"id": 2, "kind": "memory_turn", "user_id": USER,
                                  "conversation_id": "conv-1", "turn_id": "t2",
                                  "payload": {"user_message": AFTER_OPTOUT}}, "w1")
    assert "memory_withheld" not in seen     # a job queued before the field: decided in the job


# ---------------------------------------------------------------- the fc write gate
@dataclass
class _Spec:
    name: str
    description: str = "desc"
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    side_effect: str = "none"
    retry_safe: bool = False
    version: str = "1"
    terminal: bool = False


class _Result:
    def __init__(self, success=True, data=None, error=None):
        self.success, self.data, self.error = success, data, error


class _Provider:
    def __init__(self):
        self.calls = []

    def list_specs(self):
        return [_Spec("remember", side_effect="write")]

    def get(self, name):
        return type("T", (), {"version": "1", "side_effect": "write"})()

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        return _Result(True, {"saved": True})


class _Chat:
    def bind_tools(self, tools):
        return self


def _fc_state(message, paused):
    return {
        "user_query": message,
        "extracted_context": {"current_message": message, "reply_language": "en",
                              "memory_paused": paused},
        "accumulated_search_criteria": {}, "user_preferences": {}, "user_id": USER,
        "session_id": "conv-1", "run_id": "r1", "loop_turn": 1, "messages": [],
        "tool_artifacts": [], "context_tainted": False, "final_response": "",
        "response_type": "answer", "turn_tool_budget_used_s": 0.0,
    }


def _run_remember(message, content, paused):
    from langchain_core.messages import AIMessage
    import core.agent_loop as agent_loop
    provider = _Provider()
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=_Chat())
    state = _fc_state(message, paused)
    state["messages"] = [AIMessage(content="", tool_calls=[{
        "name": "remember", "args": {"content": content, "kind": "semantic"}, "id": "c1",
        "type": "tool_call"}])]
    cmd = asyncio.run(nodes["execute_tools"](state))
    return provider, (cmd.update or {})


def test_while_paused_the_model_cannot_save_on_its_own(gate):
    provider, update = _run_remember(AFTER_OPTOUT, "User wants 2 beds in Mile End", True)
    assert provider.calls == []
    tool_message = [m for m in update.get("messages") or [] if getattr(m, "name", "") == "remember"]
    assert tool_message and "asked not to be remembered" in tool_message[-1].content


def test_while_paused_an_explicit_remember_is_still_saved(gate):
    provider, _update = _run_remember("Please remember that I have a dog.", "I have a dog", True)
    assert [name for name, _params in provider.calls] == ["remember"]


def test_without_a_pause_the_write_gate_is_unchanged(gate):
    provider, _update = _run_remember(AFTER_OPTOUT, "User wants 2 beds in Mile End", False)
    assert [name for name, _params in provider.calls] == ["remember"]


# ---------------------------------------------------------------- the app turn, end to end
def _app_turns(monkeypatch, messages, conversation_id=None):
    """Run real /api/alex turns with only the GRAPH stubbed; capture what the model was given,
    the memory job's payload and the reply."""
    import uuid
    import app as appmod
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    seen = []

    class _Graph:
        async def ainvoke(self, state, *a, **kw):
            seen.append({"memory_context": state.get("memory_context") or "",
                         "user_query": state.get("user_query") or "",
                         "memory_paused": (state.get("extracted_context") or {}).get(
                             "memory_paused")})
            return {"final_response": "Understood.", "response_type": "answer", "tool_data": {}}

        async def aget_state(self, *a, **kw):
            return None

    graph = _Graph()
    setattr(graph, appmod._GRAPH_RUNNER_ATTR, appmod._graph_loop_runner)
    monkeypatch.setattr(appmod, "agent_graph", graph)
    jobs = []
    monkeypatch.setattr(appmod, "_queue_background_job",
                        lambda job: jobs.append(job) or True)
    appmod.app.config.update(TESTING=True)
    client = appmod.app.test_client()
    user = "u" + uuid.uuid4().hex[:16]
    out = []
    for message in messages:
        body = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        reply = client.post("/api/alex", json=body, headers={"X-User-Id": user}).get_json()
        conversation_id = reply.get("conversation_id") or conversation_id
        memory_jobs = [j for j in jobs if j.get("kind") == "memory_turn"]
        out.append({"reply": reply.get("message") or "", "model": seen[-1],
                    "withheld": memory_jobs[-1]["payload"].get("memory_withheld")})
    return out, user, conversation_id


@pytest.mark.parametrize("arch", ["fc_loop", "legacy"])
def test_an_app_conversation_pauses_and_resumes(gate, monkeypatch, arch):
    import app as appmod
    monkeypatch.setattr(appmod, "AGENT_ARCH", arch)
    turns, user, conversation = _app_turns(monkeypatch, [
        PRIV1_TURN1, PRIV1_OPTOUT, AFTER_OPTOUT, "You can remember things again.", AFTER_OPTOUT])
    before, optout, during, resume, after = turns
    assert before["withheld"] is False and not before["model"]["memory_paused"]
    assert memory_gate.MEMORY_PAUSED_ACK_EN not in before["reply"]

    assert optout["withheld"] is True and optout["model"]["memory_paused"] is True
    assert optout["reply"].endswith(memory_gate.MEMORY_PAUSED_ACK_EN)
    carried = optout["model"]["memory_context" if arch == "fc_loop" else "user_query"]
    assert memory_gate.MEMORY_PAUSE_NOTE in carried

    assert during["withheld"] is True and during["model"]["memory_paused"] is True
    assert memory_gate.MEMORY_PAUSED_ACK_EN not in during["reply"]      # said once, when set

    assert resume["withheld"] is False and resume["model"]["memory_paused"] is False
    assert resume["reply"].endswith(memory_gate.MEMORY_RESUMED_ACK_EN)
    assert after["withheld"] is False
    assert memory_gate.MEMORY_PAUSE_NOTE not in after["model"]["memory_context"]

    # a new conversation of the same user is not paused by the first one's request
    assert memory_gate.conversation_memory_paused(user, conversation) is False
    assert memory_gate.set_conversation_memory_paused(user, conversation, True)
    assert memory_gate.conversation_memory_paused(user, "another-conversation") is False


def test_scope_parsing_stays_fast_on_long_runs():
    import time
    start = time.perf_counter()
    for text in ("不要" * 4000, "以后不要" * 2000, "别再" * 4000, "you can " * 1000,
                 "don't save " * 700, "from now on " * 700, "1," * 30000, " " * 60000 + "x"):
        am_mod.memory_opt_out_scope(text)
        am_mod.memory_resume_requested(text)
    assert time.perf_counter() - start < 2.0
