"""Round-3 small fixes (R3-05, R3-11, ...)."""
from __future__ import annotations


# ── R3-05: the app-level contract covers every remember call of the turn ─────────────
def test_r3_05_app_contract_acknowledges_both_saves():
    import app as app_module
    from core.memory_contract import compose_memory_contract_response, strip_saved_content_quote
    final_state = {"tool_artifacts": [
        {"tool": "remember", "success": True, "raw_data": {"success": True, "stored": "I have a cat"}},
        {"tool": "search_properties", "success": True, "raw_data": {"status": "found"}},
        {"tool": "remember", "success": True, "raw_data": {"success": True, "stored": "我对花粉过敏"}},
    ], "memory_write_contract": {}}
    contract = app_module._turn_memory_contract(final_state)
    assert contract.get("stored_contents") == ["I have a cat", "我对花粉过敏"]
    text = strip_saved_content_quote("Saved both.", contract, "en")
    out = compose_memory_contract_response(text, contract, language="en")
    assert "I have a cat" in out and "我对花粉过敏" in out


def test_r3_05_without_remember_calls_the_state_contract_is_used():
    import app as app_module
    assert app_module._turn_memory_contract({"memory_write_contract": {"requested": True}}) == {"requested": True}
    assert app_module._turn_memory_contract({}) == {}


# ── R3-11: tool error strings are English (the model localises the reply) ─────────────
def _has_cjk(text):
    return any("一" <= ch <= "鿿" for ch in str(text or ""))


def test_r3_11_calculate_commute_geocode_failure_is_english(monkeypatch):
    import core.maps_service as maps_service
    from core.tools.calculate_commute import calculate_commute_impl
    monkeypatch.setattr(maps_service, "calculate_travel_details", lambda *a, **k: None)
    result = calculate_commute_impl("Nowhere Street", "Bank", "transit")
    assert result["success"] is False
    assert not _has_cjk(result["error"]) and "geocoded" in result["error"]


def test_r3_11_calculate_commute_cost_route_failure_is_english(monkeypatch):
    import core.maps_service as maps_service
    from core.tools.calculate_commute_cost import calculate_commute_cost_impl
    monkeypatch.setattr(maps_service, "calculate_travel_basis", lambda *a, **k: None)
    result = calculate_commute_cost_impl("Nowhere Street", "Bank", mode="transit")
    assert result["success"] is False
    assert not _has_cjk(result["error"]) and "route" in result["error"]


# ── R3-09: a transit request never shows £0 as its transport cost ─────────────────────
def _short_measured_route(*a, **k):
    return {"duration_minutes": 15, "source": "tfl_journey_plan", "basis": "tfl_journey_plan"}


def test_r3_09_transit_mode_short_route_has_unknown_cost_not_zero(monkeypatch):
    import core.maps_service as maps_service
    from core.tools.calculate_commute_cost import calculate_commute_cost_impl
    monkeypatch.setattr(maps_service, "calculate_travel_basis", _short_measured_route)
    result = calculate_commute_cost_impl("Whitechapel Road, London E1", "Bank, London", mode="transit")
    assert result["success"] is True
    summary = result["summary"]
    assert summary["monthly_transport_cost"] is None
    assert summary["transport_cost_status"] == "unknown"
    assert "£0" not in str(summary["note"]).replace("not £0", "")
    assert result["transport_cost_available"] is False
    assert result["uses_public_transport"] is None


def test_r3_09_walking_mode_still_reports_no_fare(monkeypatch):
    import core.maps_service as maps_service
    from core.tools.calculate_commute_cost import calculate_commute_cost_impl
    monkeypatch.setattr(maps_service, "calculate_travel_basis", _short_measured_route)
    result = calculate_commute_cost_impl("Whitechapel Road, London E1", "Bank, London", mode="walking")
    assert result["summary"]["monthly_transport_cost"] == "£0.00"


# ── R3-10: a small gap between two areas is not stretched to 100 points ───────────────
def test_r3_10_six_minutes_is_not_a_hundred_points():
    from core.tools.compare_or_rank_areas import _commute_components
    comps = _commute_components({"hackney": 24, "stratford": 30}, None)
    assert comps["hackney"] == 100.0
    assert comps["stratford"] == 80.0


def test_r3_10_large_commute_gaps_still_separate():
    from core.tools.compare_or_rank_areas import _commute_components
    comps = _commute_components({"islington": 15, "hackney": 38, "camden": 42}, None)
    assert comps["islington"] == 100.0
    assert comps["camden"] == 10.0 and comps["hackney"] == 23.3


def test_r3_10_small_rent_gap_stays_small():
    from core.tools.compare_or_rank_areas import _value_components
    stats = {"a": {"median": 1500.0, "sample_size": 50}, "b": {"median": 1550.0, "sample_size": 50}}
    comps = _value_components(stats, budget_present=False)
    assert comps["a"] == 100.0 and comps["b"] > 80.0


def test_r3_10_priorities_source_is_recorded(monkeypatch):
    import asyncio
    import core.area_stats as area_stats
    import core.tools.compare_or_rank_areas as cra

    async def candidates(*_args, **_kwargs):
        return [{"name": "Camden", "slug": "camden", "commute_minutes": None}]
    monkeypatch.setattr(cra, "_resolve_candidates", candidates)
    monkeypatch.setattr(area_stats, "aggregate", lambda *_a: {"camden": area_stats._empty()})
    default = asyncio.run(cra.compare_or_rank_areas_impl(city="London", reply_language="en"))
    assert default["priorities_source"] == "default"
    assert "your priorities" not in " ".join(default["explanation_notes"])
    supplied = asyncio.run(cra.compare_or_rank_areas_impl(
        city="London", reply_language="en", priorities=["commute", "value"]))
    assert supplied["priorities_source"] == "caller_supplied"
    assert "user's own" in supplied["priorities_note"]
