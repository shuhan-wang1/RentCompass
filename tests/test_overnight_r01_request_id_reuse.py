"""An X-Request-Id reused with a different message is refused (overnight round r01: R1-15).

POST /api/alex with a request id already used for "hello" and the message "find me a flat"
replayed the old answer with 200 and X-Idempotent-Replay: 1 — the user's new question was
silently answered with the old reply. The fork routes already answer 409
idempotency_key_reused for the same misuse (C-07); this endpoint now does too. A genuine retry
(same id, same message) still replays.
"""
from __future__ import annotations

import pytest

import app as appmod
from tests.test_alex_request_contract import _headers, _user, isolated_app  # noqa: F401
from uk_rent_agent.web.conversation_store import IdempotencyKeyReused


def _fake_agent(calls):
    async def fake_agent(user_message, context, is_continuation, user_id, conversation_id,
                         request_id, ui_language="en", turn=None):
        calls.append(user_message)
        appmod._write_back_turn(user_id, conversation_id, user_message, "durable answer", [],
                                turn_id=(turn or {}).get("id"), reply_language="en")
        return {"response_type": "chat", "message": "durable answer"}
    return fake_agent


def test_r1_15_same_request_id_different_message_is_409(isolated_app, monkeypatch):  # noqa: F811
    client, store = isolated_app
    calls = []
    monkeypatch.setattr(appmod, "handle_with_react_agent", _fake_agent(calls))
    user = _user()
    headers = _headers(user, "req-reused")
    first = client.post("/api/alex", json={"message": "hello"}, headers=headers)
    retry = client.post("/api/alex", json={"message": "hello"}, headers=headers)
    other = client.post("/api/alex", json={"message": "find me a flat in Camden"}, headers=headers)
    assert first.status_code == retry.status_code == 200
    assert retry.headers["X-Idempotent-Replay"] == "1"
    assert other.status_code == 409
    assert other.get_json()["code"] == "idempotency_key_reused"
    assert "X-Idempotent-Replay" not in other.headers
    assert calls == ["hello"]


def test_r1_15_store_refuses_reuse_inside_the_turn_transaction(tmp_path):
    from uk_rent_agent.web.conversation_store import ConversationStore

    store = ConversationStore(tmp_path / "c.sqlite3")
    turn = store.start_request_turn("u1", None, "req-1", "hello")
    assert store.request_message_differs("u1", turn, "hello") is False
    assert store.request_message_differs("u1", turn, "something else") is True
    with pytest.raises(IdempotencyKeyReused):
        store.start_request_turn("u1", None, "req-1", "something else")
    assert store.start_request_turn("u1", None, "req-1", "hello")["replayed"] is True
    store.close()
