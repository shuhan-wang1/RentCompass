"""Trusted runtime hints carried beside MCP tool arguments.

MCP servers validate ``arguments`` against each tool's public JSON Schema before
the handler runs.  The FC loop also needs a few private execution hints (an
idempotency key, a deadline, a listing's own coordinates as a commute origin), but
those must not be mixed into that public object.  This module owns the small, explicit
bridge used by both the stdio client and server.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


RUNTIME_META_KEY = "rentcompass/runtime"


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(by_alias=True)
            return dict(result) if isinstance(result, Mapping) else {}
        except Exception:
            return {}
    return {}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def split_mcp_arguments(arguments: Mapping[str, Any] | None) -> tuple[dict, dict | None]:
    """Return public schema arguments and optional namespaced MCP metadata."""
    public = dict(arguments or {})
    runtime: dict[str, Any] = {}

    key = public.pop("idempotency_key", None)
    if isinstance(key, str) and key.strip():
        runtime["idempotency_key"] = key.strip()

    if "_deadline_monotonic" in public:
        deadline = _finite_number(public.pop("_deadline_monotonic"))
        if deadline is not None:
            runtime["_deadline_monotonic"] = deadline

    if "_from_coordinates" in public:
        origin = _coordinate_pair(public.pop("_from_coordinates"))
        if origin is not None:
            runtime["_from_coordinates"] = origin

    return public, ({RUNTIME_META_KEY: runtime} if runtime else None)


# Tools that route a journey and accept a listing's own position as the origin.
_ORIGIN_COORDINATE_TOOLS = frozenset({"calculate_commute", "calculate_commute_cost"})


def _coordinate_pair(value: Any) -> dict[str, float] | None:
    """``{'lat', 'lng'}`` with finite latitude/longitude in range, else None."""
    data = _mapping(value)
    lat = _finite_number(data.get("lat"))
    lng = _finite_number(data.get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {"lat": lat, "lng": lng}


def runtime_arguments_from_meta(tool_name: str, meta: Any) -> dict[str, Any]:
    """Validate and allowlist trusted hints before restoring registry kwargs.

    Idempotency applies to every tool.  A monotonic deadline is meaningful only
    for ``search_properties`` and is intentionally not exposed to other tools.
    Malformed or unknown metadata is ignored rather than reaching tool code.
    """
    runtime = _mapping(_mapping(meta).get(RUNTIME_META_KEY))
    restored: dict[str, Any] = {}

    key = runtime.get("idempotency_key")
    if isinstance(key, str) and key.strip():
        restored["idempotency_key"] = key.strip()

    if tool_name == "search_properties":
        deadline = _finite_number(runtime.get("_deadline_monotonic"))
        if deadline is not None:
            restored["_deadline_monotonic"] = deadline

    if tool_name in _ORIGIN_COORDINATE_TOOLS:
        origin = _coordinate_pair(runtime.get("_from_coordinates"))
        if origin is not None:
            restored["_from_coordinates"] = origin

    return restored
