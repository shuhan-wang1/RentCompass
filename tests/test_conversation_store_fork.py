"""Tests for the session-fork storage layer (migration, turns, snapshots, lineage,
fork_conversation, delete cascades). No DeepSeek/agent involved."""
import sqlite3
import threading

import pytest

from uk_rent_agent.web.conversation_store import (
    ConversationStore,
    ConversationNotFound,
    NoCompletedTurn,
    TurnNotFound,
    TurnNotInConversation,
    TurnNotCompleted,
)


@pytest.fixture()
def store(tmp_path):
    s = ConversationStore(tmp_path / "conv.sqlite3")
    yield s
    s.close()


def _run_turn(store, user_id, cid, user_text, asst_text, snapshot=None,
              recommendations=None):
    """Persist a user+assistant message wrapped in a completed turn (mirrors the
    live app lifecycle). Returns (turn_dict, user_msg_id, asst_msg_id)."""
    u = store.add_message(user_id, cid, "user", user_text)
    turn = store.begin_turn(user_id, cid, user_message_id=u["id"])
    a = store.add_message(user_id, cid, "assistant", asst_text, turn_id=turn["id"],
                          recommendations=recommendations)
    turn = store.complete_turn(user_id, turn["id"], assistant_message_id=a["id"])
    if snapshot is not None:
        store.save_turn_snapshot(user_id, cid, turn["id"], snapshot)
    return turn, u["id"], a["id"]


# --------------------------------------------------------------------- migration
def test_migration_upgrades_old_db_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversations (
            user_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, id));
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            response_type TEXT, recommendations_json TEXT, timestamp TEXT NOT NULL);
        CREATE TABLE favorites (
            user_id TEXT NOT NULL, url TEXT NOT NULL, property_json TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (user_id, url));
        """
    )
    conn.execute("INSERT INTO conversations VALUES('u1','c1','old title','2020-01-01','2020-01-02')")
    conn.execute(
        "INSERT INTO messages(user_id,conversation_id,role,content,timestamp) "
        "VALUES('u1','c1','user','hi there','2020-01-01')"
    )
    conn.commit()
    conn.close()

    s = ConversationStore(path)
    try:
        conv = s.get_conversation("u1", "c1")
        assert conv is not None
        assert conv["title"] == "old title"
        # new lineage columns exist with sensible defaults; root backfilled = id
        assert conv["parent_conversation_id"] is None
        assert conv["forked_from_turn_id"] is None
        assert conv["root_conversation_id"] == "c1"
        assert conv["branch_depth"] == 0
        # old message readable, turn_id column present + None for legacy row
        msgs = s.get_messages("u1", "c1")
        assert msgs[0]["content"] == "hi there"
        assert msgs[0]["turn_id"] is None
        assert isinstance(msgs[0]["id"], int)
        # new tables usable
        assert s.list_turns("u1", "c1") == []
    finally:
        s.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.sqlite3"
    ConversationStore(path).close()
    # reopening runs _migrate again → no error, columns intact
    s = ConversationStore(path)
    try:
        conv = s.create_conversation("u1", title="x")
        assert conv["root_conversation_id"] == conv["id"]
    finally:
        s.close()


# ------------------------------------------------------------------ add_message
def test_add_message_returns_id_and_timestamp(store):
    conv = store.create_conversation("u1")
    res = store.add_message("u1", conv["id"], "user", "hello")
    assert isinstance(res, dict)
    assert isinstance(res["id"], int)
    assert isinstance(res["timestamp"], str)


def test_conversation_dict_has_lineage_fields(store):
    conv = store.create_conversation("u1", title="root")
    for key in ("parent_conversation_id", "forked_from_turn_id",
                "root_conversation_id", "branch_depth"):
        assert key in conv
    assert conv["root_conversation_id"] == conv["id"]
    assert conv["branch_depth"] == 0
    # also flows through list_conversations
    listed = store.list_conversations("u1")[0]
    assert listed["root_conversation_id"] == conv["id"]


# ----------------------------------------------------------------- turn lifecycle
def test_turn_begin_complete(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    u = store.add_message("u1", cid, "user", "q")
    turn = store.begin_turn("u1", cid, request_id="r1", user_message_id=u["id"])
    assert turn["status"] == "running"
    assert turn["completed_at"] is None
    a = store.add_message("u1", cid, "assistant", "a", turn_id=turn["id"])
    done = store.complete_turn("u1", turn["id"], assistant_message_id=a["id"])
    assert done["status"] == "completed"
    assert done["completed_at"] is not None
    assert done["assistant_message_id"] == a["id"]
    assert store.get_turn("u1", turn["id"])["status"] == "completed"


def test_complete_turn_missing_returns_none(store):
    assert store.complete_turn("u1", "nonexistent") is None


def test_fail_turn(store):
    conv = store.create_conversation("u1")
    turn = store.begin_turn("u1", conv["id"])
    assert store.fail_turn("u1", turn["id"]) is None
    t = store.get_turn("u1", turn["id"])
    assert t["status"] == "failed"
    assert t["completed_at"] is not None


def test_latest_completed_turn_skips_running_and_failed(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1")
    # a running turn (not completed)
    store.begin_turn("u1", cid)
    # a failed turn
    ft = store.begin_turn("u1", cid)
    store.fail_turn("u1", ft["id"])
    latest = store.latest_completed_turn("u1", cid)
    assert latest["id"] == t1["id"]


def test_list_turns_ordered_by_started_at(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1")
    t2, _, _ = _run_turn(store, "u1", cid, "q2", "a2")
    ids = [t["id"] for t in store.list_turns("u1", cid)]
    assert ids == [t1["id"], t2["id"]]


# --------------------------------------------------------------------- snapshots
def test_snapshot_save_get_latest(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1",
                         snapshot={"turn_id": "ignored", "user_preferences": {"a": 1}})
    got = store.get_turn_snapshot("u1", t1["id"])
    assert got["user_preferences"] == {"a": 1}
    # latest snapshot = latest completed turn that has one
    t2, _, _ = _run_turn(store, "u1", cid, "q2", "a2",
                         snapshot={"turn_id": t1["id"], "user_preferences": {"b": 2}})
    latest = store.latest_snapshot("u1", cid)
    assert latest["user_preferences"] == {"b": 2}


def test_latest_snapshot_none_when_absent(store):
    conv = store.create_conversation("u1")
    _run_turn(store, "u1", conv["id"], "q", "a")  # completed turn, no snapshot
    assert store.latest_snapshot("u1", conv["id"]) is None


# -------------------------------------------------------------------------- fork
def _build_four_turns(store, user_id="u1"):
    conv = store.create_conversation(user_id, title="source")
    cid = conv["id"]
    t1, _, a1 = _run_turn(store, user_id, cid, "q1", "a1", snapshot={"turn_id": "x", "n": 1})
    t2, _, a2 = _run_turn(store, user_id, cid, "q2", "a2", snapshot={"turn_id": "x", "n": 2})
    t3, _, a3 = _run_turn(store, user_id, cid, "q3", "a3", snapshot={"turn_id": "x", "n": 3})
    t4, _, a4 = _run_turn(store, user_id, cid, "q4", "a4", snapshot={"turn_id": "x", "n": 4})
    return cid, [t1, t2, t3, t4], [a1, a2, a3, a4]


def test_fork_copies_subset_and_remaps(store):
    cid, turns, _ = _build_four_turns(store)
    t3 = turns[2]
    child = store.fork_conversation("u1", cid, after_turn_id=t3["id"])

    # lineage fields
    assert child["parent_conversation_id"] == cid
    assert child["forked_from_turn_id"] == t3["id"]
    assert child["root_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["idempotent"] is False

    child_cid = child["id"]
    # messages: T1..T3 copied (6 msgs), T4 excluded
    msgs = store.get_messages("u1", child_cid)
    contents = [m["content"] for m in msgs]
    assert contents == ["q1", "a1", "q2", "a2", "q3", "a3"]
    assert "q4" not in contents and "a4" not in contents

    # turns copied (3 completed), fresh ids, remapped message ids point at copied msgs
    child_turns = store.list_turns("u1", child_cid)
    assert len(child_turns) == 3
    old_ids = {t["id"] for t in turns[:3]}
    assert all(ct["id"] not in old_ids for ct in child_turns)
    child_msg_ids = {m["id"] for m in msgs}
    for ct in child_turns:
        assert ct["assistant_message_id"] in child_msg_ids
        assert ct["user_message_id"] in child_msg_ids
        assert ct["status"] == "completed"

    # copied assistant messages carry the new (remapped) turn_id
    asst_turn_ids = {m["turn_id"] for m in msgs if m["role"] == "assistant"}
    assert None not in asst_turn_ids
    assert asst_turn_ids <= {ct["id"] for ct in child_turns}

    # snapshots copied for every copied turn, embedded turn_id rewritten to new id
    for ct in child_turns:
        snap = store.get_turn_snapshot("u1", ct["id"])
        assert snap is not None
        assert snap["turn_id"] == ct["id"]


def test_fork_latest_when_no_turn_given(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid)  # defaults to latest completed = T4
    assert child["forked_from_turn_id"] == turns[3]["id"]
    contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert contents == ["q1", "a1", "q2", "a2", "q3", "a3", "q4", "a4"]


def test_fork_default_and_custom_title(store):
    cid, turns, _ = _build_four_turns(store)
    default_child = store.fork_conversation("u1", cid, after_turn_id=turns[0]["id"])
    assert default_child["title"] == "source (branch)"
    named = store.fork_conversation("u1", cid, after_turn_id=turns[0]["id"],
                                    title="  My Branch  ")
    assert named["title"] == "My Branch"


def test_fork_of_fork_increments_depth_and_keeps_root(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"])
    child_turns = store.list_turns("u1", child["id"])
    grand = store.fork_conversation("u1", child["id"],
                                    after_turn_id=child_turns[-1]["id"])
    assert grand["branch_depth"] == 2
    assert grand["root_conversation_id"] == cid
    assert grand["parent_conversation_id"] == child["id"]


def test_fork_is_independent_of_parent(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"])
    # add a new turn to the parent → child unaffected
    _run_turn(store, "u1", cid, "q5", "a5")
    child_contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert "q5" not in child_contents
    # add a turn to the child → parent unaffected
    _run_turn(store, "u1", child["id"], "cq", "ca")
    parent_contents = [m["content"] for m in store.get_messages("u1", cid)]
    assert "cq" not in parent_contents


# -------------------------------------------- fork turn-membership correctness
def test_fork_excludes_interleaved_half_turn(store):
    """Concurrent same-conversation requests interleave rowids
    (user1, user2, assistant1). Forking at turn1 must copy ONLY user1+assistant1,
    never user2 (an in-flight half-turn)."""
    conv = store.create_conversation("u1", title="src")
    cid = conv["id"]
    u1 = store.add_message("u1", cid, "user", "user1")
    t1 = store.begin_turn("u1", cid, user_message_id=u1["id"])
    u2 = store.add_message("u1", cid, "user", "user2")
    t2 = store.begin_turn("u1", cid, user_message_id=u2["id"])
    a1 = store.add_message("u1", cid, "assistant", "assistant1", turn_id=t1["id"])
    t1 = store.complete_turn("u1", t1["id"], assistant_message_id=a1["id"])

    child = store.fork_conversation("u1", cid, after_turn_id=t1["id"])
    contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert contents == ["user1", "assistant1"]
    # child hot history pairs correctly (no cross-turn contamination)
    assert store.rehydrate_history("u1", child["id"]) == [
        {"user": "user1", "assistant": "assistant1"}]

    # once t2 completes, forking the parent at t2 inherits all four messages
    a2 = store.add_message("u1", cid, "assistant", "assistant2", turn_id=t2["id"])
    store.complete_turn("u1", t2["id"], assistant_message_id=a2["id"])
    child2 = store.fork_conversation("u1", cid, after_turn_id=t2["id"])
    contents2 = [m["content"] for m in store.get_messages("u1", child2["id"])]
    assert len(contents2) == 4
    assert set(contents2) == {"user1", "user2", "assistant1", "assistant2"}


def test_fork_excludes_failed_turn(store):
    """A failed turn's user + assistant(error) rows must not appear in a child
    forked at a later completed turn."""
    conv = store.create_conversation("u1", title="src")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1")
    # a failed turn: user row + tagged assistant error row, then fail_turn
    uf = store.add_message("u1", cid, "user", "qfail")
    tf = store.begin_turn("u1", cid, user_message_id=uf["id"])
    store.add_message("u1", cid, "assistant", "error reply", turn_id=tf["id"])
    store.fail_turn("u1", tf["id"])
    # a later good turn
    t2, _, _ = _run_turn(store, "u1", cid, "q2", "a2")

    child = store.fork_conversation("u1", cid, after_turn_id=t2["id"])
    contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert contents == ["q1", "a1", "q2", "a2"]
    assert "qfail" not in contents and "error reply" not in contents


def test_fork_preserves_legacy_rows(store):
    """Rows that predate the turns feature (no turn references them at all) are
    copied as legacy, alongside a subsequent completed turn's pair."""
    conv = store.create_conversation("u1", title="src")
    cid = conv["id"]
    # legacy transcript: messages with no turn rows (turn_id NULL, not in any turn)
    store.add_message("u1", cid, "user", "legacy q")
    store.add_message("u1", cid, "assistant", "legacy a")
    # then the turns feature kicks in
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1")

    child = store.fork_conversation("u1", cid, after_turn_id=t1["id"])
    contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert contents == ["legacy q", "legacy a", "q1", "a1"]
    # legacy rows carry NULL turn_id; the turn pair's assistant carries a remapped id
    msgs = store.get_messages("u1", child["id"])
    assert msgs[0]["turn_id"] is None and msgs[1]["turn_id"] is None
    child_turns = store.list_turns("u1", child["id"])
    assert len(child_turns) == 1
    assert msgs[3]["turn_id"] == child_turns[0]["id"]


# ------------------------------------------------------------- fork validation
def test_fork_conversation_not_found(store):
    with pytest.raises(ConversationNotFound):
        store.fork_conversation("u1", "does-not-exist")


def test_fork_no_completed_turn(store):
    conv = store.create_conversation("u1")
    store.begin_turn("u1", conv["id"])  # running only
    with pytest.raises(NoCompletedTurn):
        store.fork_conversation("u1", conv["id"])


def test_fork_turn_not_found(store):
    cid, _, _ = _build_four_turns(store)
    with pytest.raises(TurnNotFound):
        store.fork_conversation("u1", cid, after_turn_id="nope")


def test_fork_turn_not_in_conversation(store):
    cid, turns, _ = _build_four_turns(store)
    other = store.create_conversation("u1", title="other")
    other_turn, _, _ = _run_turn(store, "u1", other["id"], "x", "y")
    with pytest.raises(TurnNotInConversation):
        store.fork_conversation("u1", cid, after_turn_id=other_turn["id"])


def test_fork_turn_not_completed(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    _run_turn(store, "u1", cid, "q1", "a1")  # ensure a completed turn exists too
    running = store.begin_turn("u1", cid)
    with pytest.raises(TurnNotCompleted):
        store.fork_conversation("u1", cid, after_turn_id=running["id"])


def test_fork_foreign_user_cannot_fork(store):
    cid, _, _ = _build_four_turns(store, user_id="u1")
    with pytest.raises(ConversationNotFound):
        store.fork_conversation("u2", cid)


# ---------------------------------------------------------------- idempotency
def test_idempotency_same_key_one_child(store):
    cid, turns, _ = _build_four_turns(store)
    c1 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="k1")
    c2 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="k1")
    assert c1["id"] == c2["id"]
    assert c1["idempotent"] is False
    assert c2["idempotent"] is True
    # exactly one branch child exists
    branches = [c for c in store.list_conversations("u1")
                if c["parent_conversation_id"] == cid]
    assert len(branches) == 1


def test_idempotency_different_keys_two_children(store):
    cid, turns, _ = _build_four_turns(store)
    c1 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="ka")
    c2 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="kb")
    assert c1["id"] != c2["id"]
    branches = [c for c in store.list_conversations("u1")
                if c["parent_conversation_id"] == cid]
    assert len(branches) == 2


def test_idempotency_stale_key_recreates(store):
    cid, turns, _ = _build_four_turns(store)
    c1 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="k")
    store.delete_conversation("u1", c1["id"])  # recorded child now gone
    c2 = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"],
                                 idempotency_key="k")
    assert c2["id"] != c1["id"]
    assert c2["idempotent"] is False


# ------------------------------------------------------------------ concurrency
def test_concurrent_same_key_one_child(store):
    cid, turns, _ = _build_four_turns(store)
    results = {}
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait()
        results[name] = store.fork_conversation(
            "u1", cid, after_turn_id=turns[2]["id"], idempotency_key="shared")

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {r["id"] for r in results.values()}
    assert len(ids) == 1
    branches = [c for c in store.list_conversations("u1")
                if c["parent_conversation_id"] == cid]
    assert len(branches) == 1


def test_concurrent_different_keys_two_children(store):
    cid, turns, _ = _build_four_turns(store)
    results = {}
    barrier = threading.Barrier(2)

    def worker(name, key):
        barrier.wait()
        results[name] = store.fork_conversation(
            "u1", cid, after_turn_id=turns[2]["id"], idempotency_key=key)

    threads = [threading.Thread(target=worker, args=(f"t{i}", f"key{i}"))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {r["id"] for r in results.values()}
    assert len(ids) == 2
    # each child internally consistent: 6 messages, 3 turns
    for cinfo in results.values():
        assert len(store.get_messages("u1", cinfo["id"])) == 6
        assert len(store.list_turns("u1", cinfo["id"])) == 3


# --------------------------------------------------------------------- lineage
def test_branch_lineage_three_generations(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"])
    child_turns = store.list_turns("u1", child["id"])
    grand = store.fork_conversation("u1", child["id"],
                                    after_turn_id=child_turns[-1]["id"])

    lineage = store.get_branch_lineage("u1", grand["id"])
    assert [e["conversation_id"] for e in lineage] == [grand["id"], child["id"], cid]
    assert lineage[0]["before"] is None
    # child entry cutoff = started_at of grand's fork turn (a turn in child)
    grand_fork_turn = store.get_turn("u1", grand["forked_from_turn_id"])
    assert lineage[1]["before"] == grand_fork_turn["started_at"]
    # source entry cutoff = started_at of child's fork turn (turns[2], in source)
    assert lineage[2]["before"] == turns[2]["started_at"]


def test_branch_lineage_deleted_fork_turn_fallback(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"])
    # wipe the source's turns but keep the source conversation row
    store.clear_conversation_messages("u1", cid)
    lineage = store.get_branch_lineage("u1", child["id"])
    assert [e["conversation_id"] for e in lineage] == [child["id"], cid]
    # fork turn missing → fall back to the child row's created_at
    child_row = store.get_conversation("u1", child["id"])
    assert lineage[1]["before"] == child_row["created_at"]


def test_branch_lineage_missing_parent_terminates(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.fork_conversation("u1", cid, after_turn_id=turns[2]["id"])
    store.delete_conversation("u1", cid)  # parent row gone
    lineage = store.get_branch_lineage("u1", child["id"])
    assert [e["conversation_id"] for e in lineage] == [child["id"]]


# ------------------------------------------------ branch_for_edit (message edit)
def test_edit_inherits_strictly_before_turn(store):
    cid, turns, _ = _build_four_turns(store)
    # Edit T3 → inherit T1+T2 only (strictly before), NOT T3 or T4.
    child = store.branch_for_edit("u1", cid, turns[2]["id"])
    assert child["parent_conversation_id"] == cid
    assert child["root_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["fork_reason"] == "edit"
    assert child["edited_slot_turn_id"] == turns[2]["id"]
    # forked_from = last inherited turn (T2), the inclusive lineage cutoff.
    assert child["forked_from_turn_id"] == turns[1]["id"]
    contents = [m["content"] for m in store.get_messages("u1", child["id"])]
    assert contents == ["q1", "a1", "q2", "a2"]
    assert len(store.list_turns("u1", child["id"])) == 2


def test_edit_first_turn_zero_inheritance(store):
    cid, turns, _ = _build_four_turns(store)
    child = store.branch_for_edit("u1", cid, turns[0]["id"])
    assert child["parent_conversation_id"] == cid
    assert child["branch_depth"] == 1
    assert child["forked_from_turn_id"] is None      # nothing inherited
    assert child["edited_slot_turn_id"] == turns[0]["id"]
    assert store.get_messages("u1", child["id"]) == []
    assert store.list_turns("u1", child["id"]) == []
    # Lineage exposes NO ancestor context for a zero-inheritance branch.
    lineage = store.get_branch_lineage("u1", child["id"])
    assert [e["conversation_id"] for e in lineage] == [child["id"]]


def test_edit_of_failed_turn_allowed(store):
    """The edited turn's status is irrelevant — only completed turns before it are inherited."""
    conv = store.create_conversation("u1", title="src")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1")
    uf = store.add_message("u1", cid, "user", "qfail")
    tf = store.begin_turn("u1", cid, user_message_id=uf["id"])
    store.fail_turn("u1", tf["id"])
    child = store.branch_for_edit("u1", cid, tf["id"])
    assert child["edited_slot_turn_id"] == tf["id"]
    assert [m["content"] for m in store.get_messages("u1", child["id"])] == ["q1", "a1"]


def test_edit_unknown_and_foreign_turn(store):
    cid, turns, _ = _build_four_turns(store)
    with pytest.raises(TurnNotFound):
        store.branch_for_edit("u1", cid, "nope")
    other = store.create_conversation("u1", title="other")
    ot, _, _ = _run_turn(store, "u1", other["id"], "x", "y")
    with pytest.raises(TurnNotInConversation):
        store.branch_for_edit("u1", cid, ot["id"])
    with pytest.raises(ConversationNotFound):
        store.branch_for_edit("u1", "does-not-exist", turns[0]["id"])


def test_edit_idempotency(store):
    cid, turns, _ = _build_four_turns(store)
    c1 = store.branch_for_edit("u1", cid, turns[1]["id"], idempotency_key="e1")
    c2 = store.branch_for_edit("u1", cid, turns[1]["id"], idempotency_key="e1")
    assert c1["id"] == c2["id"]
    assert c1["idempotent"] is False and c2["idempotent"] is True


# -------------------------------------------------------------------- version_map
def test_version_map_transitivity_and_shape(store):
    cid, turns, _ = _build_four_turns(store)
    t2 = turns[1]["id"]
    # First edit of slot t2 → branch b, then give b a fresh turn at that same slot.
    b = store.branch_for_edit("u1", cid, t2)
    b_turn, _, _ = _run_turn(store, "u1", b["id"], "q2-v2", "a2-v2")
    # Editing b's own resent slot turn → branch c, same group by transitivity.
    c = store.branch_for_edit("u1", b["id"], b_turn["id"])
    assert c["edited_slot_turn_id"] == t2   # inherited family-stable slot key

    vm = store.version_map("u1", cid)["version_groups"]
    assert list(vm.keys()) == [t2]
    ids = [m["conversation_id"] for m in vm[t2]]
    assert ids == [cid, b["id"], c["id"]]   # created_at ASC: original, edit1, edit2
    for m in vm[t2]:
        assert set(m) == {"conversation_id", "created_at", "title"}
    # Whole family shares one map regardless of which member we query.
    assert store.version_map("u1", c["id"]) == store.version_map("u1", cid)


def test_version_map_distinct_slots_and_empty(store):
    cid, turns, _ = _build_four_turns(store)
    assert store.version_map("u1", cid)["version_groups"] == {}   # no edits yet
    b = store.branch_for_edit("u1", cid, turns[1]["id"])
    d = store.branch_for_edit("u1", cid, turns[2]["id"])
    vm = store.version_map("u1", cid)["version_groups"]
    assert set(vm) == {turns[1]["id"], turns[2]["id"]}
    assert [m["conversation_id"] for m in vm[turns[1]["id"]]] == [cid, b["id"]]
    assert [m["conversation_id"] for m in vm[turns[2]["id"]]] == [cid, d["id"]]


def test_version_map_unknown_conversation_returns_none(store):
    assert store.version_map("u1", "does-not-exist") is None


# --------------------------------------------------------------- delete cascades
def test_delete_conversation_cascades_turns_and_snapshots(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1", snapshot={"turn_id": "x"})
    assert store.list_turns("u1", cid) and store.get_turn_snapshot("u1", t1["id"])
    store.delete_conversation("u1", cid)
    assert store.list_turns("u1", cid) == []
    assert store.get_turn_snapshot("u1", t1["id"]) is None
    assert store.get_messages("u1", cid) == []


def test_clear_messages_cascades_turns_and_snapshots(store):
    conv = store.create_conversation("u1", title="keep")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1", snapshot={"turn_id": "x"})
    store.clear_conversation_messages("u1", cid)
    assert store.list_turns("u1", cid) == []
    assert store.get_turn_snapshot("u1", t1["id"]) is None
    assert store.get_conversation("u1", cid)["title"] == "keep"  # row survives


def test_delete_all_conversations_cascades(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    t1, _, _ = _run_turn(store, "u1", cid, "q1", "a1", snapshot={"turn_id": "x"})
    store.delete_all_conversations("u1")
    assert store.list_turns("u1", cid) == []
    assert store.get_turn_snapshot("u1", t1["id"]) is None
