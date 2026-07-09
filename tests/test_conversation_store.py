"""Unit tests for the durable ConversationStore (sqlite; no DeepSeek/agent involved)."""
import pytest

from uk_rent_agent.web.conversation_store import ConversationStore


@pytest.fixture()
def store(tmp_path):
    s = ConversationStore(tmp_path / "conv.sqlite3")
    yield s
    s.close()


# --------------------------------------------------------------- conversation CRUD
def test_create_and_get(store):
    conv = store.create_conversation("u1", title="My Chat")
    assert conv["title"] == "My Chat"
    assert conv["message_count"] == 0
    assert len(conv["id"]) == 32  # uuid4 hex

    fetched = store.get_conversation("u1", conv["id"])
    assert fetched["id"] == conv["id"]
    assert fetched["title"] == "My Chat"


def test_default_title(store):
    conv = store.create_conversation("u1")
    assert conv["title"] == "New chat"


def test_list_sorted_by_updated_desc(store):
    a = store.create_conversation("u1", title="A")
    b = store.create_conversation("u1", title="B")
    # touch A so it becomes most-recently-updated
    store.add_message("u1", a["id"], "user", "hi")
    ids = [c["id"] for c in store.list_conversations("u1")]
    assert ids[0] == a["id"] and b["id"] in ids


def test_user_scoping_isolates_conversations(store):
    a = store.create_conversation("u1", title="mine")
    assert store.list_conversations("u2") == []
    # foreign user can't see or fetch it
    assert store.get_conversation("u2", a["id"]) is None


def test_rename(store):
    conv = store.create_conversation("u1", title="old")
    updated = store.rename_conversation("u1", conv["id"], "new")
    assert updated["title"] == "new"
    assert store.rename_conversation("u1", "nope", "x") is None
    # foreign user rename → None (404 upstream)
    assert store.rename_conversation("u2", conv["id"], "hijack") is None


def test_delete_and_foreign_delete(store):
    conv = store.create_conversation("u1", title="del")
    store.add_message("u1", conv["id"], "user", "hi")
    assert store.delete_conversation("u2", conv["id"]) is False  # not owned
    assert store.delete_conversation("u1", conv["id"]) is True
    assert store.get_conversation("u1", conv["id"]) is None
    assert store.get_messages("u1", conv["id"]) == []  # cascade


# ------------------------------------------------------------------------ messages
def test_messages_roundtrip_with_recommendations(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    store.add_message("u1", cid, "user", "find me a flat")
    recs = [{"address": "1 High St", "price": "£1200", "geo_location": "51.5,-0.1"}]
    store.add_message("u1", cid, "assistant", "here you go",
                      response_type="search", recommendations=recs)

    msgs = store.get_messages("u1", cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["response_type"] == "search"
    assert msgs[1]["recommendations"] == recs
    # message_count reflects both rows
    assert store.get_conversation("u1", cid)["message_count"] == 2


def test_rehydrate_history_pairs_turns(store):
    conv = store.create_conversation("u1")
    cid = conv["id"]
    store.add_message("u1", cid, "user", "q1")
    store.add_message("u1", cid, "assistant", "a1")
    store.add_message("u1", cid, "user", "q2")
    store.add_message("u1", cid, "assistant", "a2")
    hist = store.rehydrate_history("u1", cid, max_len=10)
    assert hist == [{"user": "q1", "assistant": "a1"},
                    {"user": "q2", "assistant": "a2"}]


def test_clear_conversation_messages_keeps_row(store):
    conv = store.create_conversation("u1", title="keepme")
    cid = conv["id"]
    store.add_message("u1", cid, "user", "hi")
    assert store.clear_conversation_messages("u1", cid) is True
    assert store.get_messages("u1", cid) == []
    assert store.get_conversation("u1", cid)["title"] == "keepme"  # row survives


def test_delete_all_conversations_returns_ids(store):
    a = store.create_conversation("u1")
    b = store.create_conversation("u1")
    store.create_conversation("u2")  # other user untouched
    cids = set(store.delete_all_conversations("u1"))
    assert cids == {a["id"], b["id"]}
    assert store.list_conversations("u1") == []
    assert len(store.list_conversations("u2")) == 1


# ----------------------------------------------------------------------- favorites
def test_favorites_roundtrip_preserves_geo_location(store):
    prop = {"url": "http://x/1", "address": "1 High St", "price": "£1200",
            "geo_location": "51.5,-0.1", "images": ["a.jpg"], "user_id": "u1"}
    store.add_favorite("u1", prop["url"], prop)
    favs = store.list_favorites("u1")
    assert len(favs) == 1
    assert favs[0] == prop            # stored verbatim, nothing stripped
    assert favs[0]["geo_location"] == "51.5,-0.1"


def test_favorites_are_per_user(store):
    store.add_favorite("u1", "http://x/1", {"url": "http://x/1"})
    assert store.list_favorites("u2") == []


def test_favorite_upsert_on_duplicate_url(store):
    store.add_favorite("u1", "http://x/1", {"url": "http://x/1", "price": "£1"})
    store.add_favorite("u1", "http://x/1", {"url": "http://x/1", "price": "£2"})
    favs = store.list_favorites("u1")
    assert len(favs) == 1 and favs[0]["price"] == "£2"


def test_remove_favorite(store):
    store.add_favorite("u1", "http://x/1", {"url": "http://x/1"})
    assert store.remove_favorite("u1", "http://x/1") is True
    assert store.remove_favorite("u1", "http://x/1") is False  # already gone
    assert store.list_favorites("u1") == []


# --------------------------------------------------------- persistence across reopen
def test_survives_reopen(tmp_path):
    path = tmp_path / "persist.sqlite3"
    s1 = ConversationStore(path)
    conv = s1.create_conversation("u1", title="persisted")
    s1.add_message("u1", conv["id"], "user", "remember me")
    s1.add_favorite("u1", "http://x/1", {"url": "http://x/1", "geo_location": "1,2"})
    s1.close()

    s2 = ConversationStore(path)
    assert [c["title"] for c in s2.list_conversations("u1")] == ["persisted"]
    assert s2.get_messages("u1", conv["id"])[0]["content"] == "remember me"
    assert s2.list_favorites("u1")[0]["geo_location"] == "1,2"
    s2.close()
