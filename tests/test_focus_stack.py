"""Focus stack (multi-focus + top-of-stack deixis).

Covers requirement 1:
  * app.py _build_focus_stack_records — resolve a frontend focus_stack (oldest -> newest)
    against the session snapshot / registry / demo CSV into structured records
    (AST-extracted pure helper).
  * langgraph reference anchoring — when a focus stack is active, a singular near-deictic
    (this one / 这个房源 / 这套) anchors to the CURRENT (top) focus AHEAD of last_results[0];
    'the previous focus / 上一个聚焦的' anchors to the one below it. With NO focus stack the
    existing last_results behaviour is untouched (backward compatibility).
  * _is_advice_followup / _resolve_last_result / _resolve_target_address all honour the stack.
"""

import ast
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

_APP_PATH = os.path.join(_ROOT, "app", "app.py")


def _load_app_symbols(wanted_defs):
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=_APP_PATH)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in wanted_defs]
    module = ast.Module(body=picked, type_ignores=[])
    ns = {"re": re}
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    missing = wanted_defs - ns.keys()
    assert not missing, f"failed to extract {missing} from app.py"
    return ns


_APP = _load_app_symbols({"_resolve_focus_listing", "_build_focus_stack_records"})
_resolve_focus_listing = _APP["_resolve_focus_listing"]
_build_focus_stack_records = _APP["_build_focus_stack_records"]


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


# ══════════════════════════════════════════════════════════════════════════
# app.py — _build_focus_stack_records
# ══════════════════════════════════════════════════════════════════════════
def _sess_records():
    return [
        {"address": "12 Oxford Rd, Manchester M1 5AN", "price": "£1200/month",
         "url": "https://otm/manc-1/", "description": "Bright 1-bed.", "area": "Manchester",
         "travel_time": "18 min", "bedrooms": 1, "property_type": "Flat"},
        {"address": "5 Pine St, Manchester M2 3XY", "price": "£650/month",
         "url": "https://otm/manc-2/", "description": "Cosy room.", "area": "Manchester",
         "travel_time": "35 min", "bedrooms": 1, "property_type": "Room"},
    ]


def test_build_focus_stack_records_oldest_to_newest():
    focus_items = [
        {"address": "", "url": "https://otm/manc-2/"},   # oldest focus
        {"address": "", "url": "https://otm/manc-1/"},   # newest / current focus
    ]
    recs = _build_focus_stack_records(focus_items, _sess_records(), [])
    assert [r["url"] for r in recs] == ["https://otm/manc-2/", "https://otm/manc-1/"]
    # newest (last) is the current focus and carries its real resolved fields
    assert recs[-1]["address"] == "12 Oxford Rd, Manchester M1 5AN"
    assert recs[-1]["name"] == "12 Oxford Rd"
    assert recs[-1]["description"] == "Bright 1-bed."
    assert recs[-1]["area"] == "Manchester"


def test_build_focus_stack_records_skips_non_dicts():
    recs = _build_focus_stack_records(["nope", None, {"url": "https://otm/manc-1/"}],
                                      _sess_records(), [])
    assert len(recs) == 1
    assert recs[0]["url"] == "https://otm/manc-1/"


def test_build_focus_stack_records_empty():
    assert _build_focus_stack_records([], _sess_records(), []) == []
    assert _build_focus_stack_records(None, _sess_records(), []) == []


# ══════════════════════════════════════════════════════════════════════════
# langgraph — focus reference resolution
# ══════════════════════════════════════════════════════════════════════════
def _last_results():
    return [
        {"name": "Maple Court", "address": "Maple Court, 12 Oak Rd, Manchester",
         "price": "£1200 pcm", "travel_time": "20 mins", "url": "https://otm/maple/"},
        {"name": "Elm House", "address": "Elm House, 5 Pine St, Manchester",
         "price": "£650 pcm", "travel_time": "35 mins", "url": "https://otm/elm/"},
    ]


def _focus_stack_two():
    """Focus stack whose TOP (Elm House) is deliberately NOT last_results[0] (Maple Court),
    so anchoring to the top proves precedence over last_results[0]."""
    return [
        {"name": "Maple Court", "address": "Maple Court, 12 Oak Rd, Manchester",
         "price": "£1200 pcm", "travel_time": "20 mins", "url": "https://otm/maple/"},
        {"name": "Elm House", "address": "Elm House, 5 Pine St, Manchester",
         "price": "£650 pcm", "travel_time": "35 mins", "url": "https://otm/elm/"},
    ]


def test_resolve_focus_reference_top_and_previous(lga):
    fs = _focus_stack_two()
    ctx = {"focus_stack": fs}
    # singular near-deictic -> current (top) focus
    assert lga._resolve_focus_reference("这个房源怎么样", ctx) is fs[-1]
    assert lga._resolve_focus_reference("what about this one", ctx) is fs[-1]
    # 'the previous focus' -> the one below the top
    assert lga._resolve_focus_reference("上一个聚焦的怎么样", ctx) is fs[-2]
    assert lga._resolve_focus_reference("tell me about the previous focus", ctx) is fs[-2]
    # no stack -> None (fall back to legacy resolution)
    assert lga._resolve_focus_reference("这个房源怎么样", {}) is None
    # single-item stack: no 'previous' to return -> the previous phrase yields nothing here
    assert lga._resolve_focus_reference("上一个聚焦的", {"focus_stack": [fs[0]]}) is None


def test_is_previous_focus_reference(lga):
    assert lga._is_previous_focus_reference("上一个聚焦的")
    assert lga._is_previous_focus_reference("之前那个 focus")
    assert lga._is_previous_focus_reference("the previous focus please")
    assert not lga._is_previous_focus_reference("这个房源")
    assert not lga._is_previous_focus_reference("上一个区域")   # 上一个 without focus wording


def test_resolve_last_result_focus_top_beats_results0(lga):
    # 'this one' would normally map to last_results[0] (Maple Court). With a focus stack whose
    # top is Elm House, it must resolve to Elm House instead.
    ctx = {"last_results": _last_results(), "focus_stack": _focus_stack_two(),
           "current_message": "这个房源怎么样"}
    rec = lga._resolve_last_result("这个房源怎么样", ctx)
    assert rec["name"] == "Elm House"


def test_resolve_last_result_previous_focus(lga):
    ctx = {"last_results": _last_results(), "focus_stack": _focus_stack_two(),
           "current_message": "上一个聚焦的适合情侣吗"}
    rec = lga._resolve_last_result("上一个聚焦的适合情侣吗", ctx)
    assert rec["name"] == "Maple Court"


def test_resolve_last_result_ordinal_still_wins_over_focus(lga):
    # An explicit ordinal ("第二个") still resolves over last_results, not the focus top.
    ctx = {"last_results": _last_results(), "focus_stack": [_focus_stack_two()[0]],
           "current_message": "第二个怎么样"}
    rec = lga._resolve_last_result("第二个怎么样", ctx)
    assert rec["name"] == "Elm House"       # last_results[1], via the zh ordinal


def test_advice_followup_prefers_focus_top(lga):
    # _is_advice_followup 栈顶优先: an advice question with a bare deictic anchors to the
    # focus top even though last_results[0] is a different listing.
    ctx = {"last_results": _last_results(), "focus_stack": _focus_stack_two(),
           "current_message": "这个房源适合情侣吗"}
    out = lga._is_advice_followup("这个房源适合情侣吗", ctx)
    assert out is not None and out["record"]["name"] == "Elm House"


def test_resolve_target_address_focus_top(lga):
    # A location question about the focused listing targets the focus top's address.
    ctx = {"last_results": _last_results(), "focus_stack": _focus_stack_two(),
           "current_message": "这个房源附近安全吗"}
    addr = lga._resolve_target_address("这个房源附近安全吗", ctx)
    assert addr == "Elm House, 5 Pine St, Manchester"


# ══════════════════════════════════════════════════════════════════════════
# Backward compatibility — NO focus stack: existing behaviour is unchanged
# ══════════════════════════════════════════════════════════════════════════
def test_no_focus_stack_bare_deictic_maps_to_results0(lga):
    ctx = {"last_results": _last_results(), "current_message": "这个房源怎么样"}
    rec = lga._resolve_last_result("这个房源怎么样", ctx)
    assert rec["name"] == "Maple Court"     # last_results[0], legacy behaviour


def test_no_focus_stack_target_address_unchanged(lga):
    # Without a focus stack the resolver is exactly as before: _resolve_target_address has NO
    # Chinese-deictic branch, so a bare zh deictic with no property_address resolves to None
    # (the caller then asks for clarification). The focus-stack path is purely additive.
    ctx = {"last_results": _last_results(), "current_message": "这个房源附近安全吗"}
    assert lga._resolve_target_address("这个房源附近安全吗", ctx) is None
    # The English deictic branch still maps to last_results[0], unchanged.
    ctx_en = {"last_results": _last_results(), "current_message": "is this one safe"}
    assert lga._resolve_target_address("is this one safe", ctx_en) == "Maple Court, 12 Oak Rd, Manchester"
