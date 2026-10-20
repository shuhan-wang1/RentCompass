from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import app as runtime
from uk_rent_agent.agent.progress import report_progress
from uk_rent_agent.web.chat_transport import NativeChatEndpoint
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.session_store import SessionStore


@pytest.fixture
def chat(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "chat.sqlite3")
    monkeypatch.setattr(runtime, "conversation_store", store)
    monkeypatch.setattr(runtime, "_session_store", SessionStore())
    monkeypatch.setattr(runtime._api_rate_limiter, "allow", lambda *a, **k: (True, 0))
    monkeypatch.setattr(runtime, "_runtime_config", replace(
        runtime._runtime_config, allow_legacy_client_user_id=True, require_auth=False,
    ))
    endpoint = NativeChatEndpoint(runtime.app)

    async def live(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/alex", endpoint.handle, methods=["POST", "OPTIONS"]),
                            Route("/live", live)])
    yield app, endpoint, store
    store.close()


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        kind = next((line[7:] for line in frame.splitlines() if line.startswith("event: ")), None)
        data = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), None)
        if kind and data:
            result.append((kind, json.loads(data)))
    return result


def install_agent(monkeypatch, *, fail=False, started=None, release=None, loops=None):
    async def agent(message, context, continuation, user_id, conversation_id, request_id,
                    ui_language="en", turn=None):
        if loops is not None:
            loops.append(id(asyncio.get_running_loop()))
        if started is not None:
            started.set()
        report_progress("searching")
        if release is not None:
            await release.wait()
        else:
            await asyncio.sleep(0.02)
        if fail:
            raise RuntimeError("private provider error")
        report_progress("checking")
        runtime._write_back_turn(
            user_id, conversation_id, message, "已核验的回答", [],
            turn_id=turn["id"], reply_language="zh",
        )
        return {"message": "已核验的回答", "response_type": "chat"}

    monkeypatch.setattr(runtime, "handle_with_react_agent", agent)


def test_stream_keeps_cookie_identity_and_replays_durable_response(chat, monkeypatch):
    app, _, store = chat
    monkeypatch.setattr(runtime, "_runtime_config", replace(
        runtime._runtime_config, allow_legacy_client_user_id=False,
    ))
    loops = []
    install_agent(monkeypatch, loops=loops)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Accept": "text/event-stream", "X-Request-Id": "stream-repeat"}
            response = await client.post("/api/alex", json={"message": "你好"}, headers=headers)
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-request-id"] == "stream-repeat"
            assert client.cookies.get("session")
            frames = events(response)
            assert [data["stage"] for kind, data in frames if kind == "step"] == [
                "understanding", "searching", "checking", "saving",
            ]
            final = frames[-1][1]
            assert frames[-1][0] == "final" and final["status"] == 200
            assert "已核验的回答" in final["payload"]["message"]
            replay = await client.post("/api/alex", json={"message": "你好"}, headers=headers)
            assert replay.headers["x-idempotent-replay"] == "1"
            assert replay.json()["turn_id"] == final["payload"]["turn_id"]
            assert loops == [id(asyncio.get_running_loop())]

    asyncio.run(run())


def test_validation_auth_rate_limit_and_preflight_keep_http_status(chat, monkeypatch):
    app, _, _ = chat

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/alex", json={"message": []}, headers={"Accept": "text/event-stream"})
            assert response.status_code == 400
            assert response.headers["content-type"].startswith("application/json")
            preflight = await client.options("/api/alex", headers={
                "Origin": "http://localhost:5001", "Access-Control-Request-Method": "POST",
            })
            assert preflight.status_code == 200
            monkeypatch.setattr(runtime, "_runtime_config", replace(runtime._runtime_config, require_auth=True))
            assert (await client.post("/api/alex", json={"message": "hello"})).status_code == 401
            monkeypatch.setattr(runtime, "_runtime_config", replace(runtime._runtime_config, require_auth=False))
            monkeypatch.setattr(runtime._api_rate_limiter, "allow", lambda *a, **k: (False, 3))
            response = await client.post("/api/alex", json={"message": "hello"})
            assert response.status_code == 429 and response.headers["retry-after"] == "3"

    asyncio.run(run())


def test_oversized_native_request_keeps_json_and_identity_headers(chat, monkeypatch):
    app, _, _ = chat
    monkeypatch.setitem(runtime.app.config, "MAX_CONTENT_LENGTH", 16)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/alex", json={"message": "x" * 100})
            assert response.status_code == 413
            assert response.json()["error"] == "request body too large"
            assert response.headers["x-agent-arch"] == runtime.AGENT_ARCH

    asyncio.run(run())


def test_stream_preserves_agent_error_without_exposing_exception(chat, monkeypatch):
    app, _, _ = chat
    install_agent(monkeypatch, fail=True)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/alex", json={"message": "hello"}, headers={"Accept": "text/event-stream"})
            final = events(response)[-1][1]
            assert final["status"] == 502
            assert final["payload"]["response_type"] == "error"
            assert "private provider error" not in response.text

    asyncio.run(run())


def test_waiting_chat_does_not_block_liveness(chat, monkeypatch):
    app, _, _ = chat

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        install_agent(monkeypatch, started=started, release=release)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pending = asyncio.create_task(client.post("/api/alex", json={"message": "hello"}))
            await asyncio.wait_for(started.wait(), 1)
            assert (await asyncio.wait_for(client.get("/live"), 0.2)).status_code == 200
            release.set()
            assert (await pending).status_code == 200

    asyncio.run(run())


def test_stream_disconnect_releases_turn_lease(chat, monkeypatch):
    _, endpoint, store = chat

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        install_agent(monkeypatch, started=started, release=release)
        body = json.dumps({"message": "hello"}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({
            "type": "http", "http_version": "1.1", "method": "POST", "path": "/api/alex",
            "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1), "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"),
                        (b"x-user-id", b"cancel-user"), (b"x-request-id", b"cancel-request")],
        }, receive)
        response = await endpoint.handle(request)
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        turn = store.get_request_turn("cancel-user", "cancel-request")
        assert turn["status"] == "failed"
        assert turn["assistant_message_id"]
        next_turn = store.start_request_turn("cancel-user", turn["conversation_id"], "next-request", "hello")
        assert next_turn["status"] == "running"

    asyncio.run(run())


def test_response_close_before_first_body_cancels_execution(chat, monkeypatch):
    _, endpoint, store = chat

    async def run():
        install_agent(monkeypatch, release=asyncio.Event())
        body = json.dumps({"message": "hello"}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "path": "/api/alex",
            "asgi": {"spec_version": "2.4"}, "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 1), "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"),
                        (b"x-user-id", b"closed-user"), (b"x-request-id", b"closed-request")],
        }
        response = await endpoint.handle(Request(scope, receive))

        async def disconnected_send(_message):
            raise OSError("client connection closed")

        with pytest.raises(ClientDisconnect):
            await response(scope, receive, disconnected_send)
        assert response.execution_task.done()
        assert store.get_request_turn("closed-user", "closed-request")["status"] == "failed"

    asyncio.run(run())
