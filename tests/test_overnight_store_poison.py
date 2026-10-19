"""One user's failed write must never wedge the SHARED conversation-store connection.

2026-10-19 C-01 (P0): an anonymous ``POST /api/conversations {"title": "x \\ud800"}`` raised
at bind time inside the implicit transaction Python's sqlite3 opens before DML. Nothing rolled
it back, so the ONE connection every user shares stayed inside it, and every other user's
``start_request_turn`` (``BEGIN IMMEDIATE``) then failed with "cannot start a transaction
within a transaction" — 40/40 chat turns from 40 other guests over 20 s — until some
unrelated call happened to commit (which also committed the failed request's partial writes).
C-02: the same lone surrogate in 11 of 18 write-path fields was an anonymous 500.

The surrogate is only the trigger; ANY bind-time failure poisoned the connection. These tests
therefore poison with surrogates AND with non-surrogate bind failures (a list where TEXT is
bound), and always assert on the victim: a different user's next turn must start.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

import app as appmod
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "conversations.sqlite3")
    yield s
    s.close()


_VICTIM_TURNS = iter(range(10**6))


def _victim_turn_starts(store, user="victim"):
    """The exact calls every chat turn makes; raise if the connection is wedged."""
    turn = store.start_request_turn(user, None, f"req-{user}-{next(_VICTIM_TURNS)}",
                                    "find a flat", create_title="find a flat")
    store.finalize_request_turn(user, turn["id"], status="completed", assistant_content="ok",
                                snapshot={"accumulated_search_criteria": {}})
    assert store.get_turn(user, turn["id"])["status"] == "completed"
    return turn


_BIND_FAILURES = [
    ("rename to a list", lambda s, cid: s.rename_conversation("attacker", cid, ["x"])),
    ("favorite url list", lambda s, cid: s.add_favorite("attacker", ["u"], {"url": "u"})),
    ("message content dict", lambda s, cid: s.add_message("attacker", cid, "user", {"a": 1})),
    ("assignment arch list", lambda s, cid: s.set_agent_assignment(
        "attacker", cid, ["fc"], "v", False)),
]


@pytest.mark.parametrize("label,poison", _BIND_FAILURES, ids=[b[0] for b in _BIND_FAILURES])
def test_a_bind_failure_leaves_no_open_transaction(store, label, poison):
    cid = store.create_conversation("attacker", "mine")["id"]
    with pytest.raises((sqlite3.Error, TypeError, ValueError)):
        poison(store, cid)
    assert store._conn.in_transaction is False, label
    _victim_turn_starts(store)        # used to raise OperationalError
    store.claim_background_job("worker-1")   # the outbox claim failed the same way


@pytest.mark.parametrize("field", ["title", "rename", "favorite_url", "favorite_nested"])
def test_a_lone_surrogate_is_stored_sanitised_and_poisons_nothing(store, field):
    cid = store.create_conversation("attacker", "mine")["id"]
    if field == "title":
        store.create_conversation("attacker", "x \ud800")
    elif field == "rename":
        store.rename_conversation("attacker", cid, "y \ud800")
    elif field == "favorite_url":
        store.add_favorite("attacker", "https://e.x/\ud800", {"url": "https://e.x/\ud800"})
    else:
        store.add_favorite("attacker", "https://e.x/1", {"url": "https://e.x/1",
                                                         "title": {"deep": "\udfff"}})
    assert store._conn.in_transaction is False
    _victim_turn_starts(store)
    titles = [c["title"] for c in store.list_conversations("attacker")]
    assert all("\ud800" not in t for t in titles)


def test_a_failed_multi_statement_write_is_rolled_back_not_committed_later(store):
    """The other half of the old defect: the failed request's PARTIAL writes used to be
    committed by whichever unrelated call committed next."""
    cid = store.create_conversation("attacker", "keep me")["id"]
    with pytest.raises(sqlite3.Error):
        with store._lock:
            store._conn.execute("DELETE FROM conversations WHERE user_id=? AND id=?",
                                ("attacker", cid))
            store._conn.execute("INSERT INTO conversations(user_id) VALUES(?)", (["bad"],))
    store.create_conversation("someone-else", "unrelated commit")
    assert store.get_conversation("attacker", cid) is not None


def test_a_transaction_leaked_outside_the_lock_is_healed_on_next_entry(store):
    store._conn.execute("INSERT INTO favorites(user_id, url, property_json, created_at) "
                        "VALUES('ghost','u','{}','now')")
    assert store._conn.in_transaction is True
    _victim_turn_starts(store)
    assert store._conn.in_transaction is False
    assert store.list_favorites("ghost") == []      # the leaked write was discarded


# ------------------------------------------------------------------ HTTP boundary

@pytest.fixture
def client(tmp_path, monkeypatch):
    s = ConversationStore(tmp_path / "http.sqlite3")
    monkeypatch.setattr(appmod, "conversation_store", s)
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    monkeypatch.setattr(appmod, "_runtime_config", replace(
        appmod._runtime_config, allow_legacy_client_user_id=True, max_request_bytes=262144))
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True, MAX_CONTENT_LENGTH=262144)
    yield appmod.app.test_client(), s
    s.close()


def _raw(client, method, path, body, user="attacker", headers=None):
    return client.open(path, method=method, data=body, content_type="application/json",
                       headers={"X-User-Id": user, **(headers or {})})


def test_every_write_field_rejects_a_lone_surrogate_and_the_victim_is_unaffected(client):
    c, store = client
    # A conversation with a completed turn, so fork/edit_turn would otherwise SUCCEED.
    cid = _victim_turn_starts(store, user="attacker")["conversation_id"]
    S = "\\ud800"      # the JSON escape, exactly as C sent it
    attempts = [
        ("POST", "/api/conversations", '{"title":"x %s"}' % S),
        ("PATCH", f"/api/conversations/{cid}", '{"title":"y %s"}' % S),
        ("POST", f"/api/conversations/{cid}/fork", '{"idempotency_key":"k %s"}' % S),
        ("POST", f"/api/conversations/{cid}/edit_turn", '{"turn_id":"t %s"}' % S),
        ("POST", "/api/favorites", '{"url":"https://e.x/%s","title":"t"}' % S),
        ("POST", "/api/favorites", '{"url":"https://e.x/1","title":"t %s"}' % S),
        ("POST", "/api/clear_history", '{"conversation_id":"%s"}' % S),
        ("POST", "/api/auth/register", '{"username":"eve_1","password":"p %s assword"}' % S),
        ("POST", "/api/auth/login", '{"username":"eve_1","password":"p %s"}' % S),
        ("POST", "/api/alex", '{"message":"find flat %s camden"}' % S),
        ("POST", "/api/alex", '{"message":"hi","conversation_id":"%s"}' % S),
    ]
    for method, path, body in attempts:
        r = _raw(c, method, path, body)
        assert r.status_code == 400, (method, path, r.status_code, r.get_data(as_text=True))
        assert store._conn.in_transaction is False, (method, path)
    _victim_turn_starts(store)
    ok = _raw(c, "POST", "/api/conversations", '{"title":"victim chat"}', user="victim")
    assert ok.status_code == 201


@pytest.mark.parametrize("title", [["a"], 5, {"k": "v"}, True])
def test_a_non_string_title_is_a_400_not_a_500(client, title):
    c, _ = client
    r = _raw(c, "POST", "/api/conversations", json.dumps({"title": title}))
    assert r.status_code == 400


@pytest.mark.parametrize("url", [["https://e.x"], {"u": 1}, 7])
def test_a_non_string_favorite_url_is_refused(client, url):
    c, store = client
    r = _raw(c, "POST", "/api/favorites", json.dumps({"url": url}))
    assert r.status_code == 400
    assert store.list_favorites("attacker") == []


@pytest.mark.parametrize("context", [["a"], "text", 7, True])
def test_a_non_object_context_is_a_400_before_any_turn_is_stored(client, context):
    """C-06: these used to answer 502 after persisting a failed turn and a junk conversation."""
    c, store = client
    r = _raw(c, "POST", "/api/alex", json.dumps({"message": "hi", "context": context}))
    assert r.status_code == 400
    assert store.list_conversations("attacker") == []


def test_an_idempotency_key_is_bound_to_its_source_and_endpoint(client):
    """C-07: (user, key) alone used to replay the first child for ANY source or endpoint."""
    c, store = client
    first = _victim_turn_starts(store, user="attacker")
    second = _victim_turn_starts(store, user="attacker")
    a, b = first["conversation_id"], second["conversation_id"]
    key = {"Idempotency-Key": "k-1"}
    made = _raw(c, "POST", f"/api/conversations/{a}/fork", "{}", headers=key)
    assert made.status_code == 201
    replay = _raw(c, "POST", f"/api/conversations/{a}/fork", "{}", headers=key)
    assert replay.status_code == 200 and replay.get_json()["idempotent"] is True
    other_source = _raw(c, "POST", f"/api/conversations/{b}/fork", "{}", headers=key)
    assert other_source.status_code == 409
    other_endpoint = _raw(c, "POST", f"/api/conversations/{a}/edit_turn",
                          json.dumps({"turn_id": first["id"]}), headers=key)
    assert other_endpoint.status_code == 409
