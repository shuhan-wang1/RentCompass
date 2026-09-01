"""`MANAGER_V1_SPECIALISTS=1` + MCP must fail at config load, not at graph build.

THE DEFECT (R1-M3). When MCP is enabled and connects, `app/app.py` sets the
graph's tool provider to `MCPToolClient`, which exposes neither
`resolve_specialist_capability` nor `execute_resolved_specialist_capability`
(both were added to `ToolRegistry` only). `build_manager_v1_graph` then raises a
bare `RuntimeError` out of graph CONSTRUCTION — fail-closed, but on the first
turn, from a pool that already passed `/ready` and is already taking traffic.

There is a second, quieter half. `app/app.py` decides whether to start MCP from
the RAW environment::

    os.environ.get("USE_MCP_TOOLS", "0").lower() not in ("0", "false", "no")

`Config` used to read the same variable with `_bool`, which treats everything
outside {1,true,yes,on} as false. `off` and `''` therefore meant "off" to the
config validator and "ON" to the app: config accepted the pair, and the graph
build died. Both halves are asserted here.
"""
from __future__ import annotations

import pytest

from uk_rent_agent.config import Config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "AGENT_ARCH", "MANAGER_V1_SPECIALISTS", "USE_MCP_TOOLS",
        "CHECKPOINT_DB_PATH", "CHECKPOINT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_pair_is_refused_at_config_load(monkeypatch):
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")
    monkeypatch.setenv("USE_MCP_TOOLS", "1")

    with pytest.raises(ValueError) as excinfo:
        Config.from_env()

    message = str(excinfo.value)
    assert message == Config.MCP_SPECIALISTS_CONFLICT
    assert "build_manager_v1_graph" in message, "the message must name what breaks"
    assert "CANARY_USE_MCP_TOOLS=0" in message, "and the operator-facing fix"


@pytest.mark.parametrize("spelling", ["1", "true", "yes", "on", "TRUE", "On"])
def test_every_true_spelling_of_mcp_is_caught(monkeypatch, spelling):
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")
    monkeypatch.setenv("USE_MCP_TOOLS", spelling)

    with pytest.raises(ValueError, match="USE_MCP_TOOLS=0"):
        Config.from_env()


@pytest.mark.parametrize("name", ["USE_MCP_TOOLS", "MANAGER_V1_SPECIALISTS"])
@pytest.mark.parametrize("spelling", ["maybe", "2", "enabled", "-"])
def test_an_unrecognised_spelling_of_either_switch_is_refused_by_name(
    monkeypatch, name, spelling
):
    """`_bool` silently read these as false while `app/app.py` read some as true."""
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv(name, spelling)

    with pytest.raises(ValueError) as excinfo:
        Config.from_env()

    assert name in str(excinfo.value)
    assert "capability boundary" in str(excinfo.value)


@pytest.mark.parametrize("spelling", ["0", "false", "no", "off", ""])
def test_every_false_spelling_still_builds(monkeypatch, spelling):
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")
    monkeypatch.setenv("USE_MCP_TOOLS", spelling)

    config = Config.from_env()

    assert config.use_mcp_tools is False
    assert config.manager_v1_specialists_effective is True


def test_the_pair_is_harmless_on_an_architecture_that_cannot_run_specialists(monkeypatch):
    """The switch is architecture-bound: fc_loop never builds a manager graph."""
    monkeypatch.setenv("AGENT_ARCH", "fc_loop")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")
    monkeypatch.setenv("USE_MCP_TOOLS", "1")

    config = Config.from_env()

    assert config.manager_v1_specialists_effective is False


def test_readiness_reports_the_conflict_when_the_runtime_serves_tools_over_mcp():
    """`/ready` is the gate every deploy script polls, so it must say this too."""
    import sys
    import types

    from uk_rent_agent.web import asgi

    config = Config(project_root=asgi.Path("/tmp"), agent_arch="manager_v1",
                    manager_v1_specialists=True, use_mcp_tools=False)
    module = types.SimpleNamespace(
        _runtime_config=config,
        AGENT_ARCH="manager_v1",
        DEEPSEEK_STRICT=False,
        MANAGER_V1_SPECIALISTS=True,
        tool_registry=object(),
        agent_tool_provider=object(),   # an MCP client: NOT the registry
    )
    saved = asgi._legacy_module
    # Evict the cached module for this call only and put it back afterwards:
    # leaving it popped makes later tests (e.g. test_model_name_defaults) see a
    # freshly-imported core.llm_config under a different environment.
    saved_llm_config = sys.modules.pop("core.llm_config", None)
    try:
        asgi._legacy_module = lambda: module
        result = asgi._check_runtime_configuration(config)
    finally:
        asgi._legacy_module = saved
        if saved_llm_config is not None:
            sys.modules["core.llm_config"] = saved_llm_config

    assert result["status"] == "fail"
    assert result["tools_over_mcp"] is True
    assert Config.MCP_SPECIALISTS_CONFLICT in result["detail"]


def test_readiness_flags_a_quiet_mcp_provider_even_without_specialists():
    import sys
    import types

    from uk_rent_agent.web import asgi

    config = Config(project_root=asgi.Path("/tmp"), agent_arch="fc_loop",
                    manager_v1_specialists=False, use_mcp_tools=False)
    module = types.SimpleNamespace(
        _runtime_config=config,
        AGENT_ARCH="fc_loop",
        DEEPSEEK_STRICT=False,
        MANAGER_V1_SPECIALISTS=False,
        tool_registry=object(),
        agent_tool_provider=object(),
    )
    saved = asgi._legacy_module
    # Evict the cached module for this call only and put it back afterwards:
    # leaving it popped makes later tests (e.g. test_model_name_defaults) see a
    # freshly-imported core.llm_config under a different environment.
    saved_llm_config = sys.modules.pop("core.llm_config", None)
    try:
        asgi._legacy_module = lambda: module
        result = asgi._check_runtime_configuration(config)
    finally:
        asgi._legacy_module = saved
        if saved_llm_config is not None:
            sys.modules["core.llm_config"] = saved_llm_config

    assert result["status"] == "fail"
    assert "USE_MCP_TOOLS mismatch" in result["detail"]


def test_readiness_is_quiet_when_the_registry_serves_tools():
    import sys
    import types

    from uk_rent_agent.web import asgi

    config = Config(project_root=asgi.Path("/tmp"), agent_arch="manager_v1",
                    manager_v1_specialists=True, use_mcp_tools=False)
    registry = object()
    module = types.SimpleNamespace(
        _runtime_config=config,
        AGENT_ARCH="manager_v1",
        DEEPSEEK_STRICT=False,
        MANAGER_V1_SPECIALISTS=True,
        tool_registry=registry,
        agent_tool_provider=registry,
    )
    saved = asgi._legacy_module
    # Evict the cached module for this call only and put it back afterwards:
    # leaving it popped makes later tests (e.g. test_model_name_defaults) see a
    # freshly-imported core.llm_config under a different environment.
    saved_llm_config = sys.modules.pop("core.llm_config", None)
    try:
        asgi._legacy_module = lambda: module
        result = asgi._check_runtime_configuration(config)
    finally:
        asgi._legacy_module = saved
        if saved_llm_config is not None:
            sys.modules["core.llm_config"] = saved_llm_config

    assert result["status"] == "ok", result.get("detail")
    assert result["tools_over_mcp"] is False
