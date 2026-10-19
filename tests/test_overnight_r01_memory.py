"""Explicit memory requests and privacy of automatic memory (overnight round r01: R1-07, R1-08).

R1-07: "Hi, I want a 1-bed flat in Camden under £2,200 pcm. Please remember that I have a
cat." — the search result made the batch tainted, the model's paraphrase of the fact was not
the user's own words, the write was refused (``denied_tainted``) and the answer said "the
memory tool failed". The user's own remember-clause is saved instead, and a refusal is worded
as a refusal.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage

from core import memory_gate
from core.agent_loop import build_fc_nodes
from core.memory_contract import memory_contract_from_artifact, memory_notice
from tests.test_agent_contract_guards import Provider, Result, Spec, _state, _tc

MESSAGE = "Hi, I want a 1-bed flat in Camden under £2,200 pcm. Please remember that I have a cat."


def _isolated_state(message):
    """A session of its own: a refused write is FROZEN as a pending write for the session,
    and a shared synthetic session id would let a later test's "Please remember ..." confirm
    and replay it (an order-dependent second write)."""
    state = _state(message)
    state["session_id"] = f"r107-{uuid.uuid4().hex}"
    return state


@pytest.mark.parametrize("message, fact", [
    (MESSAGE, "I have a cat"),
    ("Please remember I work at UCL", "I work at UCL"),
    ("记住我有一只猫，然后帮我在Camden找房", "我有一只猫"),
    ("Please remember that I have a cat, and find me a flat in Camden", "I have a cat"),
    ("请记住：我在金丝雀码头上班", "我在金丝雀码头上班"),
    ("Do you remember my budget?", ""),
    ("remember to search Camden", ""),
    ("Find flats in Hackney", ""),
])
def test_r1_07_explicit_remember_clause(message, fact):
    assert memory_gate.explicit_remember_clause(message) == fact


@pytest.mark.parametrize("proposed", [
    "User has a cat (pet owner)",
    "User has a cat and needs a pet-friendly property",
    MESSAGE,                                    # the whole request, verbatim
])
def test_r1_07_tainted_batch_saves_the_users_own_clause(proposed):
    provider = Provider(
        [Spec("search_properties"), Spec("remember", side_effect="write", retry_safe=False)],
        {"search_properties": Result(True, {"status": "found", "recommendations": []}),
         "remember": lambda **kw: Result(True, {"success": True, "stored": kw.get("content")})})
    nodes = build_fc_nodes(provider)
    state = _isolated_state(MESSAGE)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", {"area": "Camden", "max_budget": 2200}, "c0"),
        _tc("remember", {"content": proposed, "kind": "semantic"}, "c1")])]
    out = asyncio.run(nodes["execute_tools"](state))
    writes = [params for name, params in provider.calls if name == "remember"]
    assert len(writes) == 1, "the explicit request must be saved, not refused"
    assert writes[0]["content"] == "I have a cat"
    assert out.update["memory_write_contract"]["success"] is True


def test_r1_07_matching_model_content_is_left_alone():
    provider = Provider(
        [Spec("search_properties"), Spec("remember", side_effect="write", retry_safe=False)],
        {"remember": lambda **kw: Result(True, {"success": True, "stored": kw.get("content")})})
    nodes = build_fc_nodes(provider)
    state = _isolated_state(MESSAGE)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", {"area": "Camden"}, "c0"),
        _tc("remember", {"content": "User has a cat"}, "c1")])]
    asyncio.run(nodes["execute_tools"](state))
    assert [p["content"] for n, p in provider.calls if n == "remember"] == ["User has a cat"]


def test_r1_07_tool_derived_content_without_a_remember_cue_is_still_refused():
    provider = Provider(
        [Spec("search_properties"), Spec("remember", side_effect="write", retry_safe=False)])
    nodes = build_fc_nodes(provider)
    state = _isolated_state("Show me flats in Camden under £2,200")
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", {"area": "Camden"}, "c0"),
        _tc("remember", {"content": "landlord prefers a bank transfer"}, "c1")])]
    asyncio.run(nodes["execute_tools"](state))
    assert not [n for n, _ in provider.calls if n == "remember"]


def test_r1_07_a_policy_refusal_is_not_called_a_tool_failure():
    denied = {"tool": "remember", "success": False, "denied": True, "raw_data": None,
              "error": "denied: tainted write requires confirmation"}
    contract = memory_contract_from_artifact(denied)
    assert contract["attempted"] is False
    for language in ("en", "zh"):
        notice = memory_notice(contract, language)
        assert "tool failed" not in notice and "保存失败" not in notice
    assert "confirmation" in memory_notice(contract, "en")
    failed = memory_contract_from_artifact(
        {"tool": "remember", "success": False, "raw_data": None, "error": "disk unavailable"})
    assert "memory tool failed" in memory_notice(failed, "en")


# ── R1-08 ────────────────────────────────────────────────────────────────────────────
# "My name is Priya Raman, my phone is 07700 900123 … (asthma) …" left
# "User asked: Hi. My name is Priya Raman, …" in the episodic layer while the assistant
# said "I don't have your name". A self-identifying name is now treated like contact data.

from tests.test_overnight_memory_sensitive import am_mod  # noqa: E402  (pins app/ on sys.path)

R108 = ("Hi. My name is Priya Raman, my phone is 07700 900123 and I have asthma. "
        "I want a 1-bed in Camden under £2,000.")


@pytest.fixture
def auto_memory(tmp_path, monkeypatch):
    import json

    def fake_llm(prompt, *a, **k):
        if '"facts"' in prompt:
            return json.dumps({"facts": ["User's name is Priya Raman",
                                         "User wants a 1-bed in Camden under £2,000"]})
        if '"ops"' in prompt:
            facts = json.loads(prompt.split("NEW FACTS:\n", 1)[1].split("\n\nJSON:", 1)[0])
            return json.dumps({"ops": [{"event": "ADD", "id": None, "text": f} for f in facts]})
        if '"insights"' in prompt:
            return json.dumps({"insights": ["Priya Raman prioritises Camden"]})
        return "5"

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)
    return am_mod.AgentMemory(db_path=str(tmp_path / "mem"))


def test_r1_08_automatic_memory_keeps_no_self_identifying_name(auto_memory):
    assert auto_memory.remember_turn(R108, "Noted.", session_id="s1", user_id="u_r108")
    got = auto_memory.col.get(where={"user_id": "u_r108"})
    blob = "\n".join(got.get("documents") or [])
    assert "Camden" in blob, "the housing parts must still be remembered"
    for leak in ("Priya", "Raman", "07700", "asthma"):
        assert leak not in blob, leak


@pytest.mark.parametrize("text, kept", [
    ("My name is Priya Raman, I want a flat in Camden", "I want a flat in Camden"),
    ("我叫王小明，想在Camden找一居室", "想在Camden找一居室"),
    ("call me back tomorrow about Camden", "call me back tomorrow about Camden"),
    ("a road called Mare Street in Hackney", "a road called Mare Street in Hackney"),
])
def test_r1_08_name_redaction_is_clause_level(text, kept):
    redacted, _ = am_mod.redact_sensitive(text)
    assert kept in redacted
    assert "Priya" not in redacted and "王小明" not in redacted
