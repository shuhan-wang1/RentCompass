"""Native async chat transport with a bounded Flask compatibility boundary.

Flask supplies the existing request/session/error hooks. Its async application
handler is awaited on the ASGI loop and still owns durable turn completion.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys

from flask import g
from starlette.responses import Response, StreamingResponse

from uk_rent_agent.agent.progress import progress_context
from uk_rent_agent.web.streaming import sse


def _environ(request, body):
    server = request.scope.get("server") or ("localhost", 80)
    client = request.client
    environ = {
        "REQUEST_METHOD": request.method,
        "SCRIPT_NAME": request.scope.get("root_path", ""),
        "PATH_INFO": request.url.path.encode("utf-8").decode("latin-1"),
        "QUERY_STRING": request.scope.get("query_string", b"").decode("latin-1"),
        "SERVER_NAME": str(server[0]), "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": "HTTP/" + request.scope.get("http_version", "1.1"),
        "REMOTE_ADDR": client.host if client else "unknown",
        "wsgi.version": (1, 0), "wsgi.url_scheme": request.url.scheme,
        "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
        "wsgi.multithread": True, "wsgi.multiprocess": False,
        "wsgi.run_once": False, "CONTENT_LENGTH": str(len(body)),
    }
    for key, value in request.headers.items():
        key = key.upper().replace("-", "_")
        if key == "CONTENT_TYPE":
            environ[key] = value
        elif key != "CONTENT_LENGTH":
            environ["HTTP_" + key] = value
    return environ


def _response(response):
    result = Response(response.get_data(), status_code=response.status_code)
    result.raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in response.headers.to_wsgi_list()
    ]
    return result


class NativeChatEndpoint:
    def __init__(self, flask_app):
        self.app = flask_app

    async def _execute(self, environ, progress=None):
        # The task that pushes this context also pops it. A streaming response
        # never moves a live request context into its iterator or a worker thread.
        with self.app.request_context(environ):
            try:
                result = await asyncio.to_thread(self.app.preprocess_request)
                if result is None:
                    if environ["REQUEST_METHOD"] == "OPTIONS":
                        result = self.app.make_default_options_response()
                    else:
                        with progress_context(progress):
                            result = await self.app.view_functions["api_alex"]()
            except Exception as exc:
                result = await asyncio.to_thread(self.app.handle_user_exception, exc)
            response = await asyncio.to_thread(self.app.finalize_request, result)
            return _response(response)

    async def handle(self, request):
        limit = self.app.config.get("MAX_CONTENT_LENGTH") or 256 * 1024
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                with self.app.request_context(_environ(request, b"")):
                    response = await asyncio.to_thread(
                        self.app.finalize_request, ({"error": "request body too large"}, 413),
                    )
                    return _response(response)
        environ = _environ(request, bytes(body))
        if "text/event-stream" not in request.headers.get("accept", ""):
            return await self._execute(environ)

        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        events = asyncio.Queue(maxsize=64)
        closed = False

        def progress(stage):
            def publish():
                if closed:
                    return
                if not ready.done():
                    # Identity is already resolved and the turn has been claimed
                    # when the application reports its first public stage.
                    preview = self.app.process_response(self.app.make_response(""))
                    prepared = getattr(g, "prepared_chat_request", None)
                    if prepared is not None:
                        preview.headers["X-Request-Id"] = prepared.request_id
                    g.chat_streaming = True
                    ready.set_result([
                        (key, value) for key, value in preview.headers.to_wsgi_list()
                        if key.lower() not in {"content-type", "content-length"}
                    ])
                if not events.full():
                    events.put_nowait(("step", {"stage": stage}))
            loop.call_soon_threadsafe(publish)

        async def execute():
            nonlocal closed
            try:
                response = await self._execute(environ, progress)
                if ready.done():
                    await events.put(("final", {
                        "status": response.status_code,
                        "payload": json.loads(response.body),
                    }))
                return response
            finally:
                closed = True

        task = asyncio.create_task(execute())
        try:
            await asyncio.wait({ready, task}, return_when=asyncio.FIRST_COMPLETED)
            if task.done() and not ready.done():
                return task.result()
            headers = ready.result()
        except BaseException:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise

        async def stream():
            try:
                while True:
                    try:
                        event, payload = await asyncio.wait_for(events.get(), timeout=10)
                    except asyncio.TimeoutError:
                        if task.done():
                            task.result()
                            return
                        yield b": keep-alive\n\n"
                        continue
                    yield sse(event, payload)
                    if event == "final":
                        return
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        response = ChatStreamingResponse(stream(), task=task, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        response.raw_headers.extend(
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers
        )
        return response


class ChatStreamingResponse(StreamingResponse):
    """Own execution even if a connection closes before iteration starts."""

    def __init__(self, *args, task, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_task = task

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self.execution_task.done():
                self.execution_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.execution_task
