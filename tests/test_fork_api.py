"""Integration tests for the session-fork HTTP surface (FORK_CONTRACT.md §4.6).

These exercise the REAL app/app.py Flask routes through the test client — turn
lifecycle in /api/alex and /api/search_direct, the fork endpoint, snapshot-based
rehydrate, and parent/child independence.

Importing app.py runs its heavy module-level startup (RAG/FAISS/property load), so we do
it exactly once and set the isolating env vars FIRST (mirroring the pattern documented in
tests/test_app_input_validation.py: CONVERSATION_DB_PATH to a throwaway file before the
import). The LLM graph is never invoked — handle_with_react_agent / search_properties_impl
are monkeypatched per test, so no network / model is touched.
"""

import os
import sys
import tempfile
import uuid

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Pin the real source roots ahead of any stale shadow copies under tests/.
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

# --- Isolating env, set BEFORE importing app (Config.from_env + store are read at import).
os.environ["CONVERSATION_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="fork_api_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"          # tiny bundled demo CSV, no network
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"  # lets each test pin a stable X-User-Id
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import app as appmod  # noqa: E402 — heavy one-time import after env setup


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    # /api/alex is capped at 12 req/window keyed by client IP — and the test client is a
    # single loopback IP, so the whole suite would share one bucket. Disable it per test.
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture
def user():
    """A fresh, isolated user id per test (matches USER_ID_RE)."""
    return "u" + uuid.uuid4().hex[:16]


def _headers(user, **extra):
    h = {"X-User-Id": user}
    h.update(extra)
    return h


def _install_search_agent(monkeypatch, criteria=None, marker="place"):
    """Stub handle_with_react_agent with a deterministic 'search' turn that drives the
    REAL _write_back_turn (so criteria land in the snapshot). `marker` makes each turn's
    text unique so message-subset assertions are meaningful."""
    crit = dict(criteria or {"area": "London", "max_budget": 1500, "bedrooms": 2})

    async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                    request_id, ui_language="en", turn=None):
        recs = [{"address": f"1 {marker} St, London", "price": "£1500",
                 "travel_time": "20 min", "url": f"http://x/{marker}"}]
        appmod._write_back_turn(
            user_id, conversation_id, user_message, f"Reply about {marker}.", recs,
            accumulated_search_criteria=crit,
            turn_id=(turn or {}).get("id"), reply_language="en")
        return {"response_type": "search", "message": f"Reply about {marker}.",
                "recommendations": recs, "search_criteria": {"area": crit.get("area")}}

    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


def _install_raising_agent(monkeypatch):
    async def _fake(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake)


def _alex(client, user, message, cid=None, ui_language=None):
    body = {"message": message}
    if cid:
        body["conversation_id"] = cid
    if ui_language:
        body["ui_language"] = ui_language
    return client.post("/api/alex", json=body, headers=_headers(user))


def _messages(client, user, cid):
    r = client.get(f"/api/conversations/{cid}/messages", headers=_headers(user))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["messages"]


# ---------------------------------------------------------------------------
# Turn lifecycle + turn_id in responses
# ---------------------------------------------------------------------------

def test_alex_turn_creates_completed_turn_and_snapshot(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    r = _alex(client, user, "find me a flat in London")
    assert r.status_code == 200
    body = r.get_json()
    # turn_id present alongside conversation_id on the response.
    assert body["turn_id"]
    assert body["conversation_id"]
    cid, tid = body["conversation_id"], body["turn_id"]

    # A single completed turn exists.
    turns = appmod.conversation_store.list_turns(user, cid)
    assert len(turns) == 1
    assert turns[0]["id"] == tid
    assert turns[0]["status"] == "completed"
    assert turns[0]["completed_at"]

    # The post-turn snapshot captured the accumulated criteria (durable rehydrate source).
    snap = appmod.conversation_store.latest_snapshot(user, cid)
    assert snap is not None
    assert snap["accumulated_search_criteria"]["area"] == "London"
    assert snap["accumulated_search_criteria"]["max_budget"] == 1500
    assert snap["turn_id"] == tid
    assert snap["context_revision"] == 1  # first snapshot on the conversation

    # The assistant message row carries the turn_id; messages GET flows it through.
    msgs = _messages(client, user, cid)
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst and asst[-1]["turn_id"] == tid


def test_context_revision_increments_across_turns(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    r1 = _alex(client, user, "turn one")
    cid = r1.get_json()["conversation_id"]
    _alex(client, user, "turn two", cid=cid)
    _alex(client, user, "turn three", cid=cid)
    snap = appmod.conversation_store.latest_snapshot(user, cid)
    assert snap["context_revision"] == 3  # monotonic per-conversation counter


def test_alex_error_turn_is_failed_but_still_tagged(client, user, monkeypatch):
    _install_raising_agent(monkeypatch)
    r = _alex(client, user, "trigger a crash")
    assert r.status_code == 200  # always-200 contract
    body = r.get_json()
    assert body["response_type"] == "error"
    assert body["turn_id"]
    cid, tid = body["conversation_id"], body["turn_id"]

    turns = appmod.conversation_store.list_turns(user, cid)
    assert len(turns) == 1 and turns[0]["status"] == "failed"
    # No snapshot for a failed turn.
    assert appmod.conversation_store.latest_snapshot(user, cid) is None
    # The assistant error row still carries the turn_id.
    asst = [m for m in _messages(client, user, cid) if m["role"] == "assistant"]
    assert asst and asst[-1]["turn_id"] == tid


# ---------------------------------------------------------------------------
# /api/search_direct lifecycle
# ---------------------------------------------------------------------------

def test_search_direct_turn_and_turn_id(client, user, monkeypatch):
    async def _fake_search(**kwargs):
        return {"success": True, "status": "ok",
                "recommendations": [{"address": "9 Direct Rd, Leeds", "price": "£1400",
                                     "travel_time": "15 min", "url": "http://x/direct"}],
                "summary": "Found 1.", "search_criteria": {"area": "Leeds"},
                "area_recommendations": []}
    monkeypatch.setattr(appmod, "search_properties_impl", _fake_search)

    r = client.post("/api/search_direct",
                    json={"criteria": {"area": "Leeds", "max_budget": 1400}},
                    headers=_headers(user))
    assert r.status_code == 200
    body = r.get_json()
    assert body["response_type"] == "search"
    assert body["turn_id"]
    cid, tid = body["conversation_id"], body["turn_id"]

    turns = appmod.conversation_store.list_turns(user, cid)
    assert len(turns) == 1 and turns[0]["status"] == "completed"
    snap = appmod.conversation_store.latest_snapshot(user, cid)
    assert snap and snap["accumulated_search_criteria"]["area"] == "Leeds"


# ---------------------------------------------------------------------------
# Fork happy paths
# ---------------------------------------------------------------------------

def test_fork_from_latest(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hello one").get_json()["conversation_id"]
    _alex(client, user, "hello two", cid=cid)
    parent_msgs = _messages(client, user, cid)

    r = client.post(f"/api/conversations/{cid}/fork", json={}, headers=_headers(user))
    assert r.status_code == 201
    j = r.get_json()
    assert j["idempotent"] is False
    child = j["conversation"]
    assert child["parent_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["root_conversation_id"] == cid
    assert child["forked_from_turn_id"]

    # Child inherited the full transcript up to the (latest) fork turn.
    child_msgs = _messages(client, user, child["id"])
    assert len(child_msgs) == len(parent_msgs)


def test_fork_from_explicit_earlier_turn_excludes_later(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "message T1").get_json()["conversation_id"]
    _alex(client, user, "message T2", cid=cid)
    t3 = _alex(client, user, "message T3", cid=cid).get_json()["turn_id"]
    _alex(client, user, "message T4", cid=cid)  # must be excluded by a T3 fork

    r = client.post(f"/api/conversations/{cid}/fork",
                    json={"after_turn_id": t3}, headers=_headers(user))
    assert r.status_code == 201
    child = r.get_json()["conversation"]

    child_msgs = _messages(client, user, child["id"])
    contents = " ".join(m["content"] for m in child_msgs)
    assert "message T3" in contents
    assert "message T4" not in contents  # T4 happened after the fork point
    # 3 turns * (user + assistant) = 6 inherited messages.
    assert len(child_msgs) == 6
    # Child turn history is the 3 completed turns copied over (fresh ids).
    child_turns = appmod.conversation_store.list_turns(user, child["id"])
    assert len(child_turns) == 3
    assert t3 not in {t["id"] for t in child_turns}  # copied with fresh ids


def test_fork_title_override(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/fork",
                    json={"title": "My branch"}, headers=_headers(user))
    assert r.status_code == 201
    assert r.get_json()["conversation"]["title"] == "My branch"


def test_fork_of_fork_increments_depth(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "gen0").get_json()["conversation_id"]
    c1 = client.post(f"/api/conversations/{cid}/fork", json={}, headers=_headers(user))
    child1 = c1.get_json()["conversation"]
    # Give the child a completed turn so it can itself be forked.
    _alex(client, user, "gen1 turn", cid=child1["id"])
    c2 = client.post(f"/api/conversations/{child1['id']}/fork", json={}, headers=_headers(user))
    child2 = c2.get_json()["conversation"]
    assert child2["branch_depth"] == 2
    assert child2["parent_conversation_id"] == child1["id"]
    assert child2["root_conversation_id"] == cid


# ---------------------------------------------------------------------------
# Fork error codes
# ---------------------------------------------------------------------------

def test_fork_unknown_conversation_404(client, user):
    r = client.post("/api/conversations/does-not-exist/fork", json={}, headers=_headers(user))
    assert r.status_code == 404
    assert r.get_json()["code"] == "conversation_not_found"


def test_fork_no_completed_turn_400(client, user, monkeypatch):
    _install_raising_agent(monkeypatch)
    cid = _alex(client, user, "will fail").get_json()["conversation_id"]  # only a failed turn
    r = client.post(f"/api/conversations/{cid}/fork", json={}, headers=_headers(user))
    assert r.status_code == 400
    assert r.get_json()["code"] == "no_completed_turn"


def test_fork_turn_not_found_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/fork",
                    json={"after_turn_id": "deadbeef"}, headers=_headers(user))
    assert r.status_code == 400
    assert r.get_json()["code"] == "turn_not_found"


def test_fork_turn_not_in_conversation_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid_a = _alex(client, user, "conv A").get_json()["conversation_id"]
    resp_b = _alex(client, user, "conv B")
    cid_b, tid_b = resp_b.get_json()["conversation_id"], resp_b.get_json()["turn_id"]
    # Fork A but point at B's turn.
    r = client.post(f"/api/conversations/{cid_a}/fork",
                    json={"after_turn_id": tid_b}, headers=_headers(user))
    assert r.status_code == 400
    assert r.get_json()["code"] == "turn_not_in_conversation"
    assert cid_b  # (B exists; silences lint)


def test_fork_turn_not_completed_400(client, user, monkeypatch):
    _install_raising_agent(monkeypatch)
    resp = _alex(client, user, "failing turn")
    cid, failed_tid = resp.get_json()["conversation_id"], resp.get_json()["turn_id"]
    r = client.post(f"/api/conversations/{cid}/fork",
                    json={"after_turn_id": failed_tid}, headers=_headers(user))
    assert r.status_code == 400
    assert r.get_json()["code"] == "turn_not_completed"


def test_fork_bad_after_turn_id_type_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/fork",
                    json={"after_turn_id": 123}, headers=_headers(user))
    assert r.status_code == 400  # ApiError (generic), not a fork code


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_fork_idempotency_same_key_one_child(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    h = _headers(user, **{"Idempotency-Key": "fork-key-1"})
    r1 = client.post(f"/api/conversations/{cid}/fork", json={}, headers=h)
    r2 = client.post(f"/api/conversations/{cid}/fork", json={}, headers=h)
    assert r1.status_code == 201 and r1.get_json()["idempotent"] is False
    assert r2.status_code == 200 and r2.get_json()["idempotent"] is True
    assert r1.get_json()["conversation"]["id"] == r2.get_json()["conversation"]["id"]
    # Exactly one child exists.
    children = [c for c in appmod.conversation_store.list_conversations(user)
                if c["parent_conversation_id"] == cid]
    assert len(children) == 1


def test_fork_different_keys_two_children(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r1 = client.post(f"/api/conversations/{cid}/fork", json={},
                     headers=_headers(user, **{"Idempotency-Key": "k-a"}))
    r2 = client.post(f"/api/conversations/{cid}/fork", json={},
                     headers=_headers(user, **{"Idempotency-Key": "k-b"}))
    assert r1.get_json()["conversation"]["id"] != r2.get_json()["conversation"]["id"]
    children = [c for c in appmod.conversation_store.list_conversations(user)
                if c["parent_conversation_id"] == cid]
    assert len(children) == 2


# ---------------------------------------------------------------------------
# Parent / child independence (both directions)
# ---------------------------------------------------------------------------

def test_parent_and_child_are_independent(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "shared history").get_json()["conversation_id"]
    child = client.post(f"/api/conversations/{cid}/fork", json={},
                        headers=_headers(user)).get_json()["conversation"]
    child_id = child["id"]

    child_msgs_before = _messages(client, user, child_id)
    parent_msgs_before = _messages(client, user, cid)

    # Add a turn to the PARENT → child transcript is unchanged.
    _alex(client, user, "parent-only follow-up", cid=cid)
    assert _messages(client, user, child_id) == child_msgs_before
    assert len(_messages(client, user, cid)) == len(parent_msgs_before) + 2

    # Add a turn to the CHILD → parent transcript is unchanged.
    parent_after_parent_turn = _messages(client, user, cid)
    _alex(client, user, "child-only follow-up", cid=child_id)
    assert _messages(client, user, cid) == parent_after_parent_turn
    child_contents = " ".join(m["content"] for m in _messages(client, user, child_id))
    assert "child-only follow-up" in child_contents
    assert "parent-only follow-up" not in child_contents


# ---------------------------------------------------------------------------
# Restart consistency — snapshot rehydrate restores criteria on a fresh process
# ---------------------------------------------------------------------------

def test_restart_restores_criteria_from_snapshot(client, user, monkeypatch):
    _install_search_agent(monkeypatch, criteria={"area": "Manchester", "max_budget": 900,
                                                 "bedrooms": 1})
    cid = _alex(client, user, "search manchester").get_json()["conversation_id"]

    # Simulate a process restart: brand-new store handle on the SAME sqlite file and a
    # cold SessionStore. _get_session must rebuild criteria purely from the durable snapshot.
    from uk_rent_agent.web.conversation_store import ConversationStore
    from uk_rent_agent.web.session_store import SessionStore
    fresh_store = ConversationStore(appmod.conversation_store.db_path)
    fresh_sessions = SessionStore()
    monkeypatch.setattr(appmod, "conversation_store", fresh_store)
    monkeypatch.setattr(appmod, "_session_store", fresh_sessions)

    sess = appmod._get_session(user, cid)
    crit = sess.persistent_state["accumulated_search_criteria"]
    assert crit["area"] == "Manchester"
    assert crit["max_budget"] == 900
    assert crit["bedrooms"] == 1
    fresh_store.close()


# ---------------------------------------------------------------------------
# Listing endpoints carry the new fields
# ---------------------------------------------------------------------------

def test_conversation_listing_includes_lineage_fields(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    client.post(f"/api/conversations/{cid}/fork", json={}, headers=_headers(user))
    convs = client.get("/api/conversations", headers=_headers(user)).get_json()["conversations"]
    assert convs, "expected at least the parent + child"
    for c in convs:
        for k in ("parent_conversation_id", "forked_from_turn_id",
                  "root_conversation_id", "branch_depth"):
            assert k in c


def test_turns_listing_endpoint(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.get(f"/api/conversations/{cid}/turns", headers=_headers(user))
    assert r.status_code == 200
    turns = r.get_json()["turns"]
    assert len(turns) == 1 and turns[0]["status"] == "completed"
