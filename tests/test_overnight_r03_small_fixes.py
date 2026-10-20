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
