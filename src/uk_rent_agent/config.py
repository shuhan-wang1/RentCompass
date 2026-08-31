from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from uk_rent_agent.agent.architecture import (
    SUPPORTED_AGENT_ARCHES,
    manager_v1_specialists_enabled,
)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _choice(name: str, default: str, allowed: set[str]) -> str:
    """Read a normalized finite-choice environment value or fail at startup."""
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


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
    # Agent/model identity is parsed once and passed through the ASGI -> Flask bootstrap.
    # Keeping it in Config prevents readiness from validating one environment snapshot while
    # the lazily-built graph reads a differently-normalized value later.
    agent_arch: str = "legacy"
    # Phase-2 manager specialist dispatch. The requested value is retained for
    # diagnostics; ``manager_v1_specialists_effective`` also binds it to the
    # manager_v1 architecture so another pool cannot activate it accidentally.
    manager_v1_specialists: bool = False
    deepseek_strict: bool = False
    llm_provider: str = "deepseek"
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
    # Off by default — the durable SQLite AgentMemory remains the long-term memory of record.
    enable_store: bool = False
    # Local username/password credential store (SQLite, gitignored). See web/auth_store.py.
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
    rate_limit_db_path: Path | None = None
    # Cross-process single-flight lease for one conversation turn. A crashed worker's
    # lease is reclaimed after this interval; normal FC turns are expected to finish
    # well below the default fifteen minutes.
    turn_lease_seconds: int = 15 * 60

    def __post_init__(self) -> None:
        # Phase 2 deliberately grants specialists only the trusted, in-process
        # ToolRegistry. MCP is a wider execution boundary and must fail closed.
        if self.manager_v1_specialists_effective and self.use_mcp_tools:
            raise ValueError(
                "MANAGER_V1_SPECIALISTS requires USE_MCP_TOOLS=0 "
                "(trusted in-process ToolRegistry only)"
            )

    @property
    def data_dir(self) -> Path:
        return self.project_root / "app" / "data"

    @property
    def manager_v1_specialists_effective(self) -> bool:
        """Whether specialist execution is enabled for this exact runtime."""
        return manager_v1_specialists_enabled(
            self.agent_arch,
            self.manager_v1_specialists,
        )

    @classmethod
    def from_env(cls, *, require_secret: bool = False) -> "Config":
        root_override = os.getenv("APP_PROJECT_ROOT", "").strip()
        root = (
            Path(root_override).expanduser().resolve()
            if root_override
            else Path(__file__).resolve().parents[2]
        )
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
            agent_arch=_choice("AGENT_ARCH", "legacy", set(SUPPORTED_AGENT_ARCHES)),
            manager_v1_specialists=_bool("MANAGER_V1_SPECIALISTS", False),
            deepseek_strict=_bool("DEEPSEEK_STRICT", False),
            llm_provider=_choice("LLM_PROVIDER", "deepseek", {"deepseek", "ollama"}),
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
                os.getenv("AUTH_DB_PATH", str(root / ".runtime" / "auth.sqlite3"))
            ),
            require_auth=_bool("REQUIRE_AUTH", False),
            session_cookie_secure=_bool("SESSION_COOKIE_SECURE", False),
            allow_legacy_client_user_id=_bool("ALLOW_LEGACY_CLIENT_USER_ID", False),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(256 * 1024))),
            rate_limit_window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
            rate_limit_db_path=Path(
                os.getenv(
                    "RATE_LIMIT_DB_PATH",
                    str(root / ".runtime" / "rate_limits.sqlite3"),
                )
            ),
            turn_lease_seconds=max(1, int(os.getenv("TURN_LEASE_SECONDS", str(15 * 60)))),
        )
