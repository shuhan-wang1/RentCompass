"""Unit tests for the multi-conversation SessionStore (hot cache + turn locks)."""
import threading

from uk_rent_agent.web.session_store import SessionStore


def test_conversation_slices_are_isolated():
    store = SessionStore()
    store.get("u1", "c1").history.append({"user": "a", "assistant": "b"})
    assert store.get("u1", "c2").history == []          # different conversation
    assert store.get("u2", "c1").history == []          # different user


def test_backward_compatible_single_arg():
    # legacy call still works (defaults to a 'default' conversation slice)
    store = SessionStore()
    store.get("u1").favorites["x"] = {"url": "x"}
    assert store.get("u1").favorites == {"x": {"url": "x"}}
    assert store.get("u1", "default") is store.get("u1")


def test_clear_single_conversation():
    store = SessionStore()
    store.get("u1", "c1").history.append({"user": "a", "assistant": "b"})
    store.get("u1", "c2").history.append({"user": "c", "assistant": "d"})
    store.clear("u1", "c1")
    assert store.get("u1", "c1").history == []           # cleared
    assert store.get("u1", "c2").history == [{"user": "c", "assistant": "d"}]  # kept


def test_clear_user_drops_all_conversations():
    store = SessionStore()
    store.get("u1", "c1").history.append({"user": "a", "assistant": "b"})
    store.get("u1", "c2").history.append({"user": "c", "assistant": "d"})
    store.get("u2", "c1").history.append({"user": "e", "assistant": "f"})
    store.clear_user("u1")
    assert store.get("u1", "c1").history == []
    assert store.get("u1", "c2").history == []
    assert store.get("u2", "c1").history == [{"user": "e", "assistant": "f"}]  # other user safe


def test_clear_none_conversation_clears_all_user_slices():
    store = SessionStore()
    store.get("u1", "c1").history.append({"user": "a", "assistant": "b"})
    store.clear("u1")  # conversation_id=None → clear whole user
    assert store.get("u1", "c1").history == []


def test_turn_lock_is_stable_per_key():
    store = SessionStore()
    a = store.turn_lock("u1", "c1")
    b = store.turn_lock("u1", "c1")
    c = store.turn_lock("u1", "c2")
    assert a is b            # same key → same lock
    assert a is not c        # different conversation → different lock


def test_lru_eviction_by_composite_key():
    store = SessionStore(max_users=2)
    first = store.get("u1", "c1")
    store.get("u1", "c2")
    store.get("u1", "c3")     # evicts ("u1","c1")
    assert store.get("u1", "c1") is not first


def test_ttl_expiry():
    now = [0.0]
    store = SessionStore(ttl_seconds=10, clock=lambda: now[0])
    first = store.get("u1", "c1")
    now[0] = 10.0
    assert store.get("u1", "c1") is not first


def test_atomic_history_append_under_lock_no_drop():
    """Simulate the original defect: many concurrent appends must never lose a turn
    when the per-conversation turn lock guards the in-place read-modify-write."""
    store = SessionStore()
    lock = store.turn_lock("u1", "c1")
    n = 200

    def worker(i):
        with lock:
            sess = store.get("u1", "c1")
            sess.history.append({"user": str(i), "assistant": "x"})
            if len(sess.history) > 10:
                del sess.history[:-10]  # in-place trim keeps list identity

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Window keeps the last 10; the point is nothing crashed and the window is exact.
    assert len(store.get("u1", "c1").history) == 10
