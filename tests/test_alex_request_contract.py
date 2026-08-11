from __future__ import annotations

from dataclasses import replace
import uuid

import pytest

import app as appmod
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    sessions = SessionStore()
    monkeypatch.setattr(appmod, "conversation_store", store)
    monkeypatch.setattr(appmod, "_session_store", sessions)
    monkeypatch.setattr(
        appmod,
        "_runtime_config",
        replace(appmod._runtime_config, allow_legacy_client_user_id=True),
    )
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True)
    yield appmod.app.test_client(), store
    store.close()


def _user():
    return "u" + uuid.uuid4().hex[:16]


def _headers(user, request_id):
    return {"X-User-Id": user, "X-Request-Id": request_id}


def test_duplicate_request_replays_persisted_response_without_second_agent_call(
    isolated_app, monkeypatch
):
    client, store = isolated_app
    user = _user()
    calls = []

    async def fake_agent(
        user_message,
        context,
        is_continuation,
        user_id,
        conversation_id,
        request_id,
        ui_language="en",
        turn=None,
    ):
        calls.append(request_id)
        appmod._write_back_turn(
            user_id,
            conversation_id,
            user_message,
            "durable answer",
            [],
            turn_id=(turn or {}).get("id"),
            reply_language="en",
        )
        return {"response_type": "chat", "message": "durable answer"}

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    headers = _headers(user, "req-fixed")

    first = client.post("/api/alex", json={"message": "hello"}, headers=headers)
    second = client.post("/api/alex", json={"message": "hello"}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert second.headers["X-Idempotent-Replay"] == "1"
    assert first.get_json()["turn_id"] == second.get_json()["turn_id"]
    assert first.get_json()["conversation_id"] == second.get_json()["conversation_id"]
    assert calls == ["req-fixed"]
    messages = store.get_messages(user, first.get_json()["conversation_id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_agent_failure_is_persisted_as_failed_turn_and_returns_502(
    isolated_app, monkeypatch
):
    client, store = isolated_app
    user = _user()

    async def failing_agent(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(appmod, "handle_with_react_agent", failing_agent)
    headers = _headers(user, "req-failed")

    first = client.post("/api/alex", json={"message": "hello"}, headers=headers)
    replay = client.post("/api/alex", json={"message": "hello"}, headers=headers)

    assert first.status_code == replay.status_code == 502
    body = first.get_json()
    turn = store.get_turn(user, body["turn_id"])
    assert turn["status"] == "failed"
    assert turn["assistant_message_id"] is not None
    assert replay.headers["X-Idempotent-Replay"] == "1"
    assert replay.get_json()["turn_id"] == body["turn_id"]


def test_different_request_receives_409_while_conversation_lease_is_active(
    isolated_app,
):
    client, store = isolated_app
    user = _user()
    cid = store.create_conversation(user, "busy")["id"]
    active = store.start_request_turn(user, cid, "req-active", "working")

    response = client.post(
        "/api/alex",
        json={"message": "second", "conversation_id": cid},
        headers=_headers(user, "req-second"),
    )

    assert response.status_code == 409
    assert response.get_json()["running_turn_id"] == active["id"]
    assert response.headers["Retry-After"]
    assert [message["content"] for message in store.get_messages(user, cid)] == ["working"]
