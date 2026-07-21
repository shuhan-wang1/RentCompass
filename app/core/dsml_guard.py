"""Deterministic guard against tool-call markup reaching a user-visible surface.

The failure this exists for
---------------------------
A model deep in a tool-use conversation can emit its own control tokens as plain
TEXT — DeepSeek's ``<｜tool▁calls▁begin｜>``, an ``<invoke name=...>`` block — and
that text becomes the answer. It has surfaced verbatim in live gates. It is not
merely ugly: the markup is attacker-reachable (a listing description can induce
it), and once it is stored it is replayed into the next turn's context, where the
model may act on it.

Where it must be caught
-----------------------
BEFORE persistence, not at the HTTP boundary. ``response_text`` is written to the
conversation DB and handed to auto-memory before any payload exists, so a guard
that only protected the HTTP response would still let the raw markup land in
storage and reappear on a later turn. So:

  1. ``sanitize_user_text`` runs on the final response as soon as the graph
     produces it and before anything persists it.
  2. ``scan_serialized`` re-checks the fully serialized body just before it is
     sent, as defence in depth over every nested user-visible string.

A layer-2 hit is recorded as ``dsml_leak``, not ``dsml_blocked``. Layer 2 firing
means layer 1's detection is wrong, and a gate that scored that as a successful
block would pass a release whose primary control is broken. The body is still
replaced, so nothing ships; the counter reflects that the design failed, not that
the backstop worked.

Detection is done on a normalized COPY. The text that is kept or discarded is
always the original — normalizing what we return would silently rewrite ordinary
replies (NFKC folds full-width punctuation, ligatures and much else).
"""
from __future__ import annotations

import json as _json
import re
import unicodedata
from typing import Optional, Tuple

# Zero-width and invisible joiners. Splicing one into the middle of a control
# token defeats a literal match while leaving the token intact for whatever
# re-parses it downstream, so they come out of the detection copy entirely.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿᠎­"), None)

# Separators seen inside vendor control tokens: ASCII underscore, DeepSeek's
# U+2581 LOWER ONE EIGHTH BLOCK, hyphen, and whitespace.
_SEP = r"[\s_▁-]*"


def _detection_copy(text: str) -> str:
    """The string patterns are matched against. Never returned to anyone.

    NFKC folds the full-width forms — ``＜`` to ``<`` and ``｜`` to ``|`` — so the
    patterns below only ever need their ASCII spellings. This is why a full-width
    variant cannot slip through: it stops being a variant before matching starts.
    """
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE)


# What sits between the opening bracket and the control keyword. The form actually
# observed leaking in production is `<｜｜DSML｜｜tool_calls>` — two full-width bars,
# the vendor tag, two more bars — so a single optional pipe is not enough. An
# optional namespace prefix (`antml:`) is allowed for the same reason.
_CTRL = r"[|\s_▁-]*(?:dsml[|\s_▁-]*)?(?:[a-z]+:)?"

# Every pattern requires STRUCTURE — an angle bracket, or a pipe-delimited control
# token. None fire on the bare word, so "the DSML format" or "we invoke the API" in
# an ordinary sentence passes through untouched. That restraint is load-bearing: a
# guard that mangles normal prose is a guard somebody turns off.
_MARKERS = (
    # <tool_calls>, </tool_calls>, <|tool_calls_begin|>, <｜tool▁calls▁end｜>,
    # <｜｜DSML｜｜tool_calls>
    re.compile(r"<\s*/?\s*" + _CTRL + r"tool" + _SEP + r"calls?" + _SEP
               + r"(?:begin|end|sep)?" + _SEP + r"\|*\s*>", re.I),
    # <invoke name="...">, </invoke>, <|invoke|>, <｜｜DSML｜｜invoke ...>
    re.compile(r"<\s*/?\s*" + _CTRL + r"invoke\b", re.I),
    # <parameter name="...">, </｜｜DSML｜｜parameter>
    re.compile(r"<\s*/?\s*" + _CTRL + r"parameter\b", re.I),
    # <function_calls>, <|function▁calls|>
    re.compile(r"<\s*/?\s*" + _CTRL + r"function" + _SEP + r"calls?\b", re.I),
    # Any pipe-delimited control token: <|...|>. Bounded so a stray "<|" in prose
    # followed by a distant "|>" cannot match across half a reply.
    re.compile(r"<\|[^<>]{0,80}\|>"),
)

# Deterministic replacements. No model call, no original text — the point of the
# fallback is that whatever induced the markup gets no second chance to phrase the
# reply. Language-matched so a Chinese conversation does not switch to English at
# its least reassuring moment.
_FALLBACK = {
    "zh": "抱歉，这条回复没能正常生成。可以再说一次你想找的区域和预算吗？我再帮你查一次。",
    "en": ("Sorry — I couldn't put that answer together properly. Could you tell me the "
           "area and budget you're looking for so I can try again?"),
}


def fallback_text(reply_language: Optional[str] = None) -> str:
    return _FALLBACK["zh"] if str(reply_language or "en").lower().startswith("zh") \
        else _FALLBACK["en"]


def contains_markup(text: object) -> bool:
    """True when the text carries tool-call control markup."""
    if not text or not isinstance(text, str):
        return False
    probe = _detection_copy(text)
    return any(p.search(probe) for p in _MARKERS)


def sanitize_user_text(text: object, *, reply_language: Optional[str] = None) -> Tuple[str, bool]:
    """``(safe_text, blocked)`` for one user-visible string.

    On a hit the ENTIRE text is replaced rather than the markers excised. A reply
    that was partly a control token is not a reply with a few bad characters in
    it — the model was not answering — and stripping the markers would leave a
    fragment that reads as an answer while carrying whatever prose the injection
    wrapped around it.
    """
    if not isinstance(text, str) or not contains_markup(text):
        return (text if isinstance(text, str) else ""), False
    return fallback_text(reply_language), True


def _walk_strings(node, out, depth=0):
    """Every string anywhere in a decoded JSON structure."""
    if depth > 12:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k))
            _walk_strings(v, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk_strings(v, out, depth + 1)


def scan_serialized(body: object) -> bool:
    """True when a fully serialized response body still carries control markup.

    Runs over the serialized form on purpose: it reaches every nested user-visible
    string — card text, clarifying questions, listing descriptions — without this
    module needing to know the payload's shape. Shape knowledge is exactly what
    goes stale when somebody adds a field.

    The body is DECODED before matching. Flask serializes with ``ensure_ascii``, so
    ``<｜tool▁calls▁begin｜>`` reaches the wire as ``<\\uff5ctool\\u2581calls...``
    and a scan of the raw bytes finds nothing at all. Decoding first is not an
    optimisation here — without it this whole layer silently passes everything.
    The raw text is still scanned as a fallback for non-JSON bodies.
    """
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            return False
    if not isinstance(body, str):
        return False
    try:
        strings: list = []
        _walk_strings(_json.loads(body), strings)
        if any(contains_markup(s) for s in strings):
            return True
    except Exception:
        pass  # not JSON: fall through to the raw scan
    return contains_markup(body)
