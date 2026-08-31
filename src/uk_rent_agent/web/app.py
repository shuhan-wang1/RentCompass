from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from flask import Flask

from uk_rent_agent.config import Config


def _install_runtime_environment(config: Config) -> None:
    """Normalize the few process-wide switches consumed by legacy module imports."""
    os.environ["AGENT_ARCH"] = config.agent_arch
    os.environ["MANAGER_V1_SPECIALISTS"] = (
        "1" if config.manager_v1_specialists_effective else "0"
    )
    os.environ["DEEPSEEK_STRICT"] = "1" if config.deepseek_strict else "0"
    os.environ["LLM_PROVIDER"] = config.llm_provider
    os.environ["USE_MCP_TOOLS"] = "1" if config.use_mcp_tools else "0"
    if config.flask_secret_key:
        os.environ["FLASK_SECRET_KEY"] = config.flask_secret_key


def create_app(config: Config | None = None) -> Flask:
    """Create the web application while the legacy routes are migrated incrementally."""
    config = config or Config.from_env()
    module_name = "uk_rent_agent._legacy_web_app"
    module = sys.modules.get(module_name)
    if module is not None and getattr(module, "_runtime_config", None) != config:
        # Check before publishing the new process switches: a rejected second factory call must
        # not poison the already-running graph's environment.
        raise RuntimeError("legacy Flask runtime is already loaded with a different Config")
    _install_runtime_environment(config)
    legacy_dir = config.project_root / "app"
    legacy_path = legacy_dir / "app.py"
    if not legacy_path.exists():
        raise RuntimeError(f"Legacy route module not found: {legacy_path}")
    for path in (config.project_root, legacy_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, legacy_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {legacy_path}")
        module = importlib.util.module_from_spec(spec)
        # app/app.py consumes this exact object instead of independently rebuilding Config from
        # ambient environment.  Set it before exec_module because stores are created at import.
        module._BOOTSTRAP_CONFIG = config
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            # Never cache a half-imported runtime: a corrected configuration/restart must be able
            # to attempt a clean bootstrap instead of reusing an object with missing stores/app.
            if sys.modules.get(module_name) is module:
                sys.modules.pop(module_name, None)
            raise
    app = module.app
    app.debug = False
    app.config["SESSION_STORE"] = module._session_store
    app.config["RUNTIME_CONFIG"] = config
    return app
