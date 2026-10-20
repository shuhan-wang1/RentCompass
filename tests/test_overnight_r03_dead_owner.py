"""R3-12: the first retry after a mid-turn SIGKILL is already terminal.

r04 acceptance: retries #1-#2 of an interrupted request got 409 "still in progress" until
the dead owner's heartbeat went stale; only #3+ got the terminal 502 ``interrupted``.
A same-host owner whose process is gone (or a zombie nobody reaped) cannot write, so its
lease no longer blocks.
"""
from __future__ import annotations

import subprocess
import sys

from uk_rent_agent.web import conversation_store as cs
from uk_rent_agent.web.conversation_store import ConversationStore


def _store(path, owner, monkeypatch):
    monkeypatch.setattr(cs, "_PROCESS_OWNER_ID", owner)
    return ConversationStore(path)


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_zombie_process_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        import time
        for _ in range(100):           # wait for it to exit without reaping it
            with open(f"/proc/{proc.pid}/stat") as handle:
                if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            time.sleep(0.02)
        assert cs._pid_alive(proc.pid) is False
    finally:
        proc.wait()


def test_first_retry_after_a_dead_owner_is_terminal(tmp_path, monkeypatch):
    path = tmp_path / "c.sqlite3"
    new = _store(path, "new-process", monkeypatch)
    cid = new.create_conversation("u_dead", "dead")["id"]
    turn = new.start_request_turn("u_dead", cid, "dead-req-0", "find me a flat")
    # The turn belongs to a process on this host that was SIGKILLed a moment ago: its owner
    # row still has a fresh heartbeat and its lease has not expired.
    now = cs._now_iso()
    new._conn.execute(
        "INSERT INTO store_owners (owner_id, host, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
        ("killed-process", cs._host_name(), _dead_pid(), now, now))
    new._conn.execute("UPDATE conversation_turn_leases SET owner_id=? WHERE turn_id=?",
                      ("killed-process", turn["id"]))
    new._conn.commit()
    replay = new.start_request_turn("u_dead", cid, "dead-req-0", "find me a flat")
    assert replay["replayed"] is True and replay["status"] == "failed"
    assert new.get_turn_response("u_dead", turn["id"])["interrupted"] is True
    new.close()


def test_a_live_owner_on_this_host_still_blocks(tmp_path, monkeypatch):
    import os
    path = tmp_path / "c.sqlite3"
    new = _store(path, "new-process", monkeypatch)
    cid = new.create_conversation("u_live3", "live")["id"]
    turn = new.start_request_turn("u_live3", cid, "live-req-0", "find me a flat")
    now = cs._now_iso()
    new._conn.execute(
        "INSERT INTO store_owners (owner_id, host, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?)",
        ("other-live-process", cs._host_name(), os.getppid(), now, now))
    new._conn.execute("UPDATE conversation_turn_leases SET owner_id=? WHERE turn_id=?",
                      ("other-live-process", turn["id"]))
    new._conn.commit()
    replay = new.start_request_turn("u_live3", cid, "live-req-0", "find me a flat")
    assert replay["status"] == "running"
    new.close()
