"""Regression tests for the 2026-07-17 memory hardening round:

  - auto-episodic turn logs get a STATIC importance (no per-turn LLM rating call)
  - a triviality gate skips fact-extraction for greetings/acks/bypass/short msgs
  - the per-user episodic layer is capped (newest-N kept, overflow evicted)
  - maybe_reflect's corpus is the NEWEST-by-created_at records (Chroma .get()
    has no ordering guarantee)

All tests run against a TEMP Chroma dir and stub every LLM call.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
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


class _LLMCounter:
    """Stub for call_ollama that counts invocations and returns canned JSON."""

    def __init__(self, ret='{"facts": []}'):
        self.calls = 0
        self.ret = ret

    def __call__(self, *a, **k):
        self.calls += 1
        return self.ret


@pytest.fixture()
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(am_mod, "call_ollama", _LLMCounter())
    return AgentMemory(db_path=str(tmp_path / "chroma_mem"))


def _episodic_records(memory, user_id="user-A"):
    got = memory.col.get(where={"$and": [{"user_id": user_id}, {"mtype": "episodic"}]})
    return list(zip(got.get("ids") or [], got.get("metadatas") or []))


# ------------------------------------------------ static importance (no LLM call)

def test_auto_episodic_uses_static_importance_no_llm_rating(memory, monkeypatch):
    counter = _LLMCounter()
    monkeypatch.setattr(am_mod, "call_ollama", counter)

    # Spy: _rate_importance must NOT be called for the auto-episodic turn log.
    rated = {"n": 0}
    orig_rate = memory._rate_importance

    def spy_rate(text):
        rated["n"] += 1
        return orig_rate(text)

    monkeypatch.setattr(memory, "_rate_importance", spy_rate)

    memory.remember_turn("My budget is 1200 pcm near UCL", "Noted!", user_id="user-A")

    recs = _episodic_records(memory)
    assert len(recs) == 1
    _id, meta = recs[0]
    assert int(meta["importance"]) == am_mod._AUTO_EPISODIC_IMPORTANCE
    assert rated["n"] == 0  # importance was passed in, so no LLM rating happened


def test_explicit_remember_still_rates_episodic_importance(memory, monkeypatch):
    # The memory-as-tools explicit path (importance=None) must KEEP LLM rating.
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "9")
    rated = {"n": 0}

    def spy_rate(text):
        rated["n"] += 1
        return 9

    monkeypatch.setattr(memory, "_rate_importance", spy_rate)
    memory.add("User insists on a ground-floor flat", "episodic", user_id="user-A")
    assert rated["n"] == 1


# ------------------------------------------------------------- triviality gate

@pytest.mark.parametrize("msg", [
    "hi", "Hello!", "thanks", "ok", "OK.", "sure", "yes", "no",
    "你好", "谢谢", "好的", "嗯", "行吧",
    "search anyway", "search anyway please", "先不要搜索", "直接搜索",
    "ok cool",  # short ack
])
def test_trivial_messages_skip_extraction(memory, monkeypatch, msg):
    extract = {"n": 0}
    monkeypatch.setattr(memory, "_extract_facts",
                        lambda u, a: (extract.__setitem__("n", extract["n"] + 1) or []))
    memory.remember_turn(msg, "Sure!", user_id="user-A")
    assert extract["n"] == 0
    # the episodic turn log is still written even for trivial messages
    assert len(_episodic_records(memory)) == 1


@pytest.mark.parametrize("msg", [
    "My budget is 1200 pcm and I want to be near UCL",
    "I need a studio in Manchester with a gym by September",
    "我的预算是每月1200镑住在曼彻斯特大学附近",  # short but informational CJK
])
def test_informational_messages_run_extraction(memory, monkeypatch, msg):
    extract = {"n": 0}
    monkeypatch.setattr(memory, "_extract_facts",
                        lambda u, a: (extract.__setitem__("n", extract["n"] + 1) or []))
    memory.remember_turn(msg, "Sure!", user_id="user-A")
    assert extract["n"] == 1


def test_is_trivial_helper_boundaries():
    assert am_mod._is_trivial_for_extraction("") is True
    assert am_mod._is_trivial_for_extraction("   ") is True
    assert am_mod._is_trivial_for_extraction("hi!!!") is True
    assert am_mod._is_trivial_for_extraction("好的。") is True
    assert am_mod._is_trivial_for_extraction("search anyway") is True
    assert am_mod._is_trivial_for_extraction("I have a budget of 1500 pcm") is False


# ------------------------------------------------------------ episodic cap

def _seed_episodic(memory, n, user_id="user-A", base_hour=0):
    """Insert n episodic records with strictly increasing created_at."""
    for i in range(n):
        ts = f"2026-07-17T{base_hour:02d}:{i // 60:02d}:{i % 60:02d}"
        memory.col.add(
            documents=[f"episode {i}"],
            metadatas=[{"mtype": "episodic", "user_id": user_id,
                        "created_at": ts, "importance": 5}],
            ids=[f"ep_{user_id}_{i}"],
        )


def test_episodic_cap_evicts_oldest(memory):
    _seed_episodic(memory, 8)
    memory._enforce_episodic_cap("user-A", cap=5)
    recs = _episodic_records(memory)
    assert len(recs) == 5
    kept_docs = memory.col.get(ids=[r[0] for r in recs])["documents"]
    # newest five kept: episodes 3..7
    assert set(kept_docs) == {f"episode {i}" for i in range(3, 8)}


def test_episodic_cap_noop_below_cap(memory):
    _seed_episodic(memory, 3)
    memory._enforce_episodic_cap("user-A", cap=5)
    assert len(_episodic_records(memory)) == 3


def test_episodic_cap_leaves_semantic_and_other_users(memory):
    _seed_episodic(memory, 8, user_id="user-A")
    memory.col.add(
        documents=["durable fact"],
        metadatas=[{"mtype": "semantic", "user_id": "user-A",
                    "created_at": "2026-07-17T00:00:00", "importance": 7}],
        ids=["sem_A_1"],
    )
    _seed_episodic(memory, 4, user_id="user-B")
    memory._enforce_episodic_cap("user-A", cap=5)
    # semantic untouched
    assert memory.col.get(ids=["sem_A_1"])["ids"] == ["sem_A_1"]
    # user-B episodic untouched
    assert len(_episodic_records(memory, "user-B")) == 4


def test_episodic_cap_constant_is_module_level():
    assert isinstance(am_mod.EPISODIC_MAX_PER_USER, int)
    assert am_mod.EPISODIC_MAX_PER_USER == 500


# ------------------------------------------------ maybe_reflect newest-by-created_at

def test_maybe_reflect_uses_newest_by_created_at(memory, monkeypatch):
    # Force a tiny reflection corpus so the ordering choice is observable.
    monkeypatch.setattr(am_mod, "REFLECT_CORPUS_SIZE", 2)

    captured = {"prompt": None}

    def fake_llm(prompt, *a, **k):
        captured["prompt"] = prompt
        return '{"insights": []}'

    monkeypatch.setattr(am_mod, "call_ollama", fake_llm)

    # Insert in SCRAMBLED order; created_at (not insertion order) must decide.
    rows = [
        ("OLD-A", "2026-07-17T01:00:00"),
        ("NEW-2", "2026-07-17T09:00:00"),
        ("OLD-B", "2026-07-17T02:00:00"),
        ("NEW-1", "2026-07-17T08:00:00"),
        ("OLD-C", "2026-07-17T03:00:00"),
    ]
    for doc, ts in rows:
        memory.col.add(
            documents=[doc],
            metadatas=[{"mtype": "episodic", "user_id": "user-A",
                        "created_at": ts, "importance": 5}],
            ids=[f"r_{doc}"],
        )

    memory._accum[("user-A", "default")] = am_mod.REFLECT_IMPORTANCE_THRESHOLD + 10
    memory.maybe_reflect("default", "user-A")

    prompt = captured["prompt"]
    assert prompt is not None
    assert "NEW-1" in prompt and "NEW-2" in prompt
    for older in ("OLD-A", "OLD-B", "OLD-C"):
        assert older not in prompt
