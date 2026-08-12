from __future__ import annotations

import sqlite3
import multiprocessing

import pytest

from rag.sqlite_memory_store import LegacyMemoryError, SQLiteMemoryCollection
from scripts.migrate_agent_memory import retire_legacy


def _write_process_rows(root: str, prefix: str, count: int):
    store = SQLiteMemoryCollection(root)
    for index in range(count):
        store.add(
            ids=[f"{prefix}-{index}"],
            documents=[f"{prefix} memory {index}"],
            metadatas=[{"user_id": prefix, "mtype": "episodic"}],
        )


def _write_same_agent_memory(root: str, output):
    from rag.agent_memory import AgentMemory

    memory = AgentMemory(db_path=root)
    output.put(
        memory.add(
            "User budget is £1750",
            "semantic",
            user_id="shared-user",
            importance=7,
            idempotency_key="turn-42:memory-1",
        )
    )


def _create_legacy(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "chroma.sqlite3")
    conn.executescript(
        """
        CREATE TABLE collections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE segments (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            scope TEXT NOT NULL,
            collection TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            segment_id TEXT NOT NULL,
            embedding_id TEXT NOT NULL
        );
        CREATE TABLE embedding_metadata (
            id INTEGER NOT NULL,
            key TEXT NOT NULL,
            string_value TEXT,
            int_value INTEGER,
            float_value REAL,
            bool_value INTEGER,
            PRIMARY KEY (id, key)
        );
        INSERT INTO collections(id, name) VALUES ('collection-1', 'agent_memory');
        INSERT INTO segments(id, type, scope, collection)
            VALUES ('metadata-1', 'urn:chroma:segment/metadata/sqlite', 'METADATA',
                    'collection-1');
        """
    )
    for internal_id, (record_id, document, metadata) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO embeddings(id, segment_id, embedding_id) VALUES (?, ?, ?)",
            (internal_id, "metadata-1", record_id),
        )
        values = {"chroma:document": document, **metadata}
        for key, value in values.items():
            columns = [None, None, None, None]
            if isinstance(value, bool):
                columns[3] = int(value)
            elif isinstance(value, int):
                columns[1] = value
            elif isinstance(value, float):
                columns[2] = value
            else:
                columns[0] = str(value)
            conn.execute(
                """
                INSERT INTO embedding_metadata(
                    id, key, string_value, int_value, float_value, bool_value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (internal_id, key, *columns),
            )
    conn.commit()
    conn.close()


def _replace_legacy_document(root, internal_id, document):
    conn = sqlite3.connect(root / "chroma.sqlite3")
    conn.execute(
        """
        UPDATE embedding_metadata SET string_value = ?
        WHERE id = ? AND key = 'chroma:document'
        """,
        (document, internal_id),
    )
    conn.commit()
    conn.close()


def _delete_legacy_row(root, internal_id):
    conn = sqlite3.connect(root / "chroma.sqlite3")
    conn.execute("DELETE FROM embedding_metadata WHERE id = ?", (internal_id,))
    conn.execute("DELETE FROM embeddings WHERE id = ?", (internal_id,))
    conn.commit()
    conn.close()


def test_collection_api_is_durable_filtered_and_relevant(tmp_path):
    store = SQLiteMemoryCollection(tmp_path / "memory")
    store.add(
        ids=["budget", "commute", "中文"],
        documents=[
            "User budget is £1500 per month",
            "User commute limit is 30 minutes",
            "用户想住在伦敦大学学院附近",
        ],
        metadatas=[
            {"user_id": "alice", "mtype": "semantic", "importance": 7},
            {"user_id": "alice", "mtype": "semantic", "importance": 8},
            {"user_id": "bob", "mtype": "semantic", "importance": 6},
        ],
    )

    got = store.get(where={"$and": [{"user_id": "alice"}, {"mtype": "semantic"}]})
    assert got["ids"] == ["budget", "commute"]
    assert store.query(
        query_texts=["how long is my commute"],
        n_results=1,
        where={"user_id": "alice"},
    )["ids"] == [["commute"]]
    assert store.query(
        query_texts=["大学附近"],
        n_results=1,
        where={"user_id": "bob"},
    )["ids"] == [["中文"]]

    store.update(ids=["budget"], documents=["User budget is £1700 per month"])
    reopened = SQLiteMemoryCollection(tmp_path / "memory")
    assert reopened.get(ids=["budget"])["documents"] == [
        "User budget is £1700 per month"
    ]
    reopened.delete(where={"user_id": "alice"})
    assert reopened.get(where={"user_id": "alice"})["ids"] == []
    assert reopened.count() == 1
    assert reopened.health()["status"] == "ok"


def test_two_processes_share_the_sqlite_store_without_lost_writes(tmp_path):
    root = str(tmp_path / "memory")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_process_rows, args=(root, prefix, 40))
        for prefix in ("pool-a", "pool-b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    store = SQLiteMemoryCollection(root)
    assert store.count() == 80
    assert len(store.get(where={"user_id": "pool-a"})["ids"]) == 40
    assert len(store.get(where={"user_id": "pool-b"})["ids"]) == 40


def test_two_processes_replaying_one_idempotency_key_create_one_memory(tmp_path):
    root = str(tmp_path / "memory")
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_write_same_agent_memory, args=(root, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    returned_ids = {output.get(timeout=2) for _ in processes}
    store = SQLiteMemoryCollection(root)
    rows = store.get(where={"user_id": "shared-user"})
    assert len(returned_ids) == 1
    assert rows["ids"] == list(returned_ids)


def test_legacy_chroma_rows_are_copied_without_importing_chromadb(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {
                    "user_id": "alice",
                    "mtype": "semantic",
                    "importance": 7,
                    "active": True,
                },
            ),
            (
                "legacy-2",
                "User studies at UCL",
                {"user_id": "alice", "mtype": "semantic", "importance": 8},
            ),
        ],
    )

    store = SQLiteMemoryCollection(root)

    assert store.get(where={"user_id": "alice"})["ids"] == [
        "legacy-1",
        "legacy-2",
    ]
    report = store.verify_legacy_copy()
    assert report["status"] == "verified"
    assert report["source_count"] == report["verified_count"] == 2
    assert len(report["source_digest"]) == 64


def test_native_update_wins_and_tombstone_prevents_legacy_reimport(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {"user_id": "alice", "mtype": "semantic"},
            ),
            (
                "legacy-2",
                "User studies at UCL",
                {"user_id": "alice", "mtype": "semantic"},
            ),
        ],
    )
    store = SQLiteMemoryCollection(root)

    store.update(ids=["legacy-1"], documents=["User budget is £1600"])
    store.delete(ids=["legacy-2"])
    _replace_legacy_document(root, 1, "stale source says £900")
    _replace_legacy_document(root, 2, "stale source still has UCL")
    store.sync_legacy(force=True)

    assert store.get(ids=["legacy-1"])["documents"] == ["User budget is £1600"]
    assert store.get(ids=["legacy-2"])["ids"] == []
    # The raw duplicate remains visible to the privacy inventory until an operator
    # retires the legacy source after all old processes have stopped.
    assert store.legacy_residual_count(where={"user_id": "alice"}) == 2
    report = store.verify_legacy_copy()
    assert report["verified_count"] == 1
    assert report["tombstoned_count"] == 1


def test_deleting_a_native_override_cannot_reimport_its_legacy_copy(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {"user_id": "alice", "mtype": "semantic"},
            )
        ],
    )
    store = SQLiteMemoryCollection(root)

    store.update(ids=["legacy-1"], documents=["User budget is £1600"])
    store.delete(ids=["legacy-1"])
    store.sync_legacy(force=True)

    assert store.get(ids=["legacy-1"])["ids"] == []
    assert store.verify_legacy_copy()["tombstoned_count"] == 1


def test_old_pool_deletion_erases_a_row_updated_by_the_new_pool(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {"user_id": "alice", "mtype": "semantic"},
            )
        ],
    )
    store = SQLiteMemoryCollection(root)

    # A normal recall updates last_access and promotes the migrated record to native.
    store.update(
        ids=["legacy-1"],
        metadatas=[{"user_id": "alice", "mtype": "semantic", "last_access": "now"}],
    )
    _delete_legacy_row(root, 1)
    store.sync_legacy(force=True)

    assert store.get(ids=["legacy-1"])["ids"] == []


def test_targeted_forget_reports_raw_legacy_value_until_retirement(tmp_path):
    from rag.agent_memory import AgentMemory

    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400 per month",
                {"user_id": "alice", "mtype": "semantic"},
            )
        ],
    )
    memory = AgentMemory(db_path=str(root))

    report = memory.forget_fact("alice", ("budget",))

    assert report["complete"] is False
    assert report["residual_ids"] == ()
    assert report["legacy_residual_ids"] == ("legacy-1",)
    assert memory.retrieve("budget", user_id="alice") == []


def test_unsupported_legacy_schema_fails_closed(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    conn = sqlite3.connect(root / "chroma.sqlite3")
    conn.execute("CREATE TABLE unrelated(value TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(LegacyMemoryError, match="unsupported schema"):
        SQLiteMemoryCollection(root)


def test_verified_retirement_removes_duplicate_files_and_preserves_memory(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {"user_id": "alice", "mtype": "semantic"},
            )
        ],
    )
    index = root / "2e7d7c10-4bdf-4e68-be65-5f380c65a19d"
    index.mkdir()
    (index / "data_level0.bin").write_bytes(b"legacy-vector")
    store = SQLiteMemoryCollection(root)
    verified = store.verify_legacy_copy()

    result = retire_legacy(
        root,
        expected_count=verified["source_count"],
        expected_digest=verified["source_digest"],
        confirmed_no_legacy_processes=True,
    )

    assert result["status"] == "retired"
    assert not (root / "chroma.sqlite3").exists()
    assert not index.exists()
    reopened = SQLiteMemoryCollection(root)
    assert reopened.get(ids=["legacy-1"])["documents"] == ["User budget is £1400"]
    assert reopened.legacy_residual_count(where={"user_id": "alice"}) == 0
    assert reopened.health()["legacy_source"] == "retired"


def test_retired_legacy_source_cannot_be_reimported_if_it_reappears(tmp_path):
    root = tmp_path / "memory"
    rows = [
        (
            "legacy-1",
            "User budget is £1400",
            {"user_id": "alice", "mtype": "semantic"},
        )
    ]
    _create_legacy(root, rows)
    store = SQLiteMemoryCollection(root)
    verified = store.verify_legacy_copy()
    retire_legacy(
        root,
        expected_count=verified["source_count"],
        expected_digest=verified["source_digest"],
        confirmed_no_legacy_processes=True,
    )

    _create_legacy(root, rows)
    with pytest.raises(LegacyMemoryError, match="unexpectedly reappeared"):
        SQLiteMemoryCollection(root)


def test_retirement_mismatch_is_non_destructive(tmp_path):
    root = tmp_path / "memory"
    _create_legacy(
        root,
        [
            (
                "legacy-1",
                "User budget is £1400",
                {"user_id": "alice", "mtype": "semantic"},
            )
        ],
    )
    store = SQLiteMemoryCollection(root)
    verified = store.verify_legacy_copy()

    with pytest.raises(RuntimeError, match="count changed"):
        retire_legacy(
            root,
            expected_count=verified["source_count"] + 1,
            expected_digest=verified["source_digest"],
            confirmed_no_legacy_processes=True,
        )

    assert (root / "chroma.sqlite3").exists()
    assert store.get(ids=["legacy-1"])["ids"] == ["legacy-1"]
