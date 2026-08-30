# -*- coding: utf-8 -*-
"""Four tool-layer defects that were each found and then left because the file had an owner.

Every test here fails on 4f410ab and says in its docstring what the old behaviour produced,
because the failure mode this repo keeps repeating is a number that is computed, put somewhere
a reader could find it, and then never asserted on.

  1. ``search_nearby_pois``'s internal deadline was 20.0s — EXACTLY FC_BATCH_TOOL_BUDGET_S, the
     window that abandons a straggler. A tie means the graceful partial-return path is decided
     by scheduler luck.
  2. ``calculate_commute`` and ``calculate_commute_cost`` returned different numbers for the
     same pair in the same turn (0.47 km: "estimated 11 minutes (9-14)" against "2 minutes"
     stated as fact), because only one of them went through ``core.commute_basis``.
  3. ``maps_service.calculate_travel_details`` dropped its ``mode``, so the transit-only
     calibration could be lent to a cycling request.
  4. is a capability gap in a file this change does not own — see the module note at the end.
"""
from __future__ import annotations

import pathlib

import pytest

from core import commute_basis as cb
from core import maps_service as ms


# =========================================================================== #
# 1. The tool's own deadline must be strictly inside the window that kills it #
# =========================================================================== #

def _poi_module(monkeypatch):
    """search_nearby_pois with its clock, geocoder and Overpass client stubbed out.

    Fully offline: the fake clock only advances when the fake query or the pacing sleep says
    it does, so every deadline assertion below is deterministic rather than timing-dependent.
    """
    import core.tools.search_nearby_pois as sp

    clock = {"t": 1000.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(sp.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(sp, "POI_PACING_S", 0.0)
    monkeypatch.setattr(sp, "geocode_address", lambda addr, **_kw: (51.5, -0.1))
    return sp, clock


def _measured_deadline_s(monkeypatch, step: float = 0.25, n_types: int = 1200) -> float:
    """The deadline the tool ACTUALLY enforces, measured by watching when it stops issuing.

    Behavioural on purpose: it reads the same number on the old code and the new one, so the
    assertions below fail with a wrong VALUE rather than with a missing symbol.
    """
    sp, clock = _poi_module(monkeypatch)
    monkeypatch.setattr(sp, "_infer_poi_types_from_query", lambda q: ["restaurant"] * n_types)

    issued = []

    def fake_query(lat, lon, ptype, *a, **k):
        issued.append(clock["t"] - 1000.0)
        clock["t"] += step
        return []

    monkeypatch.setattr(sp, "query_osm_pois", fake_query)
    sp.search_nearby_pois_impl(address="x", poi_type="all", user_query="whatever")
    assert issued, "the tool issued no request at all — the probe is broken, not the budget"
    # Issuing stops at the first instant >= the deadline, so the deadline is in
    # (last_issue, last_issue + step]. Report the upper end; every assertion is >= step-safe.
    return issued[-1] + step


@pytest.mark.parametrize("window", [5.0, 20.0, 30.0, 60.0])
def test_the_tool_deadline_is_strictly_inside_the_batch_window(monkeypatch, window):
    """The tie, and the fact that it survives someone retuning the window.

    FAILS BEFORE: POI_SEARCH_BUDGET_S was the literal 20.0 with no relationship to
    FC_BATCH_TOOL_BUDGET_S at all, so the measured deadline was 20.0 for every window here —
    over the window at 5s, exactly ON it at 20s (the coin flip), and far under half of it at
    60s, where the tool would abandon two thirds of its allowance for no reason.
    """
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", str(window))
    measured = _measured_deadline_s(monkeypatch)

    assert measured < window, (
        f"the tool's own deadline ({measured}s) is not strictly inside the batch window "
        f"({window}s) that abandons it — at a tie the honest partial-return is a coin flip")
    assert measured >= window * 0.5, (
        f"the tool gave up {window - measured:.1f}s of a {window}s window")


def test_the_margin_is_derived_from_the_window_not_hardcoded(monkeypatch):
    """The margin has to be a FUNCTION of the window, or raising the window silently re-breaks
    the ordering. Fixed 2s floor for the return path, 15% once the window is big enough to make
    the proportional term bind, and never less than half the window left usable.

    FAILS BEFORE: ``poi_search_budget_s`` did not exist; the budget was a module constant.
    """
    import core.tools.search_nearby_pois as sp

    monkeypatch.setattr(sp, "POI_SEARCH_BUDGET_S", None)

    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    assert sp.poi_search_budget_s() == pytest.approx(17.0)   # 15% binds: 20 - 3.0
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "60")
    assert sp.poi_search_budget_s() == pytest.approx(51.0)   # 15% binds: 60 - 9.0
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "10")
    assert sp.poi_search_budget_s() == pytest.approx(8.0)    # floor binds: 10 - 2.0
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "3")
    assert sp.poi_search_budget_s() == pytest.approx(1.5)    # half-window backstop

    # Strictly-below holds for every positive window, including absurd ones.
    for w in ("0.3", "0.4", "1", "2", "5", "20", "25", "120"):
        monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", w)
        assert 0 < sp.poi_search_budget_s() < float(w), w


def test_an_override_may_only_tighten_the_deadline_never_restore_the_tie(monkeypatch):
    """POI_SEARCH_BUDGET_S stays an ops knob, but a stale one must not reinstate the tie.

    FAILS BEFORE: an override WAS the budget, unconditionally.
    """
    import core.tools.search_nearby_pois as sp

    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setattr(sp, "POI_SEARCH_BUDGET_S", 5.0)
    assert sp.poi_search_budget_s() == pytest.approx(5.0)     # tightening is honoured
    monkeypatch.setattr(sp, "POI_SEARCH_BUDGET_S", 25.0)
    assert sp.poi_search_budget_s() == pytest.approx(17.0)    # loosening past the window is not


def test_the_partial_note_quotes_the_deadline_that_actually_fired(monkeypatch):
    """The "Xs search budget was reached" note read a module constant. Once the budget is
    derived per call, a note quoting the constant can name a different number from the clock
    that stopped the search — which is a fabricated detail in a message whose whole job is
    honesty about what was skipped.

    FAILS BEFORE: the note said "20s" while the tool stopped at 8s.
    """
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "10")        # -> 8.0s budget
    sp, clock = _poi_module(monkeypatch)

    def fake_query(lat, lon, ptype, *a, **k):
        clock["t"] += 3.0
        return [{"name": f"{ptype} A", "icon": "X", "distance_display": "10m"}]

    monkeypatch.setattr(sp, "query_osm_pois", fake_query)
    res = sp.search_nearby_pois_impl(address="x", poi_type="all")

    assert res["partial"] is True
    assert "8s search budget" in res["note"], res["note"]


def test_an_overpass_request_cannot_outlive_the_deadline_that_authorised_it(monkeypatch):
    """The deadline is checked BEFORE a request is issued, so without a clamp the LAST request
    could still burn its full 30s past the deadline and hand the batch window the win anyway.

    FAILS BEFORE: query_osm_pois took no timeout and always asked for 30s.
    """
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "10")        # -> 8.0s budget
    sp, clock = _poi_module(monkeypatch)

    seen = []

    def fake_query(lat, lon, ptype, *a, **k):
        seen.append(k.get("timeout"))
        clock["t"] += 3.0
        return []

    monkeypatch.setattr(sp, "query_osm_pois", fake_query)
    sp.search_nearby_pois_impl(address="x", poi_type="all")

    assert seen, "no request was issued"
    assert all(t is not None for t in seen), seen
    # 8.0s budget, 3s per query: the requests start with 8.0s, 5.0s and 2.0s left.
    assert seen == pytest.approx([8.0, 5.0, 2.0])
    assert all(t <= 8.0 for t in seen)


# =========================================================================== #
# 2. One pair, one number: the two commute tools may not disagree             #
# =========================================================================== #

# Tavistock Court WC1H -> UCL Gower Street: 0.47 km, the shortest CALIBRATION pair. TfL
# measures it at 12 minutes; the raw straight-line formula says 2; the fit says 11 (9-14).
_TAVISTOCK = {"lat": 51.5245, "lng": -0.1272}
_UCL = {"lat": 51.5246, "lng": -0.1340}
_FROM = "Tavistock Court, Tavistock Place, London WC1H"
_TO = "UCL, Gower Street, London WC1E 6BT"


def _offline_maps(monkeypatch, origin=_TAVISTOCK, dest=_UCL):
    """No cache, no TfL, no geocoder. Both the journey and the duration endpoints are stubbed
    to "TfL has no journey" because the old and the new code reach TfL through different ones.
    """
    monkeypatch.setattr(ms, "get_from_cache", lambda k: None)
    monkeypatch.setattr(ms, "set_to_cache", lambda k, v: None)
    # The DESTINATION is whichever address names Gower Street (the real pair) or is literally
    # "B" (the synthetic pairs below). Getting this wrong collapses the pair to zero distance,
    # which silently satisfies "not calibrated" for the wrong reason.
    monkeypatch.setattr(
        ms, "_get_coordinates",
        lambda a: dest if ("gower" in a.lower() or a.strip().upper() == "B") else origin)
    monkeypatch.setattr(ms, "_tfl_journey", lambda o, d, mode="transit": None)
    monkeypatch.setattr(ms, "_tfl_travel_time", lambda o, d, mode="transit": None)


def test_the_two_commute_tools_report_the_same_number_for_the_same_pair(monkeypatch):
    """THE disagreement, byte for byte.

    FAILS BEFORE: for this 0.47 km pair ``calculate_commute`` returned
    ``estimated_duration_minutes=11`` with the 9-14 band and a straight-line basis, while
    ``calculate_commute_cost`` returned ``commute.duration_minutes=2`` — the raw formula's
    figure, in the field that means "a journey planner measured this", with no basis attached
    and ``duration_category``/``is_acceptable`` derived from it. Same product, same turn, same
    pair, two answers, and the wrong one presented as the measured one.
    """
    import core.tools.calculate_commute as cc
    import core.tools.calculate_commute_cost as ccc

    _offline_maps(monkeypatch)

    quoted = cc.calculate_commute_impl(_FROM, _TO)
    assert quoted["duration_minutes"] is None
    assert quoted["estimated_duration_minutes"] == 11
    assert (quoted["estimate_low_minutes"], quoted["estimate_high_minutes"]) == (9, 14)
    assert quoted["estimate_model"] == cb.CALIBRATED_MODEL_ID

    cost = ccc.calculate_commute_cost_impl(_FROM, _TO)
    assert cost["success"] is True
    block = cost["commute"]

    assert block["duration_minutes"] is None, (
        "0.47 km with no TfL journey: the raw formula's 2 minutes must not occupy the measured "
        "field. That is the exact number this tool used to state as fact while calculate_commute "
        "said 11.")
    assert block["estimated_duration_minutes"] == 11
    assert (block["estimate_low_minutes"], block["estimate_high_minutes"]) == (9, 14)
    assert block["estimate_model"] == cb.CALIBRATED_MODEL_ID
    assert block["basis"] == cb.BASIS_STRAIGHT_LINE

    # The point of the change, in one assertion.
    assert (block["estimated_duration_minutes"]
            == quoted["estimated_duration_minutes"]
            == 11)
    assert block["basis"] == quoted["basis"]


def test_the_claims_built_on_the_guess_are_withheld_too(monkeypatch):
    """``duration_category``, ``is_acceptable`` and the monthly-HOURS total are the guess with a
    lever on it — the same defect one derivation further down.

    FAILS BEFORE: category 'Short (< 20 min)', is_acceptable True, and a summary reading
    "2 minutes" / "4 min/day x 22 workdays = ~1.5 hours/month", all from an unmeasured 2.
    """
    import core.tools.calculate_commute_cost as ccc

    _offline_maps(monkeypatch)
    cost = ccc.calculate_commute_cost_impl(_FROM, _TO)
    block = cost["commute"]

    assert block["duration_category"] is None
    assert block["is_acceptable"] is None

    summary = cost["summary"]
    assert summary["commute_time"] != "2 minutes"
    assert "estimated 9-14 minutes" in summary["commute_time"]
    assert "NOT a journey plan" in summary["commute_time"]
    assert "1.5 hours/month" not in str(summary)

    assert "do NOT state" in cost["recommendation"] or "estimated" in cost["recommendation"]


def test_a_measured_journey_is_still_reported_as_one(monkeypatch):
    """Non-regression: a real TfL itinerary keeps every field it always had."""
    import core.tools.calculate_commute_cost as ccc

    _offline_maps(monkeypatch)
    monkeypatch.setattr(ms, "_tfl_journey",
                        lambda o, d, mode="transit": {"duration": 24, "legs": []})

    cost = ccc.calculate_commute_cost_impl(_FROM, _TO, mode="walking")
    block = cost["commute"]
    assert block["duration_minutes"] == 24
    assert block["duration_category"] == "Medium (20-45 min)"
    assert block["is_acceptable"] is True
    assert block["basis"] == cb.BASIS_MEASURED
    assert "estimated_duration_minutes" not in block   # nothing to disclose: it was measured
    assert cost["summary"]["commute_time"] == "24 minutes (measured: TfL journey plan)"
    assert "recommendation" not in cost                # no caveat to attach to a journey plan

    # The monthly-hours clause is the one summary field that only appears on the fare branch
    # (which needs live zone lookups), so it is checked at its producer instead of driving the
    # network. A measured figure keeps the arithmetic it always had.
    assert (ccc._monthly_hours_clause(24, block)
            == " + 48 min/day × 22 workdays = ~17.6 hours/month")
    # ...and an estimate does not get it at all.
    estimated = {"estimate_low_minutes": 9, "estimate_high_minutes": 14,
                 "estimated_duration_minutes": 11}
    clause = ccc._monthly_hours_clause(None, estimated)
    assert "not stated as a fact" in clause
    assert "estimated 9-14 min" in clause
    assert ccc._monthly_hours_clause(None, {}).startswith(". Commuting HOURS are not stated:")


def test_the_commute_cost_card_honours_the_basis_it_is_handed():
    """``_format_commute_cost`` is the one DETERMINISTIC renderer of this tool's payload — no
    LLM sits between it and the user — so withholding the number in the payload is only half a
    fix if the card prints it anyway.

    FAILS BEFORE: the card did ``f"**Duration:** {dur} minutes"`` off ``duration_minutes``
    unconditionally, so the withheld figure renders as the literal "None minutes". (Before the
    payload change it rendered the unmeasured 2 as a measured duration, which is the same
    defect pointing the other way.)
    """
    from core import langgraph_agent as lga

    measured, _ = lga._format_commute_cost({
        "success": True, "from_address": "A", "to_address": "B",
        "commute": {"duration_minutes": 24, "duration_category": "Medium (20-45 min)"}})
    assert "**Duration:** 24 minutes (Medium (20-45 min))" in measured
    assert "~48 minutes" in measured

    estimated, _ = lga._format_commute_cost({
        "success": True, "from_address": "A", "to_address": "B",
        "commute": {"duration_minutes": None, "duration_category": None,
                    "estimated_duration_minutes": 11,
                    "estimate_low_minutes": 9, "estimate_high_minutes": 14}})
    assert "None minutes" not in estimated
    assert "9-14 minutes" in estimated
    assert "not a journey plan" in estimated

    refused, _ = lga._format_commute_cost({
        "success": True, "from_address": "A", "to_address": "B",
        "commute": {"duration_minutes": None, "duration_category": None,
                    "estimated_duration_minutes": None}})
    assert "None minutes" not in refused
    assert "not established" in refused


def test_the_bare_thresholding_figure_uses_the_same_model_as_the_quoted_one(monkeypatch):
    """``calculate_travel_time`` is the filter's figure. It used to be the RAW formula while the
    listing annotation (``commute.coord_commute_minutes``) was already calibrated, so one search
    could filter a property at 2 minutes and annotate it at 11.

    FAILS BEFORE: calculate_travel_time returned 2 for this pair.
    """
    from core import commute

    _offline_maps(monkeypatch)
    assert ms.calculate_travel_time(_FROM, _TO) == 11
    assert commute.coord_commute_minutes("51.5245,-0.1272", _UCL) == 11


def test_no_user_facing_caller_takes_a_bare_minutes_figure():
    """A source guard, not a promise in a docstring.

    A bare int carries no basis, so the only legitimate uses are internal thresholding (a filter
    or a sort, never rendered). Any tool that RENDERS a commute time has to read
    ``calculate_travel_basis``. This is the assertion that stops the fix above from being undone
    by the next person who wants "just the number".

    FAILS BEFORE: ``core/tools/calculate_commute_cost.py`` imported and called
    ``calculate_travel_time``, and put its result in ``commute.duration_minutes``.
    """
    app = pathlib.Path(__file__).resolve().parent.parent / "app"

    # Files allowed to take a bare int, each because the figure is thresholded and never shown:
    allowed = {
        "core/maps_service.py",             # defines both; calculate_travel_time IS the view
        "core/commute.py",                  # listing annotation / max_travel_time filter
        "core/tools/search_properties.py",  # the max_commute_time filter and the RAG recall
    }
    producers = ("calculate_travel_time(", "estimate_travel_time_simple(")

    offenders = []
    for path in sorted(app.rglob("*.py")):
        rel = path.relative_to(app).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Skip prose: only a real call site counts, not a mention in a comment or docstring.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if any(p in line for p in producers):
                offenders.append(f"{rel}: {stripped[:90]}")
    assert offenders == [], (
        "these call a bare-minutes producer; a figure without its basis must not reach a "
        "user-facing payload:\n  " + "\n  ".join(offenders))


# =========================================================================== #
# 3. The calibration is transit-only, and the producer now knows the mode     #
# =========================================================================== #

def _pair_km(km: float) -> dict:
    """A destination due north of 51.5,-0.1 so the haversine distance is exactly ``km``."""
    return {"lat": 51.5 + km / 111.19492664455873, "lng": -0.1}


def test_the_producer_does_not_lend_transit_calibration_to_a_cycling_request(monkeypatch):
    """0.8 km by bicycle. The raw cycling and transit formulas both read 4 minutes there, so
    nothing downstream of maps_service can tell the two apart from the number alone — the mode
    has to travel with it.

    FAILS BEFORE: calculate_travel_details called describe_estimate without ``mode``, so this
    returned estimated_duration_minutes=14 with estimate_model=calibrated_...v1 — a transit
    figure confidently offered as a cycling time.
    """
    _offline_maps(monkeypatch, origin={"lat": 51.5, "lng": -0.1}, dest=_pair_km(0.8))
    assert cb.legacy_straight_line_minutes(0.8, "bicycling") == 4
    assert cb.legacy_straight_line_minutes(0.8, "transit") == 4

    out = ms.calculate_travel_details("A", "B", "bicycling")
    assert out["estimate_model"] is None, (
        "the transit-only fit was applied to a bicycle: 0.8 km would have been answered as 14 "
        "minutes")
    assert out["estimated_duration_minutes"] is None      # below the raw 15-minute floor

    # ...and transit at the same distance is still corrected, so this is a mode gate and not a
    # blanket suppression.
    transit = ms.calculate_travel_details("A", "B", "transit")
    assert transit["estimate_model"] == cb.CALIBRATED_MODEL_ID
    assert transit["estimated_duration_minutes"] == 14


def test_the_mode_guard_now_protects_the_second_tool_as_well(monkeypatch):
    """``withdraw_uncalibrated_mode`` only ever ran inside ``calculate_commute``, so it
    protected exactly one of the producer's callers.

    FAILS BEFORE: calculate_commute_cost stated ``commute.duration_minutes = 4`` as a measured
    cycling time for this pair (the raw cycling formula's output, via calculate_travel_time).
    """
    import core.tools.calculate_commute_cost as ccc

    _offline_maps(monkeypatch, origin={"lat": 51.5, "lng": -0.1}, dest=_pair_km(0.8))
    cost = ccc.calculate_commute_cost_impl("A", "B", mode="bicycling")

    assert cost["success"] is True
    assert cost["commute"]["duration_minutes"] is None
    assert cost["commute"]["estimated_duration_minutes"] is None
    assert cost["commute"]["is_acceptable"] is None
    assert "not established" in cost["summary"]["commute_time"]


@pytest.mark.parametrize("mode", ["walking", "foot-walking", "bicycling", "cycling-regular",
                                  "driving", "nonsense"])
def test_no_mode_outside_the_sample_is_ever_calibrated_by_the_producer(monkeypatch, mode):
    """Sweep, because a single example only proves one branch. Every mode the 14 pairs never
    measured must come back with estimate_model None from the producer itself.
    """
    _offline_maps(monkeypatch, origin={"lat": 51.5, "lng": -0.1}, dest=_pair_km(2.0))
    out = ms.calculate_travel_details("A", "B", mode)
    assert out["estimate_model"] is None, mode
    assert cb.CALIBRATED_MODES == ("transit",)


# =========================================================================== #
# 4. CJK destination aliases share one resolver across criteria and tool routes #
# =========================================================================== #

def test_pure_chinese_imperial_destination_is_canonical_everywhere():
    """A first-turn CJK destination must not be lost to ASCII ``\\b`` semantics."""
    from core import langgraph_agent as lga

    msg = "帮我找伦敦月租不超过1400镑的单间，通勤到帝国理工不超过35分钟"
    expected = "Imperial College London, South Kensington, London SW7 2AZ"

    assert lga._KNOWN_DESTINATIONS["帝国理工"] == expected
    assert lga._resolve_destination_address(msg, {"current_message": msg}, {}) == expected

    criteria = lga._apply_explicit_criteria_updates({"no_commute": True}, msg)
    assert criteria["commute_destination"] == expected
    assert criteria["destination"] == expected
    assert criteria["max_travel_time"] == 35
    assert criteria["no_commute"] is False
