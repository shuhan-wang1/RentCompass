"""Transport-hardening regressions for the 2026-09-03 post-deploy round (angle T3).

Three defects, all reachable by an UNAUTHENTICATED caller against the configuration
that is live on both pools today (`fc_loop`, specialists off):

  * 高-1  `{"message":"hello \\ud800 world"}` -> HTTP 500 from
          `conversation_store.start_request_turn`, plus a canary record charging the
          gate an instrumentation violation. Now a 400 with an explicit reason, and
          the store itself can no longer raise on the same input.
  * 低-1  a 10 MB body -> `400 malformed JSON body`. Now 413, stating the limit.
  * 低-2  `POST /api/alex` with another user's `conversation_id` -> 200 and a
          silently minted new conversation, while `GET .../messages` 404s on the
          same id. Now both say 404.

The payloads are the ones the tester actually sent (scratchpad test5/T3), reproduced
here verbatim so a regression is caught by the original attack, not a paraphrase.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

import app as appmod
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.identity import InvalidMessage, normalize_message
from uk_rent_agent.web.session_store import SessionStore

# The exact bodies from T3's transport matrix.
T3_LONE_SURROGATE = '{"message":"hello \\ud800 world","conversation_id":null}'
T3_ESCAPED_NUL = '{"message":"nul\\u0000here"}'
T3_RAW_NUL = b'{"message":"nul\x00here"}'
T3_NON_UTF8 = b"\xff\xfe\x80"
T3_TRUNCATED = '{"message":'
T3_DEEP_NESTING = "[" * 50_000


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    monkeypatch.setattr(appmod, "conversation_store", store)
    monkeypatch.setattr(appmod, "_session_store", SessionStore())
    monkeypatch.setattr(
        appmod,
        "_runtime_config",
        replace(appmod._runtime_config, allow_legacy_client_user_id=True,
                max_request_bytes=262144),
    )
    monkeypatch.setattr(appmod._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    appmod.app.config.update(TESTING=True, MAX_CONTENT_LENGTH=262144)
    yield appmod.app.test_client(), store
    store.close()


def _post(client, body, content_type="application/json", user="u_hardening"):
    return client.post(
        "/api/alex", data=body, content_type=content_type,
        headers={"X-User-Id": user},
    )


# --------------------------------------------------------------------- 高-1

def test_t3_lone_surrogate_is_a_client_error_not_a_500(isolated_app):
    """THE defect: a valid-JSON `\\udXXX` escape used to reach sqlite and 500."""
    client, _ = isolated_app
    response = _post(client, T3_LONE_SURROGATE)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert "surrogate" in response.get_json()["error"]


@pytest.mark.parametrize(
    "body,content_type,expected",
    [
        (T3_LONE_SURROGATE, "application/json", 400),
        (T3_ESCAPED_NUL, "application/json", 400),
        (T3_RAW_NUL, "application/json", 400),
        (T3_NON_UTF8, "application/json", 400),
        (T3_TRUNCATED, "application/json", 400),
        (T3_DEEP_NESTING, "application/json", 400),
        ("hello", "text/plain", 415),
    ],
)
def test_every_t3_transport_payload_is_a_4xx(isolated_app, body, content_type, expected):
    client, _ = isolated_app
    response = _post(client, body, content_type=content_type)
    assert response.status_code == expected, response.get_data(as_text=True)
    # A JSON error object, never an HTML error page, on every /api/* path.
    assert "error" in response.get_json()


def test_the_instance_still_answers_after_the_whole_hostile_batch(isolated_app, monkeypatch):
    client, store = isolated_app
    for body, ctype in (
        (T3_LONE_SURROGATE, "application/json"),
        (T3_ESCAPED_NUL, "application/json"),
        (T3_RAW_NUL, "application/json"),
        (T3_NON_UTF8, "application/json"),
        (T3_TRUNCATED, "application/json"),
        ("hello", "text/plain"),
    ):
        assert _post(client, body, content_type=ctype).status_code >= 400

    async def fake_agent(user_message, *a, **k):
        return {"response_type": "chat", "message": "still alive"}

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    ok = client.post("/api/alex", json={"message": "hello"},
                     headers={"X-User-Id": "u_hardening"})
    assert ok.status_code == 200
    assert ok.get_json()["message"] == "still alive"


def test_normalize_message_refuses_unencodable_and_control_text():
    assert normalize_message("hello there") == "hello there"
    # Tabs/newlines/CR are ordinary text and must keep working.
    assert normalize_message("line one\n\tline two\r\n") == "line one\n\tline two\r\n"
    for bad in ("hello \ud800 world", "nul\x00here", "bell\x07here", "\x7f"):
        with pytest.raises(InvalidMessage):
            normalize_message(f"ok {bad}")


def test_the_store_itself_can_no_longer_raise_on_a_lone_surrogate(tmp_path):
    """Layer 2. Even if some future path skips the request-boundary check."""
    store = ConversationStore(tmp_path / "c.sqlite3")
    try:
        turn = store.start_request_turn("u1", None, "req-surrogate", "hello \ud800 world")
        stored = store.get_messages("u1", turn["conversation_id"])[0]["content"]
        assert stored.startswith("hello ") and stored.endswith(" world")
        assert "\ud800" not in stored
    finally:
        store.close()


# --------------------------------------------------------------------- 低-1

def test_over_size_body_is_413_with_the_limit_stated(isolated_app):
    client, _ = isolated_app
    body = json.dumps({"message": "a" * 400_000})
    response = _post(client, body)
    assert response.status_code == 413, response.get_data(as_text=True)
    error = response.get_json()["error"]
    assert "262144" in error and "exceeds" in error


# --------------------------------------------------------------------- 低-2

def test_unknown_conversation_id_is_404_on_both_routes(isolated_app, monkeypatch):
    client, store = isolated_app

    async def fake_agent(user_message, *a, **k):
        return {"response_type": "chat", "message": "answer"}

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    foreign = "667baf4300000000000000000000dead"

    post = client.post(
        "/api/alex", json={"message": "hi", "conversation_id": foreign},
        headers={"X-User-Id": "u_victim"},
    )
    get = client.get(f"/api/conversations/{foreign}/messages",
                     headers={"X-User-Id": "u_victim"})
    assert post.status_code == get.status_code == 404
    assert post.get_json()["error"] == get.get_json()["error"] == "conversation not found"
    # And nothing was created as a side effect of the rejected POST.
    assert store.list_conversations("u_victim") == []


def test_another_users_conversation_id_is_404_not_a_new_conversation(isolated_app, monkeypatch):
    client, store = isolated_app

    async def fake_agent(user_message, *a, **k):
        return {"response_type": "chat", "message": "answer"}

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    owner = client.post("/api/alex", json={"message": "mine"},
                        headers={"X-User-Id": "u_owner"})
    cid = owner.get_json()["conversation_id"]

    attacker = client.post("/api/alex", json={"message": "yours", "conversation_id": cid},
                           headers={"X-User-Id": "u_attacker"})
    assert attacker.status_code == 404
    # The owner's conversation is untouched: one turn, not two.
    assert len(store.get_messages("u_owner", cid)) == 2


def test_omitted_conversation_id_still_creates_one_implicitly(isolated_app, monkeypatch):
    """The implicit-creation contract is unchanged; only a PRESENT unknown id 404s."""
    client, _ = isolated_app

    async def fake_agent(user_message, *a, **k):
        return {"response_type": "chat", "message": "answer"}

    monkeypatch.setattr(appmod, "handle_with_react_agent", fake_agent)
    for body in ({"message": "hi"}, {"message": "hi", "conversation_id": None}):
        response = client.post("/api/alex", json=body, headers={"X-User-Id": "u_new"})
        assert response.status_code == 200
        assert response.get_json()["conversation_id"]
