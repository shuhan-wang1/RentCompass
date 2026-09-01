"""F8: per-architecture checkpoint separation must be enforced, not conventional.

`docker-compose.yml` derives the candidate's `CHECKPOINT_DB_PATH` from
`${CANARY_AGENT_ARCH}` / `${CANARY_MANAGER_V1_SPECIALISTS}`, but neither
`uk_rent_agent.config._resolve_checkpoint_path` nor
`uk_rent_agent.agent.persistence.get_sqlite_checkpointer` knew anything about
the architecture: any override, the `CHECKPOINT_PATH` fallback, or the shared
default let `manager_v1` resume `fc_loop`'s LangGraph state, whose AgentState
channels are not compatible.

The file now carries its own identity, so the separation survives a wrong path.
Nothing is moved: an unstamped legacy database is stamped on first open.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from uk_rent_agent.agent import persistence
from uk_rent_agent.agent.persistence import (
    RUNTIME_IDENTITY_TABLE,
    CheckpointIdentityError,
    get_sqlite_checkpointer,
)
from uk_rent_agent.config import Config, runtime_checkpoint_identity


FC = {"agent_arch": "fc_loop", "manager_v1_specialists": "0"}
MANAGER = {"agent_arch": "manager_v1", "manager_v1_specialists": "1"}
LEGACY = {"agent_arch": "legacy", "manager_v1_specialists": "0"}


@pytest.fixture(autouse=True)
def _isolated_checkpointer_cache():
    """The saver cache is process-wide; keep each case independent."""
    saved = dict(persistence._CHECKPOINTERS)
    persistence._CHECKPOINTERS.clear()
    try:
        yield
    finally:
        persistence._CHECKPOINTERS.clear()
        persistence._CHECKPOINTERS.update(saved)


def _stamp(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            f"SELECT key, value FROM {RUNTIME_IDENTITY_TABLE}"
        ).fetchall()
    return {str(key): str(value) for key, value in rows}


def test_the_same_runtime_reopens_its_own_database(tmp_path):
    path = tmp_path / "checkpoints_manager_v1_specialists-1.sqlite3"
    first = get_sqlite_checkpointer(path, identity=MANAGER)
    assert first is not None
    assert _stamp(path) == MANAGER
    persistence._CHECKPOINTERS.clear()
    assert get_sqlite_checkpointer(path, identity=MANAGER) is not None
    assert _stamp(path) == MANAGER


def test_a_foreign_runtime_is_refused_and_the_error_names_both_identities(tmp_path):
    path = tmp_path / "checkpoints.sqlite3"
    get_sqlite_checkpointer(path, identity=FC)
    persistence._CHECKPOINTERS.clear()

    with pytest.raises(CheckpointIdentityError) as excinfo:
        get_sqlite_checkpointer(path, identity=MANAGER)
    message = str(excinfo.value)
    assert "agent_arch=fc_loop" in message          # what the file says
    assert "agent_arch=manager_v1" in message       # what this process is
    assert str(path) in message                     # and where
    assert "CHECKPOINT_DB_PATH" in message          # and the knob that fixes it
    # The refusal must not have rewritten the stamp on its way out.
    assert _stamp(path) == FC


def test_the_specialist_bit_alone_separates_two_manager_v1_runtimes(tmp_path):
    """manager_v1 with specialists off and on are different graphs, not one."""
    path = tmp_path / "checkpoints.sqlite3"
    get_sqlite_checkpointer(path, identity={"agent_arch": "manager_v1",
                                            "manager_v1_specialists": "0"})
    persistence._CHECKPOINTERS.clear()
    with pytest.raises(CheckpointIdentityError):
        get_sqlite_checkpointer(path, identity=MANAGER)


def test_an_unstamped_legacy_database_is_stamped_in_place(tmp_path):
    """No migration, no move: the file production already has keeps its rows."""
    path = tmp_path / "checkpoints.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, blob BLOB)")
        conn.execute("INSERT INTO checkpoints VALUES ('u:c', x'00')")
    before = sqlite3.connect(path).execute("SELECT COUNT(*) FROM checkpoints").fetchone()

    assert get_sqlite_checkpointer(path, identity=LEGACY) is not None

    assert _stamp(path) == LEGACY
    after = sqlite3.connect(path).execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    assert after == before


def test_a_cached_saver_is_re_verified_rather_than_handed_over(tmp_path):
    """Two callers, one path, different identities — the cache must not launder it."""
    path = tmp_path / "checkpoints.sqlite3"
    assert get_sqlite_checkpointer(path, identity=FC) is not None
    with pytest.raises(CheckpointIdentityError):
        get_sqlite_checkpointer(path, identity=MANAGER)


def test_the_identity_comes_from_config_and_is_architecture_bound():
    manager = Config(project_root=Path("/tmp"), agent_arch="manager_v1",
                     manager_v1_specialists=True)
    assert manager.checkpoint_identity == MANAGER
    # Specialists requested on a pool that is not manager_v1 stay off, exactly as
    # `manager_v1_specialists_effective` reports them.
    fc = Config(project_root=Path("/tmp"), agent_arch="fc_loop",
                manager_v1_specialists=True)
    assert fc.checkpoint_identity == FC


def test_the_env_fallback_agrees_with_config_for_the_same_environment(monkeypatch):
    """A caller that only knows the path still gets enforcement, not an opt-out."""
    for arch, requested in (("manager_v1", "1"), ("manager_v1", "0"),
                            ("fc_loop", "1"), ("legacy", "yes")):
        monkeypatch.setenv("AGENT_ARCH", arch)
        monkeypatch.setenv("MANAGER_V1_SPECIALISTS", requested)
        monkeypatch.setenv("USE_MCP_TOOLS", "0")
        monkeypatch.setenv("APP_PROJECT_ROOT", "/tmp")
        assert runtime_checkpoint_identity() == Config.from_env().checkpoint_identity


def test_a_half_written_stamp_is_completed_rather_than_bricking_the_file(tmp_path):
    """A stamp interrupted between its two keys left `present` with one entry, which
    could never equal the two-key identity — so the file raised
    CheckpointIdentityError forever, and the `if not present` re-stamp could not
    reach it either. There was no path back for a database full of live state."""
    path = tmp_path / "checkpoints.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"CREATE TABLE {RUNTIME_IDENTITY_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            f"INSERT INTO {RUNTIME_IDENTITY_TABLE} VALUES ('agent_arch', 'manager_v1')"
        )

    assert get_sqlite_checkpointer(path, identity=MANAGER) is not None
    assert _stamp(path) == MANAGER


def test_a_half_written_stamp_still_refuses_a_runtime_it_contradicts(tmp_path):
    """Self-healing must not become laundering: the keys that ARE present decide."""
    path = tmp_path / "checkpoints.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"CREATE TABLE {RUNTIME_IDENTITY_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            f"INSERT INTO {RUNTIME_IDENTITY_TABLE} VALUES ('agent_arch', 'fc_loop')"
        )

    with pytest.raises(CheckpointIdentityError):
        get_sqlite_checkpointer(path, identity=MANAGER)
    assert _stamp(path) == {"agent_arch": "fc_loop"}   # not rewritten on the way out


def test_an_incomplete_identity_is_refused_instead_of_being_stamped(tmp_path):
    """`enforce_runtime_identity` is public API. A caller passing a partial dict got
    a bare KeyError — or, through the `""` fill-in, wrote an empty identity into the
    file that no real runtime could ever match again."""
    path = tmp_path / "checkpoints.sqlite3"
    with pytest.raises(ValueError, match="incomplete"):
        get_sqlite_checkpointer(path, identity={"agent_arch": "manager_v1"})
    with pytest.raises(ValueError, match="incomplete"):
        get_sqlite_checkpointer(path, identity={"agent_arch": "manager_v1",
                                                "manager_v1_specialists": ""})
    # Nothing was stamped, so the file is still adoptable by its real runtime.
    assert get_sqlite_checkpointer(path, identity=MANAGER) is not None
    assert _stamp(path) == MANAGER


def test_an_unresolved_identity_never_opens_the_wrong_file(tmp_path):
    """The refusal happens before any saver is cached, so a retry re-checks."""
    path = tmp_path / "checkpoints.sqlite3"
    get_sqlite_checkpointer(path, identity=FC)
    persistence._CHECKPOINTERS.clear()
    with pytest.raises(CheckpointIdentityError):
        get_sqlite_checkpointer(path, identity=MANAGER)
    assert persistence._CHECKPOINTERS == {}
