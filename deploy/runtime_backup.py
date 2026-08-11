#!/usr/bin/env python3
"""Create and restore verified backups of RentCompass runtime state.

The default archive format is an age-encrypted tarball. Plaintext archives
require an explicit --no-encrypt flag and are intended only for isolated
testing or an already encrypted storage layer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable, Sequence
from urllib.parse import quote
import uuid


FORMAT_NAME = "rentcompass-runtime-backup"
SCHEMA_VERSION = 1
DEFAULT_MAX_EXTRACT_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 250_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
AGE_RECIPIENT_ENV = "RUNTIME_BACKUP_AGE_RECIPIENT"
AGE_IDENTITY_ENV = "RUNTIME_BACKUP_AGE_IDENTITY"
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupError(RuntimeError):
    """An expected, user-actionable backup or restore failure."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_uri(path: Path) -> str:
    return "file:" + quote(os.fspath(path), safe="/") + "?mode=ro"


def _sqlite_quick_check(path: Path) -> None:
    try:
        with sqlite3.connect(_sqlite_uri(path), uri=True, timeout=10) as connection:
            rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite validation failed for {path.name}: {exc}") from exc
    if not rows or any(row != ("ok",) for row in rows):
        details = "; ".join(str(row[0]) for row in rows[:5]) or "no result"
        raise BackupError(f"SQLite quick_check failed for {path.name}: {details}")


def _online_sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(
            _sqlite_uri(source), uri=True, timeout=30
        ) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with sqlite3.connect(destination) as destination_connection:
                source_connection.backup(
                    destination_connection,
                    pages=1024,
                    sleep=0.025,
                )
                destination_connection.commit()
    except sqlite3.Error as exc:
        raise BackupError(
            f"Online SQLite backup failed for {source.name}: {exc}"
        ) from exc
    _sqlite_quick_check(destination)
    os.chmod(destination, 0o600)


def _parse_labeled_sources(
    values: Sequence[str],
    *,
    kind: str,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise BackupError(f"{kind} source must use LABEL=PATH: {value!r}")
        label, raw_path = value.split("=", 1)
        if not _LABEL_RE.fullmatch(label):
            raise BackupError(f"Invalid {kind} label: {label!r}")
        if label in result:
            raise BackupError(f"Duplicate {kind} label: {label}")
        if not raw_path:
            raise BackupError(f"Missing path for {kind} label: {label}")
        lexical_path = Path(raw_path).expanduser()
        if lexical_path.is_symlink():
            raise BackupError(f"{kind} source must not be a symlink: {label}")
        try:
            path = lexical_path.resolve(strict=True)
        except OSError as exc:
            raise BackupError(f"{kind} source is not accessible: {label}") from exc
        if kind == "SQLite" and not path.is_file():
            raise BackupError(f"SQLite source is not a regular file: {label}")
        if kind == "directory" and not path.is_dir():
            raise BackupError(f"Directory source is not a directory: {label}")
        result[label] = path
    return result


def _require_program(binary: str) -> str:
    resolved = shutil.which(binary)
    if not resolved:
        raise BackupError(
            f"Required age executable is unavailable or not executable: {binary}"
        )
    return resolved


def _age_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "age exited without an error message"
    return lines[-1][:500]


def _run_age(command: Sequence[str], destination: Path) -> None:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"Unable to execute age: {exc}") from exc
    if completed.returncode != 0:
        raise BackupError(f"age failed: {_age_error(completed.stderr)}")
    try:
        metadata = destination.lstat()
    except OSError as exc:
        raise BackupError("age reported success but produced no output") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise BackupError("age produced an invalid output file")


def _source_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _open_source_no_follow(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags), "rb")


def _copy_stable_file(source: Path, destination: Path) -> None:
    try:
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise BackupError(f"Runtime entry is not a regular file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        first_digest = hashlib.sha256()
        with _open_source_no_follow(source) as input_handle:
            opened = os.fstat(input_handle.fileno())
            if _source_signature(opened) != _source_signature(before):
                raise BackupError(f"Runtime file changed before copy: {source}")
            with destination.open("xb") as output_handle:
                for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                    first_digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
        after = source.lstat()
        if _source_signature(after) != _source_signature(before):
            raise BackupError(f"Runtime file changed during copy: {source}")
        with _open_source_no_follow(source) as verify_handle:
            second_digest = hashlib.sha256()
            for chunk in iter(lambda: verify_handle.read(1024 * 1024), b""):
                second_digest.update(chunk)
        if first_digest.digest() != second_digest.digest():
            raise BackupError(f"Runtime file contents changed during copy: {source}")
        if _sha256(destination) != first_digest.hexdigest():
            raise BackupError(f"Copied runtime file failed verification: {source}")
        os.chmod(destination, stat.S_IMODE(before.st_mode))
        os.utime(
            destination,
            ns=(before.st_atime_ns, before.st_mtime_ns),
            follow_symlinks=False,
        )
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"Unable to copy runtime file {source}: {exc}") from exc


def _copy_directory_stable(
    source: Path,
    destination: Path,
    excluded_files: set[Path],
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    source_mode = stat.S_IMODE(source.lstat().st_mode)
    directory_modes: list[tuple[Path, int]] = [(destination, source_mode)]
    for root_text, directory_names, file_names in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        root = Path(root_text)
        relative_root = root.relative_to(source)
        destination_root = destination / relative_root
        destination_root.mkdir(parents=True, exist_ok=True)

        accepted_directories: list[str] = []
        for directory_name in directory_names:
            child = root / directory_name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupError(f"Symlink found in runtime directory: {child}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise BackupError(f"Special entry found in runtime directory: {child}")
            accepted_directories.append(directory_name)
            target_directory = destination_root / directory_name
            target_directory.mkdir(exist_ok=True)
            directory_modes.append(
                (target_directory, stat.S_IMODE(metadata.st_mode))
            )
        directory_names[:] = accepted_directories

        for file_name in file_names:
            child = root / file_name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupError(f"Symlink found in runtime directory: {child}")
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupError(f"Special entry found in runtime directory: {child}")
            child_resolved = child.resolve(strict=True)
            if child_resolved in excluded_files:
                continue
            _copy_stable_file(child, destination_root / file_name)
    for path, mode in sorted(
        directory_modes,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        os.chmod(path, mode)


def _archive_rows(payload: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(payload.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupError(f"Internal staging symlink is not allowed: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"Internal staging special file is not allowed: {path}")
        rows.append(
            {
                "path": path.relative_to(payload.parent).as_posix(),
                "size": metadata.st_size,
                "sha256": _sha256(path),
            }
        )
    return rows


def _normalized_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _write_tar(staging: Path, destination: Path) -> None:
    try:
        with tarfile.open(
            destination,
            mode="w:gz",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            for path in sorted(staging.rglob("*")):
                relative = path.relative_to(staging).as_posix()
                archive.add(
                    path,
                    arcname=relative,
                    recursive=False,
                    filter=_normalized_tarinfo,
                )
        os.chmod(destination, 0o600)
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"Unable to create backup archive: {exc}") from exc


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_clobber(source: Path, destination: Path) -> None:
    os.chmod(source, 0o600)
    _fsync_file(source)
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise BackupError(f"Backup output already exists: {destination}") from exc
    except OSError as exc:
        raise BackupError(f"Unable to publish backup atomically: {exc}") from exc
    _fsync_directory(destination.parent)
    source.unlink()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def create_backup(
    *,
    sqlite_arguments: Sequence[str],
    directory_arguments: Sequence[str],
    output: Path,
    encrypt: bool = True,
    age_recipient: str | None = None,
    age_binary: str = "age",
) -> dict[str, object]:
    lexical_output = output.expanduser()
    if not lexical_output.name:
        raise BackupError("Backup output must name a file")
    if os.path.lexists(lexical_output):
        raise BackupError(f"Backup output already exists: {lexical_output}")
    try:
        output_parent = lexical_output.parent.resolve(strict=True)
    except OSError as exc:
        raise BackupError("Backup output parent does not exist") from exc
    if not output_parent.is_dir():
        raise BackupError(f"Backup output parent is not a directory: {output_parent}")
    output = output_parent / lexical_output.name
    if os.path.lexists(output):
        raise BackupError(f"Backup output already exists: {output}")

    resolved_age: str | None = None
    recipient: str | None = None
    if encrypt:
        recipient = age_recipient or os.environ.get(AGE_RECIPIENT_ENV)
        if not recipient:
            raise BackupError(
                "Encrypted backup requires --age-recipient or "
                f"{AGE_RECIPIENT_ENV}"
            )
        resolved_age = _require_program(age_binary)

    sqlite_sources = _parse_labeled_sources(
        sqlite_arguments,
        kind="SQLite",
    )
    directory_sources = _parse_labeled_sources(
        directory_arguments,
        kind="directory",
    )
    if not sqlite_sources and not directory_sources:
        raise BackupError("At least one --sqlite or --directory source is required")
    for label, directory in directory_sources.items():
        if _path_is_within(output, directory):
            raise BackupError(
                f"Backup output cannot be inside directory source: {label}"
            )

    excluded_files: set[Path] = set()
    for database in sqlite_sources.values():
        excluded_files.add(database)
        excluded_files.add(Path(os.fspath(database) + "-wal"))
        excluded_files.add(Path(os.fspath(database) + "-shm"))
        excluded_files.add(Path(os.fspath(database) + "-journal"))

    with tempfile.TemporaryDirectory(
        prefix=".runtime-backup-",
        dir=output.parent,
    ) as temporary_text:
        temporary = Path(temporary_text)
        os.chmod(temporary, 0o700)
        staging = temporary / "staging"
        payload = staging / "payload"
        sqlite_payload = payload / "sqlite"
        directory_payload = payload / "directories"
        sqlite_payload.mkdir(parents=True)
        directory_payload.mkdir(parents=True)

        sqlite_files: list[str] = []
        for label, source in sorted(sqlite_sources.items()):
            destination = sqlite_payload / f"{label}.sqlite3"
            _online_sqlite_backup(source, destination)
            sqlite_files.append(destination.relative_to(staging).as_posix())

        directory_roots: list[str] = []
        for label, source in sorted(directory_sources.items()):
            destination = directory_payload / label
            _copy_directory_stable(source, destination, excluded_files)
            directory_roots.append(destination.relative_to(staging).as_posix())

        manifest = {
            "format": FORMAT_NAME,
            "schema_version": SCHEMA_VERSION,
            "created_at": _utc_now(),
            "encryption": "age" if encrypt else "none",
            "files": _archive_rows(payload),
            "sqlite_files": sqlite_files,
            "directory_roots": directory_roots,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)

        plaintext = temporary / "runtime-backup.tar.gz"
        _write_tar(staging, plaintext)
        publish_source = plaintext
        if encrypt:
            assert resolved_age is not None
            assert recipient is not None
            encrypted = temporary / "runtime-backup.tar.gz.age"
            _run_age(
                [
                    resolved_age,
                    "-r",
                    recipient,
                    "-o",
                    os.fspath(encrypted),
                    os.fspath(plaintext),
                ],
                encrypted,
            )
            publish_source = encrypted
        _publish_no_clobber(publish_source, output)

    return {
        "operation": "create",
        "output": os.fspath(output),
        "encrypted": encrypt,
        "file_count": len(manifest["files"]),
        "sqlite_count": len(sqlite_files),
        "directory_count": len(directory_roots),
    }


def _safe_member_name(name: str) -> PurePosixPath:
    normalized = name.rstrip("/")
    if not normalized or name.startswith("/") or "\\" in name or "\x00" in name:
        raise BackupError(f"Unsafe archive member path: {name!r}")
    raw_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise BackupError(f"Unsafe archive member path: {name!r}")
    return PurePosixPath(*raw_parts)


def _safe_extract(
    archive_path: Path,
    destination: Path,
    *,
    max_extract_bytes: int,
    max_members: int,
) -> dict[str, int]:
    destination.mkdir(parents=True, exist_ok=False)
    modes: dict[str, int] = {}
    seen: set[str] = set()
    total_bytes = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for index, member in enumerate(archive, start=1):
                if index > max_members:
                    raise BackupError("Archive exceeds the member-count limit")
                relative = _safe_member_name(member.name)
                key = relative.as_posix()
                if key in seen:
                    raise BackupError(f"Duplicate archive member: {key}")
                seen.add(key)
                if not (member.isdir() or member.isreg()):
                    raise BackupError(f"Unsupported archive member type: {key}")
                if member.size < 0:
                    raise BackupError(f"Invalid archive member size: {key}")
                total_bytes += member.size
                if total_bytes > max_extract_bytes:
                    raise BackupError("Archive exceeds the expanded-size limit")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=False)
                    os.chmod(target, 0o700)
                    modes[key] = stat.S_IMODE(member.mode)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source_handle = archive.extractfile(member)
                if source_handle is None:
                    raise BackupError(f"Unable to read archive member: {key}")
                remaining = member.size
                with target.open("xb") as output_handle:
                    while remaining:
                        chunk = source_handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise BackupError(f"Truncated archive member: {key}")
                        output_handle.write(chunk)
                        remaining -= len(chunk)
                os.chmod(target, 0o600)
                modes[key] = stat.S_IMODE(member.mode)
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"Unable to extract backup archive: {exc}") from exc
    return modes


def _manifest_relative_path(value: object, *, prefix: str) -> str:
    if not isinstance(value, str):
        raise BackupError("Manifest contains a non-string path")
    relative = _safe_member_name(value)
    normalized = relative.as_posix()
    if normalized != value or not normalized.startswith(prefix):
        raise BackupError(f"Manifest contains an invalid path: {value!r}")
    return normalized


def _actual_payload_files(payload: Path) -> set[str]:
    actual: set[str] = set()
    for root_text, directory_names, file_names in os.walk(
        payload,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        root = Path(root_text)
        for name in directory_names:
            metadata = (root / name).lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise BackupError("Extracted payload contains a symlink or special entry")
        for name in file_names:
            path = root / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupError("Extracted payload contains a symlink or special entry")
            actual.add(path.relative_to(payload.parent).as_posix())
    return actual


def _load_and_verify_manifest(extracted: Path) -> dict[str, object]:
    allowed_roots = {"manifest.json", "payload"}
    actual_roots = {path.name for path in extracted.iterdir()}
    if actual_roots != allowed_roots:
        raise BackupError("Archive contains missing or unexpected top-level entries")
    manifest_path = extracted / "manifest.json"
    payload = extracted / "payload"
    if not manifest_path.is_file() or not payload.is_dir():
        raise BackupError("Archive is missing its manifest or payload")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError("Backup manifest exceeds the size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"Backup manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("Backup manifest must be an object")
    if manifest.get("format") != FORMAT_NAME:
        raise BackupError("Backup manifest has an unsupported format")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BackupError("Backup manifest has an unsupported schema version")

    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise BackupError("Backup manifest files must be a list")
    expected: dict[str, tuple[int, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("Backup manifest contains an invalid file record")
        path = _manifest_relative_path(row.get("path"), prefix="payload/")
        size = row.get("size")
        digest = row.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise BackupError(f"Backup manifest metadata is invalid for {path}")
        if path in expected:
            raise BackupError(f"Backup manifest repeats file: {path}")
        expected[path] = (size, digest)

    actual = _actual_payload_files(payload)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise BackupError(
            "Backup payload file set does not match its manifest "
            f"(missing={missing[:3]}, extra={extra[:3]})"
        )
    for relative, (expected_size, expected_digest) in expected.items():
        path = extracted.joinpath(*PurePosixPath(relative).parts)
        if path.stat().st_size != expected_size:
            raise BackupError(f"Backup payload size mismatch: {relative}")
        if _sha256(path) != expected_digest:
            raise BackupError(f"Backup payload hash mismatch: {relative}")

    sqlite_files = manifest.get("sqlite_files")
    if not isinstance(sqlite_files, list):
        raise BackupError("Backup manifest sqlite_files must be a list")
    seen_sqlite: set[str] = set()
    for value in sqlite_files:
        relative = _manifest_relative_path(value, prefix="payload/sqlite/")
        if relative in seen_sqlite or relative not in expected:
            raise BackupError(f"Backup manifest has an invalid SQLite entry: {relative}")
        seen_sqlite.add(relative)
        _sqlite_quick_check(extracted.joinpath(*PurePosixPath(relative).parts))

    directory_roots = manifest.get("directory_roots")
    if not isinstance(directory_roots, list):
        raise BackupError("Backup manifest directory_roots must be a list")
    seen_roots: set[str] = set()
    for value in directory_roots:
        relative = _manifest_relative_path(value, prefix="payload/directories/")
        if relative in seen_roots:
            raise BackupError(f"Backup manifest repeats directory root: {relative}")
        seen_roots.add(relative)
        root = extracted.joinpath(*PurePosixPath(relative).parts)
        if not root.is_dir():
            raise BackupError(f"Backup directory root is missing: {relative}")
    return manifest


def _apply_archived_modes(
    stage: Path,
    modes: dict[str, int],
) -> None:
    payload_prefix = "payload/"
    entries: list[tuple[Path, int, bool]] = []
    for archived_path, mode in modes.items():
        if not archived_path.startswith(payload_prefix):
            continue
        relative = PurePosixPath(archived_path[len(payload_prefix) :])
        if not relative.parts:
            continue
        target = stage.joinpath(*relative.parts)
        if not target.exists():
            continue
        entries.append((target, mode & 0o777, target.is_dir()))
    for target, mode, is_directory in sorted(
        entries,
        key=lambda item: (item[2], -len(item[0].parts)),
    ):
        os.chmod(target, mode)


def _target_entries(target: Path) -> list[Path]:
    try:
        return list(target.iterdir())
    except OSError as exc:
        raise BackupError(f"Unable to inspect restore target: {exc}") from exc


def _validate_restore_target(target: Path, overwrite_target: bool) -> bool:
    if not os.path.lexists(target):
        return False
    if target.is_symlink() or not target.is_dir():
        raise BackupError("Restore target must be an absent or real directory")
    nonempty = bool(_target_entries(target))
    if nonempty and not overwrite_target:
        raise BackupError("Restore target is non-empty; refusing to overwrite it")
    return nonempty


def _restore_payload(
    payload: Path,
    target: Path,
    *,
    overwrite_target: bool,
    archived_modes: dict[str, int],
) -> Path | None:
    was_nonempty = _validate_restore_target(target, overwrite_target)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.restore-",
            dir=target.parent,
        )
    )
    os.chmod(stage, 0o700)
    previous: Path | None = None
    try:
        shutil.copytree(payload, stage, dirs_exist_ok=True, symlinks=False)
        _apply_archived_modes(stage, archived_modes)
        was_nonempty = _validate_restore_target(target, overwrite_target)
        if os.path.lexists(target):
            previous = target.parent / (
                f".{target.name}.pre-restore-{uuid.uuid4().hex}"
            )
            target.rename(previous)
        try:
            stage.rename(target)
        except OSError:
            if previous is not None and not os.path.lexists(target):
                previous.rename(target)
                previous = None
            raise
        if previous is not None and not was_nonempty:
            try:
                previous.rmdir()
            except OSError:
                pass
            else:
                previous = None
        _fsync_directory(target.parent)
        return previous
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"Unable to install restored payload: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore_backup(
    *,
    input_path: Path,
    target: Path,
    encrypted: bool = True,
    age_identity: Path | None = None,
    age_binary: str = "age",
    overwrite_target: bool = False,
    max_extract_bytes: int = DEFAULT_MAX_EXTRACT_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> dict[str, object]:
    if max_extract_bytes <= 0 or max_members <= 0:
        raise BackupError("Extraction limits must be positive")
    lexical_input = input_path.expanduser()
    if lexical_input.is_symlink() or not lexical_input.is_file():
        raise BackupError("Backup input must be an accessible regular file")
    input_path = lexical_input.resolve(strict=True)
    lexical_target = target.expanduser()
    if not lexical_target.name:
        raise BackupError("Restore target must name a directory")
    if lexical_target.is_symlink():
        raise BackupError("Restore target must not be a symlink")
    try:
        target_parent = lexical_target.parent.resolve(strict=True)
    except OSError as exc:
        raise BackupError("Restore target parent must already exist") from exc
    if not target_parent.is_dir():
        raise BackupError("Restore target parent must be a directory")
    target = target_parent / lexical_target.name

    resolved_age: str | None = None
    identity: Path | None = None
    if encrypted:
        raw_identity = (
            os.fspath(age_identity)
            if age_identity is not None
            else os.environ.get(AGE_IDENTITY_ENV)
        )
        if not raw_identity:
            raise BackupError(
                "Encrypted restore requires --age-identity or "
                f"{AGE_IDENTITY_ENV}"
            )
        identity = Path(raw_identity).expanduser()
        if identity.is_symlink() or not identity.is_file():
            raise BackupError("The configured age identity file is unavailable")
        identity = identity.resolve(strict=True)
        resolved_age = _require_program(age_binary)

    _validate_restore_target(target, overwrite_target)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.verify-",
        dir=target.parent,
    ) as temporary_text:
        temporary = Path(temporary_text)
        os.chmod(temporary, 0o700)
        plaintext = input_path
        if encrypted:
            assert resolved_age is not None
            assert identity is not None
            plaintext = temporary / "runtime-backup.tar.gz"
            _run_age(
                [
                    resolved_age,
                    "--decrypt",
                    "--identity",
                    os.fspath(identity),
                    "-o",
                    os.fspath(plaintext),
                    os.fspath(input_path),
                ],
                plaintext,
            )
        extracted = temporary / "extracted"
        archived_modes = _safe_extract(
            plaintext,
            extracted,
            max_extract_bytes=max_extract_bytes,
            max_members=max_members,
        )
        manifest = _load_and_verify_manifest(extracted)
        previous = _restore_payload(
            extracted / "payload",
            target,
            overwrite_target=overwrite_target,
            archived_modes=archived_modes,
        )

    result: dict[str, object] = {
        "operation": "restore",
        "target": os.fspath(target),
        "verified_file_count": len(manifest["files"]),
        "verified_sqlite_count": len(manifest["sqlite_files"]),
    }
    if previous is not None:
        result["previous_target"] = os.fspath(previous)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or restore verified runtime-state backups.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a runtime backup")
    create.add_argument(
        "--sqlite",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="SQLite database to back up online; may be repeated",
    )
    create.add_argument(
        "--directory",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="Runtime directory to archive; may be repeated",
    )
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--age-recipient")
    create.add_argument("--age-binary", default="age")
    create.add_argument(
        "--no-encrypt",
        action="store_true",
        help="Write plaintext (only for isolated tests or encrypted storage)",
    )

    restore = commands.add_parser("restore", help="Verify and restore a backup")
    restore.add_argument("--input", dest="input_path", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--age-identity", type=Path)
    restore.add_argument("--age-binary", default="age")
    restore.add_argument(
        "--no-encrypt",
        action="store_true",
        help="Read a plaintext archive",
    )
    restore.add_argument(
        "--overwrite-target",
        action="store_true",
        help="Replace a non-empty target while preserving its old tree",
    )
    restore.add_argument(
        "--max-extract-bytes",
        type=int,
        default=DEFAULT_MAX_EXTRACT_BYTES,
    )
    restore.add_argument(
        "--max-members",
        type=int,
        default=DEFAULT_MAX_MEMBERS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_backup(
                sqlite_arguments=arguments.sqlite,
                directory_arguments=arguments.directory,
                output=arguments.output,
                encrypt=not arguments.no_encrypt,
                age_recipient=arguments.age_recipient,
                age_binary=arguments.age_binary,
            )
        else:
            result = restore_backup(
                input_path=arguments.input_path,
                target=arguments.target,
                encrypted=not arguments.no_encrypt,
                age_identity=arguments.age_identity,
                age_binary=arguments.age_binary,
                overwrite_target=arguments.overwrite_target,
                max_extract_bytes=arguments.max_extract_bytes,
                max_members=arguments.max_members,
            )
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
