"""Accumulated recommended-listings registry + the get_property_details URL guidance.

Covers requirement 2:
  * _merge_recommended_registry — merge / dedup (url then address) / stable first-seen
    index / 200-item cap / malformed-entry tolerance (pure helper, AST-extracted).
  * _resolve_focus_listing registry hit — a listing recommended in ANY earlier turn is
    resolvable (registry match) and, when a cache lookup is injected, enriched with the
    real full fields (description/amenities); without a cache it degrades to the
    lightweight registry fields.
  * render_recommended_index — compact numbered index + the explicit "call
    get_property_details with the URL for full details" instruction.
  * Snapshot persistence — the registry survives a build_turn_snapshot -> patch round trip
    (restart/fork gate).

Pure helpers are AST-extracted from app.py (no heavy Flask startup), mirroring
test_language_and_focus.py.
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


def _load_app_symbols(wanted_defs, wanted_assigns=()):
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=_APP_PATH)
    picked = []
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and getattr(node, "name", None) in wanted_defs):
            picked.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in wanted_assigns for n in names):
                picked.append(node)
    module = ast.Module(body=picked, type_ignores=[])
    ns = {"re": re}
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    missing = wanted_defs - ns.keys()
    assert not missing, f"failed to extract {missing} from app.py"
    return ns


_APP = _load_app_symbols(
    {"_registry_entry_key", "_merge_recommended_registry", "_resolve_focus_listing"},
    {"_REGISTRY_MAX_ENTRIES"},
)
_registry_entry_key = _APP["_registry_entry_key"]
_merge_recommended_registry = _APP["_merge_recommended_registry"]
_resolve_focus_listing = _APP["_resolve_focus_listing"]
_REGISTRY_MAX_ENTRIES = _APP["_REGISTRY_MAX_ENTRIES"]

from core.context_assembler import (  # noqa: E402
    render_recommended_index, build_turn_snapshot, snapshot_to_session_patch,
)


def _rec(addr, url, price="£1000", area="Manchester", tt="20 min", avail=None):
    return {"address": addr, "url": url, "price": price, "area": area,
            "travel_time": tt, "available_from": avail}


# ── _registry_entry_key ───────────────────────────────────────────────────────
def test_entry_key_prefers_url_normalized():
    assert _registry_entry_key(" HTTPS://OTM/1/ ", "addr") == ("url", "https://otm/1")
    assert _registry_entry_key("", "  12 Oak Rd ") == ("address", "12 oak rd")
    assert _registry_entry_key("", "") is None
    assert _registry_entry_key(None, None) is None


# ── merge / dedup / stable index / cap ────────────────────────────────────────
def test_merge_from_empty_assigns_sequential_indexes():
    reg = _merge_recommended_registry(None, [_rec("A", "https://otm/1"), _rec("B", "https://otm/2")])
    assert [e["index"] for e in reg] == [1, 2]
    assert reg[0]["address"] == "A" and reg[0]["url"] == "https://otm/1"
    # only the lightweight keys are stored (no description / big fields)
    assert set(reg[0]) == {"index", "address", "price", "area", "travel_time", "url", "available_from"}


def test_merge_dedups_by_url_and_keeps_first_seen_index():
    reg1 = _merge_recommended_registry(None, [_rec("A", "https://otm/1"), _rec("B", "https://otm/2")])
    # Second turn re-shows B (same url, trailing slash differs) + a new C.
    reg2 = _merge_recommended_registry(
        reg1, [_rec("B (updated)", "https://otm/2/"), _rec("C", "https://otm/3")])
    by_url = {e["url"]: e for e in reg2}
    assert len(reg2) == 3
    assert by_url["https://otm/2"]["index"] == 2      # B keeps its first-seen index
    assert by_url["https://otm/2"]["address"] == "B"  # first-seen record retained, not overwritten
    assert by_url["https://otm/3"]["index"] == 3      # C is appended next


def test_merge_dedups_by_address_when_no_url():
    reg1 = _merge_recommended_registry(None, [_rec("Same Place", "")])
    reg2 = _merge_recommended_registry(reg1, [_rec("same place", "")])  # case-insensitive addr dedup
    assert len(reg2) == 1
    assert reg2[0]["index"] == 1


def test_merge_skips_malformed_and_keyless():
    reg = _merge_recommended_registry(
        [{"index": 7, "address": "Keep", "url": "https://otm/keep"}],
        ["not a dict", {"address": "", "url": ""}, _rec("New", "https://otm/new")])
    # Malformed / keyless dropped; existing max index (7) respected -> new one is 8.
    assert len(reg) == 2
    new = [e for e in reg if e["address"] == "New"][0]
    assert new["index"] == 8


def test_merge_respects_cap():
    seed = _merge_recommended_registry(
        None, [_rec(f"A{i}", f"https://otm/{i}") for i in range(_REGISTRY_MAX_ENTRIES)])
    assert len(seed) == _REGISTRY_MAX_ENTRIES
    over = _merge_recommended_registry(seed, [_rec("OVER", "https://otm/over")])
    assert len(over) == _REGISTRY_MAX_ENTRIES              # capped: new entry rejected
    assert all(e["address"] != "OVER" for e in over)


def test_merge_does_not_mutate_input():
    existing = [{"index": 1, "address": "A", "url": "https://otm/1"}]
    _merge_recommended_registry(existing, [_rec("B", "https://otm/2")])
    assert existing == [{"index": 1, "address": "A", "url": "https://otm/1"}]  # untouched


# ── _resolve_focus_listing registry hit (historical listings) ────────────────
_REG = [_rec("Historic Flat, Leeds", "https://otm/hist-9/", price="£900", area="Leeds",
             tt="10 min", avail="2026-10-01")]


def test_focus_registry_hit_without_cache_uses_lightweight_fields():
    payload = {"address": "", "url": "https://otm/hist-9"}   # note: no trailing slash
    ctx, source = _resolve_focus_listing(payload, [], [], registry=_REG, cache_lookup=None)
    assert source == "registry"
    assert ctx["property_address"] == "Historic Flat, Leeds"
    assert ctx["property_price"] == "£900"
    assert ctx["property_travel_time"] == "10 min"
    assert ctx["area"] == "Leeds"
    assert ctx["available_from"] == "2026-10-01"
    assert "description" not in ctx  # no big fields without a cache lookup


def test_focus_registry_hit_with_cache_enriches_full_fields():
    def fake_lookup(url):
        assert url == "https://otm/hist-9/"
        return {"Address": "Historic Flat, Leeds LS1", "Price": "£950",
                "Description": "Lovely bright flat, bills included.",
                "Detailed_Amenities": "WiFi, Gym", "Guest_Policy": "No parties",
                "URL": url, "Available From": "2026-10-05"}
    payload = {"address": "", "url": "https://otm/hist-9"}
    ctx, source = _resolve_focus_listing(payload, [], [], registry=_REG, cache_lookup=fake_lookup)
    assert source == "registry+cache"
    assert ctx["description"].startswith("Lovely bright flat")
    assert ctx["amenities"] == "WiFi, Gym"
    assert ctx["guest_policy"] == "No parties"
    assert ctx["property_address"] == "Historic Flat, Leeds LS1"  # canonical from cache
    assert ctx["property_price"] == "£950"
    assert ctx["available_from"] == "2026-10-05"


def test_focus_registry_matches_by_address_when_no_url():
    payload = {"address": " historic flat, leeds ", "url": ""}
    ctx, source = _resolve_focus_listing(payload, [], [], registry=_REG, cache_lookup=None)
    assert source == "registry"
    assert ctx["area"] == "Leeds"


def test_focus_session_still_beats_registry():
    # A record present in BOTH the session snapshot and the registry resolves via session
    # (the freshest, full record) first.
    sess = [{"address": "Historic Flat, Leeds", "url": "https://otm/hist-9/",
             "description": "Session copy", "area": "Leeds"}]
    payload = {"address": "", "url": "https://otm/hist-9/"}
    ctx, source = _resolve_focus_listing(payload, sess, [], registry=_REG, cache_lookup=None)
    assert source == "session"
    assert ctx["description"] == "Session copy"


def test_focus_no_registry_is_backward_compatible():
    # Unknown listing, registry omitted -> scalar fallback exactly as before.
    payload = {"address": "99 Nowhere", "price": "£1", "travel_time": "1 min"}
    ctx, source = _resolve_focus_listing(payload, [], [])
    assert source == "scalar"
    assert set(ctx) == {"property_address", "property_price", "property_travel_time"}


# ── render_recommended_index ──────────────────────────────────────────────────
def test_render_index_is_compact_and_points_to_the_tool():
    reg = _merge_recommended_registry(
        None, [_rec("12 Oak Rd, Manchester", "https://otm/1", price="£1200", tt="20 min"),
               _rec("5 Pine St, Manchester", "https://otm/2", price="£650", avail="2026-09-01")])
    block = render_recommended_index(reg)
    assert "RECOMMENDED LISTINGS INDEX" in block
    assert "get_property_details" in block          # explicit tool guidance
    assert "[1] 12 Oak Rd, Manchester" in block
    assert "https://otm/1" in block                 # URL present for the tool to reuse
    assert "price £1200" in block
    assert "available 2026-09-01" in block
    # summaries only: no description text is ever inlined
    assert "Description" not in block


def test_render_index_empty_is_blank():
    assert render_recommended_index([]) == ""
    assert render_recommended_index(None) == ""


# ── snapshot persistence (restart / fork gate) ────────────────────────────────
def test_registry_survives_snapshot_round_trip():
    reg = _merge_recommended_registry(None, [_rec("A", "https://otm/1"), _rec("B", "https://otm/2")])
    state = {"user_preferences": {}, "accumulated_search_criteria": {},
             "extracted_context": {"recommended_registry": reg}}
    snap = build_turn_snapshot(turn_id="t1", persistent_state=state)
    assert snap["recommended_registry"] == reg
    patch = snapshot_to_session_patch(snap)
    assert patch["recommended_registry"] == reg
    # deep-copied, so mutating the snapshot cannot corrupt the source state
    snap["recommended_registry"].append({"index": 99})
    assert len(state["extracted_context"]["recommended_registry"]) == 2
