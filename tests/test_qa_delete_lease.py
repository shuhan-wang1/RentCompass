"""Destructive conversation writes respect the turn lease (2026-10-19 C-03, C-05).

C-03: ``DELETE /api/conversations/<cid>`` and ``POST /api/clear_history`` (with and without a
conversation id) answered 200 while a turn was RUNNING in that conversation. The turn then spent
its model and tool calls, failed at persistence (TurnNotFound -> 500), and its graph wrote the
deleted conversation's checkpoint thread back after the delete had dropped it — the user's words
outliving the delete. ``forget_me`` already refused to race a live lease (409); these now do too.

C-05: a 409 "turn in progress" carried ``Retry-After`` = the remaining LEASE (899 s) and the UI
turned it into a pane-wide rate-limit lock-out. The header is now a short polling hint; the lease
bound is in the body, and the UI only locks the pane for a 429.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import replace
from pathlib import Path

import pytest

import app as appmod
from uk_rent_agent.web.conversation_store import ConversationBusy, ConversationStore
from uk_rent_agent.web.session_store import SessionStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    monkeypatch.setattr(appmod, "conversation_store", store)
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    monkeypatch.setattr(appmod, "_runtime_config", replace(
        appmod._runtime_config, allow_legacy_client_user_id=True))
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    deleted_threads = []
    monkeypatch.setattr(appmod, "_delete_checkpoint_thread",
                        lambda user, cid: deleted_threads.append((user, cid)))
    appmod.app.config.update(TESTING=True)
    yield appmod.app.test_client(), store, deleted_threads
    store.close()


def _running(store, user="u_lease", cid=None):
    cid = cid or store.create_conversation(user, "busy")["id"]
    turn = store.start_request_turn(user, cid, f"req-{cid}", "find me a flat in Bow")
    return cid, turn


def _h(user="u_lease"):
    return {"X-User-Id": user}


def test_delete_during_a_running_turn_is_a_409_and_deletes_nothing(env):
    client, store, threads = env
    cid, turn = _running(store)
    r = client.delete(f"/api/conversations/{cid}", headers=_h())
    assert r.status_code == 409
    body = r.get_json()
    assert body["running_turn_id"] == turn["id"] and body["deleted"] is False
    assert threads == []                                   # checkpoint thread untouched
    assert store.get_conversation("u_lease", cid) is not None
    # The in-flight turn can still persist its answer — the C-03 500 was TurnNotFound here.
    store.finalize_request_turn("u_lease", turn["id"], status="completed",
                                assistant_content="done", snapshot={})
    assert client.delete(f"/api/conversations/{cid}", headers=_h()).status_code == 200
    assert threads == [("u_lease", cid)]


@pytest.mark.parametrize("scoped", [True, False], ids=["clear-one", "clear-all"])
def test_clear_history_during_a_running_turn_is_a_409(env, scoped):
    client, store, threads = env
    cid, turn = _running(store)
    body = {"conversation_id": cid} if scoped else {}
    r = client.post("/api/clear_history", json=body, headers=_h())
    assert r.status_code == 409 and r.get_json()["running_turn_id"] == turn["id"]
    assert threads == []
    assert [m["content"] for m in store.get_messages("u_lease", cid)] == ["find me a flat in Bow"]


def test_another_conversation_of_the_same_user_can_still_be_deleted(env):
    client, store, threads = env
    _running(store)
    idle = store.create_conversation("u_lease", "idle")["id"]
    assert client.delete(f"/api/conversations/{idle}", headers=_h()).status_code == 200
    # ...but "clear ALL" must wait for the running one.
    assert client.post("/api/clear_history", json={}, headers=_h()).status_code == 409


def test_an_expired_lease_does_not_block_a_delete(env):
    client, store, _ = env
    cid, turn = _running(store)
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=5)).isoformat()
    with store._lock:
        store._conn.execute("UPDATE conversation_turn_leases SET expires_at=? WHERE turn_id=?",
                            (past, turn["id"]))
        store._conn.commit()
    assert client.delete(f"/api/conversations/{cid}", headers=_h()).status_code == 200


def test_the_store_methods_raise_busy_directly(tmp_path):
    store = ConversationStore(tmp_path / "s.sqlite3")
    cid, _ = _running(store)
    for call in (lambda: store.delete_conversation("u_lease", cid),
                 lambda: store.clear_conversation_messages("u_lease", cid),
                 lambda: store.delete_all_conversations("u_lease")):
        with pytest.raises(ConversationBusy):
            call()
        assert store._conn.in_transaction is False
    assert store.get_conversation("u_lease", cid) is not None
    store.close()


def test_a_busy_409_carries_a_short_retry_hint_and_the_lease_bound(env):
    client, store, _ = env
    cid, turn = _running(store)
    r = client.post("/api/alex", json={"message": "second", "conversation_id": cid},
                    headers={**_h(), "X-Request-Id": "req-second"})
    assert r.status_code == 409
    assert 1 <= int(r.headers["Retry-After"]) <= 3                # was 899
    assert r.get_json()["lease_expires_in"] > 60
    assert r.get_json()["code"] == "turn_in_progress"


def test_the_ui_only_locks_the_pane_for_a_rate_limit():
    html = (Path(appmod.__file__).resolve().parent / "unified-ui.html").read_text(encoding="utf-8")
    body = html[html.index("function showChatError"):]
    body = body[:body.index("\n        }\n")]
    assert re.search(r"if \(details\.rateLimited && details\.retryAfter\) pane\.retryUntil", body)
    details = html[html.index("function apiErrorDetails"):html.index("function showRetryToast")]
    assert "rateLimited: rateLimited" in details and "status === 429" in details
