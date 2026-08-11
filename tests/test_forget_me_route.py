from __future__ import annotations

from dataclasses import replace
import uuid

import pytest

import app as appmod
from rag import agent_memory as memory_module
from uk_rent_agent.web.auth_store import AuthStore
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore


class FakeMemory:
    def __init__(self, records: int = 2, *, fail: bool = False):
        self.records = records
        self.fail = fail
        self.calls = 0

    def privacy_inventory(self, user_id):
        return {"records": self.records, "pending_buffers": 0, "total": self.records}

    def forget(self, user_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected memory failure")
        deleted = self.records
        self.records = 0
        return deleted


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    sessions = SessionStore()
    auth = AuthStore(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(appmod, "conversation_store", store)
    monkeypatch.setattr(appmod, "_session_store", sessions)
    monkeypatch.setattr(appmod, "auth_store", auth)
    monkeypatch.setattr(
        appmod,
        "_runtime_config",
        replace(appmod._runtime_config, allow_legacy_client_user_id=True),
    )
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True)
    yield appmod.app.test_client(), store, sessions
    store.close()


def _user():
    return "u" + uuid.uuid4().hex[:16]


def _seed(store, sessions, user):
    conv = store.create_conversation(user, "private")
    turn = store.begin_turn(user, conv["id"])
    user_message = store.add_message(user, conv["id"], "user", "private", turn_id=turn["id"])
    assistant = store.add_message(
        user, conv["id"], "assistant", "private", turn_id=turn["id"]
    )
    store.complete_turn(user, turn["id"], assistant_message_id=assistant["id"])
    store.add_favorite(user, "https://example.test/1", {"url": "https://example.test/1"})
    sessions.get(user, conv["id"]).history.append({"user": "private", "assistant": "private"})
    return conv["id"]


def test_forget_me_verifies_all_layers_before_claiming_success(
    isolated_app, monkeypatch
):
    client, store, sessions = isolated_app
    user = _user()
    cid = _seed(store, sessions, user)
    memory = FakeMemory()
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: memory)
    monkeypatch.setattr(
        appmod,
        "_delete_checkpoint_thread",
        lambda uid, conversation_id: {"status": "deleted", "residual": False},
    )

    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["forgotten"] is True and body["status"] == "complete"
    assert body["layers"]["memory"]["after"]["total"] == 0
    assert store.privacy_inventory(user)["total"] == 0
    assert sessions.privacy_inventory(user)["session_slices"] == 0
    assert store.get_conversation(user, cid) is None


def test_memory_failure_returns_partial_and_retains_relational_data_for_retry(
    isolated_app, monkeypatch
):
    client, store, _ = isolated_app
    user = _user()
    cid = _seed(store, SessionStore(), user)
    memory = FakeMemory(fail=True)
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: memory)
    monkeypatch.setattr(
        appmod,
        "_delete_checkpoint_thread",
        lambda uid, conversation_id: {"status": "deleted", "residual": False},
    )

    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    body = response.get_json()
    assert response.status_code == 503
    assert body["forgotten"] is False and body["status"] == "partial"
    assert body["layers"]["memory"]["status"] == "failed"
    assert body["layers"]["relational"]["status"] == "retained_for_retry"
    assert store.get_conversation(user, cid) is not None


def test_checkpoint_residual_never_returns_forgotten_true(isolated_app, monkeypatch):
    client, store, _ = isolated_app
    user = _user()
    cid = _seed(store, SessionStore(), user)
    memory = FakeMemory()
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: memory)
    monkeypatch.setattr(
        appmod,
        "_delete_checkpoint_thread",
        lambda uid, conversation_id: {
            "status": "failed",
            "residual": True,
            "error_type": "ResidualData",
        },
    )

    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    assert response.status_code == 503
    assert response.get_json()["forgotten"] is False
    assert store.get_conversation(user, cid) is not None


def test_active_turn_blocks_erasure_before_any_layer_is_deleted(
    isolated_app, monkeypatch
):
    client, store, _ = isolated_app
    user = _user()
    cid = store.create_conversation(user, "active")["id"]
    store.start_request_turn(user, cid, "req-active", "still running")
    memory = FakeMemory()
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: memory)

    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    assert response.status_code == 409
    assert response.get_json()["forgotten"] is False
    assert memory.calls == 0
    assert store.get_conversation(user, cid) is not None


def test_credential_failure_is_partial_and_never_claims_complete(
    isolated_app, monkeypatch
):
    client, store, sessions = isolated_app
    account = appmod.auth_store.register("eraseme", "hunter2")
    user = account["user_id"]
    _seed(store, sessions, user)
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: FakeMemory())
    monkeypatch.setattr(
        appmod,
        "_delete_checkpoint_thread",
        lambda uid, conversation_id: {"status": "deleted", "residual": False},
    )

    def fail_delete(user_id):
        raise RuntimeError("injected credential failure")

    monkeypatch.setattr(appmod.auth_store, "delete_user_id", fail_delete)
    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    body = response.get_json()
    assert response.status_code == 503
    assert body["forgotten"] is False
    assert body["layers"]["credentials"]["status"] == "failed"
    assert appmod.auth_store.privacy_inventory(user)["total"] == 1


def test_successful_erasure_removes_credentials(isolated_app, monkeypatch):
    client, store, sessions = isolated_app
    account = appmod.auth_store.register("deleteacct", "hunter2")
    user = account["user_id"]
    _seed(store, sessions, user)
    monkeypatch.setattr(memory_module, "get_agent_memory", lambda: FakeMemory())
    monkeypatch.setattr(
        appmod,
        "_delete_checkpoint_thread",
        lambda uid, conversation_id: {"status": "deleted", "residual": False},
    )

    response = client.post(
        "/api/forget_me", json={}, headers={"X-User-Id": user}
    )

    assert response.status_code == 200
    assert response.get_json()["layers"]["credentials"]["after"]["total"] == 0
    assert appmod.auth_store.verify("deleteacct", "hunter2") is None
