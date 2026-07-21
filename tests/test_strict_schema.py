"""Local compliance coverage for the DeepSeek strict schema adapter (app/core/strict_schema.py).

No live API anywhere — server-side acceptance is the coordinator's LIVE A/B. Here we prove the
pure transform is strict-shaped, nullability is correct, banned keywords are stripped, and the
transform is deterministic + idempotent, across every real tool schema plus synthetic edges.
"""
from __future__ import annotations

import pytest

from core.strict_schema import (
    strict_base_url,
    strip_null_args,
    to_strict_function_calling_format,
    to_strict_schema,
    validate_strict_compliance,
)
from core.tool_system import create_tool_registry


# ─── real tool schemas ──────────────────────────────────────────────
_SPECS = create_tool_registry().list_specs()
_SPEC_BY_NAME = {s.name: s for s in _SPECS}
_TOOL_NAMES = sorted(_SPEC_BY_NAME)


def test_registry_has_fourteen_tools():
    assert len(_SPECS) == 14


# The "14 schema acceptability tests" gate (design §2.9 step 2): each transformed schema
# passes local strict compliance with zero violations. Parametrised so failures name the tool.
@pytest.mark.parametrize("name", _TOOL_NAMES)
def test_transformed_schema_is_strict_compliant(name):
    strict = to_strict_schema(_SPEC_BY_NAME[name].input_schema)
    violations = validate_strict_compliance(strict)
    assert violations == [], f"{name} not strict-compliant: {violations}"


def _iter_anyof_branches(node, path="$"):
    """Yield (path, branch) for every anyOf branch anywhere in a schema."""
    if isinstance(node, dict):
        if isinstance(node.get("anyOf"), list):
            for i, br in enumerate(node["anyOf"]):
                yield f"{path}.anyOf[{i}]", br
        for k, v in node.items():
            yield from _iter_anyof_branches(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, it in enumerate(node):
            yield from _iter_anyof_branches(it, f"{path}[{i}]")


@pytest.mark.parametrize("name", _TOOL_NAMES)
def test_every_anyof_branch_carries_a_selector(name):
    # The gate that would have caught the live DeepSeek 400 "field `anyOf`: one of
    # type/anyOf/$ref field is required": every anyOf branch of every transformed tool
    # schema must carry type / anyOf / $ref.
    strict = to_strict_schema(_SPEC_BY_NAME[name].input_schema)
    for path, br in _iter_anyof_branches(strict):
        assert isinstance(br, dict) and any(k in br for k in ("type", "anyOf", "$ref")), (
            f"{name} {path} branch lacks type/anyOf/$ref: {br}")


@pytest.mark.parametrize("name", _TOOL_NAMES)
def test_top_level_all_properties_required(name):
    strict = to_strict_schema(_SPEC_BY_NAME[name].input_schema)
    props = strict.get("properties", {})
    assert set(strict.get("required", [])) == set(props)
    assert strict.get("additionalProperties") is False


# ─── nullability semantics ──────────────────────────────────────────
def _admits_null(prop: dict) -> bool:
    if isinstance(prop.get("type"), list):
        return "null" in prop["type"]
    if prop.get("type") == "null":
        return True
    return any(
        isinstance(b, dict) and b.get("type") == "null"
        for b in prop.get("anyOf", [])
    )


def test_originally_optional_became_nullable_required_stayed_non_null():
    # calculate_commute: from_address/to_address required, mode optional.
    strict = to_strict_schema(_SPEC_BY_NAME["calculate_commute"].input_schema)
    props = strict["properties"]
    assert not _admits_null(props["from_address"])
    assert not _admits_null(props["to_address"])
    assert _admits_null(props["mode"])


def test_search_properties_all_required_and_nullable_after_transform():
    raw = _SPEC_BY_NAME["search_properties"].input_schema
    strict = to_strict_schema(raw)
    props = strict["properties"]
    assert len(props) == 21, f"expected 21 props, got {len(props)}"
    assert set(strict["required"]) == set(props)
    # Every search_properties field is optional in the source, so all become nullable.
    for pname, pv in props.items():
        assert _admits_null(pv), f"{pname} should be nullable"


def test_ask_user_clarification_kind_enum_preserved_and_null_not_in_enum():
    strict = to_strict_schema(_SPEC_BY_NAME["ask_user"].input_schema)
    kind = strict["properties"]["clarification_kind"]
    # nullable -> anyOf with a null branch; enum lives on the non-null branch, null excluded.
    assert _admits_null(kind)
    non_null = [b for b in kind["anyOf"] if b.get("type") != "null"]
    assert len(non_null) == 1
    assert non_null[0]["enum"] == ["missing_area", "soft_criteria", "other"]
    assert "null" not in non_null[0]["enum"]
    # required field stays non-null and keeps its plain type.
    assert strict["properties"]["question"]["type"] == "string"


def test_nullable_array_keeps_specific_items_and_enum():
    # compare_or_rank_areas.priorities: nullable array whose items carry an enum.
    strict = to_strict_schema(_SPEC_BY_NAME["compare_or_rank_areas"].input_schema)
    pri = strict["properties"]["priorities"]
    non_null = [b for b in pri["anyOf"] if b.get("type") != "null"]
    assert len(non_null) == 1
    items = non_null[0]["items"]
    assert items["type"] == "string"
    assert items["enum"] == ["value", "commute", "safety", "amenities"]


# ─── banned keyword stripping ───────────────────────────────────────
_SYNTHETIC = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 50},
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 2},
            "minItems": 1,
            "maxItems": 10,
        },
        "nested": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 3},
            },
            "required": ["code"],
        },
        "opt": {"type": "string", "maxLength": 8},
    },
    "required": ["name", "tags", "nested"],
}


def _find_banned(node) -> list:
    found = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k in ("minLength", "maxLength", "minItems", "maxItems"):
                    found.append(k)
                walk(v)
        elif isinstance(n, list):
            for it in n:
                walk(it)

    walk(node)
    return found


def test_banned_keywords_stripped_everywhere():
    strict = to_strict_schema(_SYNTHETIC)
    assert _find_banned(strict) == []
    assert validate_strict_compliance(strict) == []
    # nested object also fully required + closed.
    nested = strict["properties"]["nested"]
    assert nested["additionalProperties"] is False
    assert set(nested["required"]) == {"code"}
    # opt was optional -> nullable; name/tags/nested stayed non-null.
    assert _admits_null(strict["properties"]["opt"])
    assert not _admits_null(strict["properties"]["name"])


# ─── typeless anyOf branch handling (DeepSeek strict 400 fix) ────────
def test_optional_enum_only_branch_gains_explicit_type():
    # An optional property whose value schema is enum-only (no `type`) — the exact
    # shape an MCP-provided inputSchema can carry — must NOT emit a typeless anyOf
    # branch; the adapter derives the type from the enum members.
    schema = {
        "type": "object",
        "properties": {
            "kind": {"enum": ["a", "b", "c"], "description": "no type key"},
            "level": {"enum": [1, 2, 3]},
            "note": {"description": "metadata only, no type at all"},
        },
        "required": [],
    }
    strict = to_strict_schema(schema)
    assert validate_strict_compliance(strict) == []
    kind = strict["properties"]["kind"]
    non_null = [b for b in kind["anyOf"] if b.get("type") != "null"]
    assert len(non_null) == 1
    assert non_null[0]["type"] == "string"
    assert non_null[0]["enum"] == ["a", "b", "c"]
    level = strict["properties"]["level"]
    non_null_level = [b for b in level["anyOf"] if b.get("type") != "null"]
    assert non_null_level[0]["type"] == "integer"
    # A truly-empty (metadata-only) branch is DROPPED, leaving only the null branch.
    note = strict["properties"]["note"]
    assert note["anyOf"] == [{"type": "null"}]
    # Idempotent under a second pass.
    assert to_strict_schema(strict) == strict


def test_validate_flags_typeless_anyof_branch():
    # A hand-built schema with an enum-only anyOf branch (no type/anyOf/$ref) must be
    # reported by the validator — the rule the local gate previously missed.
    bad = {
        "type": "object",
        "properties": {
            "kind": {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]},
        },
        "required": ["kind"],
        "additionalProperties": False,
    }
    violations = validate_strict_compliance(bad)
    assert any("anyOf[0]" in v and "type/anyOf/$ref" in v for v in violations), violations
    # An empty branch is also flagged.
    bad_empty = {
        "type": "object",
        "properties": {"x": {"anyOf": [{}, {"type": "null"}]}},
        "required": ["x"],
        "additionalProperties": False,
    }
    assert validate_strict_compliance(bad_empty), "empty anyOf branch must be flagged"


# ─── determinism + idempotency ──────────────────────────────────────
@pytest.mark.parametrize("name", _TOOL_NAMES + ["__synthetic__"])
def test_idempotent(name):
    raw = _SYNTHETIC if name == "__synthetic__" else _SPEC_BY_NAME[name].input_schema
    once = to_strict_schema(raw)
    twice = to_strict_schema(once)
    assert once == twice, f"{name} not idempotent"
    # deterministic: repeat transform of the raw input is identical.
    assert to_strict_schema(raw) == once


def test_transform_does_not_mutate_input():
    import copy

    raw = copy.deepcopy(_SPEC_BY_NAME["search_properties"].input_schema)
    before = copy.deepcopy(raw)
    to_strict_schema(raw)
    assert raw == before


# ─── function-calling wrapper ───────────────────────────────────────
def test_to_strict_function_calling_format():
    spec = _SPEC_BY_NAME["search_properties"]
    fc = to_strict_function_calling_format(spec)
    assert fc["type"] == "function"
    assert fc["function"]["name"] == "search_properties"
    assert fc["function"]["strict"] is True
    assert fc["function"]["parameters"] == to_strict_schema(spec.input_schema)
    assert validate_strict_compliance(fc["function"]["parameters"]) == []


# ─── strip_null_args ────────────────────────────────────────────────
def test_strip_null_args_drops_nulls_keeps_falsy():
    args = {
        "a": None,
        "zero": 0,
        "empty_str": "",
        "false": False,
        "empty_list": [],
        "keep": "x",
        "nested": {"inner_null": None, "inner_zero": 0},
    }
    out = strip_null_args(args)
    assert out == {
        "zero": 0,
        "empty_str": "",
        "false": False,
        "empty_list": [],
        "keep": "x",
        "nested": {"inner_zero": 0},
    }
    assert "a" not in out
    assert "inner_null" not in out["nested"]


def test_strip_null_args_non_dict_passthrough():
    assert strip_null_args("x") == "x"
    assert strip_null_args(None) is None


# ─── strict_base_url ────────────────────────────────────────────────
def test_strict_base_url_default(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    assert strict_base_url() == "https://api.deepseek.com/beta"


def test_strict_base_url_appends_beta(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    assert strict_base_url() == "https://api.deepseek.com/beta"


def test_strict_base_url_already_beta_unchanged(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/beta")
    assert strict_base_url() == "https://api.deepseek.com/beta"


def test_strict_base_url_trailing_slash(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
    assert strict_base_url() == "https://api.deepseek.com/beta"
