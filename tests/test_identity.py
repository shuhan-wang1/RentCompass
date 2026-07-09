"""Unit tests for the pure identity + validation logic (no Flask, no DeepSeek)."""
import pytest

from uk_rent_agent.web.identity import (
    resolve_user_id, normalize_message, valid_user_id,
    InvalidUserId, InvalidMessage,
)


def _mint():
    return "MINTED"


# ------------------------------------------------------------------ user_id regex
@pytest.mark.parametrize("uid", ["a", "user_1", "A-B_c", "u" * 64, "bktest-abc123"])
def test_valid_user_ids(uid):
    assert valid_user_id(uid)


@pytest.mark.parametrize("uid", ["", "u" * 65, "../evil", "a b", "a.b", "he!lo", "🙂"])
def test_invalid_user_ids(uid):
    assert not valid_user_id(uid)


# ------------------------------------------------------------- resolution priority
def test_body_beats_header_query_cookie():
    uid, minted = resolve_user_id(body_uid="body", header_uid="hdr",
                                  query_uid="qry", cookie_uid="ckie", mint=_mint)
    assert (uid, minted) == ("body", False)


def test_header_beats_query_and_cookie():
    uid, minted = resolve_user_id(body_uid=None, header_uid="hdr",
                                  query_uid="qry", cookie_uid="ckie", mint=_mint)
    assert (uid, minted) == ("hdr", False)


def test_query_beats_cookie():
    uid, minted = resolve_user_id(body_uid=None, header_uid=None,
                                  query_uid="qry", cookie_uid="ckie", mint=_mint)
    assert (uid, minted) == ("qry", False)


def test_cookie_used_when_no_client_supplied_id():
    uid, minted = resolve_user_id(body_uid=None, header_uid=None,
                                  query_uid=None, cookie_uid="ckie", mint=_mint)
    assert (uid, minted) == ("ckie", False)


def test_mint_when_nothing_present():
    uid, minted = resolve_user_id(body_uid=None, header_uid=None,
                                  query_uid=None, cookie_uid=None, mint=_mint)
    assert (uid, minted) == ("MINTED", True)


def test_blank_values_are_skipped_in_priority():
    # empty body/header fall through to the query param
    uid, minted = resolve_user_id(body_uid="  ", header_uid="",
                                  query_uid="qry", cookie_uid=None, mint=_mint)
    assert (uid, minted) == ("qry", False)


def test_whitespace_is_stripped():
    uid, _ = resolve_user_id(body_uid="  spaced  ", header_uid=None,
                             query_uid=None, cookie_uid=None, mint=_mint)
    assert uid == "spaced"


# ---------------------------------------------------- invalid client-supplied ids
def test_invalid_body_id_raises():
    with pytest.raises(InvalidUserId):
        resolve_user_id(body_uid="../evil", header_uid=None,
                        query_uid=None, cookie_uid=None, mint=_mint)


def test_invalid_header_id_raises():
    with pytest.raises(InvalidUserId):
        resolve_user_id(body_uid=None, header_uid="x" * 5000,
                        query_uid=None, cookie_uid=None, mint=_mint)


def test_invalid_query_id_raises():
    with pytest.raises(InvalidUserId):
        resolve_user_id(body_uid=None, header_uid=None,
                        query_uid="a b", cookie_uid=None, mint=_mint)


# --------------------------------------------------------------- message validation
def test_normalize_message_ok():
    assert normalize_message("hello there") == "hello there"


@pytest.mark.parametrize("bad", [None, 123, {"a": 1}, ["x"], 4.5, True])
def test_non_string_message_rejected(bad):
    with pytest.raises(InvalidMessage):
        normalize_message(bad)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_message_rejected(blank):
    with pytest.raises(InvalidMessage):
        normalize_message(blank)
