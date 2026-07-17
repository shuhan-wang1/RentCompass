"""Per-user isolation of long-term memory (privacy regression tests).

Bug: a brand-new user was told they had prior preferences. Root cause: every
AgentMemory API defaulted user_id="default", so any call site that failed to
thread the real id read from / wrote to one SHARED bucket — the where-filter was
applied, but with a shared identity. These tests pin the fixed contract:

  - retrieval is strictly filtered by user_id on every path (no global fallback)
  - a missing / blank / 'default' user_id fails CLOSED (empty read, no write)
  - records without user_id metadata (legacy/orphans) can never match anyone
  - forget(user-A) erases only user-A

All tests run against a TEMP Chroma dir — never the live store.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
    """``tests/`` has no ``__init__.py`` so pytest prepends it to ``sys.path``,
    where the stale scratch copies ``tests/core`` and ``tests/rag`` shadow the
    real ``app`` packages (same issue documented in
    test_critic_grounding._load_local_core). Pin the real root first and evict
    any shadowed module already imported."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]


_pin_app()

am_mod = importlib.import_module("rag.agent_memory")
AgentMemory = am_mod.AgentMemory
_valid_user_id = am_mod._valid_user_id


@pytest.fixture()
def memory(tmp_path, monkeypatch):
    # No LLM calls in unit tests: importance rating / extraction / reflection all
    # go through call_ollama — stub it.
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    return AgentMemory(db_path=str(tmp_path / "chroma_mem"))


# ---------------------------------------------------------------- identity gate

def test_valid_user_id_gate():
    assert _valid_user_id("alice-1") == "alice-1"
    assert _valid_user_id("  alice-1  ") == "alice-1"
    assert _valid_user_id(None) is None
    assert _valid_user_id("") is None
    assert _valid_user_id("   ") is None
    assert _valid_user_id(123) is None
    # 'default' was the shared leak bucket — must never be routable again
    assert _valid_user_id("default") is None
    assert _valid_user_id("Default") is None


# ------------------------------------------------------------------ isolation

def test_user_b_never_sees_user_a(memory):
    assert memory.add("Budget is 1200 pcm in Manchester", "semantic",
                      user_id="user-A") is not None
    assert memory.add("Wants a gym nearby", "semantic", user_id="user-A") is not None

    got_a = memory.retrieve("what is my budget and city", user_id="user-A", n=6)
    assert {m["text"] for m in got_a} == {"Budget is 1200 pcm in Manchester",
                                          "Wants a gym nearby"}

    assert memory.retrieve("what is my budget and city", user_id="user-B", n=6) == []
    assert memory.retrieve("what is my budget and city", user_id="brand-new-uuid", n=6) == []


def test_missing_or_default_user_id_fails_closed(memory):
    memory.add("Budget is 900 near UCL", "semantic", user_id="user-A")

    # retrieval: no id / blank / 'default' -> [] (no global fallback, no crash)
    assert memory.retrieve("budget") == []
    assert memory.retrieve("budget", user_id=None) == []
    assert memory.retrieve("budget", user_id="") == []
    assert memory.retrieve("budget", user_id="default") == []

    # writes: rejected, nothing lands in a shared bucket
    assert memory.add("orphan-ish fact", "semantic") is None
    assert memory.add("orphan-ish fact", "semantic", user_id="default") is None
    assert memory.col.count() == 1  # only user-A's record exists


def test_orphan_entries_match_nobody(memory):
    # Simulate a legacy record written before namespacing: NO user_id metadata.
    memory.col.add(
        documents=["Legacy: looking for a studio near UCL under 1500"],
        metadatas=[{"mtype": "semantic", "importance": 7}],
        ids=["legacy_orphan_1"],
    )
    memory.add("User-A fact: flat in Leeds", "semantic", user_id="user-A")

    for uid in ("user-A", "user-B", "fresh-uuid-xyz"):
        texts = [m["text"] for m in memory.retrieve("studio near UCL 1500", user_id=uid, n=10)]
        assert "Legacy: looking for a studio near UCL under 1500" not in texts
    # and the fail-closed paths obviously can't reach it either
    assert memory.retrieve("studio near UCL 1500", user_id="default") == []


def test_remember_turn_requires_user_id(memory, monkeypatch):
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: '{"facts": []}')
    memory.remember_turn("I need a flat in Leeds", "Sure!", user_id=None)
    memory.remember_turn("I need a flat in Leeds", "Sure!", user_id="default")
    assert memory.col.count() == 0

    memory.remember_turn("I need a flat in Leeds", "Sure!", user_id="user-A")
    assert memory.col.count() == 1
    got = memory.retrieve("flat in Leeds", user_id="user-A", n=5)
    assert got and got[0]["mtype"] == "episodic"


def test_forget_is_scoped_to_one_user(memory):
    memory.add("A fact one", "semantic", user_id="user-A")
    memory.add("A fact two", "semantic", user_id="user-A")
    memory.add("B fact", "semantic", user_id="user-B")

    wiped = memory.forget("user-A")
    assert wiped == 2
    assert memory.retrieve("fact", user_id="user-A", n=5) == []
    got_b = memory.retrieve("fact", user_id="user-B", n=5)
    assert [m["text"] for m in got_b] == ["B fact"]


def test_idempotency_key_is_user_scoped(memory):
    a_id = memory.add("A turn", "episodic", user_id="user-A",
                      importance=5, idempotency_key="turn:req-1")
    # Same idempotency key from ANOTHER user must not return user-A's record id.
    b_id = memory.add("B turn", "episodic", user_id="user-B",
                      importance=5, idempotency_key="turn:req-1")
    assert a_id is not None and b_id is not None
    assert a_id != b_id
    assert [m["text"] for m in memory.retrieve("turn", user_id="user-B", n=5)] == ["B turn"]


# ------------------------------------------------------- memory tools fail closed

@pytest.mark.asyncio
async def test_memory_tools_require_user_id(memory, monkeypatch):
    _pin_app()
    mt = importlib.import_module("core.tools.memory_tools")
    monkeypatch.setattr(mt, "_mem", lambda: memory)

    memory.add("Budget 1500 near KCL", "semantic", user_id="user-A")

    ok = await mt.recall_memory_impl("budget", user_id="user-A")
    assert ok["success"] and ok["count"] == 1

    for bad in (None, "", "default"):
        res = await mt.recall_memory_impl("budget", user_id=bad)
        assert res["success"] is False
        assert res["count"] == 0 and res["memories"] == []
        w = await mt.remember_impl("fact", user_id=bad)
        assert w["success"] is False and w["id"] is None
