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
from uk_rent_agent.config import Config  # noqa: E402


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
        # Publish per-turn signals exactly as the real handle_with_react_agent does
        # after the graph returns. Without this the stub models a turn that COMPLETED
        # but never reported, which v2 correctly treats as unobserved (-> HOLD); the
        # stub would then be exercising crash semantics by accident.
        appmod._turn_fc_signals.set(appmod._build_fc_signals({}))
        return {"response_type": "search", "message": f"Reply about {marker}.",
                "recommendations": recs, "search_criteria": {"area": "London"}}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


def _install_fc_agent(monkeypatch, final_state, write_decision=None):
    """Stub the agent to publish fc-side signals derived from a synthetic final_state — exactly
    as the real handle_with_react_agent does after the graph runs.

    ``write_decision`` is recorded mid-turn, where the graph's policy branch records
    it, so the security counters come from the same place they do in production."""
    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        if write_decision:
            appmod.turn_observations.note_write_decision(**write_decision)
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


# Schema v2: the raw user_id is replaced by an HMAC hash + status, and the
# free-form security_audit by a structured `security` object.
_REQUIRED_TURN_KEYS = {
    "event", "telemetry_schema_version", "ts", "endpoint", "agent_arch",
    "candidate_sha", "strict", "request_id", "conversation_id",
    "user_id_hash_status", "http_status", "turn_outcome",
    "soft_wrapped", "partial", "tool_budget_timeout", "security",
    "dsml_blocked", "dsml_leak", "provider_schema_400_count",
    "turn_latency_ms", "llm_calls", "tool_batches", "llm_usage_status",
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
    assert rec["user_id_hash"] != user, "raw user id must never be emitted"
    assert rec["endpoint"] == "alex"
    assert rec["turn_outcome"] == "ok"
    assert rec["security"]["denied_write_count"] == 0
    assert isinstance(rec["turn_latency_ms"], (int, float))


def test_canary_turn_record_fc_with_signals(client, user, monkeypatch, caplog):
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "7db03e7")
    monkeypatch.setattr(appmod, "DEEPSEEK_STRICT", True)
    monkeypatch.setattr(appmod.turn_observations, "_write_auditors", {"fc_loop", "legacy"})
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
    # The denial is recorded where the policy makes it, not inferred from the artifact
    # above. That distinction is the whole of item 3: legacy emits no artifacts at all,
    # so an artifact-derived count handed the control pool a permanent, fabricated 0.
    _install_fc_agent(monkeypatch, final_state, write_decision={
        "tool": "remember", "decision": "denied_tainted", "context_tainted": True,
        "user_authorized": False, "audit_key": "remember:abc"})

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
    assert rec["security"]["denied_write_count"] == 1
    assert rec["security"]["tainted_write_executed_count"] == 0, \
        "denied is not executed: a refusal is the control working, not a breach"
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


def test_response_serialization_failure_records_500_not_200(client, user, monkeypatch, caplog):
    """The record must report the status the USER received, not the one we hoped for.

    Regression for the response-boundary ordering bug: when the canary event was
    emitted BEFORE jsonify(), a payload that failed to serialise produced a record
    saying http_status=200 / outcome=ok AND set g.canary_emitted, which then made the
    500 handler skip its own record. The turn was permanently logged as a success
    while the client got a 500 — the single worst failure mode for a gate whose whole
    job is counting server errors. Emitting after serialisation makes the boundary
    handler the sole emitter for this path.
    """
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod, "APP_CANDIDATE_SHA", "shaSER")
    # Pin the observer state explicitly. It is a module-level global that stays True
    # once any LLM client has been built, so leaving it ambient made this assertion
    # depend on which tests ran first — the count below flipped between null and 0.
    monkeypatch.setattr(appmod.turn_observations, "_observer_installed", True)
    monkeypatch.setattr(appmod.turn_observations, "_write_auditors", {"fc_loop", "legacy"})

    class _Unserializable:
        pass

    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        appmod._turn_fc_signals.set(appmod._build_fc_signals({}))
        # A perfectly normal-looking turn that Flask cannot serialise.
        return {"response_type": "chat", "message": "ok", "junk": _Unserializable()}
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = _alex(client, user, "please explode at the boundary")

    assert r.status_code == 500, "precondition: jsonify must actually fail here"
    turns = _canary_turns(caplog)
    assert len(turns) == 1, f"expected exactly one record, got {len(turns)}: {turns}"
    rec = turns[0]
    assert rec["http_status"] == 500
    assert rec["turn_outcome"] == "server_error"
    assert rec["endpoint"] == "alex"
    assert rec["agent_arch"] == "fc_loop"
    # The write audit is accumulated out-of-band at the policy decision point, so it
    # survives the boundary failure exactly the way the provider counter below does:
    # "no write decision was recorded" now means no write ever reached the gate.
    # Before item 3 this asserted None, because the only source was a final_state the
    # failure had already destroyed.
    assert rec["security"]["forbidden_write_executed_count"] == 0
    assert rec["security"]["tainted_write_executed_count"] == 0
    # Provider errors are the exception, and that asymmetry is the point of Layer B.
    # They are accumulated out-of-band as each LLM call happens, not derived from a
    # final_state that no longer exists — so this 0 is a real observation ("every call
    # this turn made was watched, none returned a schema 400"), not the fabricated 0
    # the null used to guard against. Before Layer B this asserted None.
    assert rec["provider_schema_400_count"] == 0


def test_search_direct_serialization_failure_records_500(client, user, monkeypatch, caplog):
    """Same ordering guarantee on the deterministic endpoint."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", "legacy")

    class _Unserializable:
        pass

    async def _fake_search(**kwargs):
        return {"success": True, "status": "ok", "recommendations": [],
                "summary": "Found 0.", "search_criteria": {"area": "Leeds"},
                # This endpoint rebuilds the payload from named keys, so the poison
                # has to sit in one it actually copies through — and one that is NOT
                # persisted, so the failure lands squarely on jsonify().
                "area_recommendations": [_Unserializable()]}
    monkeypatch.setattr(appmod, "search_properties_impl", _fake_search)

    with caplog.at_level(logging.INFO, logger="canary"):
        r = client.post("/api/search_direct",
                        json={"criteria": {"area": "Leeds", "max_budget": 1400}},
                        headers=_headers(user))

    assert r.status_code == 500
    turns = _canary_turns(caplog)
    assert len(turns) == 1, f"expected exactly one record, got {len(turns)}: {turns}"
    assert turns[0]["http_status"] == 500
    assert turns[0]["turn_outcome"] == "server_error"
    assert turns[0]["endpoint"] == "search_direct"


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
    assert sig["tool_batches"] == 0


def test_build_fc_signals_reports_null_security_without_instrumentation(monkeypatch):
    """Null, not 0. A 0 here would claim "the write audit ran and found nothing",
    which is the fabricated observation item 3 removed.

    _write_auditors is pinned rather than left ambient: it is a module-level global,
    so whether an earlier test imported an arch module decided the answer and the
    assertion flipped between null and 0 depending on run order.
    """
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod.turn_observations, "_write_auditors", set())
    sig = appmod._build_fc_signals({"tool_artifacts": "not-a-list"})
    assert sig["security"]["denied_write_count"] is None
    assert sig["security"]["tainted_write_executed_count"] is None
    assert sig["security"]["forbidden_write_executed_count"] is None


def test_a_denied_artifact_alone_no_longer_produces_a_security_count(monkeypatch):
    """Pins where the count comes from. The artifact says a write was denied, but no
    policy decision was recorded — on legacy, which emits no artifacts at all, the old
    artifact-derived path is what made every turn report a clean audit it never ran."""
    monkeypatch.setattr(appmod, "AGENT_ARCH", "fc_loop")
    monkeypatch.setattr(appmod.turn_observations, "_write_auditors", {"fc_loop"})
    appmod.turn_observations.begin_turn()
    try:
        sig = appmod._build_fc_signals({"tool_artifacts": [
            {"turn": 1, "tool": "remember", "denied": True, "raw_data": None}]})
        assert sig["security"]["denied_write_count"] == 0
    finally:
        appmod.turn_observations.end_turn()


# ---------------------------------------------------------------------------
# DEFECT 1 — checkpoint DB env-var wiring (Config.from_env)
# ---------------------------------------------------------------------------

def test_config_reads_checkpoint_db_path(monkeypatch, tmp_path):
    """CHECKPOINT_DB_PATH (documented ops interface) is honoured as the primary env var."""
    p = tmp_path / "fc" / "checkpoints.sqlite3"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(p))
    monkeypatch.delenv("CHECKPOINT_PATH", raising=False)
    assert Config.from_env().checkpoint_path == p


def test_config_checkpoint_path_fallback(monkeypatch, tmp_path):
    """CHECKPOINT_PATH still works as the back-compat fallback when DB var is unset."""
    p = tmp_path / "legacy" / "checkpoints.sqlite3"
    monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
    monkeypatch.setenv("CHECKPOINT_PATH", str(p))
    assert Config.from_env().checkpoint_path == p


def test_config_checkpoint_db_wins_when_both_set(monkeypatch, tmp_path, capsys):
    """When both are set and differ, CHECKPOINT_DB_PATH wins and a warning is printed."""
    db = tmp_path / "db.sqlite3"
    legacy = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))
    monkeypatch.setenv("CHECKPOINT_PATH", str(legacy))
    cfg = Config.from_env()
    assert cfg.checkpoint_path == db
    assert "CHECKPOINT_DB_PATH" in capsys.readouterr().out


def test_config_checkpoint_default(monkeypatch):
    """Neither env var set → default under <root>/.runtime/checkpoints.sqlite3."""
    monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
    monkeypatch.delenv("CHECKPOINT_PATH", raising=False)
    cp = Config.from_env().checkpoint_path
    assert cp.name == "checkpoints.sqlite3"
    assert cp.parent.name == ".runtime"


# ---------------------------------------------------------------------------
# DEFECT 2 — canary telemetry file sink (_wire_canary_sink)
# ---------------------------------------------------------------------------

@pytest.fixture
def _restore_canary_sink(monkeypatch):
    """Re-wire the sink to its default after the test so a temp-path handler cannot leak into
    later tests. monkeypatch reverts CANARY_LOG_PATH first, then we rewire on the clean env."""
    yield
    monkeypatch.delenv("CANARY_LOG_PATH", raising=False)
    appmod._wire_canary_sink()


def test_canary_sink_writes_one_json_line(tmp_path, monkeypatch, user, _restore_canary_sink):
    logpath = tmp_path / "canary.jsonl"
    monkeypatch.setenv("CANARY_LOG_PATH", str(logpath))
    appmod._wire_canary_sink()

    appmod._emit_canary_turn(endpoint="alex", conversation_id="c-sink", user_id=user,
                             request_id="req-sink", http_status=200, turn_outcome="ok",
                             turn_latency_ms=12.3, fc_signals=None)
    for h in appmod._canary_logger.handlers:
        h.flush()

    lines = logpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert isinstance(rec, dict)
    assert rec["event"] == "canary.turn"
    assert _REQUIRED_TURN_KEYS.issubset(rec.keys())
    assert rec["request_id"] == "req-sink"
    assert rec["conversation_id"] == "c-sink"
    assert rec["user_id_hash"] != user


def test_canary_sink_idempotent_no_stacked_handlers(tmp_path, monkeypatch, _restore_canary_sink):
    logpath = tmp_path / "canary_idem.jsonl"
    monkeypatch.setenv("CANARY_LOG_PATH", str(logpath))
    appmod._wire_canary_sink()
    appmod._wire_canary_sink()
    appmod._wire_canary_sink()
    ours = [h for h in appmod._canary_logger.handlers
            if getattr(h, appmod._CANARY_SINK_MARKER, False)]
    assert len(ours) == 1


def test_canary_sink_disabled(tmp_path, monkeypatch, _restore_canary_sink):
    disabled_marker = tmp_path / "should_not_exist.jsonl"
    monkeypatch.setenv("CANARY_LOG_PATH", "off")
    appmod._wire_canary_sink()  # must not crash
    ours = [h for h in appmod._canary_logger.handlers
            if getattr(h, appmod._CANARY_SINK_MARKER, False)]
    assert ours == []
    # A turn emitted while disabled writes nothing to disk (and does not crash).
    appmod._emit_canary_turn(endpoint="alex", conversation_id="c", user_id="u",
                             request_id="r", http_status=200, turn_outcome="ok",
                             turn_latency_ms=1.0, fc_signals=None)
    assert not disabled_marker.exists()


# ---------------------------------------------------------------------------
# /health pool identity (Starlette-served, bypasses Flask's after_request)
# ---------------------------------------------------------------------------

def test_asgi_health_identity_headers(monkeypatch):
    """/health is served by Starlette directly, so it must derive the X-Agent-* headers
    from the loaded legacy app module — probing ops must see WHICH pool answered."""
    import types
    from uk_rent_agent.web import asgi

    fake = types.ModuleType("uk_rent_agent._legacy_web_app")
    fake.AGENT_ARCH = "fc_loop"
    fake.APP_CANDIDATE_SHA = "abc1234"
    monkeypatch.setitem(sys.modules, "uk_rent_agent._legacy_web_app", fake)
    headers = asgi._canary_identity()
    assert headers == {"X-Agent-Arch": "fc_loop", "X-Agent-Version": "abc1234"}


def test_asgi_health_identity_degrades_when_app_not_loaded(monkeypatch):
    from uk_rent_agent.web import asgi

    monkeypatch.delitem(sys.modules, "uk_rent_agent._legacy_web_app", raising=False)
    assert asgi._canary_identity() == {}
