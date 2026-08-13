"""Regression for private FC-loop kwargs leaking into strict MCP schemas."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core.mcp_client import MCPToolClient
from core.mcp_runtime import (
    RUNTIME_META_KEY,
    runtime_arguments_from_meta,
    split_mcp_arguments,
)


class _RecordingSession:
    def __init__(self, *, text: str | None = None, is_error: bool = False):
        self.calls = []
        self.text = text or json.dumps({"success": True, "data": {"ok": True}})
        self.is_error = is_error

    async def call_tool(self, name, arguments, **options):
        self.calls.append((name, arguments, options))
        content = [SimpleNamespace(text=self.text)]
        return SimpleNamespace(content=content, isError=self.is_error)


def _client(session):
    client = MCPToolClient("unused", [])
    client._session = session
    return client


@pytest.mark.parametrize(
    "name,arguments,public",
    [
        (
            "search_properties",
            {
                "area": "Canary Wharf",
                "idempotency_key": "idem-search",
                "_deadline_monotonic": 1234.5,
            },
            {"area": "Canary Wharf"},
        ),
        (
            "compare_or_rank_areas",
            {"areas": ["Stratford", "Greenwich"], "idempotency_key": "idem-rank"},
            {"areas": ["Stratford", "Greenwich"]},
        ),
        (
            "web_search",
            {"query": "Canary Wharf rent prices", "idempotency_key": "idem-web"},
            {"query": "Canary Wharf rent prices"},
        ),
    ],
)
def test_private_runtime_kwargs_never_enter_public_mcp_arguments(name, arguments, public):
    session = _RecordingSession()

    result = asyncio.run(_client(session)._call(name, arguments))

    assert result.success is True
    called_name, called_arguments, options = session.calls[0]
    assert called_name == name
    assert called_arguments == public
    assert "idempotency_key" not in called_arguments
    assert "_deadline_monotonic" not in called_arguments
    runtime = options["meta"][RUNTIME_META_KEY]
    assert runtime["idempotency_key"] == arguments["idempotency_key"]
    if name == "search_properties":
        assert runtime["_deadline_monotonic"] == 1234.5


def test_server_restores_only_allowlisted_runtime_hints_for_the_right_tool():
    public, meta = split_mcp_arguments(
        {"area": "Canary Wharf", "idempotency_key": "idem", "_deadline_monotonic": 42}
    )

    assert public == {"area": "Canary Wharf"}
    assert runtime_arguments_from_meta("search_properties", meta) == {
        "idempotency_key": "idem",
        "_deadline_monotonic": 42.0,
    }
    assert runtime_arguments_from_meta("web_search", meta) == {
        "idempotency_key": "idem"
    }
    poisoned = {RUNTIME_META_KEY: {"_idempotency_store": "bad", "unknown": "bad"}}
    assert runtime_arguments_from_meta("search_properties", poisoned) == {}


def test_non_json_mcp_validation_error_is_reported_as_an_error_not_raw_data():
    session = _RecordingSession(
        text="Input validation error: additional properties are not allowed",
        is_error=True,
    )

    result = asyncio.run(_client(session)._call("web_search", {"query": "rent"}))

    assert result.success is False
    assert result.data is None
    assert result.error.startswith("Input validation error")


def test_real_mcp_stdio_round_trips_idempotency_metadata(tmp_path):
    """Exercise MCP 1.29's real RequestContext.meta, not only the fake session."""
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "app")]),
        "IDEMPOTENCY_DB": str(tmp_path / "idempotency.sqlite3"),
        "AGENT_MEMORY_DB_PATH": str(tmp_path / "agent-memory.sqlite3"),
    }
    client = MCPToolClient(
        sys.executable,
        ["mcp_server.py"],
        cwd=str(root / "app"),
        env=env,
        connect_timeout=30,
        call_timeout=10,
    ).start()
    try:
        assert client.connected, repr(client._connect_error)
        result = asyncio.run(
            client.execute_tool(
                "ask_user",
                question="Which area?",
                idempotency_key="protocol-meta-probe",
            )
        )
        assert result.success is True, result.error
        assert result.idempotency_key == "protocol-meta-probe"
        assert result.outcome == "complete"
        assert result.data["status"] == "ask_user"
    finally:
        client.close()
