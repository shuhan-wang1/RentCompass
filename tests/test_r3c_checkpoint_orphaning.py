"""The candidate pool must not silently start on an EMPTY checkpoint database.

THE DEFECT (R3-H2). `docker-compose.yml` was changed to derive the fc pool's
`CHECKPOINT_DB_PATH` from the candidate identity::

    /app/.runtime/checkpoints_${CANARY_AGENT_ARCH:-fc_loop}_specialists-${CANARY_MANAGER_V1_SPECIALISTS:-0}.sqlite3

With the real root `.env` — which contains no `CANARY_*` key at all — that
interpolates to `checkpoints_fc_loop_specialists-0.sqlite3`, a DIFFERENT file from
the 78 MB `.runtime/checkpoints_fc.sqlite3` the public pool is writing right now.
The next deploy would have opened an empty database beside it. Two consequences,
neither of which logs anything:

  1. every in-flight graph / HITL resume is lost;
  2. `app/app.py::_delete_checkpoint_thread` deletes only from
     `Config.checkpoint_path`, so the personal graph state in the orphaned file
     becomes permanently unreachable by the account-erasure route while that
     route keeps reporting `deleted`.

Two mechanisms are asserted here. First, the identity that already owns a file
keeps opening it (`_HISTORICAL_CHECKPOINT_NAMES`). Second — because a name is
only a convention and the next rename will not be this one — `agent.persistence`
refuses to CREATE a database when one this identity already owns sits beside it.

Every path in this file is a pytest tmp_path. The live `.runtime/*.sqlite3` files
are never opened, not even read-only.
"""
from __future__ import annotations

import sqlite3

import pytest

from uk_rent_agent import config as config_module
from uk_rent_agent.agent import persistence
from uk_rent_agent.agent.persistence import (
    ALLOW_NEW_DB_ENV,
    OrphanedCheckpointError,
    enforce_runtime_identity,
)

FC_IDENTITY = {"agent_arch": "fc_loop", "manager_v1_specialists": "0"}
MANAGER_IDENTITY = {"agent_arch": "manager_v1", "manager_v1_specialists": "1"}
DERIVED = "checkpoints_fc_loop_specialists-0.sqlite3"
HISTORICAL = "checkpoints_fc.sqlite3"


@pytest.fixture(autouse=True)
def _isolate_process_caches():
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()
    yield
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()


def _database(path, *, identity=None, rows=1):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS payload (value TEXT)")
    for index in range(rows):
        connection.execute("INSERT INTO payload VALUES (?)", (f"row-{index}",))
    connection.commit()
    if identity is not None:
        enforce_runtime_identity(connection, identity, path=path)
    connection.close()
    return path


# --------------------------------------------------------------------------
# 1. the identity that owns a file keeps opening that file
# --------------------------------------------------------------------------
def test_the_compose_derived_name_resolves_to_the_file_that_identity_owns(monkeypatch, tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    _database(runtime / HISTORICAL)
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(runtime / DERIVED))
    monkeypatch.delenv("CHECKPOINT_PATH", raising=False)

    resolved = config_module._resolve_checkpoint_path(tmp_path)

    assert resolved == runtime / HISTORICAL, (
        "the compose-derived name must adopt the database this identity is "
        "already writing, not open an empty one beside it"
    )


def test_a_fresh_host_uses_the_derived_per_identity_name(monkeypatch, tmp_path):
    """With no database to adopt, the compose-derived name is used as written."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(runtime / DERIVED))

    assert config_module._resolve_checkpoint_path(tmp_path) == runtime / DERIVED


def test_a_host_already_running_the_derived_name_keeps_it(monkeypatch, tmp_path):
    """Adopting the historical name there would orphan the derived file instead."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    _database(runtime / DERIVED)
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(runtime / DERIVED))

    assert config_module._resolve_checkpoint_path(tmp_path) == runtime / DERIVED


def test_two_databases_for_one_identity_is_refused_not_silently_resolved(monkeypatch, tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    _database(runtime / DERIVED)
    _database(runtime / HISTORICAL)
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(runtime / DERIVED))

    with pytest.raises(ValueError) as excinfo:
        config_module._resolve_checkpoint_path(tmp_path)

    message = str(excinfo.value)
    assert DERIVED in message and HISTORICAL in message, "both files must be named"
    assert "orphan" in message


def test_an_explicit_operator_path_is_never_rewritten(monkeypatch, tmp_path):
    """The alias keys on the DERIVED filename, never on the identity alone."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    chosen = runtime / "somewhere-else.sqlite3"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(chosen))

    assert config_module._resolve_checkpoint_path(tmp_path) == chosen


def test_other_identities_still_get_their_own_file(monkeypatch, tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    manager = runtime / "checkpoints_manager_v1_specialists-1.sqlite3"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(manager))

    assert config_module._resolve_checkpoint_path(tmp_path) == manager, (
        "the per-identity separation the derivation exists for must survive the alias"
    )


# --------------------------------------------------------------------------
# 2. the startup guard: never CREATE a second database for one identity
# --------------------------------------------------------------------------
def test_creating_a_new_database_beside_this_identitys_own_is_refused(tmp_path):
    _database(tmp_path / HISTORICAL)

    with pytest.raises(OrphanedCheckpointError) as excinfo:
        persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)

    message = str(excinfo.value)
    assert DERIVED in message, "the expected path must be named"
    assert HISTORICAL in message, "the file that was found must be named"
    assert "account-erasure" in message
    assert not (tmp_path / DERIVED).exists(), "and nothing may be created"


def test_a_stamped_database_for_this_identity_under_any_name_is_refused(tmp_path):
    _database(tmp_path / "checkpoints_something_else.sqlite3", identity=FC_IDENTITY)

    with pytest.raises(OrphanedCheckpointError) as excinfo:
        persistence.get_sqlite_checkpointer(
            tmp_path / "checkpoints_new.sqlite3", identity=FC_IDENTITY
        )

    assert "agent_arch=fc_loop" in str(excinfo.value)


def test_another_pools_database_never_blocks_this_pool_from_starting(tmp_path):
    """A shared .runtime holds every pool's file; only OUR identity is a hazard."""
    _database(tmp_path / "checkpoints_manager_v1_specialists-1.sqlite3",
              identity=MANAGER_IDENTITY)

    saver = persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)

    assert saver is None or (tmp_path / DERIVED).exists()


def test_an_unstamped_database_for_another_pool_does_not_block_startup(tmp_path):
    """`.runtime/checkpoints.sqlite3` is the legacy pool's, and is unstamped today."""
    _database(tmp_path / "checkpoints.sqlite3")

    saver = persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)

    assert saver is None or (tmp_path / DERIVED).exists()


def test_an_empty_sibling_is_not_treated_as_data(tmp_path):
    (tmp_path / HISTORICAL).touch()

    saver = persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)

    assert saver is None or (tmp_path / DERIVED).exists()


def test_the_refusal_has_an_explicit_opt_in(tmp_path, monkeypatch, capsys):
    _database(tmp_path / HISTORICAL)
    monkeypatch.setenv(ALLOW_NEW_DB_ENV, "1")

    persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)

    printed = capsys.readouterr().out
    assert "NOT migrated" in printed, "an accepted orphan must still be announced"


def test_reopening_an_existing_database_is_never_guarded(tmp_path):
    """The guard is about CREATION; a pool restarting on its own file is normal."""
    _database(tmp_path / HISTORICAL, identity=FC_IDENTITY)
    _database(tmp_path / DERIVED, identity=FC_IDENTITY)

    persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)


# --------------------------------------------------------------------------
# 3. the alias helper the guard depends on
# --------------------------------------------------------------------------
def test_checkpoint_path_aliases_answers_in_both_directions(tmp_path):
    both = {
        path.name for path in config_module.checkpoint_path_aliases(tmp_path / DERIVED)
    }
    assert both == {DERIVED, HISTORICAL}
    assert {
        path.name for path in config_module.checkpoint_path_aliases(tmp_path / HISTORICAL)
    } == both


def test_an_unrelated_name_has_only_itself(tmp_path):
    aliases = config_module.checkpoint_path_aliases(tmp_path / "checkpoints.sqlite3")
    assert [path.name for path in aliases] == ["checkpoints.sqlite3"]


# --------------------------------------------------------------------------
# 4. the /ready hot path no longer re-verifies through the connection lock
# --------------------------------------------------------------------------
def test_an_identical_identity_is_not_re_verified_against_the_database(tmp_path, monkeypatch):
    """`/ready` calls this per request; it used to take the saver's `_db_lock`."""
    saver = persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)
    if saver is None:
        pytest.skip("langgraph sqlite checkpointer is not installed")

    calls = []
    monkeypatch.setattr(
        persistence,
        "enforce_runtime_identity",
        lambda *a, **k: calls.append(a),
    )
    persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)
    assert calls == [], "an identity already proven on disk needs no round trip"

    # A DIFFERENT identity for the same path still goes to the database, which is
    # where the cross-architecture resume is refused.
    persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=MANAGER_IDENTITY)
    assert calls, "a changed identity must still be verified against the file"


def test_a_cross_identity_reopen_is_still_refused_by_the_stamp(tmp_path):
    saver = persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=FC_IDENTITY)
    if saver is None:
        pytest.skip("langgraph sqlite checkpointer is not installed")

    with pytest.raises(persistence.CheckpointIdentityError):
        persistence.get_sqlite_checkpointer(tmp_path / DERIVED, identity=MANAGER_IDENTITY)
