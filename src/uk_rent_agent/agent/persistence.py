from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CHECKPOINTERS: dict[Path, Any] = {}
# The identity each cached checkpointer was last verified against. `/ready` calls
# `get_sqlite_checkpointer` on EVERY request, and re-running the stamp check took
# the saver's `_db_lock` each time — a long checkpoint write could push `/ready`
# past the 10 s `--max-time` update.sh and switch_pool.sh use, turning a busy pool
# into "not ready" and blocking a drain or a restore. An identical identity is
# already proven by the stamp on disk, so it needs no second round trip; a
# DIFFERENT identity still goes to the database and is still refused there.
_VERIFIED: dict[Path, dict[str, str]] = {}
_STORE: Any = None


def thread_id(user_id: str, session_id: str) -> str:
    return f"{user_id}:{session_id}"


def graph_config(user_id: str, session_id: str, *, request_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id(user_id, session_id)}
    if request_id:
        configurable["request_id"] = request_id
    return {"configurable": configurable, "metadata": {"user_id": user_id, "request_id": request_id}}


def get_prefs_store() -> Any | None:
    """Return a process-wide cross-thread Store for durable per-user criteria.

    Unlike the checkpointer (per-thread), the Store is shared across ALL of a user's
    conversations — that is the whole point. Uses an in-memory store here (the durable
    facts are cheap to rebuild from the checkpointer / SQLite memory on restart); swap for
    a Postgres/SQLite-backed BaseStore to survive process restarts. None keeps the optional
    langgraph install importable.
    """
    global _STORE
    with _LOCK:
        if _STORE is not None:
            return _STORE
        try:
            from langgraph.store.memory import InMemoryStore
        except ImportError:
            return None
        _STORE = InMemoryStore()
        return _STORE


# The checkpoint file's own record of which runtime is allowed to own it.  A side
# table (not a PRAGMA/user_version) so the value is self-describing when an operator
# opens the file by hand, and so adding it can never collide with LangGraph's schema.
RUNTIME_IDENTITY_TABLE = "rentcompass_runtime_identity"
_IDENTITY_KEYS = ("agent_arch", "manager_v1_specialists")


class CheckpointIdentityError(RuntimeError):
    """A checkpoint database belongs to a different runtime than this process."""


class OrphanedCheckpointError(RuntimeError):
    """A database this identity already owns sits beside the path being created."""


#: Set to a true value to create the new database anyway. The one legitimate use
#: is a deliberate, documented reset after the old file has been dealt with.
ALLOW_NEW_DB_ENV = "CHECKPOINT_ALLOW_NEW_DB"


def _stamped_identity(path: Path) -> dict[str, str] | None:
    """Read a checkpoint file's own identity stamp WITHOUT writing to it.

    Returns ``None`` when the file carries no stamp (every database written
    before the stamp existed) or cannot be read as one.
    """
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            f"SELECT key, value FROM {RUNTIME_IDENTITY_TABLE}"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    stamped = {str(key): str(value) for key, value in rows}
    return stamped or None


def _guard_unexpected_new_database(resolved: Path, identity: dict[str, str]) -> None:
    """Refuse to silently create a second database for one runtime identity.

    `docker-compose.yml` derives the candidate pool's `CHECKPOINT_DB_PATH` from
    two environment variables. Change either of them — or the derivation itself,
    which is exactly what happened on 2026-08-31 — and the pool opens a NEW, empty
    file beside the one it has been writing. Nothing failed, nothing logged, and
    the account-erasure route (which deletes only from `Config.checkpoint_path`)
    kept reporting success while the personal graph state in the old file became
    unreachable.

    So: when the resolved path does not exist, look for a sibling
    ``checkpoints*.sqlite3`` that holds data and that NO OTHER identity explains —
    either stamped with THIS identity, or unstamped under a name this identity is
    known to use. Refuse, naming expected vs found, unless the operator opts in.
    """
    if resolved.exists():
        return
    parent = resolved.parent
    if not parent.is_dir():
        return
    try:
        from uk_rent_agent.config import checkpoint_path_aliases

        own_names = {alias.name for alias in checkpoint_path_aliases(resolved)}
    except Exception:
        own_names = {resolved.name}
    suspects: list[tuple[Path, str]] = []
    for candidate in sorted(parent.glob("checkpoints*.sqlite3")):
        try:
            if not candidate.is_file() or candidate.stat().st_size == 0:
                continue
        except OSError:
            continue
        stamped = _stamped_identity(candidate)
        if stamped is None:
            # Unattributable. Only a name THIS identity is known to use makes it
            # ours; anything else is another pool's database, which is normal in a
            # shared .runtime directory and must not block this pool from starting.
            if candidate.name in own_names:
                suspects.append((candidate, "unstamped, but named for this runtime"))
            continue
        if all(stamped.get(key) == identity.get(key) for key in _IDENTITY_KEYS):
            suspects.append((candidate, f"stamped [{_format_identity(stamped)}]"))
    if not suspects:
        return
    found = "; ".join(f"{str(path)!r} ({why})" for path, why in suspects)
    if os.getenv(ALLOW_NEW_DB_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        print(
            f"[STARTUP] WARNING: creating a NEW checkpoint database at {str(resolved)!r} "
            f"while this runtime [{_format_identity(identity)}] already has data in "
            f"{found}. {ALLOW_NEW_DB_ENV} is set, so this was asked for; the older "
            "database is NOT migrated and is NOT reachable by account erasure."
        )
        return
    raise OrphanedCheckpointError(
        f"refusing to create a new checkpoint database at {str(resolved)!r}: this "
        f"runtime [{_format_identity(identity)}] already has data in {found}. "
        "Creating a new file here would orphan those checkpoints AND put the "
        "personal graph state in them out of reach of the account-erasure route, "
        "which deletes only from the resolved checkpoint path. Point "
        "CHECKPOINT_DB_PATH at the existing file, or rename the existing file to "
        f"{resolved.name} (with its -wal/-shm) after stopping the pool, or set "
        f"{ALLOW_NEW_DB_ENV}=1 to accept the new empty database deliberately "
        "(docs/canary_runbook.md, 'Checkpoint database names')."
    )


def _format_identity(identity: dict[str, str]) -> str:
    return " ".join(f"{key}={identity.get(key, '<unset>')}" for key in _IDENTITY_KEYS)


def _resolve_identity(identity: dict[str, str] | None) -> dict[str, str]:
    if identity is not None:
        return {key: str(identity.get(key, "")) for key in _IDENTITY_KEYS}
    from uk_rent_agent.config import runtime_checkpoint_identity

    return runtime_checkpoint_identity()


def enforce_runtime_identity(
    connection: sqlite3.Connection,
    identity: dict[str, str],
    *,
    path: Path,
) -> None:
    """Stamp `identity` on the checkpoint file, or refuse a foreign one.

    `docker-compose.yml` gives each pool a differently NAMED checkpoint DB, but a
    name is only a convention: `CHECKPOINT_DB_PATH` can be overridden, the
    `CHECKPOINT_PATH` fallback can win, and the default path is shared.  Any of
    those lets `manager_v1` resume `fc_loop` graph state, whose AgentState channels
    are not compatible.  The file therefore carries its own identity:

      * unstamped (a legacy database) -> stamped on first open, nothing moves;
      * same identity                 -> reopened normally;
      * different identity            -> `CheckpointIdentityError`, naming both
                                         identities and the path;
      * HALF stamped (one key present, and consistent) -> completed in place.

    A half-written stamp is a crash artefact, not evidence of a foreign runtime.
    Treating it as a mismatch made the file permanently unopenable with no path
    back, so the keys that ARE present decide: any of them disagreeing is still a
    refusal; otherwise the stamp is completed.
    """
    missing = [key for key in _IDENTITY_KEYS if not str(identity.get(key, "")).strip()]
    if missing:
        # This is a public entry point. An incomplete dict used to KeyError deep
        # inside, or (via the `""` fill-in) get written to the file as a real
        # identity that nothing could ever match again.
        raise ValueError(
            f"checkpoint identity is incomplete: {sorted(missing)} missing or empty "
            f"in {identity!r}; every key of {list(_IDENTITY_KEYS)} must be a "
            "non-empty string (see uk_rent_agent.config.runtime_checkpoint_identity)."
        )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RUNTIME_IDENTITY_TABLE} ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    rows = connection.execute(
        f"SELECT key, value FROM {RUNTIME_IDENTITY_TABLE}"
    ).fetchall()
    stored = {str(key): str(value) for key, value in rows}
    present = {key: stored[key] for key in _IDENTITY_KEYS if key in stored}
    expected = {key: identity[key] for key in _IDENTITY_KEYS}
    if any(present[key] != expected[key] for key in present):
        raise CheckpointIdentityError(
            "checkpoint database belongs to a different runtime: "
            f"file identity [{_format_identity(present)}] != process identity "
            f"[{_format_identity(identity)}] at {path}. Point CHECKPOINT_DB_PATH at "
            "this runtime's own file (docker-compose.yml derives it from "
            "CANARY_AGENT_ARCH / CANARY_MANAGER_V1_SPECIALISTS); never let one "
            "architecture resume another's LangGraph checkpoints."
        )
    if present != expected:
        # Unstamped, or stamped with a consistent subset: write every key in one
        # transaction so a second interruption leaves the same recoverable state
        # rather than a new one.
        connection.executemany(
            f"INSERT OR REPLACE INTO {RUNTIME_IDENTITY_TABLE} (key, value) VALUES (?, ?)",
            [(key, expected[key]) for key in _IDENTITY_KEYS],
        )
        connection.commit()


def get_sqlite_checkpointer(
    path: Path,
    *,
    identity: dict[str, str] | None = None,
) -> Any | None:
    """Return a process-wide SqliteSaver; None keeps optional installs importable.

    `identity` is the runtime the checkpoints belong to (`Config.checkpoint_identity`).
    When omitted it is derived from the process environment, so an existing caller
    that only knows the path still gets the enforcement rather than opting out of it.
    Raises `CheckpointIdentityError` when the file on disk names a different runtime.
    """
    resolved = Path(path).resolve()
    wanted = _resolve_identity(identity)
    with _LOCK:
        if resolved in _CHECKPOINTERS:
            # Re-verify when the identity DIFFERS from the one already proven for
            # this path: a second caller handing a different identity is precisely
            # the cross-arch resume being refused. An identical identity is already
            # proven by the stamp on disk, so it skips the database round trip —
            # `/ready` runs this on every request and used to take the saver's
            # connection lock each time (see `_VERIFIED`).
            cached = _CHECKPOINTERS[resolved]
            if _VERIFIED.get(resolved) != wanted:
                db_lock = getattr(cached, "_db_lock", None)
                if db_lock is None:
                    enforce_runtime_identity(cached.conn, wanted, path=resolved)
                else:
                    with db_lock:
                        enforce_runtime_identity(cached.conn, wanted, path=resolved)
                _VERIFIED[resolved] = dict(wanted)
            return cached
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:
            return None

        class AsyncCompatibleSqliteSaver(SqliteSaver):
            """Use the locked sync saver in worker threads for LangGraph.ainvoke.

            Unlike AsyncSqliteSaver this connection is not bound to one event loop,
            which matters while the Flask compatibility app is served through ASGI.

            The single sqlite connection (check_same_thread=False) is now shared across
            MANY distinct thread_ids (f"{user_id}:{conversation_id}"), each run inside a
            per-request worker thread via asyncio.to_thread. A raw sqlite3 connection is
            not safe for concurrent access from multiple threads, so every SQL-touching
            operation is serialised through `_db_lock`. Ops are short, so this simple
            connection-wide lock is both correct and cheap.
            """

            def __init__(self, conn):
                super().__init__(conn)
                self._db_lock = threading.Lock()

            # ---- sync ops: serialise all connection access ---------------------
            def get_tuple(self, config):
                with self._db_lock:
                    return super().get_tuple(config)

            def list(self, config, *, filter=None, before=None, limit=None):
                with self._db_lock:
                    # Materialise the generator while holding the lock — the cursor is
                    # live until fully drained.
                    return list(super().list(config, filter=filter, before=before, limit=limit))

            def put(self, config, checkpoint, metadata, new_versions):
                with self._db_lock:
                    return super().put(config, checkpoint, metadata, new_versions)

            def put_writes(self, config, writes, task_id, task_path=""):
                with self._db_lock:
                    return super().put_writes(config, writes, task_id, task_path)

            def delete_thread(self, thread_id):
                with self._db_lock:
                    return super().delete_thread(thread_id)

            # ---- async wrappers: delegate to the now-locked sync ops ------------
            async def aget_tuple(self, config):
                return await asyncio.to_thread(self.get_tuple, config)

            async def alist(self, config, *, filter=None, before=None, limit=None):
                items = await asyncio.to_thread(
                    lambda: list(self.list(config, filter=filter, before=before, limit=limit))
                )
                for item in items:
                    yield item

            async def aput(self, config, checkpoint, metadata, new_versions):
                return await asyncio.to_thread(
                    self.put, config, checkpoint, metadata, new_versions
                )

            async def aput_writes(self, config, writes, task_id, task_path=""):
                await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

            async def adelete_thread(self, thread_id):
                await asyncio.to_thread(self.delete_thread, thread_id)

        resolved.parent.mkdir(parents=True, exist_ok=True)
        _guard_unexpected_new_database(resolved, wanted)
        connection = sqlite3.connect(resolved, check_same_thread=False)
        try:
            enforce_runtime_identity(connection, wanted, path=resolved)
        except BaseException:
            connection.close()
            raise
        saver = AsyncCompatibleSqliteSaver(connection)
        if hasattr(saver, "setup"):
            saver.setup()
        _CHECKPOINTERS[resolved] = saver
        _VERIFIED[resolved] = dict(wanted)
        return saver
