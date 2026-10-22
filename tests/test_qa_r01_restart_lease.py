"""A process restart mid-turn must not block the conversation for 15 minutes (round r01: R1-11).

A real restart during an in-flight turn left ``turns.status='running'`` with a 900 s lease;
POST /api/alex, DELETE, clear_history and POST /api/forget_me then answered 409
``turn_in_progress`` (lease_expires_in ~866 s), 3/3. Each lease now names its owning process,
and a lease whose owner is gone no longer blocks.
"""
from __future__ import annotations

import datetime

import pytest

from uk_rent_agent.web import conversation_store as cs
from uk_rent_agent.web.conversation_store import ConversationBusy, ConversationStore


def _store(path, owner, monkeypatch):
    monkeypatch.setattr(cs, "_PROCESS_OWNER_ID", owner)
    return ConversationStore(path)


def _interrupted_turn(tmp_path, monkeypatch, user="u_restart"):
    """A turn left running by a process that then died (same host: a restart)."""
    old = _store(tmp_path / "c.sqlite3", "old-process", monkeypatch)
    cid = old.create_conversation(user, "restart")["id"]
    turn = old.start_request_turn(user, cid, "req-1", "find me a flat in Camden")
    old.close()
    new = _store(tmp_path / "c.sqlite3", "new-process", monkeypatch)
    return new, cid, turn


def test_r1_11_new_turn_after_restart_is_not_blocked(tmp_path, monkeypatch):
    store, cid, old_turn = _interrupted_turn(tmp_path, monkeypatch)
    turn = store.start_request_turn("u_restart", cid, "req-2", "try again")
    assert turn["status"] == "running" and turn["id"] != old_turn["id"]
    status = store._conn.execute("SELECT status FROM turns WHERE id=?",
                                 (old_turn["id"],)).fetchone()["status"]
    assert status == "failed"
    store.close()


def test_r1_11_delete_and_erasure_after_restart_are_not_blocked(tmp_path, monkeypatch):
    store, cid, _ = _interrupted_turn(tmp_path, monkeypatch)
    assert store.delete_conversation("u_restart", cid) is True
    store.begin_privacy_erasure("u_restart")       # raises ConversationBusy if blocked
    store.close()


def test_r1_11_a_live_turn_in_this_process_still_blocks(tmp_path, monkeypatch):
    store = _store(tmp_path / "c.sqlite3", "this-process", monkeypatch)
    cid = store.create_conversation("u_live", "live")["id"]
    store.start_request_turn("u_live", cid, "req-1", "first")
    with pytest.raises(ConversationBusy):
        store.start_request_turn("u_live", cid, "req-2", "second")
    with pytest.raises(ConversationBusy):
        store.begin_privacy_erasure("u_live")
    store.close()


def _foreign_owner(store, owner, *, age_seconds):
    beat = (datetime.datetime.fromisoformat(cs._now_iso())
            - datetime.timedelta(seconds=age_seconds)).isoformat()
    store._conn.execute(
        "INSERT INTO store_owners (owner_id, host, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
        (owner, "another-container", 4242, beat, beat))
    store._conn.commit()


@pytest.mark.parametrize("age, blocks", [(5, True), (cs.LEASE_OWNER_STALE_SECONDS + 30, False)])
def test_r1_11_other_host_owner_blocks_only_while_its_heartbeat_is_fresh(
        tmp_path, monkeypatch, age, blocks):
    other = _store(tmp_path / "c.sqlite3", "other-pool", monkeypatch)
    cid = other.create_conversation("u_pool", "pool")["id"]
    other.start_request_turn("u_pool", cid, "req-1", "first")
    other._conn.execute("DELETE FROM store_owners WHERE owner_id='other-pool'")
    other._conn.commit()
    other.close()
    store = _store(tmp_path / "c.sqlite3", "this-pool", monkeypatch)
    _foreign_owner(store, "other-pool", age_seconds=age)
    if blocks:
        with pytest.raises(ConversationBusy):
            store.start_request_turn("u_pool", cid, "req-2", "second")
    else:
        assert store.start_request_turn("u_pool", cid, "req-2", "second")["status"] == "running"
    store.close()


def test_r1_11_legacy_lease_without_owner_keeps_the_expiry_rule(tmp_path, monkeypatch):
    store = _store(tmp_path / "c.sqlite3", "this-process", monkeypatch)
    cid = store.create_conversation("u_legacy", "legacy")["id"]
    store.start_request_turn("u_legacy", cid, "req-1", "first")
    store._conn.execute("UPDATE conversation_turn_leases SET owner_id=NULL")
    store._conn.commit()
    monkeypatch.setattr(cs, "_PROCESS_OWNER_ID", "next-process")
    later = ConversationStore(tmp_path / "c.sqlite3")
    with pytest.raises(ConversationBusy):
        later.start_request_turn("u_legacy", cid, "req-2", "second")
    later.close()
    store.close()


def test_r1_11_heartbeat_refreshes_the_owner_row(tmp_path, monkeypatch):
    store = _store(tmp_path / "c.sqlite3", "beating", monkeypatch)
    store._conn.execute("UPDATE store_owners SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    store._conn.commit()
    store._heartbeat()
    beat = store._conn.execute("SELECT heartbeat_at FROM store_owners WHERE owner_id='beating'"
                               ).fetchone()["heartbeat_at"]
    assert beat > "2026"
    store.close()
