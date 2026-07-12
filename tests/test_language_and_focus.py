"""Reply-language policy + real "Ask AI listing focus" resolution.

Two production features, tested offline (NO live network / LLM):

  1. Reply-language rule (app.py helpers, AST-extracted so app.py's heavy module-level
     startup — RAG/FAISS/property load — never runs, mirroring test_app_input_validation):
       reply_language = 'zh' if the CURRENT user message contains CJK
                        else 'en' if the frontend UI language is 'en'
                        else 'zh'.
     i.e. English answers ONLY when UI=en AND the prompt is English; a missing/invalid
     ui_language defaults to 'en'.
  2. Tool override (search_properties_impl): an explicit reply_language ('zh'|'en')
     FORCES the language of every user-facing string (found summary / gate question /
     no-results), overriding the message-based is_cjk inference; unset keeps the legacy
     inference. The scrape/RAG layer is stubbed (same fixtures as
     test_move_in_availability / test_soft_criteria_gate).
  3. Ask-AI focus resolution (_resolve_focus_listing, AST-extracted, pure): url →
     address over the session's FULL last_results, then EXACT-only address over the demo
     CSV (the old substring/fuzzy branch — the wrong-city bleed — is gone), then a scalar
     fallback.
  4. search_direct + chat wiring — the endpoints forward ui_language → reply_language into
     the tool and localize their own composed strings (static wiring assertions, since
     the routes can't be imported without the heavy startup).
"""

import ast
import asyncio
import os
import re
import sys

import pytest

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

from core.scraping import on_demand
from core.tools.search_properties import search_properties_impl, set_rag_coordinator

_APP_PATH = os.path.join(_ROOT, "app", "app.py")


# ══════════════════════════════════════════════════════════════════════════
# AST extraction of the self-contained app.py helpers (no heavy startup).
# ══════════════════════════════════════════════════════════════════════════
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
    {"_has_cjk", "_normalize_ui_language", "_resolve_reply_language",
     "_resolve_focus_listing", "_compose_search_line"},
    {"_CJK_RE"},
)
_has_cjk = _APP["_has_cjk"]
_normalize_ui_language = _APP["_normalize_ui_language"]
_resolve_reply_language = _APP["_resolve_reply_language"]
_resolve_focus_listing = _APP["_resolve_focus_listing"]
_compose_search_line = _APP["_compose_search_line"]


def _func_source(name):
    """Return the exact source text of a top-level function in app.py (for wiring checks
    on routes that can't be imported without the heavy module-level startup)."""
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=_APP_PATH)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in app.py")


# ══════════════════════════════════════════════════════════════════════════
# 1. Reply-language decision matrix (helper level)
# ══════════════════════════════════════════════════════════════════════════
def test_has_cjk():
    assert _has_cjk("帮我找房")
    assert _has_cjk("a flat 在 Camden")   # mixed still counts as CJK
    assert not _has_cjk("find me a flat")
    assert not _has_cjk("")
    assert not _has_cjk(None)


def test_normalize_ui_language():
    assert _normalize_ui_language("zh") == "zh"
    assert _normalize_ui_language("EN") == "en"
    assert _normalize_ui_language("  Zh ") == "zh"
    assert _normalize_ui_language(None) == "en"      # absent -> en
    assert _normalize_ui_language("fr") == "en"      # unknown -> en
    assert _normalize_ui_language(123) == "en"       # non-string -> en
    assert _normalize_ui_language("") == "en"


@pytest.mark.parametrize("msg,ui,expected", [
    ("帮我找房", "en", "zh"),          # zh msg + ui en  -> zh (CJK wins)
    ("find me a flat", "zh", "zh"),    # en msg + ui zh  -> zh (UI zh)
    ("find me a flat", "en", "en"),    # en msg + ui en  -> en  (only case that is English)
    ("帮我找房", "zh", "zh"),          # zh msg + ui zh  -> zh
    ("find me a flat", None, "en"),    # missing ui      -> en-UI default -> en
    ("帮我找房", None, "zh"),          # zh msg + missing ui -> zh (CJK wins)
    ("find me a flat", "fr", "en"),    # invalid ui      -> en default
    ("", "en", "en"),                   # empty msg follows UI
    ("", "zh", "zh"),
    ("", None, "en"),
])
def test_reply_language_matrix(msg, ui, expected):
    assert _resolve_reply_language(msg, ui) == expected


# ══════════════════════════════════════════════════════════════════════════
# 2. _compose_search_line — localization + no emoji + backward compatibility
# ══════════════════════════════════════════════════════════════════════════
def test_compose_search_line_english():
    line = _compose_search_line("Camden", 1500, "month", 1, False, "UCL", 30,
                                "2026-09-01", reply_language="en")
    assert line.startswith("Search: Camden")
    assert "≤£1500/mo" in line
    assert "1 bed" in line
    assert "≤30min to UCL" in line
    assert "move-in ≥2026-09-01" in line
    assert "🔍" not in line   # emoji banned on dialog surfaces (this is a persisted turn)


def test_compose_search_line_chinese():
    line = _compose_search_line("Camden", 1500, "month", 1, False, "UCL", 30,
                                "2026-09-01", reply_language="zh")
    assert line.startswith("搜索：Camden")
    assert "≤£1500/月" in line
    assert "1 室" in line
    assert "≤30分钟到UCL" in line
    assert "入住 ≥2026-09-01" in line
    assert "🔍" not in line


def test_compose_search_line_default_is_english_and_backward_compatible():
    # Existing callers pass NO reply_language (positional) — must stay English + emoji-free.
    assert _compose_search_line("Camden", None, "month", None, True, None, None) \
        == "Search: Camden | no commute"
    # test_move_in_availability pins this exact contract too:
    line = _compose_search_line("Camden", 1500, "month", 1, False, None, None, "2026-09-01")
    assert "move-in ≥2026-09-01" in line
    assert "move-in" not in _compose_search_line("Camden", 1500, "month", 1, False, None, None)


# ══════════════════════════════════════════════════════════════════════════
# 3. Ask-AI focus resolution (pure helper)
# ══════════════════════════════════════════════════════════════════════════
def _manc_rec():
    """A REAL live-scraped Manchester recommendation, as stored in _sess.last_results."""
    return {
        "address": "12 Oxford Rd, Manchester M1 5AN",
        "price": "£1200/month",
        "url": "https://www.onthemarket.com/details/manc-123/",
        "description": "Bright 1-bed flat near the university. Bills included.",
        "available_from": "2026-09-01",
        "availability_status": "✅ 可入住",
        "bedrooms": 1,
        "property_type": "Flat",
        "area": "Manchester",
        "budget_status": "✅ 在预算内",
        "travel_time": "18 min to University of Manchester",
    }


_DEMO_CSV = [{
    "Address": "10 Baker Street, London NW1 6XE",
    "Room_Type_Category": "Studio",
    "Detailed_Amenities": "WiFi, Gym",
    "Guest_Policy": "No guests",
    "Payment_Rules": "Monthly",
    "Excluded_Features": "Parking",
    "Description": "London demo studio",
    "Enhanced_Description": "Enhanced London demo",
    "URL": "https://demo/london-10",
}]

_SESSION_KEYS = {
    "property_address", "property_price", "property_travel_time", "property_url",
    "description", "available_from", "availability_status", "bedrooms",
    "property_type", "area", "budget_status",
}
_SCALAR_KEYS = {"property_address", "property_price", "property_travel_time"}


def test_focus_resolves_by_url_from_session():
    payload = {"address": "12 Oxford Rd, Manchester M1 5AN", "price": "£1200/month",
               "url": "https://www.onthemarket.com/details/manc-123/"}
    ctx, source = _resolve_focus_listing(payload, [_manc_rec()], _DEMO_CSV)
    assert source == "session"
    # real record fields land under the agent-file key names
    assert ctx["description"].startswith("Bright 1-bed flat")
    assert ctx["available_from"] == "2026-09-01"
    assert ctx["availability_status"] == "✅ 可入住"
    assert ctx["bedrooms"] == 1
    assert ctx["property_type"] == "Flat"
    assert ctx["area"] == "Manchester"
    assert ctx["budget_status"] == "✅ 在预算内"
    assert ctx["property_url"] == "https://www.onthemarket.com/details/manc-123/"
    assert ctx["property_travel_time"] == "18 min to University of Manchester"
    # NO wrong-city demo bleed
    assert "amenities" not in ctx and "guest_policy" not in ctx


def test_focus_url_match_is_case_insensitive_and_trims():
    payload = {"address": "", "url": "  HTTPS://WWW.ONTHEMARKET.COM/DETAILS/MANC-123/  "}
    ctx, source = _resolve_focus_listing(payload, [_manc_rec()], _DEMO_CSV)
    assert source == "session"
    assert ctx["area"] == "Manchester"


def test_focus_resolves_by_address_when_no_url():
    payload = {"address": "  12 oxford rd, manchester m1 5an ", "url": ""}
    ctx, source = _resolve_focus_listing(payload, [_manc_rec()], _DEMO_CSV)
    assert source == "session"
    assert ctx["property_address"] == "12 Oxford Rd, Manchester M1 5AN"  # canonical from record
    assert ctx["description"].startswith("Bright")


def test_focus_session_url_beats_demo_csv_address():
    # Address would EXACT-match the demo CSV, but the url matches a session record first.
    payload = {"address": "10 Baker Street, London NW1 6XE",
               "url": "https://www.onthemarket.com/details/manc-123/"}
    ctx, source = _resolve_focus_listing(payload, [_manc_rec()], _DEMO_CSV)
    assert source == "session"
    assert ctx["area"] == "Manchester"
    assert "amenities" not in ctx


def test_focus_substring_address_does_NOT_match_demo_csv():
    # "Baker Street" is a SUBSTRING of the demo CSV address — the removed fuzzy branch
    # used to (wrongly) match it. Now it must fall through to the scalar fallback.
    payload = {"address": "Baker Street", "price": "£999/month", "travel_time": "20 min"}
    ctx, source = _resolve_focus_listing(payload, [], _DEMO_CSV)
    assert source == "scalar"
    assert "amenities" not in ctx
    assert set(ctx) == _SCALAR_KEYS
    assert ctx["property_address"] == "Baker Street"


def test_focus_exact_demo_csv_address_still_matches():
    # Branch 3 preserved: an EXACT demo-CSV address hit still loads its legacy keys.
    payload = {"address": "10 Baker Street, London NW1 6XE", "url": ""}
    ctx, source = _resolve_focus_listing(payload, [], _DEMO_CSV)
    assert source == "csv"
    assert ctx["amenities"] == "WiFi, Gym"
    assert ctx["guest_policy"] == "No guests"
    assert ctx["description"] == "London demo studio"
    assert ctx["property_url"] == "https://demo/london-10"


def test_focus_unknown_listing_scalar_fallback():
    payload = {"address": "99 Nowhere Lane, Leeds", "price": "£999/month", "travel_time": "20 min"}
    ctx, source = _resolve_focus_listing(payload, [_manc_rec()], _DEMO_CSV)
    assert source == "scalar"
    assert set(ctx) == _SCALAR_KEYS
    assert ctx["property_address"] == "99 Nowhere Lane, Leeds"
    assert ctx["property_price"] == "£999/month"
    assert ctx["property_travel_time"] == "20 min"


def test_focus_empty_payload_is_safe():
    ctx, source = _resolve_focus_listing({}, [_manc_rec()], _DEMO_CSV)
    assert source == "scalar"
    assert ctx["property_address"] == ""


# ══════════════════════════════════════════════════════════════════════════
# Tool-level stubs (identical shape to test_move_in_availability / soft_gate)
# ══════════════════════════════════════════════════════════════════════════
def _row(addr, price, geo="51.52,-0.13", rt="1 bed Flat", url=None):
    return {
        "Address": addr, "URL": url or "https://www.onthemarket.com/details/x/",
        "Price": f"£{price} pcm", "geo_location": geo, "Geo_Location": geo,
        "Room_Type_Category": rt, "Description": "Bright flat near transport. Bus 10 min.",
        "Images": [],
    }


class _FakeStore:
    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows)

    def search(self, query, top_k=10):
        return list(self.rows)


class _FakeCoordinator:
    def __init__(self):
        self.property_store = _FakeStore()

    def enhanced_search(self, query, criteria):
        rows = self.property_store.rows
        for r in rows:
            r.setdefault("similarity_score", 0.6)
        return list(rows), [], []


def _install_listings(monkeypatch, rows):
    m = {"slug": "x", "requested_location": "x", "requested_city": "london",
         "source": "scraped", "stale": False, "count": len(rows), "elapsed_s": 0.01, "message": ""}
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": list(rows), "meta": m})


def _no_scrape(monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate must fire before scraping")))


@pytest.fixture
def stub_env(monkeypatch):
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# ══════════════════════════════════════════════════════════════════════════
# 2b. Tool override — reply_language forces the language of user-facing strings
# ══════════════════════════════════════════════════════════════════════════
def test_reply_language_en_overrides_chinese_message_in_summary(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", current_message="帮我找房", no_commute=True, confirmed=True,
               max_budget=1500, bedrooms=1, reply_language="en")
    assert res["status"] == "found"
    assert "I found" in res["summary"]
    assert "为你找到" not in res["summary"]


def test_reply_language_zh_overrides_english_message_in_summary(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", current_message="find me a flat", no_commute=True, confirmed=True,
               max_budget=1500, bedrooms=1, reply_language="zh")
    assert res["status"] == "found"
    assert "为你找到" in res["summary"]
    assert "I found" not in res["summary"]


def test_reply_language_zh_with_no_message_is_the_search_direct_case(stub_env, monkeypatch):
    # /api/search_direct forwards reply_language but has NO current_message to infer from;
    # the override must still force Chinese (the original mixed-language bug).
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=1500, bedrooms=1,
               reply_language="zh")
    assert res["status"] == "found"
    assert "为你找到" in res["summary"]


def test_reply_language_unset_keeps_legacy_message_inference(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    zh = _run(area="Camden", current_message="帮我找房", no_commute=True, confirmed=True,
              max_budget=1500, bedrooms=1)
    assert "为你找到" in zh["summary"]
    en = _run(area="Camden", current_message="find me a flat", no_commute=True, confirmed=True,
              max_budget=1500, bedrooms=1)
    assert "I found" in en["summary"]


def test_reply_language_en_overrides_gate_question(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="帮我找房", reply_language="en")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert "Before I search" in res["question"]
    assert "在搜索之前" not in res["question"]


def test_reply_language_zh_overrides_gate_question(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="find me a place", reply_language="zh")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert "在搜索之前" in res["question"]
    assert "Before I search" not in res["question"]


def test_reply_language_localizes_no_results(stub_env, monkeypatch):
    _install_listings(monkeypatch, [])  # empty scrape -> honest no_results
    en = _run(area="Camden", no_commute=True, confirmed=True, current_message="帮我找房",
              reply_language="en")
    assert en["status"] == "no_results"
    assert "couldn't find" in en["message"]
    zh = _run(area="Camden", no_commute=True, confirmed=True, current_message="find me a flat",
              reply_language="zh")
    assert zh["status"] == "no_results"
    assert "没有找到" in zh["message"]


def test_reply_language_invalid_value_falls_back_to_inference(stub_env, monkeypatch):
    # A garbage reply_language must NOT force English — it falls back to message inference.
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", current_message="帮我找房", no_commute=True, confirmed=True,
               max_budget=1500, bedrooms=1, reply_language="fr")
    assert "为你找到" in res["summary"]   # inferred zh from the CJK message


# ══════════════════════════════════════════════════════════════════════════
# 4. Endpoint wiring (static — routes can't import without heavy startup)
# ══════════════════════════════════════════════════════════════════════════
def test_search_direct_forwards_ui_language_and_reply_language():
    src = _func_source("api_search_direct")
    assert "_normalize_ui_language(data.get('ui_language'))" in src
    assert "reply_language = ui_language" in src
    # forwarded into the tool call AND into the composed one-liner
    assert "reply_language=reply_language" in src
    assert "search_properties_impl(" in src


def test_alex_chat_path_sets_reply_language_and_recursion_limit():
    src = _func_source("handle_with_react_agent")
    assert "extracted_context['reply_language']" in src
    assert "_resolve_reply_language(user_message, ui_language)" in src
    assert "recursion_limit" in src
    assert "GRAPH_RECURSION_LIMIT" in src


def test_alex_chat_path_resolves_focus_from_full_last_results():
    src = _func_source("handle_with_react_agent")
    # focus resolved from the FULL last_results snapshot captured under the phase-1 lock
    assert "last_results_snapshot" in src
    assert "_resolve_focus_listing(" in src


def test_api_alex_reads_ui_language():
    src = _func_source("api_alex")
    assert "_normalize_ui_language(data.get('ui_language'))" in src
    assert "ui_language=ui_language" in src
