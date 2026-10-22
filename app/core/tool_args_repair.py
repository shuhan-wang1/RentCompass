"""Safe repair of a model's tool-call arguments that are not valid JSON (r6c).

DeepSeek sometimes emits arguments that json.loads rejects with "Expecting value" although
the object is otherwise well formed (r03/r05/r06/r18/r19: get_property_details for "Tell me
more about the first one", search_properties in r18 c14fe21d; diag obj=1 closed=1, never
"Extra data"). The one raw payload on record is r10's compare_or_rank_areas call:

    {"city": "London", "destination": Camden Road, Islington N7, "max_commute_minutes": null, ...}

an unquoted string value. The same parse error is produced by a single-quoted value
('Scape Bloomsbury', the quoting style get_property_details' own parameter descriptions
showed) and by Python literals (None / True / False).

Only those three shapes are repaired, and only when the result is unambiguous: a FLAT
object whose every key is one of the tool's own parameters, each value ending exactly at
the next ``, "<parameter>":`` or at the closing brace. Anything else (an unknown key, a
quote or bracket inside an unquoted value, nested objects, trailing text) is not repaired
and the call stays malformed: a call whose arguments are genuinely ambiguous never runs.
Deterministic, no eval.
"""
from __future__ import annotations

import json
import re

_PY_LITERALS = {"None": None, "True": True, "False": False,
                "null": None, "true": True, "false": False}
_DECODER = json.JSONDecoder(strict=False)


def _key_alternation(allowed) -> str:
    return "|".join(re.escape(k) for k in sorted(allowed, key=len, reverse=True))


def repair_tool_arguments(text, allowed_keys) -> dict | None:
    """The arguments as a dict when ``text`` is one of the repairable shapes, else None."""
    if not isinstance(text, str) or not allowed_keys:
        return None
    source = text.strip()
    if not (source.startswith("{") and source.endswith("}")) or len(source) > 20_000:
        return None
    try:
        parsed = _DECODER.decode(source)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    keys = _key_alternation(allowed_keys)
    key_re = re.compile(rf"\s*(?:\"({keys})\"|'({keys})')\s*:\s*")
    # Where the current value must end: the next known key, or the closing brace.
    boundary = re.compile(rf"\s*,\s*(?:\"(?:{keys})\"|'(?:{keys})')\s*:|\s*\}}\s*$")
    out: dict = {}
    pos = 1
    while True:
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if source[pos:].strip() == "}":
            return out if out else None
        m = key_re.match(source, pos)
        if not m:
            return None
        key = m.group(1) or m.group(2)
        if key in out:
            return None
        pos = m.end()
        value, pos = _value_at(source, pos, boundary)
        if pos is None:
            return None
        out[key] = value
        tail = re.match(r"\s*,", source[pos:])
        if tail:
            pos += tail.end()
            continue
        if re.fullmatch(r"\s*\}\s*", source[pos:]):
            return out
        return None


def _value_at(source: str, pos: int, boundary):
    """(value, end) of the value starting at ``pos``; (None, None) when not repairable."""
    try:
        value, end = _DECODER.raw_decode(source, pos)      # a well-formed JSON value
    except ValueError:
        pass
    else:
        if not re.match(r"\s*[,}]", source[end:]):
            return None, None
        if isinstance(value, dict) or (isinstance(value, list) and not _flat_list(value)):
            return None, None
        return value, end
    end_match = boundary.search(source, pos)
    if not end_match:
        return None, None
    end = end_match.start()
    raw = source[pos:end].strip()
    if not raw:
        return None, None
    if raw in _PY_LITERALS:                                # None / True / False
        return _PY_LITERALS[raw], end
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":         # 'Scape Bloomsbury'
        inner = raw[1:-1]
        if '"' in inner or "\n" in inner:
            return None, None
        return inner, end
    # An unquoted plain-text value ("Camden Road, Islington N7"); an apostrophe inside a
    # word ("King's Cross") is text, any quote, bracket or brace is not.
    if re.search(r'["{}\[\]\n]', raw) or raw[0] in "'’" or raw[-1] in "'’":
        return None, None
    if re.fullmatch(r"[-+.\d]+", raw):
        return None, None          # a malformed number is not a string to guess at
    return raw, end


def _flat_list(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, (str, int, float, bool)) or item is None for item in value)


def repair_invalid_tool_calls(invalid_calls, specs):
    """Split malformed calls into (repaired tool calls, still-invalid calls)."""
    by_name = {getattr(spec, "name", None): spec for spec in specs or []}
    repaired, remaining = [], []
    for call in invalid_calls or []:
        spec = by_name.get(call.get("name")) if isinstance(call, dict) else None
        props = ((getattr(spec, "input_schema", None) or {}).get("properties") or {}) if spec else {}
        args = repair_tool_arguments(call.get("args"), set(props)) if props else None
        if args is None or not call.get("id"):
            remaining.append(call)
            continue
        repaired.append({"name": call["name"], "args": args, "id": call["id"], "type": "tool_call"})
    return repaired, remaining


def error_position_shape(call, allowed_keys=()) -> str:
    """Content-free shape of a malformed call's parse error: the parameter whose value
    failed (only a name from the tool's own schema) and the class of the character at the
    error (alpha / quote1 / digit / other). No user text, so it may be logged (R3-07)."""
    if not isinstance(call, dict):
        return "-"
    text = call.get("args") if isinstance(call.get("args"), str) else ""
    m = re.search(r"char (\d+)", str(call.get("error") or ""))
    if not text or not m:
        return "-"
    at = int(m.group(1))
    key = None
    for km in re.finditer(r"\"([A-Za-z_][A-Za-z0-9_]{0,40})\"\s*:", text[:at]):
        if km.group(1) in allowed_keys:
            key = km.group(1)
    ch = text[at] if at < len(text) else ""
    kind = ("quote1" if ch == "'" else "alpha" if ch.isalpha() else
            "digit" if ch.isdigit() else "end" if not ch else "other")
    return f"at_key={key or '-'}:at={kind}"
