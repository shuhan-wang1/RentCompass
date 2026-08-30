"""Runtime enforcement for constraints authored in tool JSON Schema.

These tests never call an external provider: invalid input must be rejected before the tool
function is dispatched, and the synthetic tool uses a local lambda.
"""
from __future__ import annotations

import asyncio

import pytest

from core.tool_system import Tool, create_tool_registry


@pytest.fixture(scope="module")
def registry():
    return create_tool_registry()


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        (
            "calculate_commute",
            {"from_address": "A", "to_address": "B", "mode": "tube"},
        ),
        (
            "ask_user",
            {"question": "Which area?", "clarification_kind": "not-a-kind"},
        ),
        (
            "get_transport_info",
            {"query_type": "travelcard", "end_zone": 99},
        ),
        (
            "get_transport_info",
            {"query_type": "travelcard", "end_zone": "3"},
        ),
        (
            "web_search",
            {"query": "Camden", "sub_queries": [{"tool": "check_safety"}]},
        ),
        (
            "ask_user",
            {"question": "Which area?", "missing_fields": [123]},
        ),
    ],
)
def test_real_tools_reject_invalid_enum_bounds_and_nested_items(registry, tool_name, payload):
    result = asyncio.run(registry.execute_tool(tool_name, **payload))

    assert result.success is False
    assert result.error.startswith("ValidationError:")


def test_get_transport_info_end_zone_bounds_are_emitted(registry):
    prop = registry.get("get_transport_info").parameters["properties"]["end_zone"]
    concrete = next(branch for branch in prop["anyOf"] if branch.get("type") != "null")
    assert concrete["minimum"] == 2
    assert concrete["maximum"] == 6


def test_recursive_runtime_contract_enforces_all_supported_constraint_classes():
    calls = []
    tool = Tool(
        name="constraint_probe",
        description="local validation probe",
        func=lambda **kwargs: calls.append(kwargs) or {"success": True},
        parameters={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "minLength": 2,
                            "maxLength": 4,
                            "pattern": "^[A-Z]+$",
                        },
                        "move_in": {"type": "string", "format": "date"},
                        "score": {"type": "number", "minimum": 1, "maximum": 5},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["a", "b"]},
                            "minItems": 1,
                            "maxItems": 2,
                        },
                    },
                    "required": ["code", "move_in", "score", "tags"],
                    "additionalProperties": False,
                },
            },
            "required": ["payload"],
        },
        max_retries=1,
    )

    invalid_payloads = [
        {"payload": {"score": 3, "tags": ["a"]}},                 # nested required
        {"payload": {"code": "A", "move_in": "2026-09-01", "score": 3,
                     "tags": ["a"]}},
        {"payload": {"code": "abc", "move_in": "2026-09-01", "score": 3,
                     "tags": ["a"]}},
        {"payload": {"code": "ABC", "move_in": "01/09/2026", "score": 3,
                     "tags": ["a"]}},
        {"payload": {"code": "ABC", "move_in": "2026-09-01", "score": 6,
                     "tags": ["a"]}},
        {"payload": {"code": "ABC", "move_in": "2026-09-01", "score": 3,
                     "tags": []}},
        {"payload": {"code": "ABC", "move_in": "2026-09-01", "score": 3,
                     "tags": ["c"]}},
        {"payload": {"code": "ABC", "move_in": "2026-09-01", "score": 3,
                     "tags": ["a"], "extra": 1}},
    ]
    for payload in invalid_payloads:
        result = asyncio.run(tool.execute(**payload))
        assert result.success is False, payload
        assert result.error.startswith("ValidationError:"), payload

    valid = asyncio.run(
        tool.execute(payload={"code": "ABC", "move_in": "2026-09-01", "score": 3.5,
                              "tags": ["a", "b"]})
    )
    assert valid.success is True
    assert len(calls) == 1
