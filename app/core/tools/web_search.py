"""
Web Search Tool - intelligent search coordinator.

``sub_queries`` is an LLM-controlled orchestration surface, not a trusted internal API.
Only the documented set of read-only tools below may be dispatched from here. Registry
metadata is checked at runtime so registration changes cannot silently expose a write,
terminal, or memory tool through this nested dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from core.tool_system import Tool
from core.web_search import get_search_snippets
from uk_rent_agent.agent.critic import evidence_usable as _evidence_usable


logger = logging.getLogger(__name__)

# Deliberately code-level: deriving this from the registry would expose every registered tool.
_ALLOWED_NESTED_TOOLS = frozenset({
    "check_safety",
    "get_weather",
    "search_nearby_pois",
    "get_property_details",
    "calculate_commute",
    "web_search_only",
})
_SELF_TOOL_NAME = "web_search"

# Bound fan-out and hostile model-produced object graphs before any dispatch occurs.
_MAX_SUB_QUERIES = 6
_MAX_PARAM_DEPTH = 5
_MAX_PARAM_NODES = 256
_MAX_CONTAINER_ITEMS = 32
_MAX_PARAM_STRING_CHARS = 4096
_MAX_PARAM_KEY_CHARS = 128
_MAX_PARAM_TEXT_CHARS = 16384
_MAX_QUERY_CHARS = 4096

_tool_registry = None


def set_tool_registry(registry):
    """Set the registry used for safe sibling-tool dispatch."""
    global _tool_registry
    _tool_registry = registry
    logger.info("Web-search tool registry configured")


def _query_metadata(query: Any) -> Dict[str, int]:
    """Return non-sensitive request metadata suitable for logs and envelopes."""
    return {"length": len(query) if isinstance(query, str) else 0}


def _failure(reason: str, query: Any, *, subquery_count: int = 0) -> dict:
    """Build a rejection response without reflecting attacker-controlled values."""
    logger.warning(
        "Web-search request rejected reason=%s query_chars=%d subquery_count=%d",
        reason,
        _query_metadata(query)["length"],
        subquery_count,
    )
    return {
        "success": False,
        "error": "Invalid or unauthorized web-search request",
        "error_code": reason,
        "query_metadata": _query_metadata(query),
        "results": "",
        "detailed_data": {},
    }


def _metadata_value(candidate: Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _registry_metadata(registry: Any, tool_name: str) -> List[Any]:
    """Collect concrete-tool and ToolSpec metadata exposed by the registry."""
    candidates: List[Any] = []

    for getter_name in ("get", "get_tool"):
        getter = getattr(registry, getter_name, None)
        if callable(getter):
            try:
                candidate = getter(tool_name)
            except Exception as exc:
                logger.warning(
                    "Nested-tool metadata lookup failed source=%s exception_type=%s",
                    getter_name,
                    type(exc).__name__,
                )
                continue
            if candidate is not None:
                candidates.append(candidate)
                break

    list_specs = getattr(registry, "list_specs", None)
    if callable(list_specs):
        try:
            specs = list_specs()
            if isinstance(specs, (list, tuple)):
                candidates.extend(
                    spec for spec in specs
                    if _metadata_value(spec, "name") == tool_name
                )
        except Exception as exc:
            logger.warning(
                "Nested-tool spec lookup failed exception_type=%s", type(exc).__name__
            )

    return candidates


def _is_memory_tool(tool_name: str, candidates: List[Any]) -> bool:
    """Identify memory tools by stable name and optional registry classification."""
    lowered_name = tool_name.lower()
    if lowered_name == "remember" or "memory" in lowered_name:
        return True

    for candidate in candidates:
        for field in ("category", "kind", "tool_type"):
            value = _metadata_value(candidate, field, "")
            if isinstance(value, str) and "memory" in value.lower():
                return True

        func = _metadata_value(candidate, "func")
        module_names = (
            getattr(func, "__module__", ""),
            getattr(candidate.__class__, "__module__", ""),
        )
        if any("memory" in module_name.lower() for module_name in module_names):
            return True

    return False


def _nested_tool_policy(registry: Any, tool_name: str) -> Tuple[bool, str]:
    """Dynamically verify that an allowlisted local tool is still safe to nest."""
    candidates = _registry_metadata(registry, tool_name)
    if not candidates:
        return False, "nested_tool_metadata_unavailable"

    missing = object()
    side_effects = [
        _metadata_value(candidate, "side_effect", missing) for candidate in candidates
    ]
    terminals = [
        _metadata_value(candidate, "terminal", missing) for candidate in candidates
    ]

    # Missing security metadata is not equivalent to read-only/non-terminal.
    if all(value is missing for value in side_effects) or all(
        value is missing for value in terminals
    ):
        return False, "nested_tool_metadata_incomplete"
    if any(
        value is not missing and str(value).lower() != "none"
        for value in side_effects
    ):
        return False, "nested_tool_side_effect_forbidden"
    if any(value is not missing and bool(value) for value in terminals):
        return False, "nested_terminal_tool_forbidden"
    if _is_memory_tool(tool_name, candidates):
        return False, "nested_memory_tool_forbidden"

    return True, ""


def _validate_param_shape(params: Any) -> Optional[str]:
    """Validate bounded, acyclic, JSON-compatible nested-tool parameters."""
    if not isinstance(params, dict):
        return "nested_params_must_be_object"

    stack: List[Tuple[Any, int]] = [(params, 0)]
    seen_containers = set()
    nodes = 0
    text_chars = 0

    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_PARAM_NODES:
            return "nested_params_too_large"
        if depth > _MAX_PARAM_DEPTH:
            return "nested_params_too_deep"

        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                return "nested_params_cycle"
            seen_containers.add(identity)
            if len(value) > _MAX_CONTAINER_ITEMS:
                return "nested_params_too_many_items"
            for key, child in value.items():
                if not isinstance(key, str):
                    return "nested_params_invalid_key"
                if len(key) > _MAX_PARAM_KEY_CHARS:
                    return "nested_params_key_too_long"
                text_chars += len(key)
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                return "nested_params_cycle"
            seen_containers.add(identity)
            if len(value) > _MAX_CONTAINER_ITEMS:
                return "nested_params_too_many_items"
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > _MAX_PARAM_STRING_CHARS:
                return "nested_params_string_too_long"
            text_chars += len(value)
        elif value is None or isinstance(value, (bool, int)):
            pass
        elif isinstance(value, float):
            if not math.isfinite(value):
                return "nested_params_non_finite_number"
        else:
            return "nested_params_not_json"

        if text_chars > _MAX_PARAM_TEXT_CHARS:
            return "nested_params_too_large"

    return None


def _preflight_sub_queries(
    query: Any,
    sub_queries: Any,
    registry: Any,
) -> Tuple[Optional[List[Tuple[str, Dict[str, Any]]]], Optional[dict]]:
    """Validate the complete batch before dispatching any member."""
    if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
        return None, _failure("invalid_query", query)
    if not isinstance(sub_queries, list):
        return None, _failure("sub_queries_must_be_array", query)
    if len(sub_queries) > _MAX_SUB_QUERIES:
        return None, _failure(
            "too_many_sub_queries", query, subquery_count=len(sub_queries)
        )
    if sub_queries and registry is None:
        return None, _failure(
            "nested_tool_registry_unavailable",
            query,
            subquery_count=len(sub_queries),
        )

    prepared: List[Tuple[str, Dict[str, Any]]] = []
    for sub_query in sub_queries:
        if not isinstance(sub_query, dict):
            return None, _failure(
                "sub_query_must_be_object", query, subquery_count=len(sub_queries)
            )
        if set(sub_query) != {"tool", "params"}:
            return None, _failure(
                "invalid_sub_query_fields", query, subquery_count=len(sub_queries)
            )

        tool_name = sub_query.get("tool")
        params = sub_query.get("params")
        if not isinstance(tool_name, str):
            return None, _failure(
                "invalid_nested_tool_name", query, subquery_count=len(sub_queries)
            )
        if tool_name == _SELF_TOOL_NAME:
            return None, _failure(
                "nested_self_recursion_forbidden",
                query,
                subquery_count=len(sub_queries),
            )
        if tool_name not in _ALLOWED_NESTED_TOOLS:
            return None, _failure(
                "nested_tool_not_allowed", query, subquery_count=len(sub_queries)
            )

        params_error = _validate_param_shape(params)
        if params_error:
            return None, _failure(
                params_error, query, subquery_count=len(sub_queries)
            )

        if tool_name == "web_search_only":
            if set(params) - {"query"}:
                return None, _failure(
                    "invalid_web_search_only_params",
                    query,
                    subquery_count=len(sub_queries),
                )
            search_query = params.get("query", query)
            if not isinstance(search_query, str) or len(search_query) > _MAX_QUERY_CHARS:
                return None, _failure(
                    "invalid_nested_query", query, subquery_count=len(sub_queries)
                )
        else:
            allowed, reason = _nested_tool_policy(registry, tool_name)
            if not allowed:
                return None, _failure(reason, query, subquery_count=len(sub_queries))

        prepared.append((tool_name, params))

    return prepared, None


async def web_search_func(query: str, sub_queries: Optional[List[Dict]] = None) -> dict:
    """Run a plain web search or a bounded batch of safe, read-only sibling tools."""
    registry = _tool_registry
    query_meta = _query_metadata(query)
    logger.info(
        "Web-search request started query_chars=%d nested=%s subquery_count=%d",
        query_meta["length"],
        sub_queries is not None,
        len(sub_queries) if isinstance(sub_queries, list) else 0,
    )

    try:
        if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
            return _failure("invalid_query", query)

        results_parts = []
        all_data = {}

        if sub_queries:
            prepared, rejected = _preflight_sub_queries(query, sub_queries, registry)
            if rejected is not None:
                return rejected
            assert prepared is not None

            logger.info("Executing nested web-search batch count=%d", len(prepared))
            for index, (tool_name, params) in enumerate(prepared, 1):
                logger.info(
                    "Nested web-search dispatch index=%d total=%d tool=%s param_count=%d",
                    index,
                    len(prepared),
                    tool_name,
                    len(params),
                )

                if tool_name == "web_search_only":
                    search_query = params.get("query", query)
                    web_result = await asyncio.to_thread(
                        get_search_snippets, search_query, 5
                    )
                    results_parts.append(
                        f"### Web Search (query length: {len(search_query)})"
                    )
                    results_parts.append(web_result)
                    all_data[f"web_search_{index}"] = web_result
                else:
                    # Re-check immediately before dispatch to close mutable-registry races.
                    allowed, reason = _nested_tool_policy(registry, tool_name)
                    if not allowed:
                        return _failure(
                            reason, query, subquery_count=len(prepared)
                        )

                    try:
                        tool_result = await registry.execute_tool(tool_name, **params)
                    except Exception as exc:
                        logger.warning(
                            "Nested tool raised tool=%s exception_type=%s",
                            tool_name,
                            type(exc).__name__,
                        )
                        results_parts.append(f"### {tool_name}: ERROR")
                        results_parts.append("Error: nested tool execution failed")
                    else:
                        if tool_result.success:
                            results_parts.append(f"### {tool_name}")
                            results_parts.append(
                                json.dumps(tool_result.data, ensure_ascii=False, indent=2)
                            )
                            all_data[f"{tool_name}_{index}"] = tool_result.data
                            logger.info("Nested tool succeeded tool=%s", tool_name)
                        else:
                            results_parts.append(f"### {tool_name}: FAILED")
                            results_parts.append("Error: nested tool returned failure")
                            logger.warning("Nested tool failed tool=%s", tool_name)

                results_parts.append("")
        else:
            # An explicit empty list has the same semantics as omitting sub_queries.
            if sub_queries is not None:
                prepared, rejected = _preflight_sub_queries(
                    query, sub_queries, registry
                )
                if rejected is not None:
                    return rejected
                assert prepared == []

            logger.info("Executing plain web search query_chars=%d", query_meta["length"])
            web_result = await asyncio.to_thread(get_search_snippets, query, 5)

            if not _evidence_usable(web_result):
                logger.warning(
                    "Web search returned no usable evidence query_chars=%d",
                    query_meta["length"],
                )
                return {
                    "success": False,
                    "error": "No search results found",
                    "query_metadata": query_meta,
                    "results": "",
                    "detailed_data": {},
                }

            results_parts.append(web_result)
            all_data["web_search"] = web_result

        combined_results = "\n---\n".join(results_parts)

        if not _evidence_usable(all_data):
            logger.warning(
                "Web-search batch returned no usable evidence query_chars=%d count=%d",
                query_meta["length"],
                len(sub_queries) if isinstance(sub_queries, list) else 0,
            )
            return {
                "success": False,
                "error": "No usable search results",
                "query_metadata": query_meta,
                "results": combined_results,
                "detailed_data": all_data,
            }

        logger.info("Web-search request completed result_parts=%d", len(results_parts))
        return {
            "success": True,
            "query_metadata": query_meta,
            "results": combined_results,
            "detailed_data": all_data,
        }
    except Exception as exc:
        # HTTP exception strings often contain URLs and raw query parameters.
        logger.error("Web-search request failed exception_type=%s", type(exc).__name__)
        return {
            "success": False,
            "error": "Web search failed",
            "error_code": "web_search_internal_error",
            "query_metadata": query_meta,
            "results": "",
            "detailed_data": {},
        }


web_search_tool = Tool(
    name="web_search",
    description=(
        "Smart search coordinator for general/open-ended questions (UK areas, "
        "neighbourhoods, universities, living costs) and anything needing web "
        "information or a mix of web + local data. Put the main query (in English) in "
        "`query`. Optionally pass `sub_queries` to run local tools in the same call "
        "(check_safety, get_weather, search_nearby_pois, get_property_details, "
        "calculate_commute, web_search_only); each is {tool, params}. Omit sub_queries "
        "for a plain web search. 综合搜索协调器：一般性/开放性问题与需要联网的信息查询。"
    ),
    func=web_search_func,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": _MAX_QUERY_CHARS,
                "description": (
                    "主查询语句（英文）。示例: 'Scape Bloomsbury safety and amenities'"
                ),
            },
            "sub_queries": {
                "type": "array",
                "maxItems": _MAX_SUB_QUERIES,
                "description": "子查询列表（可选）。每个子查询包含 tool 和 params",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tool": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_NESTED_TOOLS),
                            "description": "允许的只读工具名称；web_search 本身不可嵌套",
                        },
                        "params": {
                            "type": "object",
                            "maxProperties": _MAX_CONTAINER_ITEMS,
                            "description": "工具参数（有界 JSON object）",
                        },
                    },
                    "required": ["tool", "params"],
                },
            },
        },
        "required": ["query"],
    },
)
