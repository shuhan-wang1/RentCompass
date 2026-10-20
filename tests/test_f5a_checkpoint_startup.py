"""Checkpoint identity must be checked at STARTUP, not on the first user turn (T3 中-3).

`enforce_runtime_identity` is real and it works — but it only ran when the
checkpointer was first OPENED, and that happens when a turn needs it. A pool pointed
at another architecture's database therefore booted cleanly, answered `/live` 200,
and failed only once real traffic arrived.

Two statements are pinned here:

  1. `verify_checkpoint_identity` (called by `create_asgi_app` before the first
     request can be served) raises on both misconfiguration classes, so a pool that
     cannot serve a turn crash-loops instead of looking alive.
  2. `_check_checkpoint` -> `/ready` reports a non-200 with the FULL error text for
     both of them. (The `CheckpointIdentityError` half of this already held on
     `1df0d4f` — T3's own `E6a.log` records `GET /ready -> 503` — so this half is a
     regression pin, not a new fix. What did not hold was 1.)

Everything runs against a tmp database; `.runtime/*.sqlite3` is never touched.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from uk_rent_agent.agent import persistence
from uk_rent_agent.agent.persistence import (
    CheckpointIdentityError,
    OrphanedCheckpointError,
    get_sqlite_checkpointer,
)
from uk_rent_agent.config import Config
from uk_rent_agent.web import asgi

FC = {"agent_arch": "fc_loop", "manager_v1_specialists": "0"}
MANAGER = {"agent_arch": "manager_v1", "manager_v1_specialists": "1"}


@pytest.fixture(autouse=True)
def _isolated_checkpointer_cache():
    """The saver cache is process-wide; keep each case independent."""
    saved = dict(persistence._CHECKPOINTERS)
    saved_verified = dict(persistence._VERIFIED)
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()
    try:
        yield
    finally:
        persistence._CHECKPOINTERS.clear()
        persistence._VERIFIED.clear()
        persistence._CHECKPOINTERS.update(saved)
        persistence._VERIFIED.update(saved_verified)


def _manager_config(path: Path) -> Config:
    """A manager_v1:1 runtime pointed at ``path``, built without touching env."""
    return replace(
        Config(project_root=path.parent),
        agent_arch="manager_v1",
        manager_v1_specialists=True,
        checkpoint_path=path,
        enable_checkpointer=True,
    )


def _fc_stamped_db(tmp_path: Path) -> Path:
    """A real checkpoint database stamped `fc_loop:0`, exactly T3's E6a setup."""
    path = tmp_path / "checkpoints_fc.sqlite3"
    assert get_sqlite_checkpointer(path, identity=FC) is not None
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()
    return path


# --------------------------------------------------------------- eager startup

def test_startup_refuses_a_foreign_identity_database(tmp_path):
    config = _manager_config(_fc_stamped_db(tmp_path))
    assert config.checkpoint_identity == MANAGER

    with pytest.raises(CheckpointIdentityError) as excinfo:
        asgi.verify_checkpoint_identity(config)

    message = str(excinfo.value)
    assert "fc_loop" in message and "manager_v1" in message
    assert "CHECKPOINT_DB_PATH" in message  # the remediation, not just the diagnosis


def test_startup_refuses_to_orphan_a_database_this_identity_already_owns(tmp_path, monkeypatch):
    monkeypatch.delenv(persistence.ALLOW_NEW_DB_ENV, raising=False)
    sibling = tmp_path / "checkpoints_fc.sqlite3"
    assert get_sqlite_checkpointer(sibling, identity=MANAGER) is not None
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()

    config = _manager_config(tmp_path / "checkpoints_manager_v1_specialists-1.sqlite3")
    with pytest.raises(OrphanedCheckpointError) as excinfo:
        asgi.verify_checkpoint_identity(config)
    assert persistence.ALLOW_NEW_DB_ENV in str(excinfo.value)


def test_startup_accepts_this_runtimes_own_database(tmp_path):
    path = tmp_path / "checkpoints_manager_v1_specialists-1.sqlite3"
    config = _manager_config(path)
    asgi.verify_checkpoint_identity(config)          # creates + stamps
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()
    asgi.verify_checkpoint_identity(config)          # reopens, still fine


def test_startup_is_a_no_op_when_the_checkpointer_is_disabled(tmp_path):
    config = replace(_manager_config(_fc_stamped_db(tmp_path)), enable_checkpointer=False)
    asgi.verify_checkpoint_identity(config)  # must not raise


def test_a_non_identity_failure_never_stops_startup(tmp_path, monkeypatch):
    """Only the two misconfiguration classes are fatal; /ready reports the rest."""
    def _boom(*a, **k):
        raise RuntimeError("sqlite checkpoint backend unavailable")

    monkeypatch.setattr(persistence, "get_sqlite_checkpointer", _boom)
    asgi.verify_checkpoint_identity(_manager_config(tmp_path / "cp.sqlite3"))


# ------------------------------------------------------------------- /ready

def test_ready_fails_with_the_full_identity_error(tmp_path):
    config = _manager_config(_fc_stamped_db(tmp_path))

    check = asgi._check_checkpoint(config)

    assert check["status"] == "fail"
    assert check["required"] is True
    assert check["identity"] == MANAGER
    assert "CheckpointIdentityError" in check["detail"]
    assert "fc_loop" in check["detail"] and "manager_v1" in check["detail"]
    # The remediation survives the length cap.
    assert "CHECKPOINT_DB_PATH" in check["detail"]


def test_ready_fails_with_the_full_orphan_error(tmp_path, monkeypatch):
    monkeypatch.delenv(persistence.ALLOW_NEW_DB_ENV, raising=False)
    sibling = tmp_path / "checkpoints_fc.sqlite3"
    assert get_sqlite_checkpointer(sibling, identity=MANAGER) is not None
    persistence._CHECKPOINTERS.clear()
    persistence._VERIFIED.clear()

    config = _manager_config(tmp_path / "checkpoints_manager_v1_specialists-1.sqlite3")
    check = asgi._check_checkpoint(config)

    assert check["status"] == "fail"
    assert "OrphanedCheckpointError" in check["detail"]
    # The remediation tail must survive the length cap. Asserted against the FULL
    # exception text rather than a fixed character budget: this message embeds two
    # ABSOLUTE paths, so a cap that is generous on one host truncates on another
    # (a long pytest --basetemp is enough to do it), and a test that only checks
    # "under N chars" passes on the machine it was written on and nowhere else.
    with pytest.raises(persistence.OrphanedCheckpointError) as excinfo:
        get_sqlite_checkpointer(config.checkpoint_path, identity=MANAGER)
    full = str(excinfo.value)
    assert full.endswith(check["detail"].split(": ", 1)[1]) or check["detail"].endswith(
        full[-80:]), "the remediation tail was truncated away"
    assert persistence.ALLOW_NEW_DB_ENV in check["detail"]
    assert "CHECKPOINT_DB_PATH" in check["detail"]


def test_a_failed_checkpoint_check_makes_readiness_non_200(tmp_path, monkeypatch):
    config = _manager_config(_fc_stamped_db(tmp_path))
    # Everything else stubbed to "ok"/"disabled": the assertion is that the
    # checkpoint verdict ALONE is enough to hold the deploy gate.
    for name in ("_check_release_metadata", "_check_runtime_configuration",
                 "_check_tool_registry", "_check_agent_graph", "_check_conversation_db",
                 "_check_agent_memory", "_check_background_jobs", "_check_canary_sink",
                 "_check_auth_store", "_check_llm_configuration", "_check_rag",
                 "_check_searx", "_check_onthemarket"):
        monkeypatch.setattr(asgi, name,
                            lambda *a, **k: {"status": "ok", "required": False})

    body, status = asgi._readiness(config)

    assert status == 503
    assert body["status"] == "not_ready"
    assert "checkpoint_store" in body["failed"]
