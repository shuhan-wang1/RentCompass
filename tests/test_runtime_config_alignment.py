from __future__ import annotations

import sys

import pytest

from uk_rent_agent.config import Config
from uk_rent_agent.web.app import _install_runtime_environment, create_app


def test_agent_switches_are_stripped_normalized_and_carried_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "  FC_LOOP  ")
    monkeypatch.setenv("DEEPSEEK_STRICT", "  YeS  ")
    monkeypatch.setenv("LLM_PROVIDER", "  OLLAMA  ")

    config = Config.from_env()

    assert config.agent_arch == "fc_loop"
    assert config.deepseek_strict is True
    assert config.llm_provider == "ollama"

    _install_runtime_environment(config)
    assert __import__("os").environ["AGENT_ARCH"] == "fc_loop"
    assert __import__("os").environ["DEEPSEEK_STRICT"] == "1"
    assert __import__("os").environ["LLM_PROVIDER"] == "ollama"


@pytest.mark.parametrize(
    ("name", "value"),
    [("AGENT_ARCH", "fc"), ("LLM_PROVIDER", "some-cloud")],
)
def test_invalid_finite_runtime_switches_fail_at_startup(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Config.from_env()


def test_flask_bootstrap_receives_the_exact_config_object(monkeypatch, tmp_path):
    # Register every process-wide switch with monkeypatch so create_app's intentional env
    # publication is restored at teardown and cannot leak into unrelated import-order tests.
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("DEEPSEEK_STRICT", "0")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("USE_MCP_TOOLS", "0")
    monkeypatch.setenv("FLASK_SECRET_KEY", "prior-test-secret")
    legacy_dir = tmp_path / "app"
    legacy_dir.mkdir()
    (legacy_dir / "app.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "_session_store = object()\n"
        "_runtime_config = _BOOTSTRAP_CONFIG\n",
        encoding="utf-8",
    )
    config = Config(
        project_root=tmp_path,
        agent_arch="fc_loop",
        deepseek_strict=True,
        llm_provider="ollama",
        flask_secret_key="test-secret",
        use_mcp_tools=True,
    )
    module_name = "uk_rent_agent._legacy_web_app"
    prior = sys.modules.pop(module_name, None)
    try:
        flask_app = create_app(config)
        loaded = sys.modules[module_name]

        assert loaded._runtime_config is config
        assert flask_app.config["RUNTIME_CONFIG"] is config
        assert flask_app.config["SESSION_STORE"] is loaded._session_store

        before = {
            name: __import__("os").environ[name]
            for name in ("AGENT_ARCH", "DEEPSEEK_STRICT", "LLM_PROVIDER", "USE_MCP_TOOLS")
        }
        with pytest.raises(RuntimeError, match="different Config"):
            create_app(Config(project_root=tmp_path))
        assert {
            name: __import__("os").environ[name]
            for name in before
        } == before
    finally:
        sys.modules.pop(module_name, None)
        if prior is not None:
            sys.modules[module_name] = prior


def test_failed_flask_bootstrap_does_not_cache_partial_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("DEEPSEEK_STRICT", "0")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("USE_MCP_TOOLS", "0")
    legacy_dir = tmp_path / "app"
    legacy_dir.mkdir()
    (legacy_dir / "app.py").write_text("raise RuntimeError('injected boot failure')\n")
    module_name = "uk_rent_agent._legacy_web_app"
    prior = sys.modules.pop(module_name, None)
    try:
        with pytest.raises(RuntimeError, match="injected boot failure"):
            create_app(Config(project_root=tmp_path))
        assert module_name not in sys.modules
    finally:
        sys.modules.pop(module_name, None)
        if prior is not None:
            sys.modules[module_name] = prior
