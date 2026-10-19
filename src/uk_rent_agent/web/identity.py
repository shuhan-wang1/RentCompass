"""Pure identity + request-validation helpers (no Flask dependency).

Kept framework-free so the resolution/validation rules can be unit-tested without
booting the heavy Flask app (RAG index, LangGraph, MCP). app.py wraps these with the
Flask request/session context.

Contract (api_contract_v1):
  - user_id: 1–64 chars, [A-Za-z0-9_-]+. Invalid client-supplied id → 400.
  - Resolution priority: body user_id > X-User-Id header > ?user_id= query > cookie > mint.
  - Non-string chat message → 400.
  - A chat message carrying an unpaired UTF-16 surrogate, a NUL, or another
    C0 control → 400 (they are unencodable/untruncatable, not merely odd).
"""
from __future__ import annotations

import re
from typing import Callable

# 身份合法性正则（契约固定）: 1–64 chars of [A-Za-z0-9_-]
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class InvalidUserId(ValueError):
    """A client explicitly supplied a user_id that violates the contract regex."""


class InvalidMessage(ValueError):
    """The chat message is missing, empty, or not a string."""


def valid_user_id(uid) -> bool:
    return isinstance(uid, str) and bool(USER_ID_RE.match(uid))


def resolve_user_id(
    *,
    body_uid,
    header_uid,
    query_uid,
    cookie_uid,
    mint: Callable[[], str],
) -> tuple[str, bool]:
    """Resolve a request's user_id per the contract priority.

    Returns ``(user_id, minted)`` where ``minted`` is True when a brand-new id was
    generated (caller should persist it into the session cookie).

    Raises ``InvalidUserId`` when a *client-supplied* id (body/header/query) fails the
    regex — cookie/minted ids are trusted (they were produced by us).
    """
    for candidate in (body_uid, header_uid, query_uid):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        if not valid_user_id(text):
            raise InvalidUserId(text)
        return text, False
    if cookie_uid:
        text = str(cookie_uid).strip()
        if text:
            return text, False
    return mint(), True


#: Any code point in the UTF-16 surrogate range. ``json.loads`` accepts a
#: ``\udXXX`` escape and Python happily holds the resulting lone surrogate in a
#: ``str`` — but it cannot be encoded as UTF-8, so the FIRST thing that tries
#: (sqlite3's strict bind-parameter encoder, on the durable conversation store)
#: raises ``UnicodeEncodeError`` deep inside the request and the caller sees a 500.
#: An unpaired surrogate is not text a user can have typed; it is rejected here, at
#: the request boundary, as the client error it is.
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

#: C0 controls (plus DEL) that are not ordinary whitespace. NUL in particular
#: truncates the string for several SQLite readers and terminates a C string in any
#: downstream consumer, so a message carrying one is refused rather than stored.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_is_storable(value) -> bool:
    """True when ``value`` is a str that can be encoded and durably stored as UTF-8.

    False for a lone surrogate or an embedded NUL — the two classes that make a
    string unencodable/untruncatable rather than merely unusual.
    """
    if not isinstance(value, str):
        return False
    return not (SURROGATE_RE.search(value) or "\x00" in value)


def normalize_message(message):
    """Validate a chat message; return the original string unchanged.

    Raises ``InvalidMessage`` for a non-string, an empty/whitespace-only string, an
    unpaired UTF-16 surrogate, or a C0 control character other than tab/newline/CR.
    The last two are rejected HERE, before any durable write, because they are the
    inputs that turn a client mistake into a server error further down (see
    ``SURROGATE_RE``/``CONTROL_RE``).
    """
    if not isinstance(message, str):
        raise InvalidMessage("message must be a string")
    if not message.strip():
        raise InvalidMessage("message is required")
    if SURROGATE_RE.search(message):
        raise InvalidMessage(
            "message contains an unpaired unicode surrogate and cannot be encoded")
    if CONTROL_RE.search(message):
        raise InvalidMessage("message contains disallowed control characters")
    return message
