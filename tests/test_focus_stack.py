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


_APP = _load_app_symbols({"_resolve_focus_listing", "_build_focus_stack_records",
                          "_listing_url_key", "_listing_price_key", "_match_listing_by_address"})
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


# ══════════════════════════════════════════════════════════════════════════
# fc_loop (core/agent_loop.py) — the focus stack must reach the PROMPT
#
# The stack was resolved correctly (tests above) and then dropped on the floor by the
# fc loop, which is the pool the public edge serves: _build_messages read a
# `focused_property` key nothing writes and fell back to {"property_address": ...} — a
# shape _format_single_result does not read. The focus block rendered
# "Property: (no details captured)" with NO url, so a focused listing was invisible to
# the model: it could only name-match, a same-name building made get_property_details
# return `ambiguous`, and the loop asked the user to disambiguate the listing the UI had
# already handed it — on every turn.
#
# _focus_records is extracted from source (AST) so this contract holds even where
# langgraph is not installed: an importorskip here is what let the defect ship.
# ══════════════════════════════════════════════════════════════════════════
_LOOP_PATH = os.path.join(_ROOT, "app", "core", "agent_loop.py")


def _load_loop_symbols(wanted_defs):
    with open(_LOOP_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=_LOOP_PATH)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name in wanted_defs]
    module = ast.Module(body=picked, type_ignores=[])
    # agent_loop has no `from __future__ import annotations`, so signature annotations are
    # evaluated at def time — AgentState is a TypedDict there; a dict stands in fine.
    ns = {"AgentState": dict}
    exec(compile(module, _LOOP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    missing = wanted_defs - ns.keys()
    assert not missing, f"failed to extract {missing} from agent_loop.py"
    return ns


_focus_records = _load_loop_symbols({"_focus_records"})["_focus_records"]

# The listing key names _format_single_result reads. A record keyed property_address
# renders as "(no details captured)" — that WAS the bug, so the shape is pinned here.
_LISTING_KEYS = {"address", "price", "travel_time", "url"}


def test_focus_records_reads_the_resolved_stack():
    ec = {"focus_stack": _build_focus_stack_records(
        [{"url": "https://otm/manc-2/"}, {"url": "https://otm/manc-1/"}],
        _sess_records(), [])}
    recs = _focus_records(ec)
    assert [r["url"] for r in recs] == ["https://otm/manc-2/", "https://otm/manc-1/"]
    # the TOP is the current focus and carries the URL — the identity the tool needs
    assert recs[-1]["address"] == "12 Oxford Rd, Manchester M1 5AN"
    assert recs[-1]["url"] == "https://otm/manc-1/"


def test_focus_records_top_uses_listing_key_names_not_property_prefixed():
    ec = {"focus_stack": _build_focus_stack_records(
        [{"url": "https://otm/manc-1/"}], _sess_records(), [])}
    top = _focus_records(ec)[-1]
    assert _LISTING_KEYS <= set(top), f"top-of-stack record must use listing keys, got {sorted(top)}"
    assert "property_address" not in top


def test_focus_records_scalar_fallback_keeps_the_url():
    # Old frontend / no resolved stack: only the flattened property_* scalars are present.
    # They must be MAPPED onto the listing key names, url included.
    ec = {"property_address": "12 Oxford Rd, Manchester M1 5AN",
          "property_price": "£1200/month", "property_travel_time": "18 min",
          "property_url": "https://otm/manc-1/", "description": "Bright 1-bed."}
    recs = _focus_records(ec)
    assert len(recs) == 1
    assert recs[0]["address"] == "12 Oxford Rd, Manchester M1 5AN"
    assert recs[0]["url"] == "https://otm/manc-1/"
    assert recs[0]["description"] == "Bright 1-bed."
    assert not any(k.startswith("property_") for k in recs[0])


def test_focus_records_empty_without_any_focus():
    assert _focus_records({}) == []
    assert _focus_records({"focus_stack": []}) == []
    assert _focus_records({"focus_stack": ["nope", None]}) == []
    # price without an address identifies nothing — no phantom focus record
    assert _focus_records({"property_price": "£1200/month"}) == []


def test_build_messages_context_block_reads_the_keys_app_py_writes():
    """Source-level contract: the context_block in _build_messages must read the keys app.py
    actually writes into extracted_context. It used to read 'focused_property' (never
    written) and 'recommendations_index' (never written — app.py writes the registry under
    'recommended_registry'), so both the focus AND the listings index were dropped."""
    with open(_LOOP_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=_LOOP_PATH)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_build_messages")
    assign = next(n for n in ast.walk(fn)
                  if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "context_block" for t in n.targets))
    block_src = ast.dump(assign)
    keys = [k.value for k in assign.value.keys if isinstance(k, ast.Constant)]
    assert "focus_stack" in keys, "context_block must carry the focus stack to the prompt"
    assert "_focus_records" in block_src or "focus_records" in block_src, \
        "the focused property must come from _focus_records, not a raw ec key"
    assert "recommended_registry" in block_src, \
        "the recommendations index must read app.py's 'recommended_registry'"


# ══════════════════════════════════════════════════════════════════════════
# fc_loop executor backstop — a name-only get_property_details call on the focused
# listing is completed with that listing's URL. Without it, a focus on a building whose
# units share a name (four "Apt 105, Castello Court" listings) can only ever resolve to
# the tool's `ambiguous` refusal, which asks the user to identify a listing the UI had
# already identified by URL — and the answer cannot change any of that, so it repeats.
# ══════════════════════════════════════════════════════════════════════════
_LOOP = _load_loop_symbols({"_focus_records", "_inject_focus_url", "_ref_matches_focus",
                            "_norm_ref"})
_inject_focus_url = _LOOP["_inject_focus_url"]

_FOCUS_TOP = {"name": "Apt 105", "price": "£1,399 pcm",
              "address": "Apt 105, Castello Court, 309-311 Harrow Road, London W9",
              "url": "https://otm/16162549/"}


def _state(**ec):
    return {"extracted_context": {"focus_stack": [_FOCUS_TOP], **ec}}


def test_focus_url_injected_when_the_model_passed_no_identity():
    args = _inject_focus_url({"question": "is it a studio?"}, _state())
    assert args["property_url"] == "https://otm/16162549/"


def test_focus_url_injected_for_a_name_reference_to_the_focused_listing():
    for ref in ({"property_name": "Apt 105"},
                {"property_name": "Castello Court"},
                {"property_name": "Apt 105, Castello Court"},
                {"property_address": "Apt 105, Castello Court, 309-311 Harrow Road, London W9"},
                {"property_address": "309-311 Harrow Road"}):
        args = _inject_focus_url(dict(ref), _state())
        assert args["property_url"] == "https://otm/16162549/", ref


def test_focus_url_not_injected_for_a_different_unit_in_the_same_building():
    # The same-name hazard cuts both ways: a reference to ANOTHER unit must keep the
    # tool's own resolution (and its ambiguity refusal), never the focused listing's URL.
    for ref in ({"property_name": "Apt 107"},
                {"property_address": "Apt 107, Castello Court, 309-311 Harrow Road, London W9"},
                {"property_name": "Apt 105", "property_address": "Burnley Road, London NW10"}):
        args = _inject_focus_url(dict(ref), _state())
        assert "property_url" not in args, ref


def test_focus_url_never_overrides_a_url_the_model_supplied():
    args = _inject_focus_url({"property_url": "https://otm/other/"}, _state())
    assert args["property_url"] == "https://otm/other/"


def test_no_focus_no_injection():
    assert "property_url" not in _inject_focus_url({"property_name": "Apt 105"},
                                                   {"extracted_context": {}})
    # a focus record without a URL cannot supply one
    assert "property_url" not in _inject_focus_url(
        {}, {"extracted_context": {"focus_stack": [{"address": "Apt 105, Castello Court"}]}})


def test_injection_reads_the_scalar_focus_fallback_too():
    st = {"extracted_context": {"property_address": _FOCUS_TOP["address"],
                                "property_url": _FOCUS_TOP["url"]}}
    assert _inject_focus_url({}, st)["property_url"] == _FOCUS_TOP["url"]
