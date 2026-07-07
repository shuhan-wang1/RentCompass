from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from uk_rent_agent.config import Config
from uk_rent_agent.web.app import create_app


async def health(_request):
    return JSONResponse({"status": "ok", "runtime": "asgi"})


def create_asgi_app(config: Config | None = None) -> Starlette:
    """Production ASGI shell; SSE-native routes can coexist with legacy Flask routes."""
    runtime = config or Config.from_env(require_secret=True)
    flask_app = create_app(runtime)
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=WSGIMiddleware(flask_app)),
        ]
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "uk_rent_agent.web.asgi:create_asgi_app",
        factory=True,
        host="127.0.0.1",
        port=5001,
    )
