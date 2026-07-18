"""Branch-scoped episodic memory for the session-fork feature.

Semantics under test (FORK_CONTRACT.md §3):
  - user-level SEMANTIC + REFLECTION memories stay GLOBAL (visible on every branch);
  - EPISODIC memories are branch-scoped: readable only from the conversation that
    wrote them and from descendants forked AFTER they were written
    (turn_started_at <= the fork cutoff);
  - legacy episodic rows (no conversation_id metadata) stay visible (back-compat);
  - branch_lineage=None reproduces the pre-fork behaviour exactly;
  - strict per-user isolation (fail-closed) is untouched.

Same stubbing pattern as tests/test_agent_memory_isolation.py: a TEMP Chroma dir
(never the live store) and call_ollama stubbed so no LLM/network is touched. No
model downloads beyond Chroma's default local embedder used by the existing suite.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
    """``tests/`` has no ``__init__.py`` so pytest prepends it to ``sys.path``,
    where stale scratch copies of ``core``/``rag`` can shadow the real ``app``
    packages. Pin the real root first and evict any shadowed module already
    imported (mirrors test_agent_memory_isolation._pin_app)."""
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


@pytest.fixture()
def memory(tmp_path, monkeypatch):
    # No LLM calls in unit tests: importance rating / extraction / reflection all
    # go through call_ollama — stub it to a no-op that yields no facts.
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: '{"facts": []}')
    return AgentMemory(db_path=str(tmp_path / "chroma_mem"))


# Cutoff timestamps used across the branch tests. Parent P forks into children at
# T_FORK; anything <= T_FORK is inherited, anything after is not.
T_EARLY = "2026-07-18T10:00:00"   # written before the fork
T_FORK = "2026-07-18T12:00:00"    # fork cutoff (inclusive)
T_LATE = "2026-07-18T14:00:00"    # written after the fork


def _add_episodic(memory, text, user_id, conversation_id, turn_started_at, turn_id="t"):
    """Write a branch-scoped episodic record directly via the public add() path."""
    return memory.add(
        text, "episodic", user_id=user_id, importance=5,
        extra_meta={"conversation_id": conversation_id, "turn_id": turn_id,
                    "turn_started_at": turn_started_at},
    )


def _texts(mems):
    return {m["text"] for m in mems}


# --------------------------------------------------------- remember_turn metadata

def test_remember_turn_writes_branch_metadata_on_episodic(memory):
    memory.remember_turn(
        "I want a 2-bed near Manchester Uni under 1400", "Noted!",
        user_id="user-A", conversation_id="conv-P", turn_id="turn-7",
        turn_started_at=T_EARLY,
    )
    got = memory.col.get(where={"$and": [{"user_id": "user-A"}, {"mtype": "episodic"}]})
    metas = got.get("metadatas") or []
    assert len(metas) == 1
    m = metas[0]
    assert m["conversation_id"] == "conv-P"
    assert m["turn_id"] == "turn-7"
    assert m["turn_started_at"] == T_EARLY


def test_remember_turn_omits_absent_branch_keys(memory):
    # Legacy call site: no branch args -> keys must be ABSENT (chroma rejects None).
    memory.remember_turn("I want a studio in Leeds by September", "Sure!", user_id="user-A")
    got = memory.col.get(where={"$and": [{"user_id": "user-A"}, {"mtype": "episodic"}]})
    m = (got.get("metadatas") or [{}])[0]
    assert "conversation_id" not in m
    assert "turn_id" not in m
    assert "turn_started_at" not in m


# --------------------------------------------------------------- same branch

def test_same_branch_sees_its_own_episodic(memory):
    _add_episodic(memory, "asked about gyms in Fallowfield", "user-A", "conv-P", T_EARLY)
    lineage = [{"conversation_id": "conv-P", "before": None}]
    got = memory.retrieve("gyms", user_id="user-A", n=10, branch_lineage=lineage)
    assert _texts(got) == {"asked about gyms in Fallowfield"}


# ------------------------------------------------ child inherits pre-fork episodic

def test_child_sees_parent_episodic_written_before_fork(memory):
    _add_episodic(memory, "parent asked about tram links pre-fork", "user-A", "conv-P", T_EARLY)
    child_lineage = [
        {"conversation_id": "conv-C", "before": None},
        {"conversation_id": "conv-P", "before": T_FORK},
    ]
    got = memory.retrieve("tram links", user_id="user-A", n=10, branch_lineage=child_lineage)
    assert _texts(got) == {"parent asked about tram links pre-fork"}


def test_child_does_not_see_parent_episodic_written_after_fork(memory):
    _add_episodic(memory, "parent asked about parking post-fork", "user-A", "conv-P", T_LATE)
    child_lineage = [
        {"conversation_id": "conv-C", "before": None},
        {"conversation_id": "conv-P", "before": T_FORK},
    ]
    got = memory.retrieve("parking", user_id="user-A", n=10, branch_lineage=child_lineage)
    assert got == []


def test_fork_cutoff_is_inclusive(memory):
    # A memory written exactly at the fork cutoff is inherited (<= is inclusive).
    _add_episodic(memory, "parent asked at the exact fork moment", "user-A", "conv-P", T_FORK)
    child_lineage = [
        {"conversation_id": "conv-C", "before": None},
        {"conversation_id": "conv-P", "before": T_FORK},
    ]
    got = memory.retrieve("fork moment", user_id="user-A", n=10, branch_lineage=child_lineage)
    assert _texts(got) == {"parent asked at the exact fork moment"}


# ------------------------------------------------------------- siblings isolated

def test_sibling_branches_are_fully_isolated(memory):
    # Two children forked from the same parent turn; each writes its own episodic.
    _add_episodic(memory, "parent shared budget before forking", "user-A", "conv-P", T_EARLY)
    _add_episodic(memory, "sibling A asked about balconies", "user-A", "conv-A", T_LATE)
    _add_episodic(memory, "sibling B asked about pets", "user-A", "conv-B", T_LATE)

    lineage_a = [
        {"conversation_id": "conv-A", "before": None},
        {"conversation_id": "conv-P", "before": T_FORK},
    ]
    lineage_b = [
        {"conversation_id": "conv-B", "before": None},
        {"conversation_id": "conv-P", "before": T_FORK},
    ]

    got_a = memory.retrieve("what did we discuss", user_id="user-A", n=10, branch_lineage=lineage_a)
    got_b = memory.retrieve("what did we discuss", user_id="user-A", n=10, branch_lineage=lineage_b)

    # A sees the inherited parent memory + its own, never sibling B's.
    assert _texts(got_a) == {"parent shared budget before forking",
                             "sibling A asked about balconies"}
    assert _texts(got_b) == {"parent shared budget before forking",
                             "sibling B asked about pets"}
    assert "sibling B asked about pets" not in _texts(got_a)
    assert "sibling A asked about balconies" not in _texts(got_b)


# -------------------------------------------- semantic + reflection stay global

def test_semantic_and_reflection_visible_on_every_branch(memory):
    memory.add("Budget is 1300 pcm in Manchester", "semantic", user_id="user-A", importance=7)
    memory.add("User prioritises commute over space", "reflection", user_id="user-A", importance=8)
    _add_episodic(memory, "episodic only in conv-P", "user-A", "conv-P", T_LATE)

    # An unrelated branch that inherits NONE of conv-P's episodic still sees globals.
    other_lineage = [{"conversation_id": "conv-OTHER", "before": None}]
    got = memory.retrieve("budget commute", user_id="user-A", n=10, branch_lineage=other_lineage)
    texts = _texts(got)
    assert "Budget is 1300 pcm in Manchester" in texts
    assert "User prioritises commute over space" in texts
    assert "episodic only in conv-P" not in texts

    # Even an empty lineage keeps the global layers visible.
    got_empty = memory.retrieve("budget commute", user_id="user-A", n=10, branch_lineage=[])
    assert {"Budget is 1300 pcm in Manchester",
            "User prioritises commute over space"} <= _texts(got_empty)


# --------------------------------------------------------- legacy episodic rows

def test_legacy_episodic_without_conversation_id_stays_visible(memory):
    # A pre-branch episodic row (no conversation_id metadata) must remain readable.
    memory.col.add(
        documents=["Legacy episodic: asked about studios near KCL"],
        metadatas=[{"mtype": "episodic", "user_id": "user-A",
                    "created_at": T_EARLY, "importance": 5}],
        ids=["legacy_ep_1"],
    )
    lineage = [{"conversation_id": "conv-C", "before": None},
               {"conversation_id": "conv-P", "before": T_FORK}]
    got = memory.retrieve("studios near KCL", user_id="user-A", n=10, branch_lineage=lineage)
    assert "Legacy episodic: asked about studios near KCL" in _texts(got)


# ------------------------------------------------- branch_lineage=None unchanged

def test_branch_lineage_none_reproduces_old_behaviour(memory):
    # Episodic in two different conversations; without lineage, BOTH are visible
    # (exactly as before branch scoping existed).
    _add_episodic(memory, "conv-P episodic about gardens", "user-A", "conv-P", T_EARLY)
    _add_episodic(memory, "conv-Q episodic about gardens", "user-A", "conv-Q", T_LATE)
    got = memory.retrieve("gardens", user_id="user-A", n=10)  # branch_lineage defaults to None
    assert _texts(got) == {"conv-P episodic about gardens", "conv-Q episodic about gardens"}


# ------------------------------------------------- user isolation still closed

def test_user_isolation_still_fail_closed_with_lineage(memory):
    _add_episodic(memory, "user-A private episodic", "user-A", "conv-P", T_EARLY)
    lineage = [{"conversation_id": "conv-P", "before": None}]

    # A real lineage never opens a hole in per-user isolation.
    assert memory.retrieve("private", user_id="user-B", n=10, branch_lineage=lineage) == []
    # Missing / blank / 'default' ids still fail CLOSED even with a lineage present.
    assert memory.retrieve("private", user_id=None, n=10, branch_lineage=lineage) == []
    assert memory.retrieve("private", user_id="", n=10, branch_lineage=lineage) == []
    assert memory.retrieve("private", user_id="default", n=10, branch_lineage=lineage) == []


def test_lineage_does_not_leak_other_users_episodic(memory):
    # Same conversation_id string reused by two users: isolation is by user_id first.
    _add_episodic(memory, "user-A note in conv-P", "user-A", "conv-P", T_EARLY)
    _add_episodic(memory, "user-B note in conv-P", "user-B", "conv-P", T_EARLY)
    lineage = [{"conversation_id": "conv-P", "before": None}]
    got_a = memory.retrieve("note", user_id="user-A", n=10, branch_lineage=lineage)
    assert _texts(got_a) == {"user-A note in conv-P"}
