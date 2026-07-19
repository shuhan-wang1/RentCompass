"""
DeepSeek strict-mode schema adapter (design §2.9, strict two-step plan — step 2).

Phase 1 uses non-strict function-calling with the schemas emitted by
``core.tool_system`` directly. Enabling strict mode is an independent Phase 2 step:
the DeepSeek strict endpoint (``https://api.deepseek.com/beta``) validates every
tool schema on the request path and rejects anything that is not strict-shaped.

Strict requires, at every object level:
  * every property listed in ``required``;
  * ``additionalProperties: false``;
  * the unsupported keywords ``minLength`` / ``maxLength`` / ``minItems`` /
    ``maxItems`` stripped (``enum`` / ``anyOf`` / ``$ref`` / ``$defs`` are supported).

Because strict forces every property into ``required``, an originally-optional
property must instead be made *nullable* so the model can still omit it (it emits
``null``, and :func:`strip_null_args` drops it before pydantic applies the default).

Canonical nullable representation — chosen: ``anyOf: [<non-null>, {"type": "null"}]``.
Rationale:
  * it is exactly what pydantic already emits for ``Optional[...]`` fields, so we
    normalise rather than double-wrap;
  * a nullable *enum* stays correct — the ``enum`` lives inside the non-null branch,
    so ``null`` is admitted by the sibling ``{"type": "null"}`` branch and is never a
    member of the enum list (``type: [T, "null"]`` cannot express this — a value must
    satisfy the whole enum, so ``null`` would fail);
  * property-level metadata (``description`` / ``default`` / ``title``) stays a sibling
    of ``anyOf``, matching the pydantic layout.

Everything here is a pure, deterministic, idempotent transform:
``to_strict_schema(to_strict_schema(x)) == to_strict_schema(x)``. No network calls.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

__all__ = [
    "to_strict_schema",
    "to_strict_function_calling_format",
    "strip_null_args",
    "validate_strict_compliance",
    "strict_base_url",
]

# Keywords DeepSeek strict does not support; stripped everywhere.
_BANNED_KEYWORDS = ("minLength", "maxLength", "minItems", "maxItems")

# Property-level annotations that stay a sibling of the (possibly nullable) value
# schema rather than describing the value's type; kept out of the anyOf branch.
_META_KEYWORDS = ("description", "default", "title", "deprecated", "examples")

_NULL_BRANCH = {"type": "null"}

# A strict ``anyOf`` branch MUST be a schema selector: DeepSeek rejects the whole
# tool ("field `anyOf`: one of `type`, `anyOf`, `$ref` field is required") when any
# branch carries none of these. An ``enum``-only or metadata-only branch is exactly
# that failure mode.
_BRANCH_SELECTOR_KEYWORDS = ("type", "anyOf", "$ref")


def _enum_member_type(values: Any) -> Any:
    """Derive an explicit JSON-Schema ``type`` from a branch's ``enum`` members.

    Returns a single type string, a de-duplicated list of type strings for a
    genuinely mixed enum, or ``None`` when nothing can be inferred. ``bool`` is
    checked before ``int`` (``bool`` is an ``int`` subclass) and ``integer`` is
    widened to ``number`` when a float is also present.
    """
    if not isinstance(values, (list, tuple)) or not values:
        return None
    types: List[str] = []
    for v in values:
        if isinstance(v, bool):
            t = "boolean"
        elif isinstance(v, int):
            t = "integer"
        elif isinstance(v, float):
            t = "number"
        elif isinstance(v, str):
            t = "string"
        elif v is None:
            t = "null"
        else:
            return None  # non-JSON-primitive enum member: cannot infer safely
        if t not in types:
            types.append(t)
    if len(types) == 1:
        return types[0]
    if set(types) <= {"integer", "number"}:
        return "number"
    return types


def _has_branch_selector(node: Any) -> bool:
    return isinstance(node, dict) and any(k in node for k in _BRANCH_SELECTOR_KEYWORDS)


def _normalize_anyof_branch(branch: Any) -> Any:
    """Make one ``anyOf`` branch strict-valid, or signal it should be dropped.

    * A branch that already carries ``type`` / ``anyOf`` / ``$ref`` is returned as-is.
    * An ``enum``-only branch gains an explicit ``type`` derived from its members.
    * A truly-empty / metadata-only branch (no selector, no inferable enum) is dropped
      by returning ``None`` — it constrains nothing and is only a strict-mode violation.
    """
    if not isinstance(branch, dict):
        return branch
    if _has_branch_selector(branch):
        return branch
    if "enum" in branch:
        inferred = _enum_member_type(branch["enum"])
        if inferred is not None:
            out = dict(branch)
            out["type"] = inferred
            return out
    return None


def _clean_anyof(branches: List[Any]) -> List[Any]:
    """Normalise every branch of an ``anyOf`` union, dropping the undroppable-empty
    ones, so each surviving branch carries ``type`` / ``anyOf`` / ``$ref``."""
    out: List[Any] = []
    for b in branches:
        nb = _normalize_anyof_branch(b)
        if nb is not None:
            out.append(nb)
    return out


def _is_null_schema(node: Any) -> bool:
    """True for the ``{"type": "null"}`` branch pydantic emits for Optional fields."""
    return isinstance(node, dict) and node.get("type") == "null" and "properties" not in node


def _is_object_node(node: Dict[str, Any]) -> bool:
    return node.get("type") == "object" or isinstance(node.get("properties"), dict)


def _split_property(pv: Dict[str, Any]):
    """Split a raw property schema into (metadata, value-keywords, non-null anyOf
    branches, null_present). Normalises both the pydantic ``anyOf`` null branch and a
    ``type: [..., "null"]`` array into a single ``null_present`` signal so that the
    output is one canonical shape regardless of the input encoding (idempotency)."""
    meta: Dict[str, Any] = {}
    value: Dict[str, Any] = {}
    non_null_branches: List[Any] = []
    null_present = False

    for key, val in pv.items():
        if key in _BANNED_KEYWORDS:
            continue
        if key in _META_KEYWORDS:
            meta[key] = val
        elif key == "anyOf" and isinstance(val, list):
            for branch in val:
                if _is_null_schema(branch):
                    null_present = True
                else:
                    non_null_branches.append(branch)
        elif key == "type" and isinstance(val, list):
            concrete = [t for t in val if t != "null"]
            if len(concrete) != len(val):
                null_present = True
            if len(concrete) == 1:
                value["type"] = concrete[0]
            elif concrete:
                value["type"] = concrete
        else:
            value[key] = val

    return meta, value, non_null_branches, null_present


def _transform_property(pv: Any, name: str, original_required: set) -> Any:
    """Transform one object property into its strict form, applying nullability."""
    if not isinstance(pv, dict):
        return pv

    meta, value, non_null_branches, null_present = _split_property(pv)

    if non_null_branches:
        branches = [_transform(b) for b in non_null_branches]
        value_core = _transform(value)
        if len(branches) == 1:
            # A single concrete branch merges with the sibling value-keywords, with the
            # property-level keywords (e.g. a more specific ``items``) taking precedence.
            core = {**branches[0], **value_core}
        else:
            core = {**value_core, "anyOf": _clean_anyof(branches)}
    else:
        core = _transform(value)

    # Nullability is derived from the schema itself (already admits null) OR from the
    # original required set — never from the *transformed* required list, which lists
    # every property. That is what makes the transform idempotent.
    nullable = (name not in original_required) or null_present

    if not nullable:
        result = dict(core)
        result.update(meta)
        return result

    # Flatten rather than nest when the core is itself a bare anyOf union. Every
    # non-null branch is normalised so it carries an explicit type/anyOf/$ref (an
    # enum-only or metadata-only ``core`` would otherwise emit a branch DeepSeek
    # rejects); the null branch is appended last.
    if list(core.keys()) == ["anyOf"]:
        anyof = _clean_anyof(list(core["anyOf"])) + [dict(_NULL_BRANCH)]
    else:
        anyof = _clean_anyof([core]) + [dict(_NULL_BRANCH)]
    result = dict(meta)
    result["anyOf"] = anyof
    return result


# Marker appended when a genuinely free-form object is represented as a JSON string
# (the server rejects "an object with no properties"); the executor decodes it back
# via decode_json_string_args so the tool still receives a dict.
_JSON_STRING_MARKER = " (free-form JSON object — pass it JSON-encoded as a string)"


def _transform(node: Any, root: bool = False) -> Any:
    """Recursively transform any schema node into DeepSeek-strict form."""
    if not isinstance(node, dict):
        return node

    is_object = _is_object_node(node)
    original_required = set(node.get("required", []))
    out: Dict[str, Any] = {}

    for key, val in node.items():
        if key in _BANNED_KEYWORDS:
            continue
        if key == "properties" and isinstance(val, dict):
            out["properties"] = {
                pname: _transform_property(pv, pname, original_required)
                for pname, pv in val.items()
            }
        elif key in ("required", "additionalProperties") and is_object:
            continue  # rebuilt deterministically below
        elif key == "anyOf" and isinstance(val, list):
            out["anyOf"] = _clean_anyof([_transform(b) for b in val])
        elif key == "items":
            items = _transform(val) if isinstance(val, dict) else val
            # Server rule (found empirically): every sub-schema needs a type selector.
            # pydantic emits ``items: {}`` for a bare ``list`` annotation — default the
            # member type to string, which matches every such array in this codebase.
            if isinstance(items, dict) and not (set(items) & {"type", "anyOf", "$ref"}):
                items = {**items, "type": "string"}
            out["items"] = items
        elif key in ("$defs", "definitions") and isinstance(val, dict):
            out[key] = {dname: _transform(dv) for dname, dv in val.items()}
        else:
            out[key] = val

    if is_object:
        props = out.get("properties", {})
        if not props and not root:
            # Server rule (found empirically): "An object with no properties is not
            # allowed." A genuinely free-form object (e.g. web_search sub_queries[]
            # .params) becomes a JSON-encoded string; decode_json_string_args restores
            # the dict on the executor side. Root parameter objects are left alone —
            # every registered tool has properties, and a paramless tool must stay an
            # object for the FC payload shape.
            return {"type": "string",
                    "description": (out.get("description") or "Free-form object")
                    + _JSON_STRING_MARKER}
        out["required"] = list(props.keys())
        out["additionalProperties"] = False

    return out


def to_strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Pure transform of one JSON schema into DeepSeek-strict form.

    Deterministic and idempotent: ``to_strict_schema(to_strict_schema(x))`` equals
    ``to_strict_schema(x)``. Does not mutate the input.
    """
    if not isinstance(schema, dict):
        return schema
    return _transform(schema, root=True)


def to_strict_function_calling_format(spec: Any) -> Dict[str, Any]:
    """Native function-calling tool definition carrying strict-adapted parameters.

    ``spec`` is duck-typed (``name`` / ``description`` / ``input_schema``), matching
    ``core.tool_system.ToolSpec``.
    """
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": to_strict_schema(spec.input_schema),
            "strict": True,
        },
    }


def strip_null_args(args: Any) -> Any:
    """Executor-side: drop ``null`` argument values before pydantic validation so tool
    defaults apply. Recurses into nested objects; keeps falsy-but-non-null values
    (``0`` / ``""`` / ``False`` / ``[]``)."""
    if not isinstance(args, dict):
        return args
    out: Dict[str, Any] = {}
    for key, val in args.items():
        if val is None:
            continue
        out[key] = strip_null_args(val) if isinstance(val, dict) else val
    return out


def decode_json_string_args(args: Any, authored_schema: Any) -> Any:
    """Executor-side reverse of the free-form-object-as-string encoding.

    Walks ``args`` against the AUTHORED (pre-strict) schema; wherever the schema says
    ``object`` (or an object inside array items) but the model — bound to the strict
    schema — sent a string, the string is json-decoded back to a dict. Tolerant: a
    non-JSON string or any mismatch leaves the value untouched.
    """
    import json as _json

    def _prop_schema(schema: Any) -> Dict[str, Any]:
        if not isinstance(schema, dict):
            return {}
        if isinstance(schema.get("anyOf"), list):
            for br in schema["anyOf"]:
                if isinstance(br, dict) and br.get("type") != "null":
                    return br
        return schema

    def _walk(value: Any, schema: Any) -> Any:
        schema = _prop_schema(schema)
        stype = schema.get("type")
        if stype == "object" or isinstance(schema.get("properties"), dict):
            if isinstance(value, str):
                try:
                    decoded = _json.loads(value)
                except (ValueError, TypeError):
                    return value
                return decoded if isinstance(decoded, dict) else value
            if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
                return {k: _walk(v, schema["properties"].get(k, {}))
                        for k, v in value.items()}
            return value
        if stype == "array" and isinstance(value, list):
            return [_walk(v, schema.get("items", {})) for v in value]
        return value

    if not isinstance(args, dict) or not isinstance(authored_schema, dict):
        return args
    props = authored_schema.get("properties") or {}
    return {k: _walk(v, props.get(k, {})) for k, v in args.items()}


def validate_strict_compliance(schema: Dict[str, Any]) -> List[str]:
    """Return a list of strict-compliance violations (empty == compliant).

    Checks, at every object level: all properties listed in ``required``,
    ``additionalProperties: false``, no banned keywords anywhere, and — at every
    ``anyOf`` union — that each branch carries one of ``type`` / ``anyOf`` / ``$ref``
    (DeepSeek strict rejects an enum-only or metadata-only branch on the request path).
    """
    violations: List[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        for banned in _BANNED_KEYWORDS:
            if banned in node:
                violations.append(f"{path}: banned keyword '{banned}'")
        if _is_object_node(node):
            if node.get("additionalProperties", None) is not False:
                violations.append(f"{path}: additionalProperties must be false")
            if not node.get("properties") and path != "$":
                # Server rule: "An object with no properties is not allowed."
                violations.append(f"{path}: object with no properties")
            props = node.get("properties") or {}
            required = node.get("required") or []
            missing = [p for p in props if p not in required]
            if missing:
                violations.append(f"{path}: properties not in required: {missing}")
            extra = [r for r in required if r not in props]
            if extra:
                violations.append(f"{path}: required lists unknown properties: {extra}")
            for pname, pv in props.items():
                walk(pv, f"{path}.properties.{pname}")
        if isinstance(node.get("anyOf"), list):
            for i, branch in enumerate(node["anyOf"]):
                if isinstance(branch, dict) and not _has_branch_selector(branch):
                    violations.append(
                        f"{path}.anyOf[{i}]: branch must have one of "
                        f"type/anyOf/$ref (got keys {sorted(branch.keys())})")
                walk(branch, f"{path}.anyOf[{i}]")
        if isinstance(node.get("items"), dict):
            # Server rule: every sub-schema (items included) needs a type selector.
            if not (set(node["items"]) & {"type", "anyOf", "$ref"}):
                violations.append(f"{path}.items: missing type/anyOf/$ref")
            walk(node["items"], f"{path}.items")
        for defkey in ("$defs", "definitions"):
            if isinstance(node.get(defkey), dict):
                for dname, dv in node[defkey].items():
                    walk(dv, f"{path}.{defkey}.{dname}")

    walk(schema, "$")
    return violations


def strict_base_url() -> str:
    """DeepSeek strict base URL derived from ``DEEPSEEK_BASE_URL`` (append ``/beta``
    when absent). For the coordinator's ModelRouter wiring."""
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if base.endswith("/beta"):
        return base
    return base + "/beta"
