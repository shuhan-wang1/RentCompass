import pytest

from uk_rent_agent.web.background_jobs import OutboxWorker
from uk_rent_agent.web.conversation_store import ConversationBusy, ConversationStore


def _queued_store(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    cid = store.create_conversation("u1", "chat")["id"]
    turn = store.start_request_turn("u1", cid, "req-1", "hello")
    store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="answer",
        snapshot={
            "schema_version": 1,
            "turn_id": turn["id"],
            "summary": None,
            "summary_through_turn_id": None,
            "context_revision": 0,
        },
        background_jobs=[{
            "kind": "memory_turn",
            "payload": {"user_message": "hello"},
        }],
    )
    return store, cid, turn


def test_worker_retries_without_losing_job_and_then_scrubs_it(tmp_path):
    store, _cid, _turn = _queued_store(tmp_path)
    calls = []

    def process(job, worker_id):
        calls.append(job["attempts"])
        if len(calls) == 1:
            raise RuntimeError("transient")
        store.save_background_job_result(job["id"], worker_id, {"ok": True})

    worker = OutboxWorker(store, process, max_attempts=3)
    assert worker.run_once() is True
    assert store.background_job_counts()["pending"] == 1
    with store._lock:
        store._conn.execute(
            "UPDATE background_jobs SET available_at='2000-01-01T00:00:00+00:00'"
        )
        store._conn.commit()
    assert worker.run_once() is True
    assert store.background_job_counts()["completed"] == 1
    assert calls == [1, 2]
    store.close()


def test_privacy_erasure_refuses_to_race_a_running_background_write(tmp_path):
    store, _cid, _turn = _queued_store(tmp_path)
    job = store.claim_background_job("worker", lease_seconds=60)

    with pytest.raises(ConversationBusy) as exc:
        store.begin_privacy_erasure("u1")
    assert exc.value.turn_id == f"background-job:{job['id']}"

    store.save_background_job_result(job["id"], "worker", {"ok": True})
    store.complete_background_job(job["id"], "worker")
    store.begin_privacy_erasure("u1")
    result = store.delete_all_user_data("u1")
    store.end_privacy_erasure("u1")
    assert result["after"]["total"] == 0
    store.close()


def test_summary_patch_is_durable_and_idempotent(tmp_path):
    store, cid, turn = _queued_store(tmp_path)
    assert store.patch_latest_snapshot_summary(
        "u1", cid, "Budget under 1800; exclude Camden", turn["id"]
    )
    first = store.latest_snapshot("u1", cid)
    assert first["summary"] == "Budget under 1800; exclude Camden"
    assert first["summary_through_turn_id"] == turn["id"]

    assert store.patch_latest_snapshot_summary(
        "u1", cid, "Budget under 1800; exclude Camden", turn["id"]
    )
    second = store.latest_snapshot("u1", cid)
    assert second["summary"] == first["summary"]
    store.close()
