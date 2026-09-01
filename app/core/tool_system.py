"""
Tool System - Agent框架的核心工具系统
核心概念：
1. Tool - 工具定义（名称、描述、参数、执行函数）
2. ToolResult - 标准化工具返回结果
3. ToolRegistry - 工具注册中心（管理、查询、执行所有工具）
"""

import asyncio
import contextvars
import datetime as _datetime
import hashlib
import ipaddress
import json
import math
import re
import time
import urllib.parse
import uuid
from typing import Callable, Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from uk_rent_agent.tools.idempotency import IdempotencyStore

logger = logging.getLogger(__name__)


def _same_json_scalar(left: Any, right: Any) -> bool:
    """JSON Schema enum equality without Python's ``True == 1`` surprise."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # JSON has one number domain: 1 and 1.0 are equal enum values.
        return left == right
    return type(left) is type(right) and left == right


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _validate_string_format(value: str, format_name: str, path: str) -> None:
    """Validate the common assertion formats used by OpenAPI/tool schemas."""
    try:
        if format_name == "date":
            _datetime.date.fromisoformat(value)
        elif format_name == "date-time":
            _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif format_name == "time":
            _datetime.time.fromisoformat(value.replace("Z", "+00:00"))
        elif format_name == "email":
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is None:
                raise ValueError
        elif format_name in {"uri", "url"}:
            parsed = urllib.parse.urlparse(value)
            if not parsed.scheme or (format_name == "url" and not parsed.netloc):
                raise ValueError
        elif format_name == "uuid":
            uuid.UUID(value)
        elif format_name == "ipv4":
            if ipaddress.ip_address(value).version != 4:
                raise ValueError
        elif format_name == "ipv6":
            if ipaddress.ip_address(value).version != 6:
                raise ValueError
        elif format_name == "regex":
            re.compile(value)
        else:
            # JSON Schema leaves unknown formats as annotations; do the same rather than making
            # a new provider-specific label a deployment-breaking runtime constraint.
            return
    except (TypeError, ValueError, re.error) as exc:
        raise ValueError(f"{path}: string is not a valid {format_name}") from exc


def _validate_schema_value(value: Any, schema: Dict[str, Any], path: str = "$") -> None:
    """Validate the JSON-Schema subset used by tool inputs.

    Pydantic still owns parsing, defaults and the public ``ValidationError`` envelope.  This
    recursive validator is installed *on the generated Pydantic model* below so constraints
    authored in tool JSON Schema are runtime guarantees too, rather than documentation that is
    merely copied back into the function-calling schema.
    """
    if not isinstance(schema, dict):
        return

    if "const" in schema and not _same_json_scalar(value, schema["const"]):
        raise ValueError(f"{path}: value must equal {schema['const']!r}")
    if "enum" in schema and not any(
        _same_json_scalar(value, candidate) for candidate in schema.get("enum", [])
    ):
        raise ValueError(f"{path}: value is not one of {schema['enum']!r}")

    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(alternatives, list):
        matches = 0
        for branch in alternatives:
            try:
                _validate_schema_value(value, branch, path)
                matches += 1
            except (TypeError, ValueError):
                pass
        expected_matches = 1 if "oneOf" in schema else None
        if matches == 0 or (expected_matches is not None and matches != expected_matches):
            label = "exactly one" if expected_matches is not None else "at least one"
            raise ValueError(f"{path}: value must match {label} allowed schema")
        return

    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_schema_type_matches(value, item) for item in expected):
            raise ValueError(f"{path}: expected one of the types {expected!r}")
    elif isinstance(expected, str) and not _schema_type_matches(value, expected):
        raise ValueError(f"{path}: expected {expected}")

    if value is None:
        return

    if isinstance(value, str):
        length = len(value)
        if "minLength" in schema and length < int(schema["minLength"]):
            raise ValueError(f"{path}: string is shorter than minLength={schema['minLength']}")
        if "maxLength" in schema and length > int(schema["maxLength"]):
            raise ValueError(f"{path}: string is longer than maxLength={schema['maxLength']}")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(str(pattern), value) is None:
            raise ValueError(f"{path}: string does not match pattern {pattern!r}")
        if schema.get("format"):
            _validate_string_format(value, str(schema["format"]), path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path}: number must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: value is above maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(
                f"{path}: value must be greater than {schema['exclusiveMinimum']}"
            )
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path}: value must be less than {schema['exclusiveMaximum']}")
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if not divisor or not math.isclose(value / divisor, round(value / divisor)):
                raise ValueError(f"{path}: value must be a multiple of {divisor}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ValueError(f"{path}: array has fewer than minItems={schema['minItems']}")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{path}: array has more than maxItems={schema['maxItems']}")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(f"{path}: missing required properties {missing!r}")
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            raise ValueError(
                f"{path}: object has fewer than minProperties={schema['minProperties']}"
            )
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            raise ValueError(
                f"{path}: object has more than maxProperties={schema['maxProperties']}"
            )
        for field_name, field_schema in properties.items():
            if field_name in value:
                _validate_schema_value(value[field_name], field_schema, f"{path}.{field_name}")
        extras = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if extras and additional is False:
            raise ValueError(f"{path}: additional properties are forbidden: {sorted(extras)!r}")
        if extras and isinstance(additional, dict):
            for field_name in extras:
                _validate_schema_value(value[field_name], additional, f"{path}.{field_name}")


def _model_from_schema(name: str, schema: Dict[str, Any]) -> type[BaseModel]:
    """Create the runtime input contract once; JSON schema is then generated from it.

    The field annotations intentionally remain shallow so the emitted schema retains the
    established no-``$defs`` contract.  The model-level validator recursively compiles enum,
    item, bound, string/array and nested-object constraints into runtime validation.
    """
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    required = set(schema.get("required", []))
    fields = {}
    for field_name, definition in schema.get("properties", {}).items():
        annotation = type_map.get(definition.get("type"), Any)
        is_required = field_name in required
        if not is_required:
            annotation = Optional[annotation]
        default = ... if is_required else definition.get("default", None)
        fields[field_name] = (
            annotation,
            Field(default=default, description=definition.get("description")),
        )
    authored_schema = schema

    @model_validator(mode="after")
    def _validate_authored_schema(instance: BaseModel) -> BaseModel:
        # exclude_unset keeps absent optional properties absent.  Authored non-null defaults are
        # explicitly applied by Tool._apply_defaults before model_validate during execution.
        _validate_schema_value(instance.model_dump(exclude_unset=True), authored_schema)
        return instance

    return create_model(
        f"{''.join(part.title() for part in name.split('_'))}Input",
        # Function-calling arguments are an external trust boundary. Coercing
        # ``\"3\"`` to ``3`` (or ``1`` to ``True``) would let malformed MCP/provider
        # payloads pass a schema they did not actually satisfy.
        __config__=ConfigDict(extra="forbid", strict=True),
        __validators__={"_validate_authored_schema": _validate_authored_schema},
        **fields,
    )


# JSON-schema constraint keywords the pydantic round-trip drops (it only captures
# type/default/description). Losing enum/items degrades native function-calling:
# the model never sees the legal values, so it guesses parameters it could have read.
_CONSTRAINT_KEYWORDS = (
    "enum", "items", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
    "uniqueItems", "minProperties", "maxProperties", "const",
)


def _merge_constraint_keywords(emitted: Dict[str, Any], original: Dict[str, Any]) -> Dict[str, Any]:
    """Restore author-written schema fidelity that the pydantic round-trip drops.

    Three losses, all real (bare ``list``/``dict`` annotations erase them):
      1. per-property constraint keywords (enum, format, bounds, ...);
      2. array ``items`` sub-schemas — pydantic emits ``items: {}`` for a bare list,
         and the DeepSeek strict endpoint rejects a sub-schema with no type selector;
      3. nested object ``properties``/``required``/``additionalProperties`` — pydantic
         emits a bare ``{"type": "object"}`` for a dict field (e.g. budget_hint lost
         its amount/period members), and strict rejects property-less objects.
    Additive/repair-only: authored values fill gaps, they never override a concrete
    pydantic emission. Recurses through matched nested properties."""
    import copy as _copy

    def _typeless(node: Any) -> bool:
        return not (isinstance(node, dict) and (set(node) & {"type", "anyOf", "$ref"}))

    def _merge_node(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        # pydantic wraps an Optional property as anyOf[<real>, {"type":"null"}] while the
        # authored schema is flat — the loss (items:{}, missing nested properties) lives
        # INSIDE the non-null branch, so descend into it rather than decorating the
        # property level with siblings of anyOf.
        if isinstance(target.get("anyOf"), list) and "anyOf" not in source:
            for br in target["anyOf"]:
                if isinstance(br, dict) and br.get("type") != "null":
                    _merge_node(br, source)
            return
        for kw in _CONSTRAINT_KEYWORDS:
            if kw in source and kw not in target:
                target[kw] = _copy.deepcopy(source[kw])
        if isinstance(source.get("items"), dict) and _typeless(target.get("items")):
            target["items"] = _copy.deepcopy(source["items"])
        if isinstance(source.get("properties"), dict) and not target.get("properties"):
            target["properties"] = _copy.deepcopy(source["properties"])
            for kw in ("required", "additionalProperties"):
                if kw in source and kw not in target:
                    target[kw] = _copy.deepcopy(source[kw])
        # Recurse where both sides have structure.
        if isinstance(target.get("items"), dict) and isinstance(source.get("items"), dict):
            _merge_node(target["items"], source["items"])
        t_props, s_props = target.get("properties"), source.get("properties")
        if isinstance(t_props, dict) and isinstance(s_props, dict):
            for pname, sdef in s_props.items():
                tdef = t_props.get(pname)
                if isinstance(tdef, dict) and isinstance(sdef, dict):
                    _merge_node(tdef, sdef)

    _merge_node(emitted, original)
    return emitted


@dataclass(frozen=True)
class ToolSpec:
    """
    统一工具描述契约（design §2.8a）。

    - in-process 由 ``Tool.to_spec()`` / ``ToolRegistry.list_specs()`` 构造；
    - MCP 由 ``MCPToolClient.list_specs()`` 从 list_tools() 的 inputSchema+annotations
      构造（缺字段时回退到 fallback_registry 的 spec）。
    两进程共享同一份工具代码，registry 是单一事实源。
    """
    name: str
    description: str
    input_schema: dict      # 原始 JSON schema（OpenAI FC 格式，即 Tool.parameters）
    side_effect: str        # "none" | "write"
    retry_safe: bool
    version: str = "1"      # 幂等键的工具版本语义——必须与 Tool.version 一致
    terminal: bool = False  # ask_user
    # Capability-boundary fields (manager_v1 specialists).  ``input_schema`` above is the
    # MODEL-VISIBLE schema frozen at Tool construction; it cannot see a later
    # ``Tool.input_model`` swap, which is what actually validates and re-shapes the kwargs
    # the callable receives.  The retry policy is here for the same reason: it decides how
    # many times a pinned callable runs.  Defaults keep every other ToolSpec producer
    # (MCP, adapters) constructing exactly as before.
    max_retries: int = 2
    retry_on_error: bool = True
    input_model_ref: str = ""
    output_model_ref: str = ""


# Memoised per MODEL OBJECT.  ``model_json_schema()`` costs ~1.5 ms and ``to_spec()`` sits on
# the hottest path there is (every super-step binds tools, every specialist read revalidates,
# every fan-out call resolves a capability), so recomputing the refs on each call made
# ``list_specs()`` ~250x slower and burned that CPU synchronously on the graph event loop.
# Each entry keeps a STRONG reference to the model, so ``id()`` cannot be recycled by another
# object while the entry lives: a hit therefore proves same-object identity.  A model SWAP —
# the K7 threat this ref exists to detect — is a different object, hence a different key and a
# freshly computed ref.  An IN-PLACE mutation of an already-cached model class is deliberately
# out of scope here (it is caught by the pinned ``input_model`` identity checks in
# ``execute_resolved_specialist_capability``, not by this string).
_MODEL_SECURITY_REF_CACHE: Dict[int, Tuple[Any, str]] = {}
_MODEL_SECURITY_REF_CACHE_MAX = 512


def _compute_model_security_ref(model: Any) -> str:
    label = "%s.%s" % (
        getattr(model, "__module__", "?"),
        getattr(model, "__qualname__", getattr(model, "__name__", type(model).__name__)),
    )
    try:
        payload = json.dumps(
            model.model_json_schema(),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except Exception:
        return f"{label}#object:{id(model):x}"
    return f"{label}#sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def model_security_ref(model: Optional[type]) -> str:
    """Stable in-process identity for a pydantic model at the capability boundary.

    Prefers the model's JSON schema, so two distinct ``create_model`` products that share a
    generated ``__qualname__`` still differ.  Falls back to the object identity when a model
    cannot produce a schema — a swap is then still visible for the lifetime of the process,
    which is the whole window a grant covers.

    Memoised by model identity; see ``_MODEL_SECURITY_REF_CACHE`` for why that keeps swap
    detection intact.
    """
    if model is None:
        return "none"
    key = id(model)
    cached = _MODEL_SECURITY_REF_CACHE.get(key)
    if cached is not None and cached[0] is model:
        return cached[1]
    ref = _compute_model_security_ref(model)
    if len(_MODEL_SECURITY_REF_CACHE) >= _MODEL_SECURITY_REF_CACHE_MAX:
        # Bounded, and dropped wholesale rather than by age: the cache is a pure function
        # of object identity, so losing it only costs one recomputation per live model.
        _MODEL_SECURITY_REF_CACHE.clear()
    _MODEL_SECURITY_REF_CACHE[key] = (model, ref)
    return ref


@dataclass(frozen=True)
class _PinnedExecution:
    """Everything ``_execute_with_callable`` must read from the GRANT, not from ``self``."""

    input_model: Any
    output_model: Any
    max_retries: int
    retry_on_error: bool


def to_function_calling_format(spec: "ToolSpec") -> Dict[str, Any]:
    """把 ToolSpec 转成原生 Function-Calling 工具定义（非 strict）。

    strict 适配（全属性 required + additionalProperties:false + 剥不支持关键词）是
    Phase 2 的独立步骤，这里不做。
    """
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


@dataclass
class ToolResult:
    """
    标准化的工具执行结果 - 所有工具都返回这个格式
    """
    success: bool
    data: Any = None
    error: Optional[str] = None
    execution_time_ms: Optional[float] = None
    tool_name: Optional[str] = None
    version: str = "1"
    idempotency_key: Optional[str] = None
    outcome: Optional[str] = None
    
    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'execution_time_ms': self.execution_time_ms,
            'tool_name': self.tool_name,
            'version': self.version,
            'idempotency_key': self.idempotency_key,
            'outcome': self.outcome,
        }


class Tool:
    """
    工具基类 - 使用 OpenAI Function Calling 格式
    这个格式被 OpenAI、Ollama、Claude、Llama 等都支持
    """
    
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        parameters: Dict[str, Any],
        return_direct: bool = False,
        max_retries: int = 2,
        retry_on_error: bool = True,
        version: str = "1",
        side_effect: str = "none",
        retry_safe: Optional[bool] = None,
        cacheable: bool = False,
        terminal: bool = False,
        input_model: Optional[type[BaseModel]] = None,
        output_model: Optional[type[BaseModel]] = None,
    ):
        """
        参数说明：
            name: 工具名（snake_case，如 'search_properties'）
            description: 详细描述，告诉 AI 何时使用这个工具
            func: 实际执行的函数（可以是同步或异步函数）
            parameters: 参数定义（OpenAI 格式的 JSON Schema）
            return_direct: 是否直接返回结果
            max_retries: 失败时最大重试次数
            retry_on_error: 是否在出错时重试
        """
        self.name = name
        self.description = description
        self.func = func
        self.input_model = input_model or _model_from_schema(name, parameters)
        self.output_model = output_model
        self.parameters = _merge_constraint_keywords(
            self.input_model.model_json_schema(), parameters)
        self.return_direct = return_direct
        self.max_retries = max_retries
        self.retry_on_error = retry_on_error
        self.version = version
        self.side_effect = side_effect
        self.retry_safe = (side_effect == "none") if retry_safe is None else retry_safe
        self.cacheable = cacheable
        self.terminal = terminal
        
        # 验证参数格式
        self._validate_parameters()
    
    def _validate_parameters(self):
        """验证参数是否符合 OpenAI Function Calling 的标准 JSON Schema 格式"""
        if not isinstance(self.parameters, dict):
            raise ValueError(f"[{self.name}] parameters 必须是字典")
        
        if 'type' not in self.parameters:
            raise ValueError(f"[{self.name}] parameters 必须包含 'type' 字段")
        
        if self.parameters['type'] != 'object':
            raise ValueError(f"[{self.name}] parameters['type'] 必须是 'object'")
        
        if 'properties' not in self.parameters:
            raise ValueError(f"[{self.name}] parameters 必须包含 'properties' 字段")

    
    async def execute(self, /, **kwargs) -> ToolResult:
        """
        执行工具（带重试和错误处理）

        ``self`` is POSITIONAL-ONLY for the same reason ``_execute_with_callable``'s harness
        parameters are (audit F9): ``**kwargs`` is the tool's own argument namespace, so a
        tool parameter literally named ``self`` must reach the tool instead of colliding with
        the bound receiver and surfacing as an opaque TypeError.
        """
        return await self._execute_with_callable(None, **kwargs)

    async def _execute_with_callable(
        self,
        pinned_callable: Optional[Callable] = None,
        pinned: Optional["_PinnedExecution"] = None,
        /,
        **kwargs,
    ) -> ToolResult:
        """Run the normal Tool pipeline, optionally against one fixed callable.

        Ordinary callers pass through :meth:`execute` and resolve ``self.func`` at the start
        of each attempt. Specialist capabilities pass the callable captured at capability
        resolution, so a later mutation of the otherwise same ``Tool`` object cannot redirect
        an already-authorised dispatch.

        ``pinned`` extends that from the callable to the rest of the execution surface —
        the validating ``input_model``, the ``output_model`` and the retry policy — because
        swapping ``input_model`` after the grant let a caller reshape the kwargs the pinned
        callable received, and raising ``max_retries`` made it run N times (audit K7).
        Both parameters are POSITIONAL-ONLY: ``**kwargs`` is the tool's own argument
        namespace and must never be able to collide with a harness parameter (audit F9).
        """
        start_time = time.time()
        input_model = self.input_model if pinned is None else pinned.input_model
        output_model = self.output_model if pinned is None else pinned.output_model
        max_retries = self.max_retries if pinned is None else pinned.max_retries
        retry_on_error = (
            self.retry_on_error if pinned is None else pinned.retry_on_error
        )
        
        idempotency_key = kwargs.pop("idempotency_key", None)
        idempotency_store = kwargs.pop("_idempotency_store", None)

        # Injected runtime params: leading-underscore kwargs supplied by the agent runtime /
        # loop executor (e.g. `_deadline_monotonic`, an absolute time.monotonic() deadline) that
        # must BYPASS the model input schema — pydantic create_model forbids underscore field
        # names and model_validate() drops unknown keys, so they can never be model-visible tool
        # parameters — yet still reach a tool func that declares them. Captured here, re-attached
        # AFTER validation, and only for a func that actually accepts the name (explicit
        # parameter or **kwargs), so tools that don't declare them are entirely unaffected.
        injected = {k: kwargs.pop(k) for k in list(kwargs) if k.startswith("_")}

        # 填充默认值
        kwargs = self._apply_defaults(kwargs)
        try:
            kwargs = input_model.model_validate(kwargs).model_dump(exclude_none=True)
        except Exception:
            return ToolResult(False, error="ValidationError: invalid parameters", tool_name=self.name,
                              version=self.version, idempotency_key=idempotency_key)

        claimed = False
        if self.side_effect == "write":
            if not idempotency_key:
                return ToolResult(False, error="idempotency_key is required for write tools",
                                  tool_name=self.name, version=self.version)
            previous = idempotency_store.get_record(idempotency_key) if idempotency_store else None
            if previous is not None:
                if previous.tool != self.name:
                    return ToolResult(
                        False,
                        error="idempotency key is already bound to a different tool",
                        tool_name=self.name,
                        version=self.version,
                        idempotency_key=idempotency_key,
                        outcome="conflict",
                    )
                if previous.status == "complete":
                    return ToolResult(
                        True, data=previous.result, tool_name=self.name,
                        version=self.version, idempotency_key=idempotency_key,
                        outcome="replayed",
                    )
                if previous.status == "failed":
                    return ToolResult(
                        False, data=previous.result,
                        error=previous.error or "logical invocation previously failed",
                        tool_name=self.name, version=self.version,
                        idempotency_key=idempotency_key, outcome="failed",
                    )
                return ToolResult(
                    False,
                    error=(previous.error or
                           f"logical invocation is {previous.status}; write outcome is unknown"),
                    tool_name=self.name,
                    version=self.version,
                    idempotency_key=idempotency_key,
                    outcome=("unknown" if previous.status == "unknown" else "running"),
                )
            if idempotency_store:
                claimed = idempotency_store.claim(idempotency_key, self.name)
                if not claimed:
                    current = idempotency_store.get_record(idempotency_key)
                    status = current.status if current is not None else "running"
                    return ToolResult(
                        False,
                        error=(getattr(current, "error", None) or
                               f"logical invocation is {status}; write was not retried"),
                        tool_name=self.name, version=self.version,
                        idempotency_key=idempotency_key,
                        outcome=("unknown" if status == "unknown" else status),
                    )

        # ``max_retries=0`` means execute once with no retry; it must never mean zero calls.
        attempts = max(1, int(max_retries)) if self.retry_safe else 1
        for attempt in range(attempts):
            try:
                logger.debug("Executing %s (attempt %s/%s)", self.name, attempt + 1, attempts)

                execution_callable = (
                    pinned_callable if pinned_callable is not None else self.func
                )
                if not callable(execution_callable):
                    raise TypeError("tool callable is not callable")
                
                # 验证输入参数
                self._validate_input(kwargs)
                
                # Re-attach injected runtime params (see above) only for funcs that accept them.
                call_kwargs = kwargs
                if injected:
                    if pinned_callable is None:
                        accepted = {
                            k: v for k, v in injected.items() if self._accepts_kwarg(k)
                        }
                    else:
                        accepted = {
                            k: v
                            for k, v in injected.items()
                            if self._callable_accepts_kwarg(execution_callable, k)
                        }
                    if accepted:
                        call_kwargs = {**kwargs, **accepted}

                # 执行函数（支持同步和异步）
                if asyncio.iscoroutinefunction(execution_callable):
                    result = await execution_callable(**call_kwargs)
                else:
                    # 同步函数在 executor 中运行（避免阻塞）。
                    #
                    # SECURITY: the copied context is not an optimisation. ``run_in_executor``
                    # runs the callable on a bare pool thread, where contextvars do NOT
                    # propagate, so a SYNC tool observed ``current_agent_context() == {}`` --
                    # i.e. no ``agent_role`` -- while its ASYNC twin saw the specialist role.
                    # ``web_search._current_specialist_role()`` reads exactly that context and
                    # maps "no role" to MANAGER authority (unrestricted nested dispatch), so
                    # declaring a granted tool ``def`` instead of ``async def`` silently
                    # restored the cross-role escalation the capability boundary exists to
                    # close (review3 R1-M2). Copying the context makes the boundary a property
                    # of the runtime rather than of how a tool happens to be declared; it also
                    # fixes the same gap in log attribution (JsonFormatter reads that context).
                    loop = asyncio.get_running_loop()
                    ctx = contextvars.copy_context()
                    result = await loop.run_in_executor(
                        None, lambda: ctx.run(execution_callable, **call_kwargs)
                    )
                
                execution_time = (time.time() - start_time) * 1000
                
                logger.info("Tool %s succeeded (%.0fms)", self.name, execution_time)
                
                logical_success = not isinstance(result, dict) or result.get('success', True) is not False
                # Many network-backed tools translate an exception into the public
                # {"success": false, "error": ...} envelope instead of raising.
                # Until this branch existed, their max_retries/retry_safe metadata was
                # inert: only thrown exceptions reached the retry loop. Read-only
                # tools may safely retry an explicit (or legacy-default) failure; a
                # tool can opt out with retryable=false for a final domain result
                # such as "no listings" or "need clarification".
                if (not logical_success and attempt < attempts - 1
                        and retry_on_error and self.retry_safe
                        and result.get("retryable", True)):
                    wait_time = 2 ** attempt
                    logger.info("Retrying %s after retryable logical failure in %ss",
                                self.name, wait_time)
                    await asyncio.sleep(wait_time)
                    continue
                if logical_success and output_model is not None:
                    result = output_model.model_validate(result).model_dump()
                if logical_success and claimed:
                    idempotency_store.complete(idempotency_key, result)
                elif not logical_success and claimed:
                    idempotency_store.fail(
                        idempotency_key,
                        result.get("error", "write returned an unsuccessful result"),
                        result,
                    )
                return ToolResult(
                    success=logical_success,
                    data=result,
                    error=result.get('error') if isinstance(result, dict) and not logical_success else None,
                    execution_time_ms=execution_time,
                    tool_name=self.name,
                    version=self.version,
                    idempotency_key=idempotency_key,
                    outcome=("complete" if logical_success else "failed"),
                )

            except asyncio.CancelledError:
                if claimed:
                    idempotency_store.mark_unknown(
                        idempotency_key,
                        "caller stopped waiting while the write was in flight; outcome is unknown",
                    )
                raise
            except Exception as e:
                execution_time = (time.time() - start_time) * 1000
                error_type = type(e).__name__
                error_msg = "Tool execution failed"
                
                logger.warning("Tool %s failed error_type=%s", self.name, error_type)
                
                # 是否重试
                if attempt < attempts - 1 and retry_on_error and self.retry_safe:
                    wait_time = 2 ** attempt  # 指数退避：2, 4, 8...
                    logger.info("Retrying %s in %ss", self.name, wait_time)
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # 最后一次尝试失败
                    logger.error("Tool %s exhausted all retries", self.name)
                    if claimed:
                        idempotency_store.fail(idempotency_key, error_msg)
                    return ToolResult(
                        success=False,
                        data=None,
                        error=error_msg,
                        execution_time_ms=execution_time,
                        tool_name=self.name,
                        version=self.version,
                        idempotency_key=idempotency_key,
                        outcome="failed",
                    )
    
    def _accepts_kwarg(self, name: str) -> bool:
        """True if ``self.func`` can receive keyword ``name`` — either as an explicit
        parameter or via ``**kwargs``. Used to decide whether an INJECTED runtime param
        (e.g. ``_deadline_monotonic``) should be forwarded to this tool's func. Result is
        cached: signature introspection runs at most once per Tool."""
        cache = getattr(self, "_accepts_cache", None)
        if cache is None:
            import inspect
            cache = {"names": set(), "varkw": False}
            try:
                for p in inspect.signature(self.func).parameters.values():
                    if p.kind is inspect.Parameter.VAR_KEYWORD:
                        cache["varkw"] = True
                    elif p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                    inspect.Parameter.KEYWORD_ONLY):
                        cache["names"].add(p.name)
            except (TypeError, ValueError):
                cache["varkw"] = True  # uninspectable callable -> be permissive
            self._accepts_cache = cache
        return cache["varkw"] or name in cache["names"]

    @staticmethod
    def _callable_accepts_kwarg(func: Callable, name: str) -> bool:
        """Check a capability-pinned callable without consulting ``self.func``."""
        import inspect

        try:
            parameters = inspect.signature(func).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or (
                parameter.name == name
                and parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            )
            for parameter in parameters
        )

    def _validate_input(self, kwargs: Dict):
        """验证是否满足 required 的参数"""
        required = self.parameters.get('required', [])
        
        for param in required:
            if param not in kwargs:
                raise ValueError(f"缺少必需参数: {param}")
    
    def _apply_defaults(self, kwargs: Dict) -> Dict:
        """为缺失的参数填充默认值"""
        result = kwargs.copy()
        properties = self.parameters.get('properties', {})
        
        for param_name, param_info in properties.items():
            if (
                param_name not in result
                and 'default' in param_info
                and param_info['default'] is not None
            ):
                result[param_name] = param_info['default']
        
        return result
    
    def to_llm_format(self) -> str:
        """
        把这个 Tool 转换为给 LLM 看的文字说明格式
        这个格式会放在 prompt 中，告诉 LLM：
        我是谁，我能做什么，我需要哪些参数
        """
        # 构建参数描述
        params_lines = []
        for param_name, param_info in self.parameters['properties'].items():
            is_required = param_name in self.parameters.get('required', [])
            required_mark = " **(必需)**" if is_required else " (可选)"
            
            param_type = param_info.get('type', 'any')
            param_desc = param_info.get('description', '无描述')
            
            # 如果有枚举值，显示出来
            if 'enum' in param_info:
                param_type += f" (可选值: {', '.join(param_info['enum'])})"
            
            # 如果有默认值，显示出来
            if 'default' in param_info:
                param_type += f" (默认: {param_info['default']})"
            
            params_lines.append(f"  • {param_name}{required_mark}: {param_type} - {param_desc}")
        
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔧 Tool: {self.name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 描述:
{self.description}

⚙️  参数:
{chr(10).join(params_lines) if params_lines else "  (无参数)"}

💡 使用示例:
{self._generate_example()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    def _generate_example(self) -> str:
        """生成使用示例"""
        example_params = {}
        for param_name, param_info in self.parameters['properties'].items():
            param_type = param_info.get('type', 'string')
            
            if param_type == 'string':
                example_params[param_name] = '"example_value"'
            elif param_type == 'integer':
                example_params[param_name] = '1500'
            elif param_type == 'number':
                example_params[param_name] = '5.0'
            elif param_type == 'boolean':
                example_params[param_name] = 'true'
            else:
                example_params[param_name] = '...'
        
        params_str = ', '.join([f'"{k}": {v}' for k, v in example_params.items()])
        return f'{{"tool": "{self.name}", "params": {{{params_str}}}}}'
    
    def to_openai_format(self) -> Dict:
        """转换为 OpenAI/Ollama Function Calling 格式"""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters
        }

    def to_spec(self) -> "ToolSpec":
        """构造统一的 ToolSpec 契约（design §2.8a）。

        ``input_model_ref``/``output_model_ref`` are recomputed from the LIVE models on
        every call, so a post-construction ``input_model`` swap changes the spec (and
        therefore the specialist security digest) even though ``parameters`` cannot.
        """
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
            side_effect=self.side_effect,
            retry_safe=self.retry_safe,
            version=self.version,
            terminal=self.terminal,
            max_retries=self.max_retries,
            retry_on_error=self.retry_on_error,
            input_model_ref=model_security_ref(getattr(self, "input_model", None)),
            output_model_ref=model_security_ref(getattr(self, "output_model", None)),
        )

    def __repr__(self) -> str:
        return f"Tool(name='{self.name}')"


class ToolRegistry:
    """
    工具注册中心 - 负责存放、检索、组织多个 Tool 实例
    """
    
    def __init__(self, idempotency_store: Optional[IdempotencyStore] = None):
        self.tools: Dict[str, Tool] = {}
        self._stats: Dict[str, Dict] = {}
        default_path = Path(
            os.getenv(
                "IDEMPOTENCY_DB",
                str(Path(__file__).resolve().parents[2] / ".runtime" / "idempotency.sqlite3"),
            )
        )
        self._idempotency_store = idempotency_store or IdempotencyStore(default_path)
    
    def register(self, tool: Tool):
        """注册一个工具"""
        if tool.name in self.tools:
            logger.warning("Tool %s already exists and will be replaced", tool.name)
        
        self.tools[tool.name] = tool
        self._stats[tool.name] = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'total_time_ms': 0
        }
        
        logger.debug("Registered tool: %s", tool.name)
    
    def register_multiple(self, tools: List[Tool]):
        """批量注册工具"""
        for tool in tools:
            self.register(tool)
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self.tools.get(name)
    
    def list_tool_names(self) -> List[str]:
        """列出所有工具名称"""
        return list(self.tools.keys())

    def list_specs(self) -> List[ToolSpec]:
        """列出所有已注册工具的 ToolSpec 契约（design §2.8a）。"""
        return [tool.to_spec() for tool in self.tools.values()]

    def resolve_specialist_capability(self, name: str, expected_spec_digest: str):
        """Pin one trusted in-process tool for read-only specialist execution.

        A normal registry dispatch intentionally resolves by name.  That is convenient for
        hot replacement, but it is the wrong primitive for a capability boundary: a tool
        could be replaced after the manager validated its metadata and before execution.
        This method snapshots the exact ``Tool`` object plus a canonical security digest;
        :meth:`execute_resolved_specialist_capability` executes that same object only after
        rechecking that the registry entry and metadata are unchanged.

        The digest implementation lives with the specialist runtime so the grant factory,
        dispatch validator and registry cannot silently disagree about which fields matter.
        Importing it lazily keeps the ordinary tool registry independent of manager_v1.
        """
        from core.specialist_runtime import (
            ResolvedSpecialistCapability,
            SpecialistDispatchError,
            tool_spec_security_digest,
        )

        tool = self.get(name)
        if tool is None:
            raise SpecialistDispatchError("specialist_capability_missing")
        spec = tool.to_spec()
        digest = tool_spec_security_digest(spec)
        if digest != expected_spec_digest:
            raise SpecialistDispatchError("specialist_capability_metadata_drift")
        if spec.side_effect != "none" or spec.terminal is not False:
            raise SpecialistDispatchError("specialist_capability_not_read_only")
        tool_callable = getattr(tool, "func", None)
        if not callable(tool_callable):
            raise SpecialistDispatchError("specialist_capability_callable_invalid")
        input_model = getattr(tool, "input_model", None)
        if input_model is None:
            # Every Tool builds one in __init__; its absence means this object is not the
            # execution surface the digest describes.
            raise SpecialistDispatchError("specialist_capability_input_model_invalid")
        output_model = getattr(tool, "output_model", None)
        return ResolvedSpecialistCapability(
            provider_identity=id(self),
            tool_name=name,
            tool_identity=id(tool),
            tool=tool,
            tool_callable_identity=id(tool_callable),
            tool_callable=tool_callable,
            spec_digest=digest,
            # Strong references, so these objects cannot be collected and their id() reused.
            input_model=input_model,
            input_model_identity=id(input_model),
            output_model=output_model,
            output_model_identity=id(output_model),
            max_retries=int(getattr(tool, "max_retries", 1) or 1),
            retry_on_error=bool(getattr(tool, "retry_on_error", False)),
        )

    async def execute_resolved_specialist_capability(
        self,
        capability,
        *,
        args: Dict[str, Any],
        expected_spec_digest: str,
    ) -> ToolResult:
        """Execute an already-resolved specialist capability without a name re-lookup race.

        The tool's own arguments arrive as ONE explicit mapping rather than ``**kwargs``:
        sharing the kwarg namespace with ``capability``/``expected_spec_digest``/``self``
        meant a tool argument with one of those names became a ``TypeError`` swallowed as
        a generic "Tool execution failed" (audit F9).
        """
        from core.specialist_runtime import (
            SpecialistDispatchError,
            tool_spec_security_digest,
        )

        if not isinstance(args, dict):
            raise SpecialistDispatchError("specialist_capability_args_invalid")
        if "_idempotency_store" in args:
            # The only harness parameter _execute_with_callable still takes by keyword.
            raise SpecialistDispatchError("specialist_capability_args_invalid")

        if getattr(capability, "provider_identity", None) != id(self):
            raise SpecialistDispatchError("specialist_capability_provider_mismatch")
        name = getattr(capability, "tool_name", "")
        tool = getattr(capability, "tool", None)
        if not name or tool is None or getattr(capability, "tool_identity", None) != id(tool):
            raise SpecialistDispatchError("specialist_capability_invalid")
        if self.get(name) is not tool:
            raise SpecialistDispatchError("specialist_capability_replaced")
        tool_callable = getattr(capability, "tool_callable", None)
        if (
            not callable(tool_callable)
            or getattr(capability, "tool_callable_identity", None) != id(tool_callable)
        ):
            raise SpecialistDispatchError("specialist_capability_callable_invalid")
        if getattr(tool, "func", None) is not tool_callable:
            raise SpecialistDispatchError("specialist_capability_callable_replaced")

        # The pinned validation/serialisation surface must still BE the live one. Checked
        # by identity (``is``) before the digest so a swap is reported as a replacement
        # rather than as anonymous metadata drift.
        input_model = getattr(capability, "input_model", None)
        if (
            input_model is None
            or getattr(capability, "input_model_identity", None) != id(input_model)
        ):
            raise SpecialistDispatchError("specialist_capability_input_model_invalid")
        if getattr(tool, "input_model", None) is not input_model:
            raise SpecialistDispatchError("specialist_capability_replaced")
        output_model = getattr(capability, "output_model", None)
        if getattr(capability, "output_model_identity", None) != id(output_model):
            raise SpecialistDispatchError("specialist_capability_input_model_invalid")
        if getattr(tool, "output_model", None) is not output_model:
            raise SpecialistDispatchError("specialist_capability_replaced")

        live_spec = tool.to_spec()
        live_digest = tool_spec_security_digest(live_spec)
        if live_digest != expected_spec_digest or live_digest != getattr(
            capability, "spec_digest", None
        ):
            raise SpecialistDispatchError("specialist_capability_metadata_drift")
        if live_spec.side_effect != "none" or live_spec.terminal is not False:
            raise SpecialistDispatchError("specialist_capability_not_read_only")

        # Execute both the pinned Tool object and its pinned callable. No await occurs before
        # entering the Tool execution pipeline; if another thread mutates ``tool.func`` after
        # the check above, the already-authorised call still invokes only this captured object.
        kwargs = dict(args)
        result = await tool._execute_with_callable(
            tool_callable,
            _PinnedExecution(
                input_model=input_model,
                output_model=output_model,
                max_retries=int(getattr(capability, "max_retries", 1) or 1),
                retry_on_error=bool(getattr(capability, "retry_on_error", False)),
            ),
            _idempotency_store=self._idempotency_store,
            **kwargs,
        )
        if result.tool_name != name or result.version != live_spec.version:
            raise SpecialistDispatchError("specialist_result_identity_mismatch")

        stats = self._stats.get(name)
        if stats is not None:
            stats["total_calls"] += 1
            if result.success:
                stats["successful_calls"] += 1
            else:
                stats["failed_calls"] += 1
            if result.execution_time_ms:
                stats["total_time_ms"] += result.execution_time_ms

        try:
            from evaluation.metrics import collector
            if collector.is_active():
                collector.record_tool_call(name, result, kwargs, mcp=False)
        except Exception:
            pass
        return result
    
    def list_tools_for_llm(self) -> str:
        """
        生成给 LLM 看的工具列表（文本格式）
        这个会放在 prompt 中，不调用任何 API
        """
        if not self.tools:
            return "暂无可用工具"
        
        tools_text = "\n".join([tool.to_llm_format() for tool in self.tools.values()])
        
        return f"""
╔═══════════════════════════════════════════════════════════╗
║                    可用工具列表                             ║
║              （共 {len(self.tools)} 个工具）                    ║
╚═══════════════════════════════════════════════════════════╝

{tools_text}

📌 使用说明:
1. 根据用户需求选择合适的工具
2. 返回 JSON 格式: {{"tool": "工具名", "params": {{参数}}}}
3. 一次只能调用一个工具
"""
    
    async def execute_tool(self, name: str, /, **kwargs) -> ToolResult:
        """执行工具

        ``name`` is positional-only (audit F9): every call site already passes it
        positionally, and leaving it positional-or-keyword meant a tool whose own schema has
        a ``name`` parameter could never be dispatched through the registry.
        """
        tool = self.get(name)
        
        if not tool:
            return ToolResult(
                success=False,
                data=None,
                error=f"工具 '{name}' 不存在",
                tool_name=name
            )
        
        # 执行工具
        result = await tool.execute(_idempotency_store=self._idempotency_store, **kwargs)
        
        # 更新统计
        stats = self._stats[name]
        stats['total_calls'] += 1
        if result.success:
            stats['successful_calls'] += 1
        else:
            stats['failed_calls'] += 1
        if result.execution_time_ms:
            stats['total_time_ms'] += result.execution_time_ms

        # Offline-eval instrumentation (additive; no-op unless active).
        try:
            from evaluation.metrics import collector
            if collector.is_active():
                collector.record_tool_call(name, result, kwargs, mcp=False)
        except Exception:
            pass

        return result
    
    def get_stats(self) -> Dict:
        """获取执行统计"""
        return self._stats
    
    def print_stats(self):
        """打印统计信息"""
        print("\n" + "="*60)
        print("📊 工具执行统计")
        print("="*60)
        
        for name, stats in self._stats.items():
            if stats['total_calls'] == 0:
                continue
            
            success_rate = (stats['successful_calls'] / stats['total_calls']) * 100
            avg_time = stats['total_time_ms'] / stats['total_calls']
            
            print(f"\n🔧 {name}")
            print(f"   总调用: {stats['total_calls']}")
            print(f"   成功: {stats['successful_calls']} ({success_rate:.1f}%)")
            print(f"   失败: {stats['failed_calls']}")
            print(f"   平均耗时: {avg_time:.0f}ms")
        
        print("="*60 + "\n")


# ============================================================================
# 工具注册表创建和初始化
# ============================================================================

def create_tool_registry() -> ToolRegistry:
    """
    创建并配置工具注册表
    返回 ToolRegistry 实例，包含所有已注册的工具
    """
    from core.tools import (
        search_properties_tool,
        calculate_commute_tool,
        check_safety_tool,
        get_weather_tool,
        web_search_tool,
        search_nearby_pois_tool,
        get_property_details_tool,
        calculate_commute_cost_tool  # 🆕 综合通勤成本计算工具
    )
    from core.tools.check_transport_cost import check_transport_cost_tool
    from core.tools.get_transport_info import get_transport_info_tool
    from core.tools.memory_tools import recall_memory_tool, remember_tool
    from core.tools.ask_user import ask_user_tool
    from core.tools.compare_or_rank_areas import compare_or_rank_areas_tool

    registry = ToolRegistry()

    # 注册所有工具
    registry.register(search_properties_tool)
    registry.register(calculate_commute_tool)
    registry.register(calculate_commute_cost_tool)  # 🆕 综合通勤成本计算工具（时间+费用）
    registry.register(check_safety_tool)
    registry.register(get_weather_tool)
    registry.register(web_search_tool)
    registry.register(search_nearby_pois_tool)
    registry.register(get_property_details_tool)
    registry.register(check_transport_cost_tool)  # 交通费用查询工具
    registry.register(get_transport_info_tool)    # 🚇 实时 TfL：journey/fare/travelcard/line status
    registry.register(recall_memory_tool)         # 🧠 长期记忆：召回
    registry.register(remember_tool)              # 🧠 长期记忆：写入
    registry.register(ask_user_tool)              # ❓ 终止型：向用户反问澄清
    registry.register(compare_or_rank_areas_tool)  # 🏙️ 区域性价比排序/比较（design §2.5b）

    logger.info("Tool registry initialized with %s tools", len(registry.tools))

    return registry
