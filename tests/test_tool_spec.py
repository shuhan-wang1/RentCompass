"""Unit tests for the ToolSpec contract (design §2.8a) built by Agent T.

Covers:
  - ToolRegistry.list_specs(): 13 specs (12 existing + ask_user), side_effect /
    retry_safe correct, version mirrors Tool.version, ask_user terminal=True.
  - to_function_calling_format(): non-strict FC shape.
  - ask_user execute round-trip: four fields + status echoed, nothing fabricated.
  - MCPToolClient.list_specs(): fallback path (no live server) == registry specs.
  - MCPToolClient._spec_from_mcp_tool(): reads annotations; falls back per-field.
  - mcp_server list_tools(): annotations carry the ToolSpec metadata.
"""
import asyncio

import pytest

from core.tool_system import (
    ToolSpec,
    create_tool_registry,
    to_function_calling_format,
)

EXPECTED_TOOL_COUNT = 14  # 12 existing + ask_user + compare_or_rank_areas


@pytest.fixture(scope="module")
def registry():
    return create_tool_registry()


def test_list_specs_count_and_type(registry):
    specs = registry.list_specs()
    assert len(specs) == EXPECTED_TOOL_COUNT
    assert all(isinstance(s, ToolSpec) for s in specs)
    names = {s.name for s in specs}
    assert "ask_user" in names
    # ToolSpec is frozen (immutable contract).
    with pytest.raises(Exception):
        specs[0].name = "mutated"


def test_specs_mirror_registered_tools(registry):
    """Every spec field mirrors the live Tool — registry is the single source of truth."""
    for spec in registry.list_specs():
        tool = registry.get(spec.name)
        assert tool is not None
        assert spec.description == tool.description
        assert spec.input_schema is tool.parameters or spec.input_schema == tool.parameters
        assert spec.side_effect == tool.side_effect
        assert spec.retry_safe == tool.retry_safe
        assert spec.version == tool.version  # idempotency-key version semantics preserved
        assert spec.terminal == tool.terminal


def test_remember_is_write_and_not_retry_safe(registry):
    spec = {s.name: s for s in registry.list_specs()}["remember"]
    assert spec.side_effect == "write"
    assert spec.retry_safe is False
    assert spec.terminal is False


def test_ask_user_spec(registry):
    spec = {s.name: s for s in registry.list_specs()}["ask_user"]
    assert spec.terminal is True
    assert spec.side_effect == "none"
    assert spec.retry_safe is True
    props = spec.input_schema["properties"]
    assert set(props) >= {
        "question",
        "clarification_kind",
        "missing_fields",
        "missing_optional_fields",
    }
    assert spec.input_schema["required"] == ["question"]
    # NOTE: enum/items are dropped by _model_from_schema (pre-existing for ALL tools);
    # the generated schema keeps the field + its default only.
    assert props["clarification_kind"]["default"] == "other"
    assert props["missing_fields"]["default"] == []


def test_read_only_tools_default_retry_safe(registry):
    """side_effect='none' tools are retry_safe by default."""
    for spec in registry.list_specs():
        if spec.side_effect == "none":
            assert spec.retry_safe is True


def test_to_function_calling_format_shape(registry):
    spec = {s.name: s for s in registry.list_specs()}["get_weather"]
    fc = to_function_calling_format(spec)
    assert fc["type"] == "function"
    assert fc["function"]["name"] == "get_weather"
    assert fc["function"]["description"] == spec.description
    assert fc["function"]["parameters"] == spec.input_schema
    # Non-strict: no strict flag injected (strict adapter is Phase 2).
    assert "strict" not in fc["function"]
    # parameters remain a valid OpenAI-FC object schema.
    assert fc["function"]["parameters"]["type"] == "object"
    assert "properties" in fc["function"]["parameters"]


def test_ask_user_execute_round_trip(registry):
    result = asyncio.run(
        registry.execute_tool(
            "ask_user",
            question="Where would you like to live?",
            clarification_kind="missing_area",
            missing_fields=["area"],
            missing_optional_fields=["budget"],
        )
    )
    assert result.success is True
    data = result.data
    assert data["status"] == "ask_user"
    assert data["question"] == "Where would you like to live?"
    assert data["clarification_kind"] == "missing_area"
    assert data["missing_fields"] == ["area"]
    assert data["missing_optional_fields"] == ["budget"]
    # Must NOT fabricate known_criteria (the loop executor enriches it deterministically).
    assert "known_criteria" not in data


def test_ask_user_execute_defaults(registry):
    result = asyncio.run(registry.execute_tool("ask_user", question="Q?"))
    data = result.data
    assert data["clarification_kind"] == "other"
    assert data["missing_fields"] == []
    assert data["missing_optional_fields"] == []


# --------------------------------------------------------------------------- MCP client

def test_mcp_client_list_specs_fallback(registry):
    """No live server: list_specs() falls back to the in-process registry specs."""
    from core.mcp_client import MCPToolClient

    client = MCPToolClient(
        command="python", args=["mcp_server.py"], fallback_registry=registry
    )
    # start() not called -> _tool_specs empty -> fallback path.
    specs = client.list_specs()
    assert len(specs) == EXPECTED_TOOL_COUNT
    assert {s.name for s in specs} == {s.name for s in registry.list_specs()}


def test_mcp_client_spec_from_mcp_tool_reads_annotations(registry):
    """_spec_from_mcp_tool reads ToolSpec metadata off annotations, falls back per-field."""
    import mcp.types as types
    from core.mcp_client import MCPToolClient

    client = MCPToolClient(
        command="python", args=["mcp_server.py"], fallback_registry=registry
    )

    # Full annotations present -> read straight through.
    full = types.Tool(
        name="remember",
        description="stored desc",
        inputSchema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(
            side_effect="write", retry_safe=False, version="1", terminal=False
        ),
    )
    spec = client._spec_from_mcp_tool(full)
    assert spec.side_effect == "write"
    assert spec.retry_safe is False
    assert spec.version == "1"
    assert spec.terminal is False

    # No annotations -> every field filled from the fallback registry spec.
    bare = types.Tool(
        name="ask_user",
        description="",
        inputSchema={"type": "object", "properties": {}},
    )
    spec2 = client._spec_from_mcp_tool(bare)
    fb = {s.name: s for s in registry.list_specs()}["ask_user"]
    assert spec2.side_effect == fb.side_effect
    assert spec2.retry_safe == fb.retry_safe
    assert spec2.terminal is True  # ask_user terminal recovered from registry


# --------------------------------------------------------------------------- MCP server

def test_mcp_server_list_tools_annotations():
    """list_tools() advertises side_effect/retry_safe/version/terminal on annotations."""
    import sys

    _orig_stdout = sys.stdout
    try:
        import mcp_server  # module-level: builds registry, redirects stdout->stderr
    finally:
        sys.stdout = _orig_stdout

    tools = asyncio.run(mcp_server.list_tools())
    by_name = {t.name: t for t in tools}
    assert len(by_name) == EXPECTED_TOOL_COUNT

    remember = by_name["remember"]
    assert remember.annotations is not None
    assert getattr(remember.annotations, "side_effect") == "write"
    assert getattr(remember.annotations, "retry_safe") is False
    assert getattr(remember.annotations, "version") == "1"
    assert getattr(remember.annotations, "terminal") is False
    assert remember.annotations.readOnlyHint is False

    ask = by_name["ask_user"]
    assert getattr(ask.annotations, "terminal") is True
    assert getattr(ask.annotations, "side_effect") == "none"
    assert ask.annotations.readOnlyHint is True


# ─── constraint-keyword preservation (coordinator fix) ──────────────
# _model_from_schema's pydantic round-trip used to drop enum/items from EVERY tool
# schema, so the FC-bound model never saw the legal values. _merge_constraint_keywords
# copies them back from the author-written schema.

def _prop(registry, tool, name):
    return _resolved(registry.get(tool).parameters["properties"][name])


def _resolved(prop):
    """Constraints live on the non-null anyOf branch for Optional properties (the
    canonical spot the strict adapter consumes); resolve through it."""
    if isinstance(prop, dict) and isinstance(prop.get("anyOf"), list):
        for br in prop["anyOf"]:
            if isinstance(br, dict) and br.get("type") != "null":
                return br
    return prop


def test_enum_preserved_in_emitted_schema(registry):
    assert _prop(registry, "ask_user", "clarification_kind")["enum"] == [
        "missing_area", "soft_criteria", "other"]
    assert set(_prop(registry, "calculate_commute", "mode")["enum"]) == {
        "transit", "driving", "walking", "bicycling"}
    assert set(_prop(registry, "calculate_commute_cost", "travel_type")["enum"]) == {
        "student", "adult"}


def test_array_items_preserved_in_emitted_schema(registry):
    items = _prop(registry, "ask_user", "missing_fields").get("items")
    assert items == {"type": "string"}


def test_specs_carry_preserved_constraints(registry):
    spec = next(s for s in registry.list_specs() if s.name == "ask_user")
    assert _resolved(spec.input_schema["properties"]["clarification_kind"])["enum"] == [
        "missing_area", "soft_criteria", "other"]
