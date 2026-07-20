"""Canary-rollout in-repo support (Shuhan's design, 2026-07-20).

Covers the four contract points:
  1. Sticky per-conversation arch assignment persisted at CREATION and immutable across turns.
  2. Emergency-rollback reconciliation: a stored arch != serving arch logs canary.arch_mismatch
     and overwrites the stored assignment.
  3. X-Agent-Arch / X-Agent-Version response headers.
  4. Exactly one canary.turn structured record per completed turn on BOTH archs.

Mirrors tests/test_fork_api.py: the REAL Flask routes are exercised through the test client;
handle_with_react_agent / search_properties_impl are monkeypatched so no LLM/network runs. The
process constants (appmod.AGENT_ARCH / APP_CANDIDATE_SHA / DEEPSEEK_STRICT) are read once at
import, so tests monkeypatch the module globals — the app code resolves them as globals at call
time, so a monkeypatched value flows through creation, reconciliation, and the telemetry record.
"""

import json
import logging
import os
import sys
import tempfile
import uuid

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ["CONVERSATION_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="canary_api_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import app as appmod  # noqa: E402 — heavy one-time import after env setup


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture
def user():
    return "u" + uuid.uuid4().hex[:16]


def _headers(user, **extra):
    h = {"X-User-Id": user}
    h.update(extra)
    return h


def _install_search_agent(monkeypatch, marker="place"):
    """Stub the agent with a deterministic 'search' turn (drives the real _write_back_turn)."""
    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        recs = [{"address": f"1 {marker} St, London", "price": "£1500",
                 "travel_time": "20 min", "url": f"http://x/{marker}"}]
        appmod._write_back_turn(
            user_id, conversation_id, user_message, f"Reply about {marker}.", recs,
            accumulated_search_criteria={"area": "London", "max_budget": 1500},
            turn_id=(turn or {}).get("id"), reply_language="en")
        return {"response_type": "search", "message": f"Reply about {marker}.",
                "recommendations": recs, "search_criteria": {"area": "London"}}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


def _install_fc_agent(monkeypatch, final_state):
    """Stub the agent to publish fc-side signals derived from a synthetic final_state — exactly
    as the real handle_with_react_agent does after the graph runs."""
    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        appmod._write_back_turn(
            user_id, conversation_id, user_message, "fc reply", [],
            turn_id=(turn or {}).get("id"), reply_language="en")
        appmod._turn_fc_signals.set(appmod._build_fc_signals(final_state))
        return {"response_type": "chat", "message": "fc reply"}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


def _alex(client, user, message, cid=None):
    body = {"message": message}
    if cid:
        body["conversation_id"] = cid
    return client.post("/api/alex", json=body, headers=_headers(user))


def _canary_turns(caplog):
    out = []
    for rec in caplog.records:
        if rec.name != "canary":
            continue
        try:
            obj = json.loads(rec.getMessage())
        except Exception:
            continue
        if obj.get("event") == "canary.turn":
            out.append(obj)
    return out


def _mismatch_records(caplog):
    out = []
    for rec in caplog.records:
        if rec.name != "canary":
            continue
        try:
            obj = json.loads(rec.getMessage())
        except Exception:
            continue
        if obj.get("event") == "canary.arch_mismatch":
            out.append(obj)
    return out


_REQUIRED_TURN_KEYS = {
    "event", "agent_arch", "candidate_sha", "strict", "request_id", "conversation_id",
    "user_id", "soft_wrapped", "partial", "tool_budget_timeout", "security_audit",
    "turn_latency_ms", "llm_calls", "tool_batches",
}


# ---------------------------------------------------------------------------
# 1. Sticky assignment persisted at creation + immutable across turns
# ---------------------------------------------------------------------------

def test_assignment_persisted_at_creation_legacy(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "shaLEGACY")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", False)
    _install_search_agent(monkeypatch)

    cid = _alex(client, user, "find me a flat").get_json()["conversation_id"]
    conv = appmod.conversation_store.get_conversation(user, cid)
    assert conv["agent_arch"] == "legacy"
    assert conv["agent_version"] == "shaLEGACY"
    assert conv["strict"] is False


def test_assignment_persisted_at_creation_fc(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    _install_search_agent(monkeypatch)

    cid = _alex(client, user, "find me a flat").get_json()["conversation_id"]
    conv = appmod.conversation_store.get_conversation(user, cid)
    assert conv["agent_arch"] == "fc_loop"
    assert conv["agent_version"] == "7db03e7"
    assert conv["strict"] is True


def test_assignment_immutable_across_turns_same_process(client, user, monkeypatch):
    """Same process arch across turns → stored assignment never changes (the scaling case:
    sticky routing keeps in-flight conversations on their pool, so no flip)."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    _install_search_agent(monkeypatch)

    cid = _alex(client, user, "turn one").get_json()["conversation_id"]
    _alex(client, user, "turn two", cid=cid)
    _alex(client, user, "turn three", cid=cid)

    conv = appmod.conversation_store.get_conversation(user, cid)
    assert conv["agent_arch"] == "fc_loop"
    assert conv["agent_version"] == "7db03e7"
    assert conv["strict"] is True


def test_explicit_create_endpoint_stamps_assignment(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    r = client.post("/api/conversations", json={"title": "x"}, headers=_headers(user))
    assert r.status_code == 201
    conv = r.get_json()["conversation"]
    assert conv["agent_arch"] == "fc_loop"
    assert conv["agent_version"] == "7db03e7"


def test_fork_inherits_assignment(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    _install_search_agent(monkeypatch)

    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/fork", json={}, headers=_headers(user))
    assert r.status_code == 201
    child = r.get_json()["conversation"]
    # Branch continues the same family → inherits the source's sticky assignment even if the
    # forking process's own env were different.
    assert child["agent_arch"] == "fc_loop"
    assert child["agent_version"] == "7db03e7"
    assert child["strict"] is True


# ---------------------------------------------------------------------------
# 2. Emergency-rollback reconciliation (arch_mismatch)
# ---------------------------------------------------------------------------

def test_arch_mismatch_overwrites_and_warns(client, user, monkeypatch, caplog):
    # Conversation is CREATED while the process serves fc_loop.
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "turn on fc").get_json()["conversation_id"]

    # Emergency rollback: a rebuilt LEGACY process now serves the same conversation.
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "shaLEGACY")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", False)

    with caplog.at_level(logging.WARNING, logger="canary"):
        _alex(client, user, "next turn on legacy", cid=cid)

    # Structured warning emitted with both archs.
    mm = _mismatch_records(caplog)
    assert len(mm) == 1
    assert mm[0]["stored_arch"] == "fc_loop"
    assert mm[0]["serving_arch"] == "legacy"
    assert mm[0]["conversation_id"] == cid

    # Stored assignment overwritten to THIS process's arch so subsequent turns are consistent.
    conv = appmod.conversation_store.get_conversation(user, cid)
    assert conv["agent_arch"] == "legacy"
    assert conv["agent_version"] == "shaLEGACY"
    assert conv["strict"] is False


def test_no_mismatch_when_arch_matches(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "one").get_json()["conversation_id"]
    with caplog.at_level(logging.WARNING, logger="canary"):
        _alex(client, user, "two", cid=cid)
    assert _mismatch_records(caplog) == []


# ---------------------------------------------------------------------------
# 3. Response headers
# ---------------------------------------------------------------------------

def test_headers_present_on_alex(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    _install_search_agent(monkeypatch)
    r = _alex(client, user, "hi")
    assert r.headers["X-Agent-Arch"] == "fc_loop"
    assert r.headers["X-Agent-Version"] == "7db03e7"
    assert r.headers["X-Request-Id"]


def test_headers_present_on_crud(client, user, monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "shaLEGACY")
    r = client.get("/api/conversations", headers=_headers(user))
    assert r.headers["X-Agent-Arch"] == "legacy"
    assert r.headers["X-Agent-Version"] == "shaLEGACY"


# ---------------------------------------------------------------------------
# 4. Per-turn canary.turn record on both archs + search_direct
# ---------------------------------------------------------------------------

def test_canary_turn_record_legacy(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "shaLEGACY")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", False)
    _install_search_agent(monkeypatch)

    with caplog.at_level(logging.INFO, logger="canary"):
        cid = _alex(client, user, "hi").get_json()["conversation_id"]

    turns = _canary_turns(caplog)
    assert len(turns) == 1
    rec = turns[0]
    assert _REQUIRED_TURN_KEYS.issubset(rec.keys())
    assert rec["agent_arch"] == "legacy"
    assert rec["candidate_sha"] == "shaLEGACY"
    assert rec["strict"] is False
    assert rec["conversation_id"] == cid
    assert rec["user_id"] == user
    assert rec["security_audit"] == {"denied_writes": 0}
    assert isinstance(rec["turn_latency_ms"], (int, float))


def test_canary_turn_record_fc_with_signals(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    # Synthetic fc final_state: soft-wrapped turn, a partial search, a budget kill, a denied write.
    final_state = {
        "soft_wrapped": True,
        "loop_turn": 3,
        "tool_artifacts": [
            {"turn": 0, "tool": "search_properties", "raw_data": {"partial": True}},
            {"turn": 1, "tool": "check_safety", "timed_out": True, "raw_data": None},
            {"turn": 1, "tool": "remember", "denied": True, "raw_data": None},
        ],
    }
    _install_fc_agent(monkeypatch, final_state)

    with caplog.at_level(logging.INFO, logger="canary"):
        _alex(client, user, "hi")

    turns = _canary_turns(caplog)
    assert len(turns) == 1
    rec = turns[0]
    assert _REQUIRED_TURN_KEYS.issubset(rec.keys())
    assert rec["agent_arch"] == "fc_loop"
    assert rec["strict"] is True
    assert rec["soft_wrapped"] is True
    assert rec["partial"] is True
    assert rec["tool_budget_timeout"] is True
    assert rec["security_audit"] == {"denied_writes": 1}
    assert rec["llm_calls"] == 3
    assert rec["tool_batches"] == 2  # distinct artifact turns {0, 1}


def test_canary_turn_record_crashed_turn_defaults(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")

    async def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(appmod, "handle_with_react_agent", _boom)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = _alex(client, user, "crash please")
    assert r.status_code == 200  # always-200 contract
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    rec = turns[0]
    # Crash → no fc signals published → safe defaults, but the record is still emitted.
    assert rec["soft_wrapped"] is False
    assert rec["partial"] is False
    assert rec["tool_budget_timeout"] is False
    assert rec["llm_calls"] is None
    assert rec["tool_batches"] is None


def test_search_direct_emits_canary_turn(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")

    async def _fake_search(**kwargs):
        return {"success": True, "status": "ok",
                "recommendations": [{"address": "9 Direct Rd, Leeds", "price": "£1400",
                                     "travel_time": "15 min", "url": "http://x/direct"}],
                "summary": "Found 1.", "search_criteria": {"area": "Leeds"},
                "area_recommendations": []}
    monkeypatch.setattr(appmod, "search_properties_impl", _fake_search)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/search_direct",
                        json={"criteria": {"area": "Leeds", "max_budget": 1400}},
                        headers=_headers(user))
    assert r.status_code == 200
    turns = _canary_turns(caplog)
    assert len(turns) == 1
    rec = turns[0]
    assert _REQUIRED_TURN_KEYS.issubset(rec.keys())
    assert rec["agent_arch"] == "legacy"
    # Deterministic path: no fc graph, so fc fields default.
    assert rec["llm_calls"] is None and rec["tool_batches"] is None


# ---------------------------------------------------------------------------
# _build_fc_signals unit derivation
# ---------------------------------------------------------------------------

def test_build_fc_signals_legacy_arch_nulls(monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")
    sig = appmod._build_fc_signals({"loop_turn": 5, "tool_artifacts": [{"turn": 0}]})
    assert sig["llm_calls"] is None
    assert sig["tool_batches"] is None
    assert sig["soft_wrapped"] is False


def test_build_fc_signals_robust_to_junk(monkeypatch):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    sig = appmod._build_fc_signals({"tool_artifacts": "not-a-list"})
    assert sig["partial"] is False
    assert sig["tool_budget_timeout"] is False
    assert sig["security_audit"] == {"denied_writes": 0}
    assert sig["tool_batches"] == 0
