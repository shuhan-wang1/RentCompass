"""Unit tests for the local credential store (framework-free; no Flask, no DeepSeek)."""
import json
import sqlite3
import threading

import pytest
from werkzeug.security import generate_password_hash

from uk_rent_agent.web.auth_store import (
    AuthStore, valid_username, valid_password,
    AuthStoreCorrupt, InvalidUsername, WeakPassword, UsernameTaken,
)
from uk_rent_agent.web.identity import valid_user_id


@pytest.fixture()
def store(tmp_path):
    return AuthStore(tmp_path / "users.sqlite3")


# --------------------------------------------------------------------- validators
@pytest.mark.parametrize("name", ["abc", "user_1", "a.b-c", "u" * 32, "Alice.99"])
def test_valid_usernames(name):
    assert valid_username(name)


@pytest.mark.parametrize("name", ["", "ab", "u" * 33, "a b", "a@b", "有效", None, 123])
def test_invalid_usernames(name):
    assert not valid_username(name)


@pytest.mark.parametrize("pw", ["123456", "p" * 128, "s3cr3t!"])
def test_valid_passwords(pw):
    assert valid_password(pw)


@pytest.mark.parametrize("pw", ["", "12345", "p" * 129, None, 123456])
def test_invalid_passwords(pw):
    assert not valid_password(pw)


# ------------------------------------------------------------------- registration
def test_register_returns_public_view(store):
    view = store.register("alice", "hunter2", display_name="Alice")
    assert view == {"username": "alice", "user_id": view["user_id"], "display_name": "Alice"}
    # the minted user_id must satisfy the identity contract so it flows through unchanged
    assert valid_user_id(view["user_id"])
    assert "password_hash" not in view


def test_display_name_defaults_to_username(store):
    view = store.register("bob", "hunter2")
    assert view["display_name"] == "bob"


def test_duplicate_username_rejected_case_insensitively(store):
    store.register("Carol", "hunter2")
    with pytest.raises(UsernameTaken):
        store.register("carol", "different")


def test_invalid_username_rejected(store):
    with pytest.raises(InvalidUsername):
        store.register("a b", "hunter2")


def test_weak_password_rejected(store):
    with pytest.raises(WeakPassword):
        store.register("dave", "123")


# ------------------------------------------------------------------- verification
def test_verify_correct_password(store):
    reg = store.register("erin", "hunter2", display_name="Erin")
    view = store.verify("erin", "hunter2")
    assert view is not None
    assert view["user_id"] == reg["user_id"]
    assert view["display_name"] == "Erin"


def test_verify_is_case_insensitive_on_username(store):
    store.register("Frank", "hunter2")
    assert store.verify("frank", "hunter2") is not None


def test_verify_wrong_password_returns_none(store):
    store.register("grace", "hunter2")
    assert store.verify("grace", "wrong") is None


def test_verify_unknown_user_returns_none(store):
    assert store.verify("nobody", "whatever") is None


def test_verify_non_string_inputs_return_none(store):
    store.register("heidi", "hunter2")
    assert store.verify("heidi", None) is None
    assert store.verify(None, "hunter2") is None


# ---------------------------------------------------------------------- persistence
def test_password_is_never_stored_in_plaintext(store, tmp_path):
    store.register("ivan", "sup3rSecret", display_name="Ivan")
    assert b"sup3rSecret" not in (tmp_path / "users.sqlite3").read_bytes()
    with sqlite3.connect(tmp_path / "users.sqlite3") as db:
        password_hash = db.execute(
            "SELECT password_hash FROM users WHERE username_key='ivan'"
        ).fetchone()[0]
    assert password_hash and password_hash != "sup3rSecret"
    assert password_hash.startswith("pbkdf2:")


def test_accounts_survive_reload(tmp_path):
    path = tmp_path / "users.sqlite3"
    s1 = AuthStore(path)
    reg = s1.register("judy", "hunter2", display_name="Judy")
    # A fresh instance pointed at the same file re-reads the account.
    s2 = AuthStore(path)
    view = s2.verify("judy", "hunter2")
    assert view is not None
    assert view["user_id"] == reg["user_id"]
    assert view["display_name"] == "Judy"


def test_user_ids_are_unique_and_stable(store):
    a = store.register("mallory", "hunter2")
    b = store.register("oscar", "hunter2")
    assert a["user_id"] != b["user_id"]
    # verify returns the SAME id on subsequent logins
    assert store.verify("mallory", "hunter2")["user_id"] == a["user_id"]


def test_corrupt_file_fails_closed_and_is_not_overwritten(tmp_path):
    path = tmp_path / "users.sqlite3"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(AuthStoreCorrupt):
        AuthStore(path)
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_legacy_json_is_atomically_migrated_in_place(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({
        "version": 1,
        "users": {
            "alice": {
                "username": "Alice",
                "user_id": "legacy-user-1",
                "display_name": "Alice",
                "password_hash": generate_password_hash(
                    "hunter2", method="pbkdf2:sha256"
                ),
                "created_at": 123,
            }
        },
    }), encoding="utf-8")

    migrated = AuthStore(path)
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert migrated.verify("alice", "hunter2")["user_id"] == "legacy-user-1"


def test_two_process_instances_do_not_lose_concurrent_registrations(tmp_path):
    path = tmp_path / "users.sqlite3"
    stores = [AuthStore(path), AuthStore(path)]
    barrier = threading.Barrier(20)
    errors = []

    def register(index):
        try:
            barrier.wait()
            stores[index % 2].register(f"user{index:02d}", "hunter2")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    fresh = AuthStore(path)
    assert all(fresh.exists(f"user{i:02d}") for i in range(20))


def test_privacy_erasure_deletes_and_verifies_credentials(store):
    user = store.register("eraseme", "hunter2")
    assert store.privacy_inventory(user["user_id"])["total"] == 1
    assert store.delete_user_id(user["user_id"]) == 1
    assert store.privacy_inventory(user["user_id"])["total"] == 0
    assert store.verify("eraseme", "hunter2") is None


def test_set_display_name(store):
    store.register("peggy", "hunter2", display_name="Peggy")
    updated = store.set_display_name("peggy", "Peg")
    assert updated["display_name"] == "Peg"
    assert store.verify("peggy", "hunter2")["display_name"] == "Peg"
