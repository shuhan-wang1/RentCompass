"""Pure identity + request-validation helpers (no Flask dependency).

Kept framework-free so the resolution/validation rules can be unit-tested without
booting the heavy Flask app (RAG index, LangGraph, MCP). app.py wraps these with the
Flask request/session context.

Contract (api_contract_v1):
  - user_id: 1–64 chars, [A-Za-z0-9_-]+. Invalid client-supplied id → 400.
  - Resolution priority: body user_id > X-User-Id header > ?user_id= query > cookie > mint.
  - Non-string chat message → 400.
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


def normalize_message(message):
    """Validate a chat message; return the original string unchanged.

    Raises ``InvalidMessage`` for a non-string or an empty/whitespace-only string.
    """
    if not isinstance(message, str):
        raise InvalidMessage("message must be a string")
    if not message.strip():
        raise InvalidMessage("message is required")
    return message
