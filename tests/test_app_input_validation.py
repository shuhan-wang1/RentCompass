"""Offline unit tests for the input-validation helpers in app/app.py.

Importing app.py directly triggers heavy module-level startup (RAG/FAISS/property
load), which is unsuitable for a fast unit test. Instead we parse app.py with `ast`
and extract ONLY the self-contained pieces under test — the `ApiError` class and the
pure helpers `_validate_conversation_id`, `_coerce_optional_int`, `_derive_title`
(they depend only on `re` and `ApiError`). This exercises the ACTUAL source in app.py
without running its module-level side effects.

Covers the four fixes:
  1. conversation_id type validation (list/dict/number → 400; string → OK)
  2. numeric range/fractional validation for the search_direct criteria
  3. (ownership check lives in the Flask handler; the cid-type guard it relies on is here)
  4. _derive_title HTML/angle-bracket stripping (stored-XSS defense-in-depth)
"""

import ast
import os
import re

import pytest

_APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "app", "app.py"
)

_WANTED = {
    "ApiError",
    "_validate_conversation_id",
    "_coerce_optional_int",
    "_derive_title",
}


def _load_helpers():
    """Extract the wanted class/functions from app.py and exec them in isolation."""
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_APP_PATH)
    picked = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and getattr(node, "name", None) in _WANTED
    ]
    module = ast.Module(body=picked, type_ignores=[])
    ns = {"re": re}
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    missing = _WANTED - ns.keys()
    assert not missing, f"failed to extract {missing} from app.py"
    return ns


_H = _load_helpers()
ApiError = _H["ApiError"]
_validate_conversation_id = _H["_validate_conversation_id"]
_coerce_optional_int = _H["_coerce_optional_int"]
_derive_title = _H["_derive_title"]


# ---------------------------------------------------------------------------
# Fix 1 — conversation_id must be a (non-empty) string when present
# ---------------------------------------------------------------------------

def test_conversation_id_none_ok():
    assert _validate_conversation_id({}) is None
    assert _validate_conversation_id({"conversation_id": None}) is None


def test_conversation_id_valid_string_passthrough():
    # A nonexistent *string* is still valid here (→ handled as new/unknown downstream).
    assert _validate_conversation_id({"conversation_id": "cid-does-not-exist"}) == "cid-does-not-exist"


@pytest.mark.parametrize("bad", [["a", "b"], {"x": 1}, 123, 4.5, True, "", "   "])
def test_conversation_id_non_string_rejected(bad):
    with pytest.raises(ApiError) as ei:
        _validate_conversation_id({"conversation_id": bad})
    assert ei.value.status == 400
    assert "conversation_id" in ei.value.message


# ---------------------------------------------------------------------------
# Fix 2 — numeric range + fractional validation
# ---------------------------------------------------------------------------

def test_optional_int_none_and_blank():
    assert _coerce_optional_int(None, "x", min_value=1, max_value=10) is None
    assert _coerce_optional_int("", "x", min_value=1, max_value=10) is None


def test_max_budget_zero_rejected():
    with pytest.raises(ApiError) as ei:
        _coerce_optional_int(0, "max_budget", min_value=1, max_value=100000)
    assert ei.value.status == 400


def test_max_budget_fractional_rejected():
    with pytest.raises(ApiError) as ei:
        _coerce_optional_int(3.7, "max_budget", min_value=1, max_value=100000)
    assert ei.value.status == 400
    with pytest.raises(ApiError):
        _coerce_optional_int("3.7", "max_budget", min_value=1, max_value=100000)


def test_max_budget_absurdly_large_rejected():
    with pytest.raises(ApiError):
        _coerce_optional_int(10_000_000, "max_budget", min_value=1, max_value=100000)


def test_max_budget_normal_ok():
    assert _coerce_optional_int(1500, "max_budget", min_value=1, max_value=100000) == 1500
    assert _coerce_optional_int("1500", "max_budget", min_value=1, max_value=100000) == 1500
    assert _coerce_optional_int(3.0, "max_budget", min_value=1, max_value=100000) == 3


def test_bedrooms_range():
    # 0 allowed (studio/any)
    assert _coerce_optional_int(0, "bedrooms", min_value=0, max_value=20) == 0
    assert _coerce_optional_int(3, "bedrooms", min_value=0, max_value=20) == 3
    with pytest.raises(ApiError):
        _coerce_optional_int(1000, "bedrooms", min_value=0, max_value=20)
    with pytest.raises(ApiError):
        _coerce_optional_int(-1, "bedrooms", min_value=0, max_value=20)


def test_commute_time_range():
    assert _coerce_optional_int(30, "max_commute_time", min_value=1, max_value=300) == 30
    with pytest.raises(ApiError):
        _coerce_optional_int(0, "max_commute_time", min_value=1, max_value=300)
    with pytest.raises(ApiError):
        _coerce_optional_int(-5, "max_commute_time", min_value=1, max_value=300)
    with pytest.raises(ApiError):
        _coerce_optional_int(99999, "max_commute_time", min_value=1, max_value=300)


def test_non_numeric_string_rejected():
    with pytest.raises(ApiError) as ei:
        _coerce_optional_int("abc", "max_budget", min_value=1, max_value=100000)
    assert ei.value.status == 400


def test_bool_rejected():
    # JSON true/false are ints in Python — must not pass as a numeric count.
    with pytest.raises(ApiError):
        _coerce_optional_int(True, "bedrooms", min_value=0, max_value=20)


# ---------------------------------------------------------------------------
# Fix 4 — _derive_title strips HTML / angle brackets (stored-XSS sink)
# ---------------------------------------------------------------------------

def test_derive_title_strips_img_onerror():
    title = _derive_title("<img src=x onerror=alert(1)>hello")
    assert "<" not in title and ">" not in title
    assert "onerror" not in title.lower() or "<" not in title  # tag gone
    assert title.startswith("hello")


def test_derive_title_strips_script_tag():
    title = _derive_title("<script>alert(1)</script>find me a flat")
    assert "<" not in title and ">" not in title
    assert "script" not in title.lower()


def test_derive_title_unclosed_bracket_neutralized():
    title = _derive_title("cheap flat <img src=x")
    assert "<" not in title and ">" not in title


def test_derive_title_plain_text_preserved():
    assert _derive_title("2 bed flat near Oxford Circus") == "2 bed flat near Oxford Circus"


def test_derive_title_empty():
    assert _derive_title("") == "New chat"
    assert _derive_title("   ") == "New chat"
    assert _derive_title("<>") == "New chat"


def test_derive_title_truncation():
    long = "x" * 100
    out = _derive_title(long)
    assert out.endswith("…")
    assert len(out) == 41  # 40 chars + ellipsis
