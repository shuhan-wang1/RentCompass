from __future__ import annotations

import sys

from starlette.applications import Starlette
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from uk_rent_agent.config import Config
from uk_rent_agent.web.app import create_app


def _canary_identity() -> dict:
    """Pool identity (arch/candidate-sha) from the loaded legacy app module.

    /health is served by Starlette directly and bypasses Flask's after_request hook, so
    without this the one endpoint ops probe first would be the only one NOT identifying
    which canary pool answered. Read the process constants off the already-loaded module;
    degrade to empty if unavailable (identity must never break the health probe)."""
    mod = sys.modules.get("uk_rent_agent._legacy_web_app")
    if mod is None:
        return {}
    try:
        return {
            "X-Agent-Arch": str(getattr(mod, "AGENT_ARCH", "")),
            "X-Agent-Version": str(getattr(mod, "APP_CANDIDATE_SHA", "")),
        }
    except Exception:
        return {}


async def health(_request):
    return JSONResponse({"status": "ok", "runtime": "asgi"}, headers=_canary_identity())


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
