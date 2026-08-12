from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from uk_rent_agent.config import Config
from uk_rent_agent.logging_setup import configure_logging
from uk_rent_agent.web.app import create_app


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")


def _legacy_module():
    return sys.modules.get("uk_rent_agent._legacy_web_app")


def _canary_identity() -> dict[str, str]:
    """Backward-compatible pool identity helper used by existing ops tests."""
    mod = _legacy_module()
    if mod is None:
        return {}
    try:
        return {
            "X-Agent-Arch": str(getattr(mod, "AGENT_ARCH", "")),
            "X-Agent-Version": str(getattr(mod, "APP_CANDIDATE_SHA", "")),
        }
    except Exception:
        return {}


def _release_manifest() -> dict[str, Any]:
    """Return immutable build/runtime identity without consulting git at request time."""
    mod = _legacy_module()
    arch = str(getattr(mod, "AGENT_ARCH", "") or os.getenv("AGENT_ARCH", "legacy"))
    source_sha = str(
        getattr(mod, "APP_CANDIDATE_SHA", "") or os.getenv("APP_SOURCE_SHA", "unknown")
    )
    runtime_specs: dict[str, Any] = {}
    try:
        from core.loop_prompts import get_system_prompt_metadata

        runtime_specs = {
            language: get_system_prompt_metadata(language)
            for language in ("en", "zh")
        }
    except Exception:
        runtime_specs = {}
    return {
        "arch": arch,
        "source_sha": source_sha,
        "image_digest": os.getenv("APP_IMAGE_DIGEST", "unknown"),
        "prompt": {
            "version": os.getenv("PROMPT_VERSION", "unknown"),
            "schema_sha": os.getenv("PROMPT_SCHEMA_SHA", "unknown"),
            "runtime_specs": runtime_specs,
        },
        "routing_mode": os.getenv("ROUTING_MODE", "single_pool"),
        "strict": bool(getattr(mod, "DEEPSEEK_STRICT", False)),
    }


def _identity_headers(manifest: dict[str, Any] | None = None) -> dict[str, str]:
    release = manifest or _release_manifest()
    prompt = release["prompt"]
    return {
        "X-Agent-Arch": str(release["arch"]),
        "X-Agent-Version": str(release["source_sha"]),
        "X-Image-Digest": str(release["image_digest"]),
        "X-Prompt-Version": str(prompt["version"]),
        "X-Prompt-Schema-Sha": str(prompt["schema_sha"]),
    }


def _check_release_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    prompt = manifest["prompt"]
    problems: list[str] = []
    if manifest["arch"] not in {"legacy", "fc_loop"}:
        problems.append("arch")
    if not _FULL_SHA.fullmatch(str(manifest["source_sha"])):
        problems.append("source_sha")
    if not _IMAGE_DIGEST.fullmatch(str(manifest["image_digest"])):
        problems.append("image_digest")
    if str(prompt["version"]) in {"", "unknown"}:
        problems.append("prompt.version")
    if not re.fullmatch(r"[0-9a-f]{64}", str(prompt["schema_sha"])):
        problems.append("prompt.schema_sha")
    runtime_specs = prompt.get("runtime_specs")
    if not isinstance(runtime_specs, dict) or not {"en", "zh"}.issubset(runtime_specs):
        problems.append("prompt.runtime_specs")
    else:
        prompt_ids: set[str] = set()
        for language in ("en", "zh"):
            spec = runtime_specs.get(language)
            prefix = f"prompt.runtime_specs.{language}"
            if not isinstance(spec, dict):
                problems.append(prefix)
                continue
            prompt_id = str(spec.get("prompt_id", ""))
            prompt_version = str(spec.get("prompt_version", ""))
            prompt_hash = str(spec.get("prompt_hash", ""))
            prompt_variant = str(spec.get("prompt_variant", ""))
            if not prompt_id:
                problems.append(f"{prefix}.prompt_id")
            else:
                prompt_ids.add(prompt_id)
            if prompt_version != str(prompt["version"]):
                problems.append(f"{prefix}.prompt_version")
            if prompt_variant != language:
                problems.append(f"{prefix}.prompt_variant")
            if not re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
                problems.append(f"{prefix}.prompt_hash")
        if len(prompt_ids) != 1:
            problems.append("prompt.runtime_specs.prompt_id_consistency")
    required = os.getenv("RELEASE_METADATA_REQUIRED", "0").lower() in {"1", "true", "yes"}
    return {
        "status": "ok" if not problems else ("fail" if required else "degraded"),
        "required": required,
        "missing_or_invalid": problems,
    }


def _check_conversation_db() -> dict[str, Any]:
    mod = _legacy_module()
    store = getattr(mod, "conversation_store", None)
    if store is None:
        return {"status": "fail", "required": True, "detail": "store unavailable"}
    try:
        db_parent = Path(store.db_path).parent
        if not db_parent.is_dir() or not os.access(db_parent, os.R_OK | os.W_OK):
            raise PermissionError("conversation runtime directory is not writable")
        with store._lock:
            row = store._conn.execute("PRAGMA quick_check(1)").fetchone()
        verdict = row[0] if row else "no result"
        if verdict != "ok":
            raise RuntimeError(str(verdict))
        return {"status": "ok", "required": True}
    except Exception as exc:
        return {
            "status": "fail",
            "required": True,
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


def _check_agent_memory() -> dict[str, Any]:
    """Durable long-term memory and any legacy-copy migration are required."""
    try:
        from rag.agent_memory import get_agent_memory

        result = dict(get_agent_memory().health())
        if result.get("status") != "ok":
            raise RuntimeError("memory health probe did not return ok")
        result["required"] = True
        return result
    except Exception as exc:
        return {
            "status": "fail",
            "required": True,
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


def _check_background_jobs() -> dict[str, Any]:
    """Durable memory/summary outbox health; dead work degrades but does not drop chat."""
    mod = _legacy_module()
    store = getattr(mod, "conversation_store", None)
    counts = getattr(store, "background_job_counts", None)
    if not callable(counts):
        return {"status": "disabled", "required": False}
    try:
        values = {str(key): int(value) for key, value in counts().items()}
        dead = int(values.get("dead", 0))
        return {
            "status": "degraded" if dead else "ok",
            "required": False,
            "counts": values,
            **({"detail": "dead durable background jobs require operator action"} if dead else {}),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "required": False,
            "detail": f"outbox probe failed: {type(exc).__name__}",
        }


def _check_canary_sink() -> dict[str, Any]:
    mod = _legacy_module()
    probe = getattr(mod, "canary_sink_health", None)
    if not callable(probe):
        return {"status": "degraded", "required": False, "detail": "sink probe unavailable"}
    try:
        result = dict(probe())
        result.setdefault("required", False)
        return result
    except Exception as exc:
        return {
            "status": "degraded",
            "required": False,
            "detail": f"sink probe failed: {type(exc).__name__}",
        }


def _check_checkpoint(config: Config) -> dict[str, Any]:
    if not config.enable_checkpointer:
        return {"status": "disabled", "required": False}
    path = config.checkpoint_path
    if path is None:
        return {"status": "fail", "required": True, "detail": "path is unset"}
    parent = Path(path).parent
    if not parent.is_dir() or not os.access(parent, os.R_OK | os.W_OK):
        return {"status": "fail", "required": True, "detail": "runtime directory unavailable"}
    try:
        from uk_rent_agent.agent.persistence import get_sqlite_checkpointer

        if get_sqlite_checkpointer(path) is None:
            raise RuntimeError("sqlite checkpoint backend unavailable")
        return {"status": "ok", "required": True}
    except Exception as exc:
        return {
            "status": "fail",
            "required": True,
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


def _check_auth_store(config: Config) -> dict[str, Any]:
    if not config.require_auth:
        return {"status": "disabled", "required": False}
    mod = _legacy_module()
    if getattr(mod, "auth_store", None) is None or config.auth_db_path is None:
        return {"status": "fail", "required": True, "detail": "auth store unavailable"}
    parent = Path(config.auth_db_path).parent
    if not parent.is_dir() or not os.access(parent, os.R_OK | os.W_OK):
        return {"status": "fail", "required": True, "detail": "auth runtime directory unavailable"}
    return {"status": "ok", "required": True}


def _check_rag() -> dict[str, Any]:
    """RAG is an honest degradation: deterministic listing search remains available."""
    mod = _legacy_module()
    coordinator = getattr(mod, "rag_coordinator", None)
    if coordinator is None:
        return {"status": "degraded", "required": False, "detail": "embedding RAG unavailable"}
    properties = getattr(mod, "all_properties", None)
    if not properties:
        return {"status": "degraded", "required": False, "detail": "property corpus is empty"}
    store = getattr(coordinator, "property_store", None)
    ready = getattr(store, "is_ready", None)
    if callable(ready):
        try:
            if not ready():
                return {"status": "degraded", "required": False, "detail": "index warming"}
        except Exception as exc:
            return {
                "status": "degraded",
                "required": False,
                "detail": f"readiness probe failed: {type(exc).__name__}",
            }
    return {"status": "ok", "required": False}


def _check_searx() -> dict[str, Any]:
    """Probe SearXNG's process health endpoint; never issue a metasearch."""
    # Match the web-search client's local-development default. Compose overrides
    # this with the internal service URL.
    base = os.getenv("SEARXNG_URL", "http://localhost:8080").strip()
    if not base:
        return {"status": "disabled", "required": False}
    try:
        timeout = max(0.1, float(os.getenv("READINESS_SEARX_TIMEOUT_SECONDS", "1.5")))
        with urllib.request.urlopen(base.rstrip("/") + "/healthz", timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
        return {"status": "ok", "required": False}
    except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
        return {
            "status": "degraded",
            "required": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


def _check_llm_configuration() -> dict[str, Any]:
    provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    required = os.getenv("READINESS_REQUIRE_LLM", "0").lower() in {"1", "true", "yes"}
    if provider not in {"deepseek", "ollama"}:
        return {
            "status": "fail" if required else "degraded",
            "required": required,
            "detail": f"unsupported provider: {provider or '<empty>'}",
        }
    missing = provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY", "").strip()
    if missing:
        return {
            "status": "fail" if required else "degraded",
            "required": required,
            "detail": "provider credential unavailable",
        }
    return {"status": "ok", "required": required, "provider": provider}


async def live(_request):
    manifest = _release_manifest()
    return JSONResponse(
        {"status": "live", "runtime": "asgi", "release": manifest},
        headers=_identity_headers(manifest),
    )


def _readiness(config: Config) -> tuple[dict[str, Any], int]:
    manifest = _release_manifest()
    checks = {
        "release_metadata": _check_release_metadata(manifest),
        "conversation_db": _check_conversation_db(),
        "agent_memory": _check_agent_memory(),
        "background_jobs": _check_background_jobs(),
        "canary_sink": _check_canary_sink(),
        "checkpoint_store": _check_checkpoint(config),
        "auth_store": _check_auth_store(config),
        "llm_configuration": _check_llm_configuration(),
        "rag": _check_rag(),
        "searxng": _check_searx(),
    }
    failed = [
        name
        for name, result in checks.items()
        if result["required"] and result["status"] != "ok"
    ]
    degraded = [
        name
        for name, result in checks.items()
        if result["status"] in {"degraded", "disabled"}
    ]
    body = {
        "status": "not_ready" if failed else "ready",
        "runtime": "asgi",
        "release": manifest,
        "checks": checks,
        "failed": failed,
        "degraded": degraded,
    }
    return body, (503 if failed else 200)


def create_asgi_app(config: Config | None = None) -> Starlette:
    """Production ASGI shell; SSE-native routes can coexist with legacy Flask routes."""
    configure_logging()
    runtime = config or Config.from_env(require_secret=True)
    flask_app = create_app(runtime)

    async def ready(_request):
        body, status_code = await asyncio.to_thread(_readiness, runtime)
        return JSONResponse(
            body,
            status_code=status_code,
            headers=_identity_headers(body["release"]),
        )

    return Starlette(
        routes=[
            Route("/live", live, methods=["GET"]),
            Route("/ready", ready, methods=["GET"]),
            # Backward compatibility for pre-readiness operators. Deployment
            # gates deliberately use /ready.
            Route("/health", live, methods=["GET"]),
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
