from __future__ import annotations

import sys

import pytest

from uk_rent_agent.config import Config
from uk_rent_agent.web.app import _install_runtime_environment, create_app


def test_agent_switches_are_stripped_normalized_and_carried_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "  FC_LOOP  ")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "0")
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


def test_manager_v1_is_normalized_and_published_but_not_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "  MANAGER_V1  ")
    monkeypatch.delenv("MANAGER_V1_SPECIALISTS", raising=False)

    config = Config.from_env()

    assert config.agent_arch == "manager_v1"
    assert config.manager_v1_specialists is False
    assert config.manager_v1_specialists_effective is False
    _install_runtime_environment(config)
    assert __import__("os").environ["AGENT_ARCH"] == "manager_v1"
    assert __import__("os").environ["MANAGER_V1_SPECIALISTS"] == "0"


def test_manager_v1_specialists_are_parsed_once_and_published_effectively(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", " YeS ")
    monkeypatch.setenv("USE_MCP_TOOLS", "0")

    config = Config.from_env()

    assert config.manager_v1_specialists is True
    assert config.manager_v1_specialists_effective is True
    # A later ambient change cannot alter the immutable Config snapshot.
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "0")
    assert config.manager_v1_specialists_effective is True
    _install_runtime_environment(config)
    assert __import__("os").environ["MANAGER_V1_SPECIALISTS"] == "1"


@pytest.mark.parametrize("arch", ["legacy", "fc_loop"])
def test_specialist_switch_is_forced_off_outside_manager_v1(monkeypatch, tmp_path, arch):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", arch)
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")

    config = Config.from_env()

    assert config.manager_v1_specialists is True
    assert config.manager_v1_specialists_effective is False
    _install_runtime_environment(config)
    assert __import__("os").environ["MANAGER_V1_SPECIALISTS"] == "0"


def test_manager_specialists_reject_mcp_execution_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "manager_v1")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "1")
    monkeypatch.setenv("USE_MCP_TOOLS", "1")

    with pytest.raises(ValueError, match="trusted in-process ToolRegistry only"):
        Config.from_env()


def test_agent_arch_default_remains_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_ARCH", raising=False)

    assert Config.from_env().agent_arch == "legacy"


@pytest.mark.parametrize(
    ("name", "value"),
    [("AGENT_ARCH", "fc"), ("LLM_PROVIDER", "some-cloud")],
)
def test_invalid_finite_runtime_switches_fail_at_startup(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("APP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "0")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Config.from_env()


def test_flask_bootstrap_receives_the_exact_config_object(monkeypatch, tmp_path):
    # Register every process-wide switch with monkeypatch so create_app's intentional env
    # publication is restored at teardown and cannot leak into unrelated import-order tests.
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "0")
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
    # create_app puts <project_root> and <project_root>/app at sys.path[0]. Leaving
    # this tmp tree there makes a LATER `import app` in another test file resolve to
    # THIS stub instead of the repo's app/app.py, which then fails on the
    # _BOOTSTRAP_CONFIG the real loader would have injected. Restore sys.path so the
    # stub cannot escape this test.
    path_before = list(sys.path)
    try:
        flask_app = create_app(config)
        loaded = sys.modules[module_name]

        assert loaded._runtime_config is config
        assert flask_app.config["RUNTIME_CONFIG"] is config
        assert flask_app.config["SESSION_STORE"] is loaded._session_store

        before = {
            name: __import__("os").environ[name]
            for name in (
                "AGENT_ARCH",
                "MANAGER_V1_SPECIALISTS",
                "DEEPSEEK_STRICT",
                "LLM_PROVIDER",
                "USE_MCP_TOOLS",
            )
        }
        with pytest.raises(RuntimeError, match="different Config"):
            create_app(Config(project_root=tmp_path))
        assert {
            name: __import__("os").environ[name]
            for name in before
        } == before
    finally:
        sys.path[:] = path_before
        sys.modules.pop(module_name, None)
        if prior is not None:
            sys.modules[module_name] = prior


def test_failed_flask_bootstrap_does_not_cache_partial_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ARCH", "legacy")
    monkeypatch.setenv("MANAGER_V1_SPECIALISTS", "0")
    monkeypatch.setenv("DEEPSEEK_STRICT", "0")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("USE_MCP_TOOLS", "0")
    legacy_dir = tmp_path / "app"
    legacy_dir.mkdir()
    (legacy_dir / "app.py").write_text("raise RuntimeError('injected boot failure')\n")
    module_name = "uk_rent_agent._legacy_web_app"
    prior = sys.modules.pop(module_name, None)
    # create_app inserts <project_root> and <project_root>/app at sys.path[0] BEFORE
    # it execs the module, and a failed bootstrap leaves them there. This tmp tree
    # contains an `app/app.py` that does nothing but raise, so leaking it onto
    # sys.path makes a LATER, unrelated `import app` in another test file explode
    # with "injected boot failure". Snapshot and restore sys.path so the failure this
    # test injects stays inside this test.
    path_before = list(sys.path)
    try:
        with pytest.raises(RuntimeError, match="injected boot failure"):
            create_app(Config(project_root=tmp_path))
        assert module_name not in sys.modules
    finally:
        sys.path[:] = path_before
        sys.modules.pop(module_name, None)
        if prior is not None:
            sys.modules[module_name] = prior
