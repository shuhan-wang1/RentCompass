from __future__ import annotations

import asyncio
import inspect
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

from uk_rent_agent.agent.architecture import (
    MANAGER_V1_ARCH,
    SUPPORTED_AGENT_ARCHES,
    uses_fc_runtime,
)
from uk_rent_agent.config import Config
from uk_rent_agent.logging_setup import configure_logging
from uk_rent_agent.web.app import create_app


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")

_EXPECTED_TOOL_NAMES = frozenset({
    "search_properties",
    "calculate_commute",
    "calculate_commute_cost",
    "check_safety",
    "get_weather",
    "web_search",
    "search_nearby_pois",
    "get_property_details",
    "check_transport_cost",
    "get_transport_info",
    "recall_memory",
    "remember",
    "ask_user",
    "compare_or_rank_areas",
})


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
            "X-Agent-Specialists": (
                "1" if bool(getattr(mod, "MANAGER_V1_SPECIALISTS", False)) else "0"
            ),
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
        "manager_v1_specialists": bool(
            getattr(mod, "MANAGER_V1_SPECIALISTS", False)
        ),
    }


def _identity_headers(manifest: dict[str, Any] | None = None) -> dict[str, str]:
    release = manifest or _release_manifest()
    prompt = release["prompt"]
    return {
        "X-Agent-Arch": str(release["arch"]),
        "X-Agent-Version": str(release["source_sha"]),
        "X-Agent-Specialists": (
            "1" if bool(release.get("manager_v1_specialists", False)) else "0"
        ),
        "X-Image-Digest": str(release["image_digest"]),
        "X-Prompt-Version": str(prompt["version"]),
        "X-Prompt-Schema-Sha": str(prompt["schema_sha"]),
    }


def _check_release_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    prompt = manifest["prompt"]
    problems: list[str] = []
    if manifest["arch"] not in SUPPORTED_AGENT_ARCHES:
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
        db_path = Path(store.db_path)
        db_parent = db_path.parent
        if not db_parent.is_dir() or not os.access(db_parent, os.R_OK | os.W_OK):
            raise PermissionError("conversation runtime directory is not writable")
        if db_path.exists() and (
            not db_path.is_file() or not os.access(db_path, os.R_OK | os.W_OK)
        ):
            raise PermissionError("conversation database is not readable/writable")
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
    checkpoint = Path(path)
    parent = checkpoint.parent
    if not parent.is_dir() or not os.access(parent, os.R_OK | os.W_OK):
        return {"status": "fail", "required": True, "detail": "runtime directory unavailable"}
    if checkpoint.exists() and (
        not checkpoint.is_file() or not os.access(checkpoint, os.R_OK | os.W_OK)
    ):
        return {"status": "fail", "required": True,
                "detail": "checkpoint database is not readable/writable"}
    identity = config.checkpoint_identity
    try:
        from uk_rent_agent.agent.persistence import get_sqlite_checkpointer

        # Passing the identity explicitly is what makes the per-arch checkpoint
        # separation enforced rather than conventional: opening a file that another
        # architecture already stamped raises here, so readiness fails instead of the
        # pool quietly resuming foreign LangGraph state.
        if get_sqlite_checkpointer(path, identity=identity) is None:
            raise RuntimeError("sqlite checkpoint backend unavailable")
        return {"status": "ok", "required": True, "identity": identity}
    except Exception as exc:
        # `CheckpointIdentityError` is the one exception here whose TAIL is the
        # useful part: it ends with the path and "Point CHECKPOINT_DB_PATH at this
        # runtime's own file ...". The message runs to ~450 characters, so a 200
        # cap cut the path in half and dropped the remediation entirely, leaving
        # `/ready` showing a diagnosis with no fix. It keeps its full message; the
        # generic cap still bounds every other (unbounded) exception string.
        limit = 200
        if type(exc).__name__ == "CheckpointIdentityError":
            limit = 600
        return {
            "status": "fail",
            "required": True,
            "identity": identity,
            "path": str(path),
            "detail": f"{type(exc).__name__}: {str(exc)[:limit]}",
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


def _non_null_schema(schema: Any) -> dict[str, Any]:
    """Return the concrete branch of a Pydantic Optional schema."""
    if not isinstance(schema, dict):
        return {}
    branches = schema.get("anyOf")
    if isinstance(branches, list):
        for branch in branches:
            if isinstance(branch, dict) and branch.get("type") != "null":
                return branch
    return schema


def _check_tool_registry() -> dict[str, Any]:
    """Validate the live 14-tool registry and the runtime/schema adapter contract."""
    mod = _legacy_module()
    registry = getattr(mod, "tool_registry", None)
    if registry is None:
        return {"status": "fail", "required": True, "detail": "registry unavailable"}
    try:
        specs = list(registry.list_specs())
        by_name = {str(spec.name): spec for spec in specs}
        names = set(by_name)
        if names != _EXPECTED_TOOL_NAMES or len(specs) != len(_EXPECTED_TOOL_NAMES):
            missing = sorted(_EXPECTED_TOOL_NAMES - names)
            extra = sorted(names - _EXPECTED_TOOL_NAMES)
            raise RuntimeError(
                f"expected 14 unique tools; missing={missing!r}, extra={extra!r}, "
                f"spec_count={len(specs)}"
            )

        from core.strict_schema import to_strict_schema, validate_strict_compliance

        for name, spec in by_name.items():
            schema = spec.input_schema
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise RuntimeError(f"{name}: input schema is not an object")
            if not isinstance(schema.get("properties"), dict):
                raise RuntimeError(f"{name}: input schema properties unavailable")
            violations = validate_strict_compliance(to_strict_schema(schema))
            if violations:
                raise RuntimeError(f"{name}: strict schema violations: {violations[:2]!r}")

        def prop(tool_name: str, field_name: str) -> dict[str, Any]:
            schema = by_name[tool_name].input_schema
            return _non_null_schema(schema["properties"][field_name])

        expected_constraints = (
            ("calculate_commute.mode", prop("calculate_commute", "mode").get("enum"),
             ["transit", "driving", "walking", "bicycling"]),
            ("ask_user.clarification_kind",
             prop("ask_user", "clarification_kind").get("enum"),
             ["missing_area", "soft_criteria", "other"]),
            ("check_transport_cost.end_zone",
             prop("check_transport_cost", "end_zone").get("enum"), [2, 3, 4, 5, 6]),
            ("get_transport_info.end_zone.minimum",
             prop("get_transport_info", "end_zone").get("minimum"), 2),
            ("get_transport_info.end_zone.maximum",
             prop("get_transport_info", "end_zone").get("maximum"), 6),
        )
        for label, actual, expected in expected_constraints:
            if actual != expected:
                raise RuntimeError(f"{label}: expected {expected!r}, got {actual!r}")

        sub_queries_schema = prop("web_search", "sub_queries")
        nested = sub_queries_schema.get("items", {})
        if nested.get("type") != "object" or not {"tool", "params"}.issubset(
            set(nested.get("required", []))
        ):
            raise RuntimeError("web_search.sub_queries item contract is incomplete")
        allowed_nested = {
            "check_safety", "get_weather", "search_nearby_pois", "get_property_details",
            "calculate_commute", "web_search_only",
        }
        nested_tool = nested.get("properties", {}).get("tool", {})
        if set(nested_tool.get("enum", [])) != allowed_nested:
            raise RuntimeError("web_search nested-tool allowlist schema is incomplete")
        if nested.get("additionalProperties") is not False:
            raise RuntimeError("web_search sub-query objects are not closed")
        if sub_queries_schema.get("maxItems") != 6:
            raise RuntimeError("web_search nested fan-out bound is unavailable")

        # Prove the emitted constraints are also installed on the Pydantic runtime models.
        invalid_examples = {
            "calculate_commute": {
                "from_address": "A", "to_address": "B", "mode": "tube",
            },
            "ask_user": {"question": "Q?", "clarification_kind": "invalid"},
            "get_transport_info": {"query_type": "travelcard", "end_zone": 99},
            "web_search": {"query": "x", "sub_queries": [{"tool": "check_safety"}]},
        }
        for tool_name, payload in invalid_examples.items():
            tool = registry.get(tool_name)
            model = getattr(tool, "input_model", None)
            if model is None:
                raise RuntimeError(f"{tool_name}: runtime input model unavailable")
            try:
                model.model_validate(payload)
            except Exception:
                pass
            else:
                raise RuntimeError(f"{tool_name}: runtime model accepted invalid input")

        return {
            "status": "ok",
            "required": True,
            "tool_count": len(specs),
            "strict_schemas": "valid",
            "runtime_constraints": "valid",
        }
    except Exception as exc:
        return {
            "status": "fail",
            "required": True,
            "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _check_runtime_configuration(config: Config) -> dict[str, Any]:
    """Ensure ASGI, Flask, graph selection and model factories share one Config."""
    mod = _legacy_module()
    if mod is None:
        return {"status": "fail", "required": True, "detail": "Flask runtime unavailable"}
    problems: list[str] = []
    loaded = getattr(mod, "_runtime_config", None)
    if loaded is None:
        problems.append("Flask Config unavailable")
    else:
        for field in (
            "agent_arch",
            "manager_v1_specialists",
            "deepseek_strict",
            "llm_provider",
        ):
            default = False if field == "manager_v1_specialists" else None
            if getattr(loaded, field, default) != getattr(config, field):
                problems.append(f"Config.{field} mismatch")
    if str(getattr(mod, "AGENT_ARCH", "")) != config.agent_arch:
        problems.append("AGENT_ARCH mismatch")
    if bool(getattr(mod, "DEEPSEEK_STRICT", False)) != config.deepseek_strict:
        problems.append("DEEPSEEK_STRICT mismatch")
    if bool(getattr(mod, "MANAGER_V1_SPECIALISTS", False)) != (
        config.manager_v1_specialists_effective
    ):
        problems.append("MANAGER_V1_SPECIALISTS mismatch")
    llm_module = sys.modules.get("core.llm_config")
    if llm_module is not None and str(getattr(llm_module, "LLM_PROVIDER", "")) != config.llm_provider:
        problems.append("LLM_PROVIDER factory mismatch")
    # R1-M3. `app/app.py` picks the tool provider from the RAW environment
    # (`USE_MCP_TOOLS not in ("0", "false", "no")`) — a DIFFERENT rule from
    # `Config.use_mcp_tools`, so spellings like `off` or an empty string mean
    # "off" to the config and "on" to the app. With specialists effective that
    # disagreement is not cosmetic: `MCPToolClient` exposes neither
    # `resolve_specialist_capability` nor `execute_resolved_specialist_capability`,
    # so `build_manager_v1_graph` raises a bare RuntimeError out of graph
    # construction on the first turn. Compare against the provider the Flask
    # module ACTUALLY holds rather than deriving the answer a third time.
    provider = getattr(mod, "agent_tool_provider", None)
    registry = getattr(mod, "tool_registry", None)
    serving_over_mcp = (
        provider is not None and registry is not None and provider is not registry
    )
    if serving_over_mcp:
        if config.manager_v1_specialists_effective:
            problems.append(Config.MCP_SPECIALISTS_CONFLICT)
        elif not config.use_mcp_tools:
            problems.append(
                "USE_MCP_TOOLS mismatch: tools are being served over MCP while "
                "Config.use_mcp_tools is False — check the spelling in the "
                "environment ('off' and '' are false to Config and true to app.py)"
            )
    return {
        "status": "fail" if problems else "ok",
        "required": True,
        "agent_arch": config.agent_arch,
        "manager_v1_specialists": config.manager_v1_specialists_effective,
        "use_mcp_tools": config.use_mcp_tools,
        "tools_over_mcp": serving_over_mcp,
        "llm_provider": config.llm_provider,
        "deepseek_strict": config.deepseek_strict,
        **({"detail": "; ".join(problems)} if problems else {}),
    }


def _check_agent_graph(config: Config) -> dict[str, Any]:
    """Accept an already-compiled graph or validate the complete lazy factory path."""
    mod = _legacy_module()
    if mod is None:
        return {"status": "fail", "required": True, "detail": "Flask runtime unavailable"}
    try:
        graph = getattr(mod, "agent_graph", None)
        initializer = getattr(mod, "_ensure_agent_runtime", None)
        if graph is None and callable(initializer):
            # Readiness is the startup gate: compile the real graph here (no model call is
            # made) instead of declaring an unexecuted factory signature healthy.
            graph = initializer()
            if graph is None:
                raise RuntimeError("runtime initializer returned no graph")
        if graph is not None:
            if not any(callable(getattr(graph, name, None)) for name in ("invoke", "ainvoke")):
                raise RuntimeError("compiled graph has no invoke/ainvoke entry point")
            return {
                "status": "ok", "required": True, "state": "compiled",
                "arch": config.agent_arch,
            }

        factory = getattr(mod, "build_agent_graph", None)
        initial_state_factory = getattr(mod, "create_initial_state", None)
        if not callable(factory) or not callable(initial_state_factory):
            raise RuntimeError("graph factory or initial-state factory unavailable")
        parameters = inspect.signature(factory).parameters
        if "tool_registry" not in parameters or "agent_llm" not in parameters:
            raise RuntimeError("graph factory signature is incompatible")
        provider = getattr(mod, "agent_tool_provider", None)
        if provider is None or not callable(getattr(provider, "list_specs", None)):
            raise RuntimeError("agent tool provider unavailable")
        if uses_fc_runtime(config.agent_arch):
            if config.agent_arch == MANAGER_V1_ARCH:
                from core.manager_v1 import build_manager_v1_graph as loop_graph_builder
            else:
                from core.agent_loop import build_fc_graph as loop_graph_builder
            if not callable(loop_graph_builder):
                raise RuntimeError(
                    f"{config.agent_arch} graph builder unavailable"
                )
            # Ollama must be injectable; the default fc driver is DeepSeek-specific.
            if config.llm_provider == "ollama":
                from core.llm_config import get_react_llm
                if not callable(get_react_llm):
                    raise RuntimeError("configured Ollama model factory unavailable")
                if not callable(getattr(mod, "_configured_fc_agent_llm", None)):
                    raise RuntimeError(
                        "Flask runtime lacks the Ollama FC-runtime injection hook"
                    )

        # Compatibility path for isolated factories that do not expose the Flask runtime
        # initializer. Actually compile and validate the result before declaring readiness.
        model_factory = getattr(mod, "_configured_fc_agent_llm", None)
        probe_llm = model_factory() if callable(model_factory) else object()
        accepts_extra_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        accepts_specialist_flag = (
            "manager_v1_specialists" in parameters or accepts_extra_kwargs
        )
        if config.manager_v1_specialists_effective and not accepts_specialist_flag:
            raise RuntimeError(
                "graph factory cannot receive the enabled manager_v1 specialist flag"
            )
        factory_kwargs = {"agent_llm": probe_llm}
        if accepts_specialist_flag:
            factory_kwargs["manager_v1_specialists"] = (
                config.manager_v1_specialists_effective
            )
        graph = factory(provider, **factory_kwargs)
        if not any(callable(getattr(graph, name, None)) for name in ("invoke", "ainvoke")):
            raise RuntimeError("graph factory returned no invoke/ainvoke entry point")

        return {
            "status": "ok", "required": True, "state": "factory_compiled",
            "arch": config.agent_arch, "provider": config.llm_provider,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "required": True,
            "detail": f"graph initialization failed ({type(exc).__name__})",
        }


def _check_searx() -> dict[str, Any]:
    """Probe only SearXNG process health; report result capability as unknown."""
    # Match the web-search client's local-development default. Compose overrides
    # this with the internal service URL.
    base = os.getenv("SEARXNG_URL", "http://localhost:8080").strip()
    if not base:
        return {
            "status": "disabled",
            "required": False,
            "process_health": "disabled",
            "search_result_capability": "disabled",
        }
    try:
        timeout = max(0.1, float(os.getenv("READINESS_SEARX_TIMEOUT_SECONDS", "1.5")))
        with urllib.request.urlopen(base.rstrip("/") + "/healthz", timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
        return {
            "status": "degraded",
            "required": False,
            "process_health": "ok",
            "search_result_capability": "unknown",
            "detail": "healthz responded; upstream engines/results are not exercised by readiness",
        }
    except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
        return {
            "status": "degraded",
            "required": False,
            "process_health": "unavailable",
            "search_result_capability": "unknown",
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


def _unprobed_provider(provider: str, tools: list[str]) -> dict[str, Any]:
    return {
        "status": "degraded",
        "required": False,
        "provider": provider,
        "capability": "unknown",
        "tools": tools,
        "detail": "live provider is intentionally not called by readiness",
    }


def _check_onthemarket(config: Config) -> dict[str, Any]:
    """Live scraping is not a safe readiness dependency; make the unknown explicit."""
    return {
        "status": "degraded",
        "required": False,
        "provider": "OnTheMarket",
        "capability": "unknown",
        "property_source": config.property_source,
        "detail": "live scrape/schema compatibility is not probed; cached results may still work",
    }


def _check_llm_configuration(config: Config) -> dict[str, Any]:
    provider = config.llm_provider
    required = os.getenv("READINESS_REQUIRE_LLM", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    try:
        from uk_rent_agent.llm.router import llm_max_retries, llm_request_timeout_seconds
        transport = {
            "timeout_seconds": llm_request_timeout_seconds(),
            "max_retries": llm_max_retries(),
        }
    except (TypeError, ValueError) as exc:
        return {"status": "fail", "required": True, "detail": type(exc).__name__}
    missing = provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY", "").strip()
    if missing:
        return {
            "status": "fail" if required else "degraded",
            "required": required,
            "detail": "provider credential unavailable",
        }
    return {
        "status": "ok",
        "required": required,
        "provider": provider,
        "transport": transport,
        "connectivity": "not_probed",
    }


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
        "runtime_configuration": _check_runtime_configuration(config),
        "tool_registry": _check_tool_registry(),
        "agent_graph": _check_agent_graph(config),
        "conversation_db": _check_conversation_db(),
        "agent_memory": _check_agent_memory(),
        "background_jobs": _check_background_jobs(),
        "canary_sink": _check_canary_sink(),
        "checkpoint_store": _check_checkpoint(config),
        "auth_store": _check_auth_store(config),
        "llm_configuration": _check_llm_configuration(config),
        "rag": _check_rag(),
        "searxng": _check_searx(),
        "onthemarket": _check_onthemarket(config),
        "tfl": _unprobed_provider(
            "Transport for London", ["calculate_commute", "calculate_commute_cost",
                                      "get_transport_info"],
        ),
        "police_data": _unprobed_provider("data.police.uk", ["check_safety"]),
        "openstreetmap": _unprobed_provider(
            "Nominatim/Overpass", ["search_nearby_pois"],
        ),
        "weather": _unprobed_provider("Open-Meteo", ["get_weather"]),
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
