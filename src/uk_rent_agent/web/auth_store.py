"""Local username/password credential store (framework-free, SQLite-backed).

Kept Flask-free so the hashing / validation / persistence rules can be unit-tested
without booting the heavy app (RAG index, LangGraph, MCP). app.py wraps this with the
Flask request/session context — see the /api/auth/* routes.

SECURITY
  - Passwords are NEVER stored in plaintext — only salted werkzeug PBKDF2 hashes.
  - The backing SQLite file is local-only, gitignored and transactionally shared by
    blue/green processes. It contains password hashes, never plaintext.
  - A legacy JSON file at the configured path is atomically migrated in place once;
    malformed legacy data fails closed instead of being treated as an empty database.

CONTRACT
  - username: 3–32 chars of [A-Za-z0-9_.-]; uniqueness is case-insensitive
    (the lowercased username is the storage key; original casing is preserved for display).
  - password: 6–128 chars.
  - Each account is minted a stable ``user_id`` (uuid4 hex) that satisfies the identity
    contract regex ([A-Za-z0-9_-]{1,64}), so it flows through the existing identity
    pipeline unchanged — conversations / favorites / long-term memory stay keyed by it
    across logins, independent of the (mutable) display name.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

# username: 3–32 chars of letters/digits/underscore/dot/hyphen (contract-fixed).
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
PASSWORD_MIN = 6
PASSWORD_MAX = 128
DISPLAY_NAME_MAX = 64

# PBKDF2 is chosen over scrypt for portability — it needs no OpenSSL scrypt support and
# is available in every hashlib build.
_HASH_METHOD = "pbkdf2:sha256"
# A well-formed hash of a value no real password will ever equal — used to burn a constant
# amount of CPU on a login for a nonexistent user, so response timing does not leak whether
# a username is registered.
_DUMMY_HASH = generate_password_hash("uk-rent-auth-dummy-password", method=_HASH_METHOD)


class AuthError(ValueError):
    """Base class for credential-store validation failures (register-time)."""


class InvalidUsername(AuthError):
    """Username missing or fails the contract regex."""


class WeakPassword(AuthError):
    """Password missing or outside the allowed length."""


class UsernameTaken(AuthError):
    """A user with this username (case-insensitive) already exists."""


class AuthStoreCorrupt(RuntimeError):
    """The credential database or legacy migration source is malformed."""


def valid_username(name) -> bool:
    return isinstance(name, str) and bool(USERNAME_RE.match(name))


def valid_password(pw) -> bool:
    return isinstance(pw, str) and PASSWORD_MIN <= len(pw) <= PASSWORD_MAX


class AuthStore:
    """Transactional local account database shared across application processes.

    Public dicts returned to callers never contain the password hash — only
    ``{username, user_id, display_name}``.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_json_if_needed()
        try:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS users (
                           username_key TEXT PRIMARY KEY,
                           username TEXT NOT NULL,
                           user_id TEXT NOT NULL UNIQUE,
                           display_name TEXT NOT NULL,
                           password_hash TEXT NOT NULL,
                           created_at INTEGER NOT NULL
                       )"""
                )
        except sqlite3.DatabaseError as exc:
            raise AuthStoreCorrupt(f"credential database is invalid: {self.path}") from exc

    # ------------------------------------------------------------------ persistence
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as handle:
            prefix = handle.read(16)
        if prefix == b"SQLite format 3\x00":
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            users = raw.get("users") if isinstance(raw, dict) else None
            if not isinstance(users, dict):
                raise ValueError("legacy users object is missing")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AuthStoreCorrupt(
                f"legacy credential file is corrupt and was not overwritten: {self.path}"
            ) from exc

        migration_path = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.migrating"
        )
        try:
            db = sqlite3.connect(migration_path)
            try:
                db.execute(
                    """CREATE TABLE users (
                           username_key TEXT PRIMARY KEY,
                           username TEXT NOT NULL,
                           user_id TEXT NOT NULL UNIQUE,
                           display_name TEXT NOT NULL,
                           password_hash TEXT NOT NULL,
                           created_at INTEGER NOT NULL
                       )"""
                )
                for key, record in users.items():
                    if not isinstance(record, dict) or not record.get("password_hash"):
                        raise ValueError("legacy user record is invalid")
                    username = str(record.get("username") or key)
                    user_id = str(record.get("user_id") or "")
                    if not valid_username(username) or not user_id:
                        raise ValueError("legacy user identity is invalid")
                    db.execute(
                        """INSERT INTO users
                           (username_key, username, user_id, display_name,
                            password_hash, created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            username.lower(), username, user_id,
                            str(record.get("display_name") or username)[:DISPLAY_NAME_MAX],
                            str(record["password_hash"]),
                            int(record.get("created_at") or 0),
                        ),
                    )
                db.commit()
            finally:
                db.close()
            os.replace(migration_path, self.path)
        except Exception as exc:
            try:
                migration_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AuthStoreCorrupt("legacy credential migration failed") from exc

    # ------------------------------------------------------------------ public view
    @staticmethod
    def public_view(record: dict) -> dict:
        """Strip the hash; return only what is safe to hand to a client."""
        return {
            "username": record.get("username"),
            "user_id": record.get("user_id"),
            "display_name": record.get("display_name") or record.get("username"),
        }

    # ------------------------------------------------------------------ queries
    def exists(self, username: str) -> bool:
        if not isinstance(username, str):
            return False
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM users WHERE username_key=?", (username.lower(),)
            ).fetchone()
        return row is not None

    def get(self, username: str) -> dict | None:
        if not isinstance(username, str):
            return None
        with self._connect() as db:
            rec = db.execute(
                "SELECT * FROM users WHERE username_key=?", (username.lower(),)
            ).fetchone()
        return self.public_view(dict(rec)) if rec else None

    # ------------------------------------------------------------------ mutations
    def register(self, username, password, display_name=None) -> dict:
        """Create a new account. Returns the public view.

        Raises InvalidUsername / WeakPassword / UsernameTaken on failure.
        """
        if not valid_username(username):
            raise InvalidUsername(
                "username must be 3–32 chars of letters, digits, '_', '.' or '-'"
            )
        if not valid_password(password):
            raise WeakPassword(
                f"password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters"
            )
        display = (display_name or username).strip()[:DISPLAY_NAME_MAX] or username

        record = {
            "username": username,
            "user_id": uuid.uuid4().hex,
            "display_name": display,
            "password_hash": generate_password_hash(password, method=_HASH_METHOD),
            "created_at": int(time.time()),
        }
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT INTO users
                       (username_key, username, user_id, display_name,
                        password_hash, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        username.lower(), record["username"], record["user_id"],
                        record["display_name"], record["password_hash"],
                        record["created_at"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise UsernameTaken("that username is already taken") from exc
        return self.public_view(record)

    def verify(self, username, password) -> dict | None:
        """Return the public view if (username, password) is valid, else None.

        Runs a constant dummy hash check for unknown usernames so login timing does not
        reveal whether an account exists.
        """
        rec = None
        if isinstance(username, str):
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM users WHERE username_key=?", (username.lower(),)
                ).fetchone()
            rec = dict(row) if row else None
        if rec is None:
            check_password_hash(_DUMMY_HASH, password if isinstance(password, str) else "")
            return None
        if not isinstance(password, str) or not check_password_hash(rec["password_hash"], password):
            return None
        return self.public_view(rec)

    def set_display_name(self, username, display_name) -> dict | None:
        """Update the display name of an existing account. Returns the public view or None."""
        display = (str(display_name).strip() if display_name is not None else "")[:DISPLAY_NAME_MAX]
        if not display:
            return None
        if not isinstance(username, str):
            return None
        with self._connect() as db:
            updated = db.execute(
                "UPDATE users SET display_name=? WHERE username_key=?",
                (display, username.lower()),
            )
            if updated.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM users WHERE username_key=?", (username.lower(),)
            ).fetchone()
        return self.public_view(dict(row))

    def privacy_inventory(self, user_id: str) -> dict[str, int]:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        count = int(row["n"] if row else 0)
        return {"credentials": count, "total": count}

    def delete_user_id(self, user_id: str) -> int:
        """Delete and verify the credential row for a GDPR erasure request."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM users WHERE user_id=?", (user_id,)
            ).rowcount
            residual = db.execute(
                "SELECT COUNT(*) FROM users WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            if residual:
                raise RuntimeError("credential erasure left residual rows")
        return int(deleted)
