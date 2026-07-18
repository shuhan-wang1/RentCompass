"""Integration tests for the message-edit (ChatGPT-style edit-and-resend) HTTP surface.

These exercise the REAL app/app.py Flask routes through the test client:
  * GET /api/conversations/<cid>/messages now carries turn_id on BOTH user and assistant rows;
  * POST /api/conversations/<cid>/edit_turn branches BEFORE a turn (incl. zero-inheritance for
    the first turn) and records version-group metadata;
  * GET /api/conversations/<cid>/version_map returns the family's version groups.

Same isolation pattern as tests/test_fork_api.py: heavy app import happens once after the
env vars are set, and handle_with_react_agent is monkeypatched so no LLM/network is touched.
"""

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
    tempfile.mkdtemp(prefix="edit_api_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ["ALLOW_LEGACY_CLIENT_USER_ID"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import app as appmod  # noqa: E402


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
    """Deterministic 'search' turn that drives the REAL _write_back_turn."""
    def _factory(mk):
        async def _fake(user_message, context, is_continuation, user_id, conversation_id,
                        request_id, ui_language="en", turn=None):
            recs = [{"address": f"1 {mk} St, London", "price": "£1500",
                     "travel_time": "20 min", "url": f"http://x/{mk}"}]
            appmod._write_back_turn(
                user_id, conversation_id, user_message, f"Reply about {mk}.", recs,
                accumulated_search_criteria={"area": "London"},
                turn_id=(turn or {}).get("id"), reply_language="en")
            return {"response_type": "search", "message": f"Reply about {mk}.",
                    "recommendations": recs, "search_criteria": {"area": "London"}}
        return _fake
    monkeypatch.setattr(appmod, "handle_with_react_agent", _factory(marker))


def _alex(client, user, message, cid=None):
    body = {"message": message}
    if cid:
        body["conversation_id"] = cid
    return client.post("/api/alex", json=body, headers=_headers(user))


def _messages(client, user, cid):
    r = client.get(f"/api/conversations/{cid}/messages", headers=_headers(user))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["messages"]


def _edit(client, user, cid, turn_id, **extra):
    body = {"turn_id": turn_id}
    body.update(extra)
    return client.post(f"/api/conversations/{cid}/edit_turn", json=body,
                       headers=_headers(user))


def _version_map(client, user, cid):
    r = client.get(f"/api/conversations/{cid}/version_map", headers=_headers(user))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["version_groups"]


# ---------------------------------------------------------------------------
# messages endpoint carries turn_id on user AND assistant rows
# ---------------------------------------------------------------------------

def test_messages_carry_turn_id_on_user_and_assistant(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    r = _alex(client, user, "find me a flat")
    cid, tid = r.get_json()["conversation_id"], r.get_json()["turn_id"]
    msgs = _messages(client, user, cid)
    assert len(msgs) == 2
    user_msg = [m for m in msgs if m["role"] == "user"][0]
    asst_msg = [m for m in msgs if m["role"] == "assistant"][0]
    # The frontend edits the USER bubble → it must carry the turn_id.
    assert user_msg["turn_id"] == tid
    assert asst_msg["turn_id"] == tid
    # turn_id is always a key (null for legacy), never absent.
    assert all("turn_id" in m for m in msgs)


# ---------------------------------------------------------------------------
# edit_turn: ordinary branch inherits everything BEFORE the edited turn
# ---------------------------------------------------------------------------

def test_edit_turn_inherits_strictly_before(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "message T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "message T2", cid=cid).get_json()["turn_id"]
    _alex(client, user, "message T3", cid=cid)  # after T2 → must be excluded

    r = _edit(client, user, cid, t2)
    assert r.status_code == 201
    j = r.get_json()
    assert j["idempotent"] is False
    child = j["conversation"]
    assert child["parent_conversation_id"] == cid
    assert child["root_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["fork_reason"] == "edit"
    assert child["edited_slot_turn_id"] == t2  # slot key = edited turn id in the origin

    contents = " ".join(m["content"] for m in _messages(client, user, child["id"]))
    assert "message T1" in contents       # inherited (before T2)
    assert "message T2" not in contents   # the edited turn itself is NOT inherited
    assert "message T3" not in contents   # after the edit point
    # T1 only: user + assistant = 2 messages, 1 inherited turn.
    assert len(_messages(client, user, child["id"])) == 2
    assert len(appmod.conversation_store.list_turns(user, child["id"])) == 1

    # Source is completely untouched (still 3 turns / 6 messages).
    assert len(_messages(client, user, cid)) == 6


def test_edit_turn_source_untouched_and_independent(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]
    child = _edit(client, user, cid, t2).get_json()["conversation"]

    before = _messages(client, user, cid)
    # Resend the rewritten message on the branch → source unchanged.
    _alex(client, user, "T2 rewritten", cid=child["id"])
    assert _messages(client, user, cid) == before
    child_contents = " ".join(m["content"] for m in _messages(client, user, child["id"]))
    assert "T2 rewritten" in child_contents
    assert "T2" not in child_contents.replace("T2 rewritten", "")


# ---------------------------------------------------------------------------
# edit_turn: first turn → zero-inheritance branch
# ---------------------------------------------------------------------------

def test_edit_first_turn_zero_inheritance(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    r1 = _alex(client, user, "the very first message")
    cid, t1 = r1.get_json()["conversation_id"], r1.get_json()["turn_id"]
    _alex(client, user, "second message", cid=cid)

    r = _edit(client, user, cid, t1)
    assert r.status_code == 201
    child = r.get_json()["conversation"]
    # Lineage preserved even though nothing is inherited.
    assert child["parent_conversation_id"] == cid
    assert child["root_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["forked_from_turn_id"] is None   # zero-inheritance marker
    assert child["edited_slot_turn_id"] == t1

    # No messages / turns copied.
    assert _messages(client, user, child["id"]) == []
    assert appmod.conversation_store.list_turns(user, child["id"]) == []

    # Zero-inheritance branch sees no ancestor context in its lineage.
    lineage = appmod.conversation_store.get_branch_lineage(user, child["id"])
    assert [e["conversation_id"] for e in lineage] == [child["id"]]


# ---------------------------------------------------------------------------
# Version groups + multi-level transitivity
# ---------------------------------------------------------------------------

def test_version_map_groups_original_and_edit(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]
    child = _edit(client, user, cid, t2).get_json()["conversation"]

    groups = _version_map(client, user, cid)
    assert list(groups.keys()) == [t2]
    members = groups[t2]
    # original (parent cid) then the edit branch, created_at ASC.
    ids = [m["conversation_id"] for m in members]
    assert ids == [cid, child["id"]]
    # each member entry has exactly the contracted shape.
    for m in members:
        assert set(m.keys()) == {"conversation_id", "created_at", "title"}
    # Ordering is by created_at ascending.
    assert members[0]["created_at"] <= members[1]["created_at"]
    # Same map is visible from the branch (family-wide, same root).
    assert _version_map(client, user, child["id"]) == groups


def test_version_map_transitivity_multi_level_edit(client, user, monkeypatch):
    """Editing the SAME position again on an edit branch must land in the SAME group."""
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]

    # First edit → branch B, then resend the rewritten 2nd message on B.
    b = _edit(client, user, cid, t2).get_json()["conversation"]
    b_turn = _alex(client, user, "T2 v2", cid=b["id"]).get_json()["turn_id"]

    # Second edit: edit B's own (freshly-resent) 2nd message → branch C.
    c = _edit(client, user, b["id"], b_turn).get_json()["conversation"]

    # All three conversations belong to one group keyed by the ORIGINAL slot turn id.
    groups = _version_map(client, user, cid)
    assert list(groups.keys()) == [t2]
    ids = [m["conversation_id"] for m in groups[t2]]
    assert ids == [cid, b["id"], c["id"]]  # created_at ASC = original, edit1, edit2
    # C carries the inherited (family-stable) slot key, not its own turn id.
    assert c["edited_slot_turn_id"] == t2


def test_version_map_sibling_edits_same_slot(client, user, monkeypatch):
    """Two independent edits of the same source turn are siblings in one group."""
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]
    b1 = _edit(client, user, cid, t2, **{"title": "edit-a"}).get_json()["conversation"]
    b2 = _edit(client, user, cid, t2, **{"title": "edit-b"}).get_json()["conversation"]

    groups = _version_map(client, user, cid)
    assert set(groups.keys()) == {t2}
    ids = [m["conversation_id"] for m in groups[t2]]
    assert ids == [cid, b1["id"], b2["id"]]


def test_version_map_distinct_slots_are_separate_groups(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]
    t3 = _alex(client, user, "T3", cid=cid).get_json()["turn_id"]
    b = _edit(client, user, cid, t2).get_json()["conversation"]
    d = _edit(client, user, cid, t3).get_json()["conversation"]

    groups = _version_map(client, user, cid)
    assert set(groups.keys()) == {t2, t3}
    assert [m["conversation_id"] for m in groups[t2]] == [cid, b["id"]]
    assert [m["conversation_id"] for m in groups[t3]] == [cid, d["id"]]


def test_version_map_empty_when_no_edits(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    assert _version_map(client, user, cid) == {}


# ---------------------------------------------------------------------------
# Concurrency / failed-turn boundary
# ---------------------------------------------------------------------------

def test_edit_failed_turn_is_allowed(client, user, monkeypatch):
    """A failed turn is a valid edit target — the branch inherits what came BEFORE it."""
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "good T1").get_json()["conversation_id"]

    async def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(appmod, "handle_with_react_agent", _boom)
    r = _alex(client, user, "failing T2", cid=cid)
    failed_tid = r.get_json()["turn_id"]
    assert r.get_json()["response_type"] == "error"

    resp = _edit(client, user, cid, failed_tid)
    assert resp.status_code == 201
    child = resp.get_json()["conversation"]
    # Inherits the good T1 only; the failed turn's own rows are excluded.
    contents = " ".join(m["content"] for m in _messages(client, user, child["id"]))
    assert "good T1" in contents
    assert "failing T2" not in contents
    assert child["edited_slot_turn_id"] == failed_tid


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_edit_idempotency_same_key_one_branch(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "T1").get_json()["conversation_id"]
    t2 = _alex(client, user, "T2", cid=cid).get_json()["turn_id"]
    h = _headers(user, **{"Idempotency-Key": "edit-key-1"})
    r1 = client.post(f"/api/conversations/{cid}/edit_turn",
                     json={"turn_id": t2}, headers=h)
    r2 = client.post(f"/api/conversations/{cid}/edit_turn",
                     json={"turn_id": t2}, headers=h)
    assert r1.status_code == 201 and r1.get_json()["idempotent"] is False
    assert r2.status_code == 200 and r2.get_json()["idempotent"] is True
    assert r1.get_json()["conversation"]["id"] == r2.get_json()["conversation"]["id"]
    branches = [c for c in appmod.conversation_store.list_conversations(user)
                if c["parent_conversation_id"] == cid]
    assert len(branches) == 1


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

def test_edit_unknown_conversation_404(client, user):
    r = client.post("/api/conversations/nope/edit_turn",
                    json={"turn_id": "whatever"}, headers=_headers(user))
    assert r.status_code == 404
    assert r.get_json()["code"] == "conversation_not_found"


def test_edit_missing_turn_id_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = client.post(f"/api/conversations/{cid}/edit_turn", json={}, headers=_headers(user))
    assert r.status_code == 400  # generic ApiError (turn_id required)


def test_edit_turn_not_found_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "hi").get_json()["conversation_id"]
    r = _edit(client, user, cid, "deadbeef")
    assert r.status_code == 400
    assert r.get_json()["code"] == "turn_not_found"


def test_edit_turn_not_in_conversation_400(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid_a = _alex(client, user, "conv A").get_json()["conversation_id"]
    tid_b = _alex(client, user, "conv B").get_json()["turn_id"]
    r = _edit(client, user, cid_a, tid_b)
    assert r.status_code == 400
    assert r.get_json()["code"] == "turn_not_in_conversation"


def test_version_map_unknown_conversation_404(client, user):
    r = client.get("/api/conversations/nope/version_map", headers=_headers(user))
    assert r.status_code == 404


def test_edit_foreign_user_cannot_branch(client, user, monkeypatch):
    _install_search_agent(monkeypatch)
    cid = _alex(client, user, "mine").get_json()["conversation_id"]
    t = appmod.conversation_store.list_turns(user, cid)[0]["id"]
    other = "u" + uuid.uuid4().hex[:16]
    r = _edit(client, other, cid, t)
    assert r.status_code == 404
    assert r.get_json()["code"] == "conversation_not_found"
