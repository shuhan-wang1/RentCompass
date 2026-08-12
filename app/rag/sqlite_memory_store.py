"""SQLite-backed collection API for durable per-user agent memory.

The public surface intentionally mirrors the small subset of the Chroma collection
API used by AgentMemory.  New records live in agent_memory.sqlite3.  If the same
directory contains a legacy Chroma chroma.sqlite3, its document/metadata rows are
copied into the new database without importing the vulnerable Chroma runtime.

Legacy rows remain read-through compatible during a rolling deployment.  Native
updates take ownership of a row, and tombstones prevent a deleted legacy row from
being re-imported.  Retiring the duplicate legacy files is a separate, explicit
operator action after every old application process has stopped.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import quote


_SCHEMA_VERSION = 2
_NATIVE_DB_NAME = "agent_memory.sqlite3"
_LEGACY_DB_NAME = "chroma.sqlite3"
LEGACY_RETIREMENT_MARKER = "legacy_retirement.json"
LEGACY_QUARANTINE_DIR = ".legacy-retirement-quarantine"
_LEGACY_INDEX_DIR = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class LegacyMemoryError(RuntimeError):
    """The legacy store exists but cannot be read or verified safely."""


def _canonical_metadata(value: dict | None) -> tuple[dict, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TypeError("metadata must be a mapping")
    cleaned = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("metadata keys must be non-empty strings")
        if item is None:
            continue
        if not isinstance(item, (str, int, float, bool)):
            item = str(item)
        cleaned[key] = item
    return cleaned, json.dumps(
        cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _matches_where(metadata: dict, where: dict | None) -> bool:
    if not where:
        return True
    if not isinstance(where, dict):
        raise TypeError("where must be a mapping")
    if "$and" in where:
        clauses = where["$and"]
        if not isinstance(clauses, list):
            raise ValueError("$and must contain a list")
        return all(_matches_where(metadata, clause) for clause in clauses)
    if "$or" in where:
        clauses = where["$or"]
        if not isinstance(clauses, list):
            raise ValueError("$or must contain a list")
        return any(_matches_where(metadata, clause) for clause in clauses)
    for key, expected in where.items():
        if key.startswith("$"):
            raise ValueError(f"unsupported where operator: {key}")
        if isinstance(expected, dict):
            if set(expected) != {"$eq"}:
                raise ValueError(f"unsupported where predicate for {key}")
            expected = expected["$eq"]
        if metadata.get(key) != expected:
            return False
    return True


def _features(text: str) -> Counter:
    """Return deterministic multilingual lexical features for cosine retrieval."""
    value = unicodedata.normalize("NFKC", text or "").casefold()
    out: Counter = Counter()
    for token in re.findall(r"[a-z0-9£]+", value):
        out[f"w:{token}"] += 2.0
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        for char in run:
            out[f"c:{char}"] += 1.5
        for pos in range(max(0, len(run) - 1)):
            out[f"b:{run[pos:pos + 2]}"] += 2.0
    compact = "".join(
        char for char in value
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )
    for pos in range(max(0, len(compact) - 2)):
        out[f"g:{compact[pos:pos + 3]}"] += 0.25
    return out


def _cosine(left: Counter, right: Counter) -> float:
    if not left or not right:
        return 0.0
    common = left.keys() & right.keys()
    dot = sum(float(left[key]) * float(right[key]) for key in common)
    lnorm = math.sqrt(sum(float(value) ** 2 for value in left.values()))
    rnorm = math.sqrt(sum(float(value) ** 2 for value in right.values()))
    return dot / (lnorm * rnorm) if lnorm and rnorm else 0.0


class SQLiteMemoryCollection:
    """Small, process-safe collection used by AgentMemory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise RuntimeError(f"memory path is not a directory: {self.root}")
        self.db_path = self.root / _NATIVE_DB_NAME
        self.legacy_path = self.root / _LEGACY_DB_NAME
        self._sync_lock = threading.RLock()
        # WAL/schema setup takes an exclusive SQLite lock. Both production pools
        # can start at the same time against the shared bind mount, so serialize
        # only this short bootstrap section across processes.
        with (self.root / ".agent-memory-init.lock").open(
            "a+", encoding="utf-8"
        ) as init_lock:
            fcntl.flock(init_lock.fileno(), fcntl.LOCK_EX)
            self._initialise()
        self.sync_legacy()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY,
                    document TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    user_id TEXT,
                    mtype TEXT,
                    hash TEXT,
                    idempotency_key TEXT,
                    created_at TEXT,
                    last_access TEXT,
                    origin TEXT NOT NULL CHECK(origin IN ('native', 'legacy'))
                );
                CREATE INDEX IF NOT EXISTS memory_records_user_idx
                    ON memory_records(user_id);
                CREATE INDEX IF NOT EXISTS memory_records_user_type_idx
                    ON memory_records(user_id, mtype);
                CREATE INDEX IF NOT EXISTS memory_records_hash_idx
                    ON memory_records(user_id, hash);
                CREATE INDEX IF NOT EXISTS memory_records_idempotency_idx
                    ON memory_records(user_id, idempotency_key);
                CREATE TABLE IF NOT EXISTS legacy_tombstones (
                    id TEXT PRIMARY KEY,
                    deleted_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_lineage (
                    id TEXT PRIMARY KEY,
                    first_seen_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_sync_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    synced_at INTEGER NOT NULL
                );
                """
            )
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"agent memory SQLite quick_check failed: {check}")

    def _source_fingerprint(self) -> str | None:
        if not self.legacy_path.exists():
            return None
        parts = []
        for path in (self.legacy_path, Path(str(self.legacy_path) + "-wal")):
            try:
                stat = path.stat()
                parts.append((path.name, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                parts.append((path.name, None, None))
        return json.dumps(parts, separators=(",", ":"))

    def legacy_artifacts(self) -> list[Path]:
        """Return only known Chroma artifacts; unknown files are never selected."""
        selected = []
        for path in self.root.iterdir():
            if path.name in {
                _LEGACY_DB_NAME,
                _LEGACY_DB_NAME + "-wal",
                _LEGACY_DB_NAME + "-shm",
            }:
                selected.append(path)
            elif path.is_dir() and _LEGACY_INDEX_DIR.fullmatch(path.name):
                selected.append(path)
        return sorted(selected, key=lambda path: path.name)

    def _retirement_marker(self) -> dict | None:
        path = self.root / LEGACY_RETIREMENT_MARKER
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyMemoryError(
                f"legacy retirement marker is unreadable: {exc}"
            ) from exc
        if not isinstance(value, dict) or value.get("status") not in {
            "pending",
            "retired",
        }:
            raise LegacyMemoryError("legacy retirement marker is invalid")
        return value

    def _read_legacy_once(self) -> list[tuple[str, str, dict]]:
        uri = f"file:{quote(str(self.legacy_path), safe='/')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        except sqlite3.Error as exc:
            raise LegacyMemoryError(f"cannot open legacy memory database: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            required = {"collections", "segments", "embeddings", "embedding_metadata"}
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(required - tables)
            if missing:
                raise LegacyMemoryError(
                    "legacy memory database has an unsupported schema; missing "
                    + ", ".join(missing)
                )
            collection = conn.execute(
                "SELECT id FROM collections WHERE name = ?", ("agent_memory",)
            ).fetchone()
            if collection is None:
                return []
            segments = conn.execute(
                """
                SELECT id FROM segments
                WHERE collection = ?
                  AND (upper(scope) = 'METADATA' OR lower(type) LIKE '%metadata%')
                """,
                (collection["id"],),
            ).fetchall()
            if len(segments) != 1:
                raise LegacyMemoryError(
                    f"expected one legacy metadata segment, found {len(segments)}"
                )
            rows = conn.execute(
                """
                SELECT e.id AS internal_id, e.embedding_id, em.key,
                       em.string_value, em.int_value, em.float_value, em.bool_value
                FROM embeddings AS e
                LEFT JOIN embedding_metadata AS em ON em.id = e.id
                WHERE e.segment_id = ?
                ORDER BY e.id, em.key
                """,
                (segments[0]["id"],),
            ).fetchall()
        except LegacyMemoryError:
            raise
        except sqlite3.Error as exc:
            raise LegacyMemoryError(f"cannot read legacy memory database: {exc}") from exc
        finally:
            conn.close()

        grouped: dict[str, dict] = {}
        for row in rows:
            record_id = row["embedding_id"]
            entry = grouped.setdefault(record_id, {"document": None, "metadata": {}})
            key = row["key"]
            if key is None:
                continue
            if row["string_value"] is not None:
                value = row["string_value"]
            elif row["int_value"] is not None:
                value = row["int_value"]
            elif row["float_value"] is not None:
                value = row["float_value"]
            elif row["bool_value"] is not None:
                value = bool(row["bool_value"])
            else:
                continue
            if key == "chroma:document":
                entry["document"] = str(value)
            else:
                entry["metadata"][key] = value

        records = []
        for record_id, entry in sorted(grouped.items()):
            if entry["document"] is None:
                raise LegacyMemoryError(
                    f"legacy memory row {record_id!r} has no document"
                )
            metadata, _ = _canonical_metadata(entry["metadata"])
            records.append((record_id, entry["document"], metadata))
        return records

    @staticmethod
    def _records_digest(records: Iterable[tuple[str, str, dict]]) -> str:
        digest = hashlib.sha256()
        for record_id, document, metadata in records:
            payload = json.dumps(
                [record_id, document, metadata],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest.update(payload.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _read_stable_legacy(self) -> tuple[str, list[tuple[str, str, dict]]]:
        for _ in range(3):
            before = self._source_fingerprint()
            if before is None:
                return "", []
            records = self._read_legacy_once()
            after = self._source_fingerprint()
            if before == after:
                return after or "", records
        raise LegacyMemoryError(
            "legacy memory database changed repeatedly during migration"
        )

    @staticmethod
    def _record_columns(metadata: dict) -> tuple:
        return (
            metadata.get("user_id"),
            metadata.get("mtype"),
            metadata.get("hash"),
            metadata.get("idempotency_key"),
            metadata.get("created_at"),
            metadata.get("last_access"),
        )

    def sync_legacy(self, force: bool = False) -> dict:
        """Import a stable legacy snapshot; never overwrite native rows."""
        with self._sync_lock:
            marker = self._retirement_marker()
            if marker and marker.get("status") == "pending":
                raise LegacyMemoryError("legacy retirement is incomplete")
            if marker and marker.get("status") == "retired":
                if self.legacy_path.exists():
                    raise LegacyMemoryError(
                        "retired legacy source unexpectedly reappeared"
                    )
                return {
                    "status": "retired",
                    "source_count": int(marker.get("source_count", 0)),
                    "source_digest": marker.get("source_digest"),
                }
            fingerprint = self._source_fingerprint()
            if fingerprint is None:
                return {"status": "absent", "source_count": 0}
            with self._connect() as conn:
                state = conn.execute(
                    "SELECT schema_version, source_fingerprint, source_count, source_digest "
                    "FROM legacy_sync_state WHERE singleton = 1"
                ).fetchone()
            if (
                not force
                and state
                and state["schema_version"] == _SCHEMA_VERSION
                and state["source_fingerprint"] == fingerprint
            ):
                return {
                    "status": "current",
                    "source_count": state["source_count"],
                    "source_digest": state["source_digest"],
                }

            stable_fingerprint, records = self._read_stable_legacy()
            source_digest = self._records_digest(records)
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS legacy_seen "
                    "(id TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM legacy_seen")
                tombstones = {
                    row[0]
                    for row in conn.execute("SELECT id FROM legacy_tombstones")
                }
                for record_id, document, metadata in records:
                    conn.execute(
                        "INSERT OR IGNORE INTO legacy_seen(id) VALUES (?)", (record_id,)
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO legacy_lineage(id, first_seen_at) "
                        "VALUES (?, ?)",
                        (record_id, int(time.time())),
                    )
                    if record_id in tombstones:
                        continue
                    _, metadata_json = _canonical_metadata(metadata)
                    columns = self._record_columns(metadata)
                    conn.execute(
                        """
                        INSERT INTO memory_records(
                            id, document, metadata_json, user_id, mtype, hash,
                            idempotency_key, created_at, last_access, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy')
                        ON CONFLICT(id) DO UPDATE SET
                            document=excluded.document,
                            metadata_json=excluded.metadata_json,
                            user_id=excluded.user_id,
                            mtype=excluded.mtype,
                            hash=excluded.hash,
                            idempotency_key=excluded.idempotency_key,
                            created_at=excluded.created_at,
                            last_access=excluded.last_access
                        WHERE memory_records.origin = 'legacy'
                        """,
                        (record_id, document, metadata_json, *columns),
                    )
                # A deletion performed by an old Chroma-backed pool during a rolling
                # release must also erase a row that the new pool has since touched or
                # updated (and therefore promoted to origin='native').  Keep lineage in
                # a separate table so metadata updates cannot sever that privacy edge.
                removed_legacy_ids = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT lineage.id
                        FROM legacy_lineage AS lineage
                        LEFT JOIN legacy_seen AS seen ON seen.id = lineage.id
                        WHERE seen.id IS NULL
                        """
                    )
                ]
                now = int(time.time())
                conn.executemany(
                    "INSERT OR REPLACE INTO legacy_tombstones(id, deleted_at) "
                    "VALUES (?, ?)",
                    [(record_id, now) for record_id in removed_legacy_ids],
                )
                conn.executemany(
                    "DELETE FROM memory_records WHERE id = ?",
                    [(record_id,) for record_id in removed_legacy_ids],
                )
                conn.execute(
                    """
                    INSERT INTO legacy_sync_state(
                        singleton, schema_version, source_fingerprint, source_count,
                        source_digest, synced_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        schema_version=excluded.schema_version,
                        source_fingerprint=excluded.source_fingerprint,
                        source_count=excluded.source_count,
                        source_digest=excluded.source_digest,
                        synced_at=excluded.synced_at
                    """,
                    (
                        _SCHEMA_VERSION,
                        stable_fingerprint,
                        len(records),
                        source_digest,
                        int(time.time()),
                    ),
                )
                conn.commit()
            return {
                "status": "synced",
                "source_count": len(records),
                "source_digest": source_digest,
            }

    def _rows(self, ids: Sequence[str] | None = None) -> list[sqlite3.Row]:
        self.sync_legacy()
        with self._connect() as conn:
            if ids is None:
                return conn.execute(
                    "SELECT * FROM memory_records ORDER BY rowid"
                ).fetchall()
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM memory_records WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        order = {record_id: pos for pos, record_id in enumerate(ids)}
        rows.sort(key=lambda row: order.get(row["id"], len(order)))
        return rows

    @staticmethod
    def _decode(row: sqlite3.Row) -> tuple[str, str, dict]:
        return row["id"], row["document"], json.loads(row["metadata_json"])

    def get(
        self,
        ids: Sequence[str] | None = None,
        where: dict | None = None,
        **_kwargs,
    ) -> dict:
        decoded = [
            self._decode(row)
            for row in self._rows(ids)
        ]
        decoded = [
            item for item in decoded if _matches_where(item[2], where)
        ]
        return {
            "ids": [item[0] for item in decoded],
            "documents": [item[1] for item in decoded],
            "metadatas": [item[2] for item in decoded],
        }

    @staticmethod
    def _validate_batch(
        ids: Sequence[str],
        documents: Sequence[str] | None,
        metadatas: Sequence[dict] | None,
    ) -> None:
        if not ids:
            raise ValueError("ids must not be empty")
        if len(set(ids)) != len(ids):
            raise ValueError("ids must be unique")
        if documents is not None and len(documents) != len(ids):
            raise ValueError("documents and ids must have the same length")
        if metadatas is not None and len(metadatas) != len(ids):
            raise ValueError("metadatas and ids must have the same length")

    def add(
        self,
        documents: Sequence[str],
        metadatas: Sequence[dict],
        ids: Sequence[str],
        **_kwargs,
    ) -> None:
        self._validate_batch(ids, documents, metadatas)
        self.sync_legacy()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record_id, document, metadata in zip(ids, documents, metadatas):
                if not isinstance(record_id, str) or not record_id:
                    raise ValueError("memory ids must be non-empty strings")
                if not isinstance(document, str):
                    raise TypeError("documents must be strings")
                clean, metadata_json = _canonical_metadata(metadata)
                columns = self._record_columns(clean)
                conn.execute(
                    """
                    INSERT INTO memory_records(
                        id, document, metadata_json, user_id, mtype, hash,
                        idempotency_key, created_at, last_access, origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'native')
                    """,
                    (record_id, document, metadata_json, *columns),
                )
                conn.execute(
                    "DELETE FROM legacy_tombstones WHERE id = ?", (record_id,)
                )
            conn.commit()

    def update(
        self,
        ids: Sequence[str],
        documents: Sequence[str] | None = None,
        metadatas: Sequence[dict] | None = None,
        **_kwargs,
    ) -> None:
        self._validate_batch(ids, documents, metadatas)
        self.sync_legacy()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for pos, record_id in enumerate(ids):
                row = conn.execute(
                    "SELECT document, metadata_json FROM memory_records WHERE id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    continue
                document = (
                    documents[pos] if documents is not None else row["document"]
                )
                metadata = (
                    metadatas[pos]
                    if metadatas is not None
                    else json.loads(row["metadata_json"])
                )
                clean, metadata_json = _canonical_metadata(metadata)
                columns = self._record_columns(clean)
                conn.execute(
                    """
                    UPDATE memory_records SET
                        document=?, metadata_json=?, user_id=?, mtype=?, hash=?,
                        idempotency_key=?, created_at=?, last_access=?, origin='native'
                    WHERE id=?
                    """,
                    (document, metadata_json, *columns, record_id),
                )
                conn.execute(
                    "DELETE FROM legacy_tombstones WHERE id = ?", (record_id,)
                )
            conn.commit()

    def delete(
        self,
        ids: Sequence[str] | None = None,
        where: dict | None = None,
        **_kwargs,
    ) -> None:
        selected = self.get(ids=ids, where=where)
        doomed = selected["ids"]
        if not doomed:
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in doomed)
            now = int(time.time())
            # Tombstone every deleted id, not merely rows whose current origin is
            # 'legacy'. A migrated row can become native after _touch()/UPDATE while
            # the duplicate legacy source still contains the old personal data.
            conn.executemany(
                "INSERT OR REPLACE INTO legacy_tombstones(id, deleted_at) VALUES (?, ?)",
                [(record_id, now) for record_id in doomed],
            )
            conn.execute(
                f"DELETE FROM memory_records WHERE id IN ({placeholders})",
                tuple(doomed),
            )
            conn.commit()

    def query(
        self,
        query_texts: Sequence[str],
        n_results: int = 10,
        where: dict | None = None,
        **_kwargs,
    ) -> dict:
        if n_results < 1:
            raise ValueError("n_results must be positive")
        candidates = self.get(where=where)
        decoded = list(
            zip(
                candidates["ids"],
                candidates["documents"],
                candidates["metadatas"],
            )
        )
        all_ids, all_documents, all_metadatas, all_distances = [], [], [], []
        doc_features = {
            record_id: _features(document)
            for record_id, document, _metadata in decoded
        }
        for text in query_texts:
            query_features = _features(text)
            ranked = sorted(
                (
                    (
                        1.0 - _cosine(query_features, doc_features[record_id]),
                        record_id,
                        document,
                        metadata,
                    )
                    for record_id, document, metadata in decoded
                ),
                key=lambda item: (item[0], item[1]),
            )[:n_results]
            all_distances.append([item[0] for item in ranked])
            all_ids.append([item[1] for item in ranked])
            all_documents.append([item[2] for item in ranked])
            all_metadatas.append([item[3] for item in ranked])
        return {
            "ids": all_ids,
            "documents": all_documents,
            "metadatas": all_metadatas,
            "distances": all_distances,
        }

    def count(self) -> int:
        self.sync_legacy()
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0])

    def legacy_residual_records(self, where: dict | None = None) -> dict:
        """Read raw duplicate rows for privacy verification, never for recall."""
        self.sync_legacy()
        if not self.legacy_path.exists():
            return {"ids": [], "documents": [], "metadatas": []}
        _fingerprint, records = self._read_stable_legacy()
        selected = [
            item for item in records if _matches_where(item[2], where)
        ]
        return {
            "ids": [item[0] for item in selected],
            "documents": [item[1] for item in selected],
            "metadatas": [item[2] for item in selected],
        }

    def legacy_residual_count(self, where: dict | None = None) -> int:
        """Count raw legacy rows still containing data for a privacy inventory."""
        return len(self.legacy_residual_records(where=where)["ids"])

    def verify_legacy_copy(self) -> dict:
        """Prove every non-tombstoned legacy row has an identical durable copy."""
        if not self.legacy_path.exists():
            return {"status": "absent", "source_count": 0, "verified_count": 0}
        self.sync_legacy(force=True)
        _fingerprint, records = self._read_stable_legacy()
        with self._connect() as conn:
            tombstones = {
                row[0] for row in conn.execute("SELECT id FROM legacy_tombstones")
            }
            verified = 0
            exact_copies = 0
            native_overrides = 0
            for record_id, document, metadata in records:
                if record_id in tombstones:
                    continue
                row = conn.execute(
                    "SELECT document, metadata_json, origin "
                    "FROM memory_records WHERE id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise LegacyMemoryError(
                        f"legacy row {record_id!r} is missing from the new store"
                    )
                # Once the new application updates a migrated row, its native version
                # is deliberately authoritative and may differ from the stale source.
                if row["origin"] == "native":
                    verified += 1
                    native_overrides += 1
                    continue
                _clean, expected_json = _canonical_metadata(metadata)
                if row["document"] != document or row["metadata_json"] != expected_json:
                    raise LegacyMemoryError(
                        f"legacy row {record_id!r} differs in the new store"
                    )
                verified += 1
                exact_copies += 1
        return {
            "status": "verified",
            "source_count": len(records),
            "verified_count": verified,
            "tombstoned_count": len(records) - verified,
            "exact_copy_count": exact_copies,
            "native_override_count": native_overrides,
            "source_digest": self._records_digest(records),
        }

    def health(self) -> dict:
        sync = self.sync_legacy()
        marker = self._retirement_marker()
        quarantine = self.root / LEGACY_QUARANTINE_DIR
        if quarantine.exists():
            raise LegacyMemoryError("legacy retirement quarantine requires recovery")
        if marker and marker.get("status") == "pending":
            raise LegacyMemoryError("legacy retirement is incomplete")
        with self._connect() as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"agent memory SQLite quick_check failed: {check}")
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            total = int(
                conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
            )
            native = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_records WHERE origin='native'"
                ).fetchone()[0]
            )
            lineage = int(
                conn.execute("SELECT COUNT(*) FROM legacy_lineage").fetchone()[0]
            )
        legacy = total - native
        artifacts = self.legacy_artifacts()
        if not self.legacy_path.exists() and artifacts:
            raise LegacyMemoryError("orphaned legacy memory artifacts remain")
        if not self.legacy_path.exists() and lineage:
            if not marker or marker.get("status") != "retired":
                raise LegacyMemoryError(
                    "legacy source disappeared without a verified retirement marker"
                )
        if self.legacy_path.exists() and marker and marker.get("status") == "retired":
            raise LegacyMemoryError("retired legacy source unexpectedly reappeared")
        return {
            "status": "ok",
            "backend": "sqlite",
            "schema_version": _SCHEMA_VERSION,
            "records": total,
            "native_records": native,
            "legacy_records": legacy,
            "legacy_lineage_records": lineage,
            "legacy_source": (
                "retired"
                if marker and marker.get("status") == "retired"
                else sync["status"]
            ),
        }
