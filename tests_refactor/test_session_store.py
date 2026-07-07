from uk_rent_agent.web.session_store import SessionStore


def test_users_are_isolated():
    store = SessionStore()
    store.get("a").favorites["x"] = {"URL": "x"}
    assert store.get("b").favorites == {}


def test_lru_eviction():
    store = SessionStore(max_users=2)
    first = store.get("first")
    store.get("second")
    store.get("third")
    assert store.get("first") is not first


def test_ttl_expiry():
    now = [0.0]
    store = SessionStore(ttl_seconds=10, clock=lambda: now[0])
    first = store.get("a")
    now[0] = 10.0
    assert store.get("a") is not first
