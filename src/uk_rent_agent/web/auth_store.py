"""Local username/password credential store (framework-free, JSON-backed).

Kept Flask-free so the hashing / validation / persistence rules can be unit-tested
without booting the heavy app (RAG index, LangGraph, MCP). app.py wraps this with the
Flask request/session context — see the /api/auth/* routes.

SECURITY
  - Passwords are NEVER stored in plaintext — only salted werkzeug PBKDF2 hashes.
  - The backing JSON file (default .runtime/users.json) is *local only* by design and is
    gitignored. It contains password *hashes*, not passwords, but must never be committed.
  - The store is thread-safe (an internal re-entrant lock) and writes atomically
    (temp file + os.replace) so a crash mid-write cannot corrupt the account database.

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
import threading
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


def valid_username(name) -> bool:
    return isinstance(name, str) and bool(USERNAME_RE.match(name))


def valid_password(pw) -> bool:
    return isinstance(pw, str) and PASSWORD_MIN <= len(pw) <= PASSWORD_MAX


class AuthStore:
    """Persistent local account database backed by a single JSON file.

    Public dicts returned to callers never contain the password hash — only
    ``{username, user_id, display_name}``.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        with self._lock:
            self._users = {}
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                users = raw.get("users", {}) if isinstance(raw, dict) else {}
                if isinstance(users, dict):
                    # Re-key defensively on the lowercased username so a hand-edited file
                    # can't smuggle in duplicate-by-case keys.
                    for key, rec in users.items():
                        if isinstance(rec, dict) and rec.get("password_hash"):
                            self._users[str(key).lower()] = rec
            except (json.JSONDecodeError, OSError, ValueError):
                # A corrupt file must not crash startup; treat as empty. It is never
                # overwritten until the next successful register, which preserves the
                # bad file for manual inspection.
                self._users = {}

    def _atomic_write(self) -> None:
        """Serialize the in-memory table to disk atomically (temp + os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "users": self._users}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

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
        with self._lock:
            return isinstance(username, str) and username.lower() in self._users

    def get(self, username: str) -> dict | None:
        with self._lock:
            if not isinstance(username, str):
                return None
            rec = self._users.get(username.lower())
            return self.public_view(rec) if rec else None

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

        with self._lock:
            key = username.lower()
            if key in self._users:
                raise UsernameTaken("that username is already taken")
            record = {
                "username": username,
                "user_id": uuid.uuid4().hex,  # stable, contract-valid identity
                "display_name": display,
                "password_hash": generate_password_hash(password, method=_HASH_METHOD),
                "created_at": int(time.time()),
            }
            self._users[key] = record
            self._atomic_write()
            return self.public_view(record)

    def verify(self, username, password) -> dict | None:
        """Return the public view if (username, password) is valid, else None.

        Runs a constant dummy hash check for unknown usernames so login timing does not
        reveal whether an account exists.
        """
        with self._lock:
            rec = self._users.get(username.lower()) if isinstance(username, str) else None
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
        with self._lock:
            rec = self._users.get(username.lower()) if isinstance(username, str) else None
            if rec is None:
                return None
            rec["display_name"] = display
            self._atomic_write()
            return self.public_view(rec)
