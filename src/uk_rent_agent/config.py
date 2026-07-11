from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    project_root: Path
    property_source: str = "auto"
    scrape_on_startup: bool = False
    scraper_cache_ttl_hours: float = 24.0
    flask_secret_key: str = ""
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5001", "http://localhost:5001")
    use_mcp_tools: bool = False
    session_max_users: int = 10_000
    session_ttl_seconds: int = 7 * 24 * 3600
    checkpoint_path: Path | None = None
    enable_checkpointer: bool = True
    # Local username/password credential store (JSON, gitignored). See web/auth_store.py.
    auth_db_path: Path | None = None
    # When True, every /api/* route except /api/auth/* requires an authenticated session
    # (401 otherwise). Default False keeps the guest flow working for the local demo.
    require_auth: bool = False

    @property
    def data_dir(self) -> Path:
        return self.project_root / "app" / "data"

    @classmethod
    def from_env(cls, *, require_secret: bool = False) -> "Config":
        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / "app" / ".env", override=False)
        secret = os.getenv("FLASK_SECRET_KEY", "")
        if require_secret and not secret:
            raise RuntimeError("FLASK_SECRET_KEY is required for the production server")
        source = os.getenv("PROPERTY_SOURCE", "auto").strip().lower()
        if source not in {"auto", "csv", "scraper"}:
            raise ValueError("PROPERTY_SOURCE must be auto, csv, or scraper")
        origins = tuple(
            item.strip()
            for item in os.getenv(
                "CORS_ORIGINS", "http://127.0.0.1:5001,http://localhost:5001"
            ).split(",")
            if item.strip()
        )
        return cls(
            project_root=root,
            property_source=source,
            scrape_on_startup=_bool("SCRAPE_ON_STARTUP"),
            scraper_cache_ttl_hours=float(os.getenv("SCRAPER_CACHE_TTL_HOURS", "24")),
            flask_secret_key=secret,
            cors_origins=origins,
            use_mcp_tools=_bool("USE_MCP_TOOLS"),
            session_max_users=int(os.getenv("SESSION_MAX_USERS", "10000")),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 3600))),
            checkpoint_path=Path(
                os.getenv("CHECKPOINT_PATH", str(root / ".runtime" / "checkpoints.sqlite3"))
            ),
            enable_checkpointer=_bool("ENABLE_CHECKPOINTER", True),
            auth_db_path=Path(
                os.getenv("AUTH_DB_PATH", str(root / ".runtime" / "users.json"))
            ),
            require_auth=_bool("REQUIRE_AUTH", False),
        )
