"""Harness-injected nulls must never reach a strict tool input model (2026-10-19 A-02, B-06).

Every registered tool validates its kwargs with a pydantic model built ``strict=True`` that
re-checks the AUTHORED schema, where an optional parameter is absent or typed — never null.
``strip_null_args`` already removed the model's own nulls, but values the harness injected
AFTER it were not:

* A-02 — a user who does not commute ("I work from home") got ``max_commute_time = None`` from
  ``agent_loop._inject_search_params`` and ``commute_destination = None`` from
  ``search_properties.ground_hard_constraints``; every search they ran failed with
  "ValidationError: invalid parameters" (live 11/11).
* B-06 — ``_canonical_poi_args`` rebuilt a merged multi-type POI call from the RAW batch args,
  so the strict-mode ``latitude: null`` / ``longitude: null`` came back and every merged call
  failed on first dispatch (live 5/5).

The provider below validates exactly the way ``Tool._execute_with_callable`` does, against the
real registered tool, so these tests fail on the defect rather than on a stand-in.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.strict_schema import to_strict_schema
from core.tool_system import create_tool_registry
from tests.test_agent_contract_guards import Result, Spec, _state, _tc

_REGISTRY = create_tool_registry()
_SPECS = {spec.name: spec for spec in _REGISTRY.list_specs()}


class _ValidatingProvider:
    """Records each call and validates it with the real tool's strict input model."""

    def __init__(self, names):
        self.specs = [Spec(n, input_schema=_SPECS[n].input_schema) for n in names]
        self.calls = []
        self.rejected = []

    def list_specs(self):
        return list(self.specs)

    def get(self, name):
        return next((s for s in self.specs if s.name == name), None)

    async def execute_tool(self, name, **params):
        tool = _REGISTRY.get(name)
        clean = {k: v for k, v in params.items()
                 if not k.startswith("_") and k != "idempotency_key"}
        self.calls.append((name, clean))
        try:
            tool.input_model.model_validate(tool._apply_defaults(dict(clean)))
        except Exception as exc:  # the exact gate the live turns failed
            self.rejected.append((name, clean, str(exc).splitlines()[0]))
            return Result(False, error="ValidationError: invalid parameters")
        return Result(True, {"status": "found", "recommendations": [], "pois": {}})


def _strict_null_args(name, args):
    """What a strict-mode model emits: every property present, null where unset."""
    props = to_strict_schema(_SPECS[name].input_schema).get("properties") or {}
    return {key: args.get(key) for key in props}


def _run(state, calls, names):
    provider = _ValidatingProvider(names)
    nodes = build_fc_nodes(provider)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc(name, args, f"c{i}") for i, (name, args) in enumerate(calls)])]
    asyncio.run(nodes["execute_tools"](state))
    return provider


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_STRICT", "1")


@pytest.mark.parametrize("message", [
    "I work from home so I don't commute. Find me a 1-bed flat in Camden under £2,000 a month.",
    "No commute for me, I'm remote. 1 bedroom in Hackney, max £1,800 pcm please.",
])
def test_a_no_commute_search_reaches_the_tool_with_valid_params(message):
    state = _state(message)
    # The accumulated criteria exactly as the state node writes them for this message.
    state["accumulated_search_criteria"] = _apply_explicit_criteria_updates({}, message)
    assert state["accumulated_search_criteria"].get("no_commute") is True
    args = _strict_null_args("search_properties", {"user_query": message})
    provider = _run(state, [("search_properties", args)], ["search_properties"])

    assert provider.rejected == []
    (_, params), = provider.calls
    assert params.get("no_commute") is True
    assert "max_commute_time" not in params and "commute_destination" not in params
    assert all(value is not None for value in params.values())


def test_a_stale_destination_does_not_survive_no_commute():
    message = "Actually I don't commute any more. Search Camden again."
    state = _state(message)
    state["accumulated_search_criteria"] = {
        "area": "Camden", "max_budget": 2000, "no_commute": True,
        "commute_destination": "King's Cross", "max_travel_time": 30}
    args = _strict_null_args("search_properties", {"user_query": message, "area": "Camden",
                                                   "max_commute_time": 30})
    provider = _run(state, [("search_properties", args)], ["search_properties"])

    assert provider.rejected == []
    (_, params), = provider.calls
    assert "max_commute_time" not in params and "commute_destination" not in params


def test_a_cleared_move_in_date_is_absent_not_null():
    from core.tools.search_properties import ground_hard_constraints

    params, _ = ground_hard_constraints(
        {"area": "Camden", "move_in_date": "2026-11-01"},
        {"area": "Camden", "move_in_date": None, "no_commute": True,
         "constraint_sources": {"commute_destination": "user_message"}},
        "forget the move-in date, any time is fine")
    assert "move_in_date" not in params
    assert all(value is not None for value in params.values())


def test_a_merged_multi_type_poi_call_keeps_its_nulls_stripped():
    message = "Is there a supermarket and a GP surgery within walking distance of NW1 8NH?"
    state = _state(message)
    calls = [("search_nearby_pois", _strict_null_args("search_nearby_pois", {
        "address": "NW1 8NH", "poi_type": t, "user_query": message})) for t in
        ("supermarket", "gp")]
    assert calls[0][1]["latitude"] is None    # the strict-mode shape the model sent
    provider = _run(state, calls, ["search_nearby_pois"])

    assert provider.rejected == []
    assert provider.calls, "the merged call must be dispatched"
    for _, params in provider.calls:
        assert "latitude" not in params and "longitude" not in params
        assert {"supermarket", "gp"} <= set(params["poi_type"].split(","))


def test_only_listing_coordinates_are_labelled_as_the_listings_own(monkeypatch):
    """B-06 (second half): after the null-stripped retry the MODEL supplied coordinates of
    its own (~250 m off) and the tool labelled them listing_coordinates/is_specific_address."""
    from core.tools import search_nearby_pois as poi

    monkeypatch.setattr(poi, "query_osm_pois", lambda *a, **k: [])
    supplied = poi.search_nearby_pois_impl("NW1 8NH", poi_type="supermarket",
                                           latitude=51.5415, longitude=-0.1445)
    ref = supplied["reference_point"]
    assert ref["precision"] == "caller_supplied_coordinates"
    assert ref["is_specific_address"] is False
    listing = poi.search_nearby_pois_impl("NW1 8NH", poi_type="supermarket",
                                          latitude=51.5397, longitude=-0.1425,
                                          _coordinate_source="listing")
    assert listing["reference_point"]["precision"] == "listing_coordinates"
