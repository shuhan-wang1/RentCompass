"""Boundary normalisation for free-text TOOL ARGUMENTS.

Tool arguments arrive from three places that all get them wrong occasionally: a model's
JSON (``None``, a number, a stray list where a string was declared), a deterministic
planner, and a browser that can put a lone UTF-16 surrogate into a message. Each of those
used to surface deep inside a tool body as an opaque ``TypeError`` (``len(None)``) or, for
a surrogate echoed back into the result, as a ``UnicodeEncodeError`` in some later
serializer — far from the argument that caused it.

One helper, imported by the tools that take free text, so a hostile or malformed argument
becomes a structured refusal that still names what was searched.
"""

from __future__ import annotations

import re

# U+D800-U+DFFF survive JSON decoding but have no UTF-8 encoding.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def clean_arg_text(value) -> str:
    """``value`` as a safe, whitespace-collapsed ``str`` ("" when absent).

    Accepts anything and NEVER raises. Lone surrogates are replaced with U+FFFD rather
    than dropped or rejected: the rest of the user's words still describe a real place,
    and the replacement character is visible in a log without being able to break one.
    """
    try:
        if value is None:
            return ""
        text = value if isinstance(value, str) else str(value)
        return " ".join(_SURROGATE_RE.sub("�", text).split())
    except Exception:
        return ""
