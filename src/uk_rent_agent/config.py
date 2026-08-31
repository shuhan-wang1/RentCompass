from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from uk_rent_agent.agent.architecture import (
    SUPPORTED_AGENT_ARCHES,
    manager_v1_specialists_enabled,
    normalize_agent_arch,
)

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", ""})


def _bool_token(value: str | None) -> str | None:
    """Normalize one boolean spelling to the canonical ``"0"``/``"1"`` token.

    Returns ``None`` for a value that is neither a true nor a false spelling, so
    callers can fail closed instead of inventing a third interpretation.
    """
    lowered = str(value or "").strip().lower()
    if lowered in _TRUE_TOKENS:
        return "1"
    if lowered in _FALSE_TOKENS:
        return "0"
    return None


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_TOKENS


def _choice(name: str, default: str, allowed: set[str]) -> str:
    """Read a normalized finite-choice environment value or fail at startup."""
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


# `docker-compose.yml` builds the candidate checkpoint path by interpolating the
# RAW root-.env value:
#
#   CHECKPOINT_DB_PATH: "/app/.runtime/checkpoints_${CANARY_AGENT_ARCH:-fc_loop}"
#                       "_specialists-${CANARY_MANAGER_V1_SPECIALISTS:-0}.sqlite3"
#
# Compose interpolation cannot normalize, so an operator writing `true` instead of
# `1` silently produces a THIRD database (`…_specialists-true.sqlite3`) that shares
# no state with the `…_specialists-1.sqlite3` the same pool used yesterday. The
# token is normalized back to `0`/`1` here so every boolean spelling of the same
# runtime resolves to the same file, and an unrecognised spelling fails closed.
_SPECIALIST_TOKEN = re.compile(
    r"(?P<prefix>_specialists-)(?P<value>[^/]*?)(?P<suffix>\.sqlite3)\Z"
)


def _normalize_specialist_token(path: str) -> str:
    match = _SPECIALIST_TOKEN.search(path)
    if match is None:
        return path
    canonical = _bool_token(match.group("value"))
    if canonical is None:
        raise ValueError(
            "CHECKPOINT_DB_PATH names a specialist mode that is neither true nor "
            f"false: {match.group('value')!r} in {path!r}. Set "
            "CANARY_MANAGER_V1_SPECIALISTS to 0 or 1."
        )
    if canonical == match.group("value"):
        return path
    normalized = (
        path[: match.start()]
        + match.group("prefix")
        + canonical
        + match.group("suffix")
    )
    # Normalizing is the right answer for a path that does not exist yet: every
    # boolean spelling of one runtime then lands on one file. It is the WRONG
    # answer for a host that has already been running with the non-canonical
    # spelling, because "use a different file" silently ORPHANS that pool's live
    # checkpoints — a data loss dressed up as a warning, and one nothing here
    # migrates. So an existing database keeps its own path; only the name it would
    # have had is reported, with the one-line rename that adopts it.
    if Path(path).exists():
        print(
            "[STARTUP] WARNING: CHECKPOINT_DB_PATH specialist token "
            f"{match.group('value')!r} is a non-canonical spelling of {canonical!r}, "
            f"but {path!r} already exists and holds this pool's checkpoints, so it is "
            f"used AS IS (nothing is migrated). To adopt the canonical name, stop the "
            f"pool and `mv {path} {normalized}`, then set "
            "CANARY_MANAGER_V1_SPECIALISTS to 0 or 1."
        )
        return path
    print(
        "[STARTUP] WARNING: CHECKPOINT_DB_PATH specialist token "
        f"{match.group('value')!r} normalized to {canonical!r}; using "
        f"{normalized!r} so a non-canonical boolean spelling cannot fork a third "
        "checkpoint database. (No database exists at the non-canonical path, so "
        "nothing is orphaned.)"
    )
    return normalized


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
    return Path(_normalize_specialist_token(chosen))


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

    @property
    def checkpoint_identity(self) -> dict[str, str]:
        """The runtime identity a checkpoint database is allowed to belong to.

        `docker-compose.yml` gives each pool its own `CHECKPOINT_DB_PATH`, but that
        separation is a naming convention: any override, fallback or typo lets one
        architecture resume another's LangGraph state. `persistence.get_sqlite_checkpointer`
        stamps this pair into the SQLite file and refuses a file that already carries
        a different one, so the isolation is enforced by the database itself.
        """
        return {
            "agent_arch": normalize_agent_arch(self.agent_arch),
            "manager_v1_specialists": "1" if self.manager_v1_specialists_effective else "0",
        }

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


def runtime_checkpoint_identity() -> dict[str, str]:
    """Checkpoint identity for the current process environment.

    Used only when a caller of `get_sqlite_checkpointer` cannot hand over its own
    `Config`. It reads the same two switches `Config.from_env` reads and applies the
    same architecture binding, so the ambient answer can never disagree with the
    explicit one for the same environment.
    """
    arch = normalize_agent_arch(os.getenv("AGENT_ARCH", "legacy"))
    requested = _bool_token(os.getenv("MANAGER_V1_SPECIALISTS", "0")) == "1"
    return {
        "agent_arch": arch,
        "manager_v1_specialists": (
            "1" if manager_v1_specialists_enabled(arch, requested) else "0"
        ),
    }
