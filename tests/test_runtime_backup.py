from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest

from deploy import runtime_backup as rb


def _new_sqlite(path: Path, values: tuple[str, ...] = ("alpha",)) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO records(value) VALUES (?)",
        [(value,) for value in values],
    )
    connection.commit()
    return connection


def _make_plain_backup(tmp_path: Path, name: str = "sample") -> Path:
    source = tmp_path / f"{name}-source"
    source.mkdir()
    database = source / "state.sqlite3"
    connection = _new_sqlite(database, ("alpha", "beta"))
    (source / "nested").mkdir()
    (source / "nested" / "note.txt").write_text("runtime-note", encoding="utf-8")
    archive = tmp_path / f"{name}.tar.gz"
    try:
        rb.create_backup(
            sqlite_arguments=[f"main={database}"],
            directory_arguments=[f"runtime={source}"],
            output=archive,
            encrypt=False,
        )
    finally:
        connection.close()
    return archive


def _trusted_unpack(archive: Path, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            relative = Path(*member.name.rstrip("/").split("/"))
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = handle.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())
            else:
                raise AssertionError("helper only accepts archives produced by the test")


def _repack(root: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as handle:
        for path in sorted(root.rglob("*")):
            handle.add(
                path,
                arcname=path.relative_to(root).as_posix(),
                recursive=False,
            )


def _rewrite_archive(
    archive: Path,
    destination: Path,
    mutation,
) -> None:
    unpacked = destination.parent / f"{destination.stem}-unpacked"
    _trusted_unpack(archive, unpacked)
    mutation(unpacked)
    _repack(unpacked, destination)


def test_default_create_fails_without_recipient_before_reading_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(rb.AGE_RECIPIENT_ENV, raising=False)
    output = tmp_path / "backup.tar.gz.age"

    status = rb.main(
        [
            "create",
            "--sqlite",
            f"missing={tmp_path / 'does-not-exist.sqlite3'}",
            "--output",
            os.fspath(output),
        ]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert "requires --age-recipient" in captured.err
    assert "source is not accessible" not in captured.err
    assert not output.exists()


def test_default_create_fails_when_age_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(rb.AGE_RECIPIENT_ENV, raising=False)
    database = tmp_path / "state.sqlite3"
    connection = _new_sqlite(database)
    output = tmp_path / "backup.tar.gz.age"
    try:
        status = rb.main(
            [
                "create",
                "--sqlite",
                f"main={database}",
                "--output",
                os.fspath(output),
                "--age-recipient",
                "age1-test-recipient",
                "--age-binary",
                os.fspath(tmp_path / "missing-age"),
            ]
        )
    finally:
        connection.close()

    captured = capsys.readouterr()
    assert status == 2
    assert "age executable is unavailable" in captured.err
    assert not output.exists()


def test_plaintext_round_trip_uses_online_sqlite_and_complete_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live-runtime"
    source.mkdir()
    database = source / "state.sqlite3"
    connection = _new_sqlite(database, ("committed-in-wal",))
    (source / "nested").mkdir()
    (source / "nested" / "data.bin").write_bytes(b"chroma-like-data")
    archive = tmp_path / "runtime.tar.gz"

    try:
        result = rb.create_backup(
            sqlite_arguments=[f"main={database}"],
            directory_arguments=[f"runtime={source}"],
            output=archive,
            encrypt=False,
        )
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone() == (1,)
    finally:
        connection.close()

    assert result["encrypted"] is False
    with tarfile.open(archive, "r:gz") as handle:
        names = set(handle.getnames())
        manifest_member = handle.extractfile("manifest.json")
        assert manifest_member is not None
        manifest_bytes = manifest_member.read()
        manifest = json.loads(manifest_bytes)
        for row in manifest["files"]:
            member = handle.extractfile(row["path"])
            assert member is not None
            payload = member.read()
            assert len(payload) == row["size"]
            assert hashlib.sha256(payload).hexdigest() == row["sha256"]

    assert "payload/sqlite/main.sqlite3" in names
    assert "payload/directories/runtime/nested/data.bin" in names
    assert "payload/directories/runtime/state.sqlite3" not in names
    assert "payload/directories/runtime/state.sqlite3-wal" not in names
    assert os.fspath(tmp_path).encode() not in manifest_bytes

    target = tmp_path / "restored"
    restored = rb.restore_backup(
        input_path=archive,
        target=target,
        encrypted=False,
    )
    assert restored["verified_sqlite_count"] == 1
    with sqlite3.connect(target / "sqlite" / "main.sqlite3") as restored_db:
        assert restored_db.execute("SELECT value FROM records").fetchall() == [
            ("committed-in-wal",)
        ]
    assert (
        target / "directories" / "runtime" / "nested" / "data.bin"
    ).read_bytes() == b"chroma-like-data"


def test_restore_rejects_hash_mismatch_before_target_mutation(
    tmp_path: Path,
) -> None:
    archive = _make_plain_backup(tmp_path, "hash")
    tampered = tmp_path / "hash-tampered.tar.gz"

    def mutate(root: Path) -> None:
        (root / "payload" / "directories" / "runtime" / "nested" / "note.txt").write_text(
            "tampered!!!!",
            encoding="utf-8",
        )

    _rewrite_archive(archive, tampered, mutate)
    target = tmp_path / "empty-target"
    target.mkdir()

    with pytest.raises(rb.BackupError, match="hash mismatch"):
        rb.restore_backup(
            input_path=tampered,
            target=target,
            encrypted=False,
        )

    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_restore_rejects_corrupt_sqlite_with_matching_manifest_hash(
    tmp_path: Path,
) -> None:
    archive = _make_plain_backup(tmp_path, "sqlite")
    tampered = tmp_path / "sqlite-tampered.tar.gz"

    def mutate(root: Path) -> None:
        sqlite_path = root / "payload" / "sqlite" / "main.sqlite3"
        sqlite_path.write_bytes(b"not a sqlite database")
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in manifest["files"]:
            if row["path"] == "payload/sqlite/main.sqlite3":
                row["size"] = sqlite_path.stat().st_size
                row["sha256"] = hashlib.sha256(sqlite_path.read_bytes()).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    _rewrite_archive(archive, tampered, mutate)
    target = tmp_path / "not-created"

    with pytest.raises(rb.BackupError, match="SQLite validation failed"):
        rb.restore_backup(
            input_path=tampered,
            target=target,
            encrypted=False,
        )

    assert not target.exists()


def test_restore_refuses_nonempty_target_by_default(tmp_path: Path) -> None:
    archive = _make_plain_backup(tmp_path, "refuse")
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("do-not-touch", encoding="utf-8")

    with pytest.raises(rb.BackupError, match="non-empty"):
        rb.restore_backup(
            input_path=archive,
            target=target,
            encrypted=False,
        )

    assert marker.read_text(encoding="utf-8") == "do-not-touch"
    assert list(target.iterdir()) == [marker]


def test_explicit_overwrite_preserves_previous_target(tmp_path: Path) -> None:
    archive = _make_plain_backup(tmp_path, "overwrite")
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("old-state", encoding="utf-8")

    result = rb.restore_backup(
        input_path=archive,
        target=target,
        encrypted=False,
        overwrite_target=True,
    )

    previous = Path(str(result["previous_target"]))
    assert previous.parent == target.parent
    assert previous.name.startswith(".existing.pre-restore-")
    assert (previous / "keep.txt").read_text(encoding="utf-8") == "old-state"
    assert (target / "sqlite" / "main.sqlite3").is_file()


def test_default_age_encryption_and_restore_with_fake_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_age = tmp_path / "fake-age"
    fake_age.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, shutil, sys\n"
        "args = sys.argv[1:]\n"
        "destination = args[args.index('-o') + 1]\n"
        "source = args[-1]\n"
        "shutil.copyfile(source, destination)\n"
        "with open(os.environ['FAKE_AGE_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n",
        encoding="utf-8",
    )
    fake_age.chmod(0o755)
    age_log = tmp_path / "age.log"
    monkeypatch.setenv("FAKE_AGE_LOG", os.fspath(age_log))
    database = tmp_path / "encrypted-source.sqlite3"
    connection = _new_sqlite(database, ("encrypted-row",))
    archive = tmp_path / "encrypted.tar.gz.age"
    try:
        created = rb.create_backup(
            sqlite_arguments=[f"main={database}"],
            directory_arguments=[],
            output=archive,
            age_recipient="age1-offline-test",
            age_binary=os.fspath(fake_age),
        )
    finally:
        connection.close()
    assert created["encrypted"] is True

    identity = tmp_path / "identity.txt"
    identity.write_text("offline-test-identity", encoding="utf-8")
    target = tmp_path / "encrypted-restore"
    rb.restore_backup(
        input_path=archive,
        target=target,
        age_identity=identity,
        age_binary=os.fspath(fake_age),
    )

    calls = [json.loads(line) for line in age_log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert calls[0][calls[0].index("-r") + 1] == "age1-offline-test"
    assert "--decrypt" in calls[1]
    assert calls[1][calls[1].index("--identity") + 1] == os.fspath(identity)
    with sqlite3.connect(target / "sqlite" / "main.sqlite3") as restored_db:
        assert restored_db.execute("SELECT value FROM records").fetchone() == (
            "encrypted-row",
        )


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tar.gz"
    content = b"escape"
    with tarfile.open(archive, "w:gz") as handle:
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(content)
        import io

        handle.addfile(member, io.BytesIO(content))

    target = tmp_path / "target"
    with pytest.raises(rb.BackupError, match="Unsafe archive member path"):
        rb.restore_backup(
            input_path=archive,
            target=target,
            encrypted=False,
        )

    assert not (tmp_path / "escape.txt").exists()
    assert not target.exists()


def test_directory_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    source.mkdir()
    (source / "real.txt").write_text("data", encoding="utf-8")
    (source / "linked.txt").symlink_to(source / "real.txt")

    with pytest.raises(rb.BackupError, match="Symlink"):
        rb.create_backup(
            sqlite_arguments=[],
            directory_arguments=[f"runtime={source}"],
            output=tmp_path / "symlink.tar.gz",
            encrypt=False,
        )
