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


def _resolve_checkpoint_path(root: Path) -> Path:
    """Resolve the LangGraph checkpoint DB path for this process.

    Canary rollout (2026-07-20): the legacy and fc pools MUST use SEPARATE checkpoint DBs
    (divergent AgentState channels — a cross-arch resume corrupts the run). Ops points each
    pool at its own file via the documented `CHECKPOINT_DB_PATH` env var.

    Precedence:
      1. `CHECKPOINT_DB_PATH`  — the documented primary ops interface.
      2. `CHECKPOINT_PATH`     — legacy/back-compat fallback.
      3. `<root>/.runtime/checkpoints.sqlite3` — default.

    If BOTH env vars are set and DIFFER, `CHECKPOINT_DB_PATH` wins and a one-line warning is
    printed so the ops mistake is visible in startup logs (rather than silently no-op'ing).
    """
    db_path = os.getenv("CHECKPOINT_DB_PATH")
    legacy_path = os.getenv("CHECKPOINT_PATH")
    if db_path and legacy_path and db_path.strip() != legacy_path.strip():
        print(
            "[STARTUP] WARNING: both CHECKPOINT_DB_PATH and CHECKPOINT_PATH are set and "
            f"differ; using CHECKPOINT_DB_PATH ({db_path!r}), ignoring CHECKPOINT_PATH "
            f"({legacy_path!r})."
        )
    chosen = db_path or legacy_path or str(root / ".runtime" / "checkpoints.sqlite3")
    return Path(chosen)


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
    # HITL: pause before the expensive multi-search fan-out for human approval. Requires a
    # checkpointer. Off by default — the graph runs end-to-end without pausing.
    enable_hitl: bool = False
    # Cross-thread Store: persist the user's durable structured criteria across conversations.
    # Off by default — the existing Chroma AgentMemory remains the long-term memory of record.
    enable_store: bool = False
    # Local username/password credential store (JSON, gitignored). See web/auth_store.py.
    auth_db_path: Path | None = None
    # When True, every /api/* route except /api/auth/* requires an authenticated session
    # (401 otherwise). Default False keeps the guest flow working for the local demo.
    require_auth: bool = False
    session_cookie_secure: bool = False
    # Client-provided user IDs are not an authorization mechanism. Keep this opt-in
    # only for controlled legacy migrations; guest identities otherwise live in the
    # signed session cookie minted by the server.
    allow_legacy_client_user_id: bool = False
    max_request_bytes: int = 256 * 1024
    rate_limit_window_seconds: int = 60

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
            checkpoint_path=_resolve_checkpoint_path(root),
            enable_checkpointer=_bool("ENABLE_CHECKPOINTER", True),
            enable_hitl=_bool("ENABLE_HITL", False),
            enable_store=_bool("ENABLE_STORE", False),
            auth_db_path=Path(
                os.getenv("AUTH_DB_PATH", str(root / ".runtime" / "users.json"))
            ),
            require_auth=_bool("REQUIRE_AUTH", False),
            session_cookie_secure=_bool("SESSION_COOKIE_SECURE", False),
            allow_legacy_client_user_id=_bool("ALLOW_LEGACY_CLIENT_USER_ID", False),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(256 * 1024))),
            rate_limit_window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
        )
