"""Legacy-arch + tool defects found by a post-release test round (F4).

Covered here, one section per finding:

* P1-4  the legacy planner asked ``search_nearby_pois`` about the CITY ("London") when the
        user had named an area ("Shoreditch"), and the geocode ladder rejects a bare city,
        so the tool returned "Could not find coordinates for address: London" and 0 POIs.
* P1-10 one legacy safety reply printed two different monthly averages for one total
        (7681 -> "about 2560 per month" and "Monthly average: 1280").
* P1-11 a zh answer kept its exclusion reasons in English.
* P1-12 a listing returned for a CAMDEN search was labelled "Islington WC1N" — a Camden
        postcode.
* P2-4  legacy tool artifacts carried no timings at all, so every legacy canary record
        shipped ``tool_latency`` with null p50/max while ``tool_ledger_status`` said
        "complete".
* tools hygiene — a hostile argument (lone surrogate, empty string, wrong type, non-ASCII
  place name) must produce a STRUCTURED refusal, never an exception.
"""

import asyncio
import json
import math
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from core.candidate_validation import candidate_reason_text, render_candidate_status  # noqa: E402
from core.tools import search_nearby_pois as poi_mod  # noqa: E402
from core.tools._argtext import clean_arg_text  # noqa: E402
from core.tools.check_safety import (  # noqa: E402
    _generate_safety_analysis,
    _generate_scoring_explanation,
    check_safety_impl,
    crime_window,
)
from core.tools.search_properties import (  # noqa: E402
    borough_for_postcode,
    most_specific_known_area,
    reconcile_area_label,
)

# The exact text T1 sent to BOTH pools (agenttest-t1-e1-legacy / -e1-fc).
T1_POI_MESSAGE = ("What's near Shoreditch in London? I mean supermarkets, gyms and cafes "
                  "within walking distance.")


# ───────────────────────── P1-4: POI geocode target ─────────────────────────

def test_bare_city_is_not_a_geocodable_target():
    """Why the old fallback could never work: the ladder refuses a bare city outright."""
    assert poi_mod.address_variants("London") == []
    assert poi_mod.address_variants("Shoreditch")[0] == "Shoreditch, London, UK"


def test_most_specific_area_prefers_the_london_area_over_the_city():
    assert most_specific_known_area(T1_POI_MESSAGE) == "Shoreditch"
    assert most_specific_known_area("a 2 bed in Camden, London under 2000") == "Camden"
    # a bare city stays a city when nothing more specific was said
    assert most_specific_known_area("flats in Manchester") == "Manchester"
    # nothing curated named -> None, never a guess
    assert most_specific_known_area("somewhere nice and central") is None
    for hostile in ("", "   ", None, 12345, "\ud800"):
        assert most_specific_known_area(hostile) is None


def test_legacy_planner_targets_the_area_the_user_named_not_the_city():
    """P1-4 regression: this is the exact params dict the legacy graph builds."""
    from core.langgraph_agent import _build_tool_params

    ctx = {"current_message": T1_POI_MESSAGE}
    decision = _build_tool_params("search_nearby_pois", T1_POI_MESSAGE, ctx, None)
    assert decision["tool"] == "search_nearby_pois"
    assert decision["params"]["address"] == "Shoreditch"
    # and the target the two arches geocode is now the SAME string fc resolved
    assert poi_mod.address_variants(decision["params"]["address"])


def test_legacy_planner_still_prefers_an_explicit_postcode():
    from core.langgraph_agent import _build_tool_params

    msg = "What supermarkets are near E1 6AN?"
    decision = _build_tool_params("search_nearby_pois", msg, {"current_message": msg}, None)
    assert decision["params"]["address"] == "E1 6AN"


def test_poi_tool_upgrades_a_generic_address_from_the_message():
    """Defence in depth: any caller that still passes a bare city gets the area searched."""
    assert poi_mod._upgrade_generic_address("London", T1_POI_MESSAGE) == "Shoreditch"
    # a real address is NEVER overridden by a place mentioned in passing
    assert poi_mod._upgrade_generic_address(
        "12 Well Street, E9 7QX", T1_POI_MESSAGE) == "12 Well Street, E9 7QX"
    # nothing to upgrade to -> unchanged
    assert poi_mod._upgrade_generic_address("London", "what is nearby?") == "London"


def test_poi_tool_searches_shoreditch_for_the_t1_message(monkeypatch):
    seen = {}

    def fake_geocode(address, deadline=None):
        seen["address"] = address
        return (51.5265, -0.0784)

    monkeypatch.setattr(poi_mod, "geocode_address", fake_geocode)
    monkeypatch.setattr(poi_mod, "query_osm_pois",
                        lambda *a, **k: [])
    out = poi_mod.search_nearby_pois_impl(address="London", user_query=T1_POI_MESSAGE)
    assert seen["address"] == "Shoreditch"
    assert out["success"] is True
    assert out["address"] == "Shoreditch"


# ───────────────────── P1-10: one total, one monthly average ─────────────────

_E1_6AN = {
    "total_crimes_6m": 7681,
    "months_covered": 3,
    "crimes_per_month": 7681 / 3,
    "most_recent_month_count": 2567,
    "category_breakdown": {"Violent Crime": 1473, "Anti Social Behaviour": 1169},
    "crime_trend": "stable",
}


def _numbers_after(text, needle):
    import re
    return [line for line in text.splitlines() if needle in line]


@pytest.mark.parametrize("chinese", [False, True])
def test_one_reply_states_one_monthly_average(chinese):
    months, per_month = crime_window(_E1_6AN, _E1_6AN["crimes_per_month"])
    assert months == 3 and round(per_month) == 2560
    scoring = _generate_scoring_explanation(
        7681, 15, _E1_6AN, chinese, band="among the busiest areas we sampled",
        per_month=per_month)
    analysis = _generate_safety_analysis(_E1_6AN, "E1 6AN", chinese, per_month=per_month)
    # the number the old build contradicted itself with
    assert "1280" not in analysis
    assert "2560" in scoring and "2560" in analysis
    window_line = _numbers_after(analysis, "总计" if chinese else "total")[0]
    assert "3" in window_line and "6-month" not in window_line and "6个月" not in window_line


def test_crime_window_survives_a_missing_or_junk_payload():
    assert crime_window({}) == (3, 0.0)
    assert crime_window({"months_covered": None, "total_crimes_6m": "junk"}) == (3, 0.0)
    assert crime_window({"months_covered": 0, "total_crimes_6m": 60})[1] == 20.0
    months, rate = crime_window({"months_covered": 6, "total_crimes_6m": 600})
    assert (months, rate) == (6, 100.0)
    assert math.isfinite(rate)


# ───────────────────── P1-11: exclusion reasons follow the reply language ─────

_T1_VALIDATION = {
    "eligible": [{"candidate": {"address": "Rugby House 6 Great Ormond Street, Camden WC1N",
                                "price": "£1500/month"}}],
    "excluded": [{"candidate": {"address": "Grafton Road, London NW5", "price": "£2200/month"},
                  "reasons": ["alternative is outside the declared hard result set",
                              "monthly price 2200 exceeds budget 2000"]}],
    "unknown": [],
}


def test_zh_answer_has_no_english_exclusion_boilerplate():
    zh = render_candidate_status(_T1_VALIDATION, language="zh")
    assert "alternative is outside the declared hard result set" not in zh
    assert "exceeds budget" not in zh
    # 7483f77 owns the zh wording (candidate_validation._candidate_reason_zh).
    assert "月租 £2200 超过预算 £2000" in zh


def test_en_reasons_are_unchanged():
    en = render_candidate_status(_T1_VALIDATION, language="en")
    assert "alternative is outside the declared hard result set" in en
    assert "monthly price 2200 exceeds budget 2000" in en


def test_every_producible_reason_shape_has_a_chinese_rendering():
    produced = [
        "alternative is outside the declared hard result set",
        "monthly price 2200 exceeds budget 2000",
        "bedroom count 1 does not equal requested 2",
        "room type does not match studio",
        "area islington is outside the requested areas",
        "missing required features: step-free access",
        "available from 2026-10-01, after requested date",
        "measured commute 55 min exceeds limit 30 min",
        "hard constraint failed",
        "price is not structured",
        "bedroom count is not verified",
        "room type is not verified",
        "area is not verified",
        "property features are not structurally verified",
        "required features are not verified: lift",
        "availability is not verified",
        "commute evidence is missing",
        "commute tool timeout",
        "commute result cannot be linked to this listing",
        "commute result has no measured duration",
        "insufficient evidence",
    ]
    for reason in produced:
        zh = candidate_reason_text(reason, language="zh")
        assert zh != reason, reason
        assert not any(ch.isascii() and ch.isalpha() for ch in zh.replace("-", "")) or \
            any("一" <= ch <= "鿿" for ch in zh), reason
        # English is the canonical form and must be returned verbatim
        assert candidate_reason_text(reason, language="en") == reason


def test_an_unknown_reason_gets_a_generic_chinese_label_not_english():
    # Ported from 0105aeb, whose localize_reason passed unknown reasons through verbatim.
    # 7483f77's renderer (the one that survived the merge) uses a generic zh label instead,
    # so a zh answer never carries an English reason body.
    zh = candidate_reason_text("something nobody wrote a rule for", language="zh")
    assert zh and not any(ch.isascii() and ch.isalpha() for ch in zh)


# ───────────────── P1-12: the label agrees with the postcode ──────────────────

def test_the_t1_listing_is_no_longer_labelled_islington():
    assert reconcile_area_label("Rugby House 6 Great Ormond Street, Islington WC1N") == \
        "Rugby House 6 Great Ormond Street, Camden WC1N"


def test_reconciliation_only_ever_removes_a_contradiction():
    # agreeing label -> untouched
    assert reconcile_area_label("Camden Park Road, London, NW1") == \
        "Camden Park Road, London, NW1"
    # a borough word inside a STREET name is never rewritten
    assert reconcile_area_label("Camden Park Road, London, E1 4AA") == \
        "Camden Park Road, London, E1 4AA"
    # a neighbourhood name is not a borough claim
    assert reconcile_area_label("Shoreditch High Street, E1 6AN") == \
        "Shoreditch High Street, E1 6AN"
    # an AMBIGUOUS district is deliberately absent from the table -> no correction
    assert borough_for_postcode("N1 4AB") is None
    assert reconcile_area_label("Somewhere, Islington N1 4AB") == "Somewhere, Islington N1 4AB"
    assert borough_for_postcode("SE1 9SG") is None
    # sub-district override wins over its district
    assert borough_for_postcode("WC1X 0AA") == "Islington"
    assert borough_for_postcode("WC1N 3JH") == "Camden"


def test_reconciliation_never_raises():
    for hostile in ("", "   ", None, 12345, "\ud800", ["x"], {"a": 1}):
        assert isinstance(reconcile_area_label(hostile), str)
        assert isinstance(borough_for_postcode(str(hostile)), (str, type(None)))


def test_search_rows_expose_the_reconciled_and_the_raw_provider_label():
    """The row builders must keep the provider string for traceability."""
    import re
    source = open(os.path.join(_ROOT, "app", "core", "tools", "search_properties.py"),
                  encoding="utf-8").read()
    assert source.count("'address': reconcile_area_label(") == 2
    assert source.count("'provider_address': prop.get('Address'") == 2
    assert not re.search(r"^\s+'address': prop\.get\('Address'", source, re.M)


# ─────────────────────── P2-4: legacy per-tool timings ───────────────────────

def test_legacy_artifact_carries_monotonic_timings():
    from core.langgraph_agent import _legacy_tool_artifact

    art = _legacy_tool_artifact(0, "check_safety", {"ok": True}, "digest",
                                started_at=100.0, ended_at=100.25)
    assert art["started_at"] == 100.0 and art["ended_at"] == 100.25
    assert art["elapsed_ms"] == 250
    # a backwards clock can never produce a negative duration
    assert _legacy_tool_artifact(0, "t", None, started_at=5.0, ended_at=4.0)["elapsed_ms"] == 0


def _fold_tool_latency(artifacts):
    """The same fold app.py::_tool_latency_from_artifacts performs (it is a nested
    function there, so the CONTRACT is reproduced rather than imported)."""
    samples = {}
    for a in artifacts:
        slot = samples.setdefault(a["tool"], [])
        elapsed = a.get("elapsed_ms")
        if (not a.get("timed_out") and not a.get("abandoned")
                and not isinstance(elapsed, bool)
                and isinstance(elapsed, (int, float))
                and math.isfinite(float(elapsed)) and float(elapsed) >= 0):
            slot.append(float(elapsed))
    return {name: {"p50_ms": (sorted(s)[max(0, math.ceil(0.5 * len(s)) - 1)] if s else None),
                   "max_ms": (max(s) if s else None)}
            for name, s in samples.items()}


def test_a_legacy_turn_produces_non_null_tool_latency():
    """End to end through the real execute_tool node: the artifact the turn summariser
    folds must yield numbers, not the nulls every legacy canary record used to carry."""
    from core.langgraph_agent import _make_execute_tool_node
    from core.tool_system import ToolResult

    class _FakeTool:
        side_effect = "none"
        version = "1"

    class _Registry:
        def get(self, name):
            return _FakeTool()

        async def execute_tool(self, name, **kwargs):
            await asyncio.sleep(0.03)
            return ToolResult(True, data={"ok": True}, tool_name=name)

    node = _make_execute_tool_node(_Registry())
    state = {
        "tool_decision": {"tool": "check_safety", "params": {"address": "E1 6AN"}},
        "accumulated_search_criteria": {},
        "extracted_context": {"current_message": "is E1 6AN safe?"},
        "user_query": "is E1 6AN safe?",
        "loop_turn": 0, "run_id": "f5d", "tool_artifacts": [],
        "context_tainted": False, "turn_start_monotonic": time.monotonic(),
    }
    command = asyncio.run(node(state))
    artifacts = command.update["tool_artifacts"]
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["tool"] == "check_safety" and art["success"] is True
    assert art["ended_at"] >= art["started_at"]
    assert art["elapsed_ms"] >= 25
    folded = _fold_tool_latency(artifacts)
    assert folded["check_safety"]["p50_ms"] is not None
    assert folded["check_safety"]["max_ms"] is not None


def test_wave_task_results_carry_the_same_timings():
    """The wave writes task_results, not artifacts (tool_artifacts has no reducer), which
    is why a turn could report tool_batches=1 with no tool_latency at all."""
    from core.langgraph_agent import _make_task_worker_node
    from core.tool_system import ToolResult

    class _Registry:
        def get(self, name):
            return type("T", (), {"side_effect": "none", "version": "1"})()

        async def execute_tool(self, name, **kwargs):
            await asyncio.sleep(0.02)
            return ToolResult(True, data={"results": "x"}, tool_name=name)

    worker = _make_task_worker_node(_Registry())
    out = asyncio.run(worker({"task": {"id": "t1", "index": 0, "tool": "web_search",
                                       "params": {"query": "q"}}, "run_id": "f5d"}))
    result = out["task_results"][0]
    assert result["elapsed_ms"] >= 15
    assert result["ended_at"] >= result["started_at"]


# ───────────────────────── tools hygiene: hostile input ──────────────────────

HOSTILE = ["\ud800", "𐏿 text", "", "   ", None, 12345, ["x"],
           "Café Münster", "北京市朝阳区", "München Straße", "A" * 4000]


def _is_utf8_serialisable(obj):
    json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return True


@pytest.mark.parametrize("hostile", HOSTILE)
def test_clean_arg_text_never_raises(hostile):
    out = clean_arg_text(hostile)
    assert isinstance(out, str)
    out.encode("utf-8")


@pytest.mark.parametrize("hostile", HOSTILE)
def test_search_nearby_pois_returns_a_structured_error(hostile, monkeypatch):
    monkeypatch.setattr(poi_mod, "geocode_address", lambda a, deadline=None: None)
    out = poi_mod.search_nearby_pois_impl(address=hostile, user_query=hostile,
                                          poi_type=hostile)
    assert isinstance(out, dict)
    assert out["success"] is False
    assert isinstance(out["error"], str) and out["error"]
    assert _is_utf8_serialisable(out)


@pytest.mark.parametrize("hostile", ["", "   ", None, 12345, ["x"]])
def test_check_safety_refuses_a_missing_location_without_raising(hostile):
    out = check_safety_impl(address=hostile, area=None, user_query="\ud800")
    assert isinstance(out, dict)
    if not clean_arg_text(hostile):
        assert out["safety_score"] is None
        assert "address" in out["error"]
    assert _is_utf8_serialisable(out)


@pytest.mark.parametrize("hostile", HOSTILE)
def test_search_properties_argument_boundary_never_raises(hostile):
    """The statements before search_properties_impl's big try/except must survive a
    malformed argument (they used to escape as an unhandled TypeError)."""
    from core.tools.search_properties import _as_text_list

    assert _as_text_list(hostile) is None or isinstance(_as_text_list(hostile), list)
    for item in (_as_text_list(hostile) or []):
        assert isinstance(item, str)
        item.encode("utf-8")


def test_search_properties_impl_returns_a_dict_for_a_hostile_query(monkeypatch):
    import core.tools.search_properties as sp

    async def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(sp, "get_listings", _boom, raising=False)
    out = asyncio.run(sp.search_properties_impl(
        user_query="\ud800", current_message=None, area=12345, areas="Camden",
        property_features="lift", max_budget=None, limit=1))
    assert isinstance(out, dict)
    assert _is_utf8_serialisable(out)


# ──────── found while verifying P1-11 live: zh area extraction lost the borough ────────

def test_zh_area_extraction_keeps_the_borough_not_the_city():
    """"我想在伦敦卡姆登租..." names BOTH 伦敦 (London) and 卡姆登 (Camden). The scan used to
    return whichever came first in table order — 伦敦, at index 4 — so a Camden request
    searched all of London (observed live: fixtest-f4-b2-legacy, area "London")."""
    from core.tools.search_properties import _ZH_AREA_MAP, _extract_area

    assert list(_ZH_AREA_MAP).index("伦敦") < list(_ZH_AREA_MAP).index("卡姆登")
    assert _extract_area("我想在伦敦卡姆登租一套两居室，预算每月最多2000英镑，有什么推荐？") == "Camden"
    assert _extract_area("在伦敦的格林威治") == "Greenwich"
    assert _extract_area("我想在伦敦肯辛顿租房") == "Kensington"
    # a bare city is still a city
    assert _extract_area("我想住伦敦") == "London"
    assert _extract_area("曼彻斯特有什么房源") == "Manchester"
    # the longest key still wins inside one place ("大象城堡" over "象堡")
    assert _extract_area("大象城堡附近") == "Elephant and Castle"
    # nonsense still yields nothing
    assert _extract_area("火星上有房子吗") is None


# ─── P1-9: internal vocabulary must not reach the reader (langgraph_agent half) ───

_LEAKED_VOCABULARY = ("focus stack", "focus_stack", "context stack", "focus-stack",
                      "current focus")


def _assert_no_internal_vocabulary(text, where):
    low = str(text).lower()
    for token in _LEAKED_VOCABULARY:
        assert token not in low, f"{where} leaks {token!r}: {text[:200]!r}"


def test_the_focused_listings_block_uses_plain_language():
    """Live, the model quoted this block's vocabulary straight back at the user:
    "I don't have a clear **focus stack** showing which listings are being discussed"
    (agenttest-t1-f3-fc). The block the model reads must not contain the words."""
    from core.langgraph_agent import build_context_info

    ctx = {"focus_stack": [
        {"address": "Torriano Avenue, Kentish Town", "price": "£1370/month",
         "url": "https://example.invalid/1"},
        {"address": "Barker Drive, Camden NW1", "price": "£2000/month",
         "travel_time": "22 min"},
    ]}
    rendered = build_context_info(ctx, "get_property_details", {})
    _assert_no_internal_vocabulary(rendered, "build_context_info focus block")
    # behaviour is unchanged: both listings, oldest -> newest, the last one marked
    assert rendered.index("Torriano Avenue") < rendered.index("Barker Drive")
    assert "[DISCUSSING NOW]" in rendered
    assert rendered.count("[DISCUSSING NOW]") == 1
    assert "£1370/month" in rendered and "22 min" in rendered


def test_the_advertised_reference_phrasings_are_ones_the_resolver_accepts():
    """The block tells the model what a user's phrasing means; if the resolver did not
    recognise those phrasings the prompt would be advertising a broken contract."""
    from core.langgraph_agent import _is_previous_focus_reference, _resolve_focus_reference

    assert _is_previous_focus_reference("what about the one before")
    assert _is_previous_focus_reference("上一个看的那套呢")
    ctx = {"focus_stack": [{"address": "First Rd"}, {"address": "Second Rd"}]}
    assert _resolve_focus_reference("the one before", ctx)["address"] == "First Rd"
    assert _resolve_focus_reference("this one", ctx)["address"] == "Second Rd"
    assert _resolve_focus_reference("这个房源怎么样", ctx)["address"] == "Second Rd"
    # PRE-EXISTING and deliberately NOT changed here: the block has always advertised
    # "this listing", which _EN_FOCUS_DEICTICS does not contain. Adding it would be a
    # behaviour change, which this P1-9 wording pass is explicitly not allowed to make.
    from core.langgraph_agent import _EN_FOCUS_DEICTICS
    assert "this listing" not in _EN_FOCUS_DEICTICS


def test_no_user_facing_string_in_langgraph_agent_carries_the_internal_vocabulary():
    """Every string LITERAL in the module (prompts and rendered copy alike) is checked;
    dict-key lookups of the internal identifier `focus_stack` are the only allowance,
    and comments are not strings so they are out of scope by construction."""
    import ast

    source = open(os.path.join(_ROOT, "app", "core", "langgraph_agent.py"),
                  encoding="utf-8").read()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value == "focus_stack":          # the internal state key, never rendered
            continue
        low = node.value.lower()
        if any(token in low for token in _LEAKED_VOCABULARY):
            offenders.append((node.lineno, node.value[:120]))
    assert offenders == [], offenders


# ── P1-1 (legacy arch): "raise the budget by £200" must apply to the CURRENT value ──
# F3 fixed the fc path (search_readiness.detect_relative_budget_delta / resolve_relative_budget,
# agent_loop._relative_budget_repair, _inject_search_params) and asked F4 for the legacy half.
# These live next to F3's tests/test_f5c_relative_budget.py in coverage, in my own file per the
# ownership split. Live pre-fix evidence: agenttest-t1-f4b-legacy — prior ceiling £2,200, the
# reply claimed "raised the budget by £200, bringing the maximum to £2,200", 0 results.

_PRIOR = {"max_budget": 2200, "area": "Islington", "bedrooms": 2}

_RELATIVE_EN = [
    "Raise the budget by 200 pounds and search again.",
    "raise the budget by £200",
    "increase my budget by 200 pounds please",
    "can you bump the budget up by £200 and search again",
]
_RELATIVE_ZH = [
    "把预算提高200镑，再搜一次。",
    "预算再加200镑",
    "预算上调200英镑",
]


@pytest.mark.parametrize("message", _RELATIVE_EN + _RELATIVE_ZH)
def test_legacy_applies_a_relative_budget_edit_to_the_current_ceiling(message):
    from core.langgraph_agent import _apply_explicit_criteria_updates

    out = _apply_explicit_criteria_updates(_PRIOR, message)
    assert out["max_budget"] == 2400, (message, out["max_budget"])
    # the delta must never become the ceiling — that IS the defect
    assert out["max_budget"] != 200


@pytest.mark.parametrize("message", ["lower the budget by 300 pounds",
                                     "reduce my budget by £300",
                                     "预算降低300镑"])
def test_legacy_handles_a_downward_relative_edit(message):
    from core.langgraph_agent import _apply_explicit_criteria_updates

    assert _apply_explicit_criteria_updates(_PRIOR, message)["max_budget"] == 1900


def test_a_relative_edit_with_no_prior_ceiling_clears_the_filter():
    """A delta is not a ceiling. Searching at £200 is precisely the failure, so with no
    base the budget filter is dropped instead."""
    from core.langgraph_agent import _apply_explicit_criteria_updates

    out = _apply_explicit_criteria_updates({"area": "Islington"},
                                           "Raise the budget by 200 pounds and search again.")
    assert out.get("max_budget") is None


@pytest.mark.parametrize("message,expected", [
    ("raise the budget to 2400 pounds", 2400),      # absolute target, still a ceiling
    ("提高到2400镑", 2400),
    ("my budget is 1500 a month", 1500),
    ("add 2 bedrooms", 2200),                        # not a money unit
    ("raise the commute limit by 10 minutes", 2200),  # not a budget
    ("加2个卧室", 2200),
])
def test_absolute_and_non_budget_phrasings_are_untouched(message, expected):
    from core.langgraph_agent import _apply_explicit_criteria_updates

    assert _apply_explicit_criteria_updates(_PRIOR, message)["max_budget"] == expected


def test_the_tool_never_reinstalls_the_delta_as_a_ceiling():
    """The graph resolves the ceiling and then passes the SAME raw message to the tool.
    Re-reading the figure there would either restore the £200 bug or double-apply the
    delta, so a relative edit states no ceiling as far as the tool's override is
    concerned. Idempotent: applying it twice yields the same number."""
    from core.tools.search_properties import (_states_relative_budget_edit,
                                              ground_hard_constraints)

    message = "Raise the budget by 200 pounds and search again."
    assert _states_relative_budget_edit(message) is True
    assert _states_relative_budget_edit("raise the budget to 2400 pounds") is False
    for hostile in (None, 12345, "", ["x"]):
        assert _states_relative_budget_edit(hostile) is False

    # a model that echoed the delta is corrected to the resolved accumulated ceiling
    params, ungrounded = ground_hard_constraints(
        {"max_budget": 200, "area": "Camden"}, {"max_budget": 2400, "area": "Camden"}, message)
    assert params["max_budget"] == 2400 and "max_budget" in ungrounded
    # with no accumulated ceiling the filter is DROPPED, never set to the delta
    params, _ = ground_hard_constraints({"max_budget": 200, "area": "Camden"}, {}, message)
    assert params.get("max_budget") is None
    # already-resolved value passes through untouched (no double application)
    params, _ = ground_hard_constraints(
        {"max_budget": 2400, "area": "Camden"}, {"max_budget": 2400}, message)
    assert params["max_budget"] == 2400
    # an absolute restatement still overrides exactly as before
    params, _ = ground_hard_constraints(
        {"max_budget": 1500, "area": "Camden"}, {"max_budget": 2400}, "my budget is 1500 a month")
    assert params["max_budget"] == 1500


@pytest.mark.parametrize("message,expected", [
    # REGRESSION GUARD. search_readiness's loose ZH pattern
    # `(?:提高|提升|上调|增加|加大|加|涨|多)\s*(\d…)` matches the single character 多 INSIDE 最多,
    # so a plain ceiling reads as a +2200 DELTA; with no prior ceiling the accumulator then
    # CLEARS max_budget. Reproduced live before the guard: fixtest-f4-rz2-legacy recorded
    # max_budget null where the pre-change run (fixtest-f4-b2-legacy) recorded 2000.
    ("我想在伦敦伊斯灵顿租一套两居室，预算每月最多2200英镑。", 2200),
    ("我想在伦敦卡姆登租一套两居室，预算每月最多2000英镑，有什么推荐？", 2000),
    ("预算不超过1800英镑", 1800),
    ("预算2000以内", 2000),
    ("预算最高2500镑", 2500),
    ("a 2 bed in Camden under 2000 pounds a month", 2000),
    ("budget no more than £1800", 1800),
])
def test_a_plain_ceiling_is_never_read_as_a_relative_edit(message, expected):
    from core.langgraph_agent import _apply_explicit_criteria_updates
    from core.tools.search_properties import _states_relative_budget_edit

    assert _states_relative_budget_edit(message) is False, message
    # with no prior ceiling the amount must become the ceiling, not clear the filter
    assert _apply_explicit_criteria_updates({}, message)["max_budget"] == expected
    # and it must not be re-read as a delta against an existing ceiling either
    assert _apply_explicit_criteria_updates({"max_budget": 3000}, message)["max_budget"] == expected


@pytest.mark.parametrize("message", _RELATIVE_EN + _RELATIVE_ZH +
                         ["lower the budget by 300 pounds", "预算降低300镑"])
def test_the_ceiling_guard_does_not_swallow_a_real_delta(message):
    from core.tools.search_properties import _states_relative_budget_edit

    assert _states_relative_budget_edit(message) is True, message
