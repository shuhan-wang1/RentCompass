from __future__ import annotations

import sqlite3

import pytest

from uk_rent_agent.web.conversation_store import (
    ConversationBusy,
    ConversationStore,
)


@pytest.fixture
def store(tmp_path):
    value = ConversationStore(tmp_path / "conversations.sqlite3")
    yield value
    value.close()


def _conversation(store: ConversationStore) -> str:
    return store.create_conversation("u1", "chat")["id"]


def test_request_start_is_atomic_and_duplicate_is_replayed(store):
    cid = _conversation(store)

    first = store.start_request_turn("u1", cid, "req-1", "hello")
    replay = store.start_request_turn("u1", cid, "req-1", "must not be stored")

    assert replay["id"] == first["id"]
    assert replay["replayed"] is True
    messages = store.get_messages("u1", cid)
    assert [(m["role"], m["content"]) for m in messages] == [("user", "hello")]
    assert messages[0]["turn_id"] == first["id"]


def test_different_request_cannot_overlap_same_conversation(store):
    cid = _conversation(store)
    first = store.start_request_turn("u1", cid, "req-1", "one")

    with pytest.raises(ConversationBusy) as error:
        store.start_request_turn("u1", cid, "req-2", "two")
    assert error.value.turn_id == first["id"]
    assert [m["content"] for m in store.get_messages("u1", cid)] == ["one"]

    store.fail_turn("u1", first["id"])
    second = store.start_request_turn("u1", cid, "req-2", "two")
    assert second["id"] != first["id"]


def test_expired_lease_is_reclaimed_and_old_turn_is_failed(store):
    cid = _conversation(store)
    first = store.start_request_turn("u1", cid, "req-1", "one")
    with store._lock:
        store._conn.execute(
            """UPDATE conversation_turn_leases
               SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE user_id='u1' AND conversation_id=?""",
            (cid,),
        )
        store._conn.commit()

    second = store.start_request_turn("u1", cid, "req-2", "two")

    assert store.get_turn("u1", first["id"])["status"] == "failed"
    assert second["status"] == "running"


def test_finalize_commits_assistant_status_and_snapshot_together(store):
    cid = _conversation(store)
    turn = store.start_request_turn("u1", cid, "req-1", "hello")

    done = store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="answer",
        response_type="chat",
        recommendations=[{"url": "https://example.test/1"}],
        snapshot={"turn_id": turn["id"], "context_revision": 1},
    )

    assert done["status"] == "completed"
    assert done["assistant_message_id"] is not None
    assert store.get_turn_snapshot("u1", turn["id"])["context_revision"] == 1
    response = store.get_turn_response("u1", turn["id"])
    assert response["message"] == "answer"
    assert response["recommendations"][0]["url"].endswith("/1")

    # A second finalize is an idempotent read, not a second assistant message.
    store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="different",
        snapshot={"turn_id": turn["id"], "context_revision": 2},
    )
    assert [m["content"] for m in store.get_messages("u1", cid)] == ["hello", "answer"]


def test_finalize_commits_outbox_and_serializes_jobs_per_conversation(store):
    cid = _conversation(store)
    turn = store.start_request_turn("u1", cid, "req-outbox", "hello")
    store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="answer",
        snapshot={"schema_version": 1, "turn_id": turn["id"]},
        background_jobs=[
            {"kind": "memory_turn", "payload": {"user_message": "hello"}},
            {"kind": "rolling_summary", "payload": {"dropped_turns": []}},
        ],
    )

    assert store.background_job_counts()["pending"] == 2
    first = store.claim_background_job("worker-1")
    assert first["payload"]
    assert store.claim_background_job("worker-2") is None
    store.save_background_job_result(first["id"], "worker-1", {"ok": True})
    store.complete_background_job(first["id"], "worker-1")
    second = store.claim_background_job("worker-2")
    assert second is not None and second["id"] != first["id"]


def test_completed_outbox_tombstone_scrubs_payload_and_preserves_dedupe(store):
    cid = _conversation(store)
    turn = store.start_request_turn("u1", cid, "req-outbox", "private input")
    store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="private output",
        snapshot={"schema_version": 1, "turn_id": turn["id"]},
        background_jobs=[{
            "kind": "memory_turn",
            "dedupe_key": "memory:req-outbox",
            "payload": {"secret": "must be scrubbed"},
        }],
    )
    job = store.claim_background_job("worker")
    store.save_background_job_result(job["id"], "worker", {"secret": "also scrubbed"})
    store.complete_background_job(job["id"], "worker")

    with store._lock:
        row = store._conn.execute(
            "SELECT status, payload_json, result_json FROM background_jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
    assert dict(row) == {
        "status": "completed", "payload_json": "{}", "result_json": None
    }


def test_finalize_rolls_back_every_layer_when_snapshot_is_not_serializable(store):
    cid = _conversation(store)
    turn = store.start_request_turn("u1", cid, "req-1", "hello")

    with pytest.raises(TypeError):
        store.finalize_request_turn(
            "u1",
            turn["id"],
            status="completed",
            assistant_content="answer",
            snapshot={"bad": object()},
        )

    assert store.get_turn("u1", turn["id"])["status"] == "running"
    assert [m["role"] for m in store.get_messages("u1", cid)] == ["user"]
    assert store.get_turn_snapshot("u1", turn["id"]) is None


def test_database_trigger_rejects_completed_turn_without_assistant(store):
    cid = _conversation(store)
    turn = store.begin_turn("u1", cid)
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_turn("u1", turn["id"])


def test_privacy_delete_reports_and_verifies_every_relational_layer(store):
    cid = _conversation(store)
    turn = store.start_request_turn("u1", cid, "req-1", "hello")
    store.add_favorite("u1", "https://example.test/1", {"url": "https://example.test/1"})
    store.finalize_request_turn(
        "u1",
        turn["id"],
        status="completed",
        assistant_content="answer",
        snapshot={"schema_version": 1, "turn_id": turn["id"]},
        background_jobs=[{
            "kind": "memory_turn", "payload": {"user_message": "hello"}
        }],
    )

    before = store.privacy_inventory("u1")
    result = store.delete_all_user_data("u1")

    assert before["total"] > 0
    assert before["background_jobs"] == 1
    assert result["conversation_ids"] == [cid]
    assert result["after"]["total"] == 0
    assert store.privacy_inventory("u1")["total"] == 0
    assert store.get_turn("u1", turn["id"]) is None
