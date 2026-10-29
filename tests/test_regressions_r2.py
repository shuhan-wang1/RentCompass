"""Regression tests (R2-xx ids in each test name)."""
from __future__ import annotations

import pytest

import app as appmod
from tests.test_alex_request_contract import _headers, _user, isolated_app  # noqa: F401
from uk_rent_agent.web.conversation_store import CANCELLED_TURN_TEXTS


# R2-01 ── a same-id retry after a disconnect re-runs over HTTP ──────────────────────
def _answering_agent(calls):
    async def fake_agent(user_message, context, is_continuation, user_id, conversation_id,
                         request_id, ui_language="en", turn=None):
        calls.append(user_message)
        appmod._write_back_turn(user_id, conversation_id, user_message, "fresh answer", [],
                                turn_id=(turn or {}).get("id"), reply_language="en")
        return {"response_type": "chat", "message": "fresh answer"}
    return fake_agent


@pytest.mark.parametrize("variant", ["cancelled", "interrupted"])
def test_r2_01_retry_of_a_turn_that_delivered_nothing_runs_again(isolated_app, monkeypatch,  # noqa: F811
                                                                  variant):
    client, store = isolated_app
    calls = []
    monkeypatch.setattr(appmod, "handle_with_react_agent", _answering_agent(calls))
    user = _user()
    first = store.start_request_turn(user, None, "rid-disc-1", "weather near the park?")
    if variant == "cancelled":
        store.finalize_request_turn(user, first["id"], status="failed", response_type="error",
                                    assistant_content=CANCELLED_TURN_TEXTS[0])
    else:
        store.fail_turn(user, first["id"])
    headers = _headers(user, "rid-disc-1")
    retry = client.post("/api/alex", json={"message": "weather near the park?"}, headers=headers)
    assert retry.status_code == 200, retry.get_json()
    assert "X-Idempotent-Replay" not in retry.headers
    assert retry.get_json()["message"] == "fresh answer"
    assert retry.get_json()["conversation_id"] == first["conversation_id"]
    # The answered retry now replays without running the agent again.
    again = client.post("/api/alex", json={"message": "weather near the park?"}, headers=headers)
    assert again.status_code == 200 and again.headers["X-Idempotent-Replay"] == "1"
    assert again.get_json()["message"] == "fresh answer"
    # The same id with a different message is still refused.
    other = client.post("/api/alex", json={"message": "find a flat"}, headers=headers)
    assert other.status_code == 409 and other.get_json()["code"] == "idempotency_key_reused"
    assert calls == ["weather near the park?"]


def test_r2_01_a_delivered_error_still_replays_over_http(isolated_app, monkeypatch):  # noqa: F811
    client, store = isolated_app
    calls = []
    monkeypatch.setattr(appmod, "handle_with_react_agent", _answering_agent(calls))
    user = _user()
    first = store.start_request_turn(user, None, "rid-err-1", "find a flat")
    store.finalize_request_turn(user, first["id"], status="failed", response_type="error",
                                assistant_content="The search service is unavailable right now.")
    replay = client.post("/api/alex", json={"message": "find a flat"},
                         headers=_headers(user, "rid-err-1"))
    assert replay.status_code == 502 and replay.headers["X-Idempotent-Replay"] == "1"
    assert calls == []
