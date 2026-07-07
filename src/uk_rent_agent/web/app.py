from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from flask import Flask

from uk_rent_agent.config import Config


def create_app(config: Config | None = None) -> Flask:
    """Create the web application while the legacy routes are migrated incrementally."""
    config = config or Config.from_env()
    if config.flask_secret_key:
        os.environ["FLASK_SECRET_KEY"] = config.flask_secret_key
    os.environ["USE_MCP_TOOLS"] = "1" if config.use_mcp_tools else "0"
    legacy_dir = config.project_root / "local_data_demo"
    legacy_path = legacy_dir / "app.py"
    if not legacy_path.exists():
        raise RuntimeError(f"Legacy route module not found: {legacy_path}")
    for path in (config.project_root, legacy_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    module_name = "uk_rent_agent._legacy_web_app"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, legacy_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {legacy_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    app = module.app
    app.debug = False
    app.config["SESSION_STORE"] = module._session_store
    return app
