"""A retry of a request whose process died mid-turn gets a terminal answer (R2-22).

After a real restart the dead process's turn stayed status='running' forever, and a retry
with the same X-Request-Id got 409 "request is still in progress" + Retry-After 1 — still
22 minutes later.
"""
from __future__ import annotations

from uk_rent_agent.web import conversation_store as cs
from uk_rent_agent.web.conversation_store import ConversationStore


def _store(path, owner, monkeypatch):
    monkeypatch.setattr(cs, "_PROCESS_OWNER_ID", owner)
    return ConversationStore(path)


def test_r2_22_replay_of_an_interrupted_request_is_terminal(tmp_path, monkeypatch):
    old = _store(tmp_path / "c.sqlite3", "old-process", monkeypatch)
    cid = old.create_conversation("u_rs", "restart")["id"]
    turn = old.start_request_turn("u_rs", cid, "rs-req-0", "find me a flat in Camden")
    old.close()

    new = _store(tmp_path / "c.sqlite3", "new-process", monkeypatch)
    replay = new.start_request_turn("u_rs", cid, "rs-req-0", "find me a flat in Camden")
    assert replay["replayed"] is True and replay["id"] == turn["id"]
    assert replay["status"] == "failed"
    payload = new.get_turn_response("u_rs", turn["id"])
    assert payload["response_type"] == "error" and payload["interrupted"] is True
    # The conversation is free for a new request.
    fresh = new.start_request_turn("u_rs", cid, "rs-req-1", "try again")
    assert fresh["status"] == "running"
    new.close()


def test_r2_22_a_live_turn_in_this_process_still_replays_as_running(tmp_path, monkeypatch):
    store = _store(tmp_path / "c.sqlite3", "this-process", monkeypatch)
    cid = store.create_conversation("u_live2", "live")["id"]
    store.start_request_turn("u_live2", cid, "req-1", "first")
    replay = store.start_request_turn("u_live2", cid, "req-1", "first")
    assert replay["replayed"] is True and replay["status"] == "running"
    store.close()
