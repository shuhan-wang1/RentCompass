"""Security regressions for web_search's nested tool dispatcher."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import core.tools.web_search as ws


@dataclass
class _Meta:
    name: str
    side_effect: str = "none"
    terminal: bool = False
    category: str = ""


class _Registry:
    def __init__(self, metadata=(), *, result=None, raises=None):
        self._metadata = {item.name: item for item in metadata}
        self.calls = []
        self._result = result or {
            "forecast": "Clear skies and dry weather are expected in London today."
        }
        self._raises = raises

    def get(self, name):
        return self._metadata.get(name)

    def list_specs(self):
        return list(self._metadata.values())

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(success=True, data=self._result, error=None)


def _run(*, query="ordinary query", sub_queries=None):
    return asyncio.run(ws.web_search_func(query=query, sub_queries=sub_queries))


@pytest.mark.parametrize(
    ("tool_name", "expected_code"),
    [
        ("remember", "nested_tool_not_allowed"),
        ("recall_memory", "nested_tool_not_allowed"),
        ("ask_user", "nested_tool_not_allowed"),
        ("web_search", "nested_self_recursion_forbidden"),
        ("totally_unknown_tool", "nested_tool_not_allowed"),
    ],
)
def test_forbidden_and_unknown_nested_tools_are_never_dispatched(
    monkeypatch, tool_name, expected_code
):
    registry = _Registry([
        _Meta("remember", side_effect="write"),
        _Meta("recall_memory"),
        _Meta("ask_user", terminal=True),
        _Meta("web_search"),
    ])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(sub_queries=[{"tool": tool_name, "params": {}}])

    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert registry.calls == []


def test_entire_batch_is_preflighted_before_first_dispatch(monkeypatch):
    registry = _Registry([_Meta("get_weather"), _Meta("remember", side_effect="write")])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(
        sub_queries=[
            {"tool": "get_weather", "params": {"location": "London"}},
            {"tool": "remember", "params": {"content": "do not write this"}},
        ]
    )

    assert result["success"] is False
    assert registry.calls == []


@pytest.mark.parametrize(
    ("tool_name", "meta", "expected_code"),
    [
        (
            "check_safety",
            _Meta("check_safety", side_effect="write"),
            "nested_tool_side_effect_forbidden",
        ),
        (
            "ask_user",
            _Meta("ask_user", terminal=True),
            "nested_terminal_tool_forbidden",
        ),
        (
            "recall_memory",
            _Meta("recall_memory"),
            "nested_memory_tool_forbidden",
        ),
    ],
)
def test_runtime_metadata_still_denies_unsafe_tools_if_allowlist_expands(
    monkeypatch, tool_name, meta, expected_code
):
    # Simulate a future allowlist edit: metadata remains a second, dynamic policy boundary.
    monkeypatch.setattr(ws, "_ALLOWED_NESTED_TOOLS", ws._ALLOWED_NESTED_TOOLS | {tool_name})
    registry = _Registry([meta])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(sub_queries=[{"tool": tool_name, "params": {}}])

    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert registry.calls == []


def test_conflicting_tool_and_spec_metadata_fails_closed(monkeypatch):
    safe = _Meta("get_weather")
    unsafe = _Meta("get_weather", side_effect="write")

    class _ConflictingRegistry(_Registry):
        def get(self, name):
            return safe

        def list_specs(self):
            return [unsafe]

    registry = _ConflictingRegistry()
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(
        sub_queries=[{"tool": "get_weather", "params": {"location": "London"}}]
    )

    assert result["error_code"] == "nested_tool_side_effect_forbidden"
    assert registry.calls == []


def test_allowlisted_read_only_nonterminal_tool_runs(monkeypatch):
    registry = _Registry([_Meta("get_weather")])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(
        sub_queries=[{"tool": "get_weather", "params": {"location": "London"}}]
    )

    assert result["success"] is True
    assert registry.calls == [("get_weather", {"location": "London"})]
    assert "get_weather_1" in result["detailed_data"]


@pytest.mark.parametrize(
    ("sub_queries", "expected_code"),
    [
        ([{"tool": "get_weather", "params": "not-an-object"}],
         "nested_params_must_be_object"),
        ([{"tool": "get_weather", "params": {"items": list(range(33))}}],
         "nested_params_too_many_items"),
        ([{"tool": "get_weather", "params": {"blob": "x" * 4097}}],
         "nested_params_string_too_long"),
        ({"tool": "get_weather", "params": {}}, "sub_queries_must_be_array"),
    ],
)
def test_malformed_or_oversized_params_are_rejected(
    monkeypatch, sub_queries, expected_code
):
    registry = _Registry([_Meta("get_weather")])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    result = _run(sub_queries=sub_queries)

    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert registry.calls == []


def test_deep_and_cyclic_params_are_rejected(monkeypatch):
    registry = _Registry([_Meta("get_weather")])
    monkeypatch.setattr(ws, "_tool_registry", registry)

    deep = {}
    cursor = deep
    for _ in range(ws._MAX_PARAM_DEPTH + 1):
        cursor["next"] = {}
        cursor = cursor["next"]
    deep_result = _run(
        sub_queries=[{"tool": "get_weather", "params": deep}]
    )

    cyclic = {}
    cyclic["self"] = cyclic
    cyclic_result = _run(
        sub_queries=[{"tool": "get_weather", "params": cyclic}]
    )

    assert deep_result["error_code"] == "nested_params_too_deep"
    assert cyclic_result["error_code"] == "nested_params_cycle"
    assert registry.calls == []


def test_subquery_fanout_is_bounded(monkeypatch):
    registry = _Registry([_Meta("get_weather")])
    monkeypatch.setattr(ws, "_tool_registry", registry)
    sub_queries = [
        {"tool": "get_weather", "params": {"location": str(index)}}
        for index in range(ws._MAX_SUB_QUERIES + 1)
    ]

    result = _run(sub_queries=sub_queries)

    assert result["error_code"] == "too_many_sub_queries"
    assert registry.calls == []


def test_query_and_params_are_not_reflected_in_output_or_logs(
    monkeypatch, caplog, capsys
):
    secret_query = "PRIVATE_QUERY_CANARY_user@example.test"
    secret_param = "PRIVATE_ADDRESS_CANARY_14 Secret Street"
    registry = _Registry([_Meta("get_weather")])
    monkeypatch.setattr(ws, "_tool_registry", registry)
    caplog.set_level("INFO", logger=ws.__name__)

    result = _run(
        query=secret_query,
        sub_queries=[{"tool": "get_weather", "params": {"location": secret_param}}],
    )

    captured = capsys.readouterr()
    visible = "\n".join(
        [caplog.text, captured.out, captured.err, json.dumps(result, ensure_ascii=False)]
    )
    assert result["success"] is True
    assert secret_query not in visible
    assert secret_param not in visible
    assert result["query_metadata"] == {"length": len(secret_query)}


def test_nested_exception_does_not_leak_exception_or_params(
    monkeypatch, caplog, capsys
):
    secret = "PRIVATE_FAILURE_CANARY_55 Secret Road"
    registry = _Registry(
        [_Meta("get_weather")], raises=RuntimeError(f"backend rejected {secret}")
    )
    monkeypatch.setattr(ws, "_tool_registry", registry)
    caplog.set_level("INFO", logger=ws.__name__)

    result = _run(
        query="weather",
        sub_queries=[{"tool": "get_weather", "params": {"location": secret}}],
    )

    captured = capsys.readouterr()
    visible = "\n".join(
        [caplog.text, captured.out, captured.err, json.dumps(result, ensure_ascii=False)]
    )
    assert secret not in visible
    assert "RuntimeError" in caplog.text


def test_web_search_only_uses_backend_without_query_reflection(monkeypatch):
    secret = "PRIVATE_WEB_QUERY_CANARY"
    seen = []

    def fake_search(query, max_results):
        seen.append((query, max_results))
        return "Public source says the London rental market remains competitive."

    monkeypatch.setattr(ws, "_tool_registry", _Registry())
    monkeypatch.setattr(ws, "get_search_snippets", fake_search)

    result = _run(
        query="main query",
        sub_queries=[{"tool": "web_search_only", "params": {"query": secret}}],
    )

    assert result["success"] is True
    assert seen == [(secret, 5)]
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_function_schema_advertises_the_same_closed_allowlist_and_bound():
    sub_schema = ws.web_search_tool.parameters["properties"]["sub_queries"]
    # Optional fields are emitted as anyOf[array, null] by Pydantic; constraints live
    # on the concrete array branch after Tool's schema-fidelity merge.
    array_schema = next(
        branch for branch in sub_schema.get("anyOf", [sub_schema])
        if branch.get("type") == "array"
    )
    items = array_schema["items"]
    assert array_schema["maxItems"] == ws._MAX_SUB_QUERIES
    assert set(items["properties"]["tool"]["enum"]) == ws._ALLOWED_NESTED_TOOLS
    assert items["additionalProperties"] is False
    assert {"remember", "recall_memory", "ask_user", "web_search"}.isdisjoint(
        items["properties"]["tool"]["enum"]
    )
