"""Audit seal/F5: the sealed arguments must be the manager's arguments, byte for byte.

``PreparedSpecialistCall.args`` round-trips through JSON. ``encoded != round_trip`` cannot
see a coercion that is IDEMPOTENT — ``{1: "a"}`` encodes to ``{"1": "a"}`` and re-encodes
identically, a ``tuple`` becomes a ``list`` the same way — so a harness-injected Python
value (``_inject_search_params`` / ``_canonical_poi_args`` build these natively) could reach
the tool silently reshaped.  The snapshot now validates JSON-nativeness explicitly.
"""

from __future__ import annotations

import pytest

from core.agent_loop import build_fc_nodes
from core.specialist_runtime import (
    ReadCall,
    SpecialistDispatchError,
    prepare_specialist_batch,
    seal_specialist_args,
)
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _state,
    _tc,
)
from tests.test_specialist_failure_radius import _Spec


# A realistic search_properties call: strings, ints, bools, a list and a nested object,
# validated against the REAL tool schema so the harness re-injection path runs unchanged.
SEARCH_ARGS = {
    "location": "London",
    "area": "Camden",
    "areas": ["Camden", "Kentish Town"],
    "bedrooms": 2,
    "max_budget": 2200,
    "min_budget": 1200,
    "care_about_safety": True,
    "property_features": ["garden", "gym"],
    "accumulated_preferences": [
        "quiet street",
        {"kind": "budget", "value": {"max": 2200, "currency": "GBP"}},
    ],
    "current_message": "two-bed flat near Camden under 2200",
    "limit": 5,
}


def _probe_parameters():
    from core.tools.search_properties import search_properties_tool

    return search_properties_tool.parameters


def test_sealed_args_equal_the_manager_path_args(tmp_path):
    """Audit missing-test #5."""
    observed = {}

    def probe(label):
        async def run(**kwargs):
            observed[label] = kwargs
            return {"status": "found", "recommendations": [],
                    "candidate_validation": {}, "commute_evidence": []}

        return run

    def run_once(label, *, specialist_dispatch):
        from core.tool_system import Tool
        from core.tools.search_properties import search_properties_tool

        registry = _registry(
            tmp_path,
            [Tool(
                name="search_properties",
                description="probe search_properties",
                func=probe(label),
                parameters=_probe_parameters(),
                # Reuse the real validation model, so the harness re-injection path
                # (_inject_search_params, ground_hard_constraints) runs exactly as in
                # production on BOTH arms.
                input_model=search_properties_tool.input_model,
                max_retries=1,
                side_effect="none",
                retry_safe=True,
            )],
        )
        return _execute(
            build_fc_nodes(registry, specialist_dispatch=specialist_dispatch),
            _state([_tc("search_properties", dict(SEARCH_ARGS), "c1")],
                   message="two-bed flat near Camden under 2200"),
        )

    manager_state = run_once("manager", specialist_dispatch=False)
    specialist_state = run_once("specialist", specialist_dispatch=True)

    volatile = {"_deadline_monotonic"}
    manager_kwargs = {k: v for k, v in observed["manager"].items() if k not in volatile}
    specialist_kwargs = {
        k: v for k, v in observed["specialist"].items() if k not in volatile
    }

    assert specialist_kwargs == manager_kwargs
    # The one permitted post-snapshot override is present on both sides and is a float.
    assert isinstance(observed["specialist"]["_deadline_monotonic"], float)
    assert isinstance(observed["manager"]["_deadline_monotonic"], float)
    # And the sealed path really was the specialist path.
    assert specialist_state["tool_artifacts"][0]["agent_role"] == "listings"
    assert "agent_role" not in manager_state["tool_artifacts"][0]


@pytest.mark.parametrize(
    ("args", "coerced_to"),
    [
        ({"nested": {1: "a"}}, {"nested": {"1": "a"}}),
        ({"pair": ("a", "b")}, {"pair": ["a", "b"]}),
        ({"nested": {"deep": {2: ["x"]}}}, {"nested": {"deep": {"2": ["x"]}}}),
    ],
)
def test_idempotent_json_coercions_are_detected_not_silently_applied(args, coerced_to):
    """These survive ``encoded != round_trip`` unchanged, which is why they need a check."""
    import json

    canonical = json.dumps(args, default=list, sort_keys=True, separators=(",", ":"))
    assert json.loads(canonical) == coerced_to, "fixture is not an idempotent coercion"

    with pytest.raises(SpecialistDispatchError):
        seal_specialist_args(args)

    batch = prepare_specialist_batch(
        [ReadCall(index=0, tool_name="get_weather", args=args,
                  params_digest="0" * 16, tool_call_id="c1")],
        live_specs=[_Spec("get_weather")],
        root_task_id="turn:request-1",
        run_id="run-1",
        turn=0,
    )
    assert batch.call(0) is None
    assert batch.rejected[0].endswith("not_json_native")


def test_json_native_values_pass_through_untouched():
    payload = {
        "s": "text", "i": 7, "f": 1.5, "b": True, "n": None,
        "list": [1, "two", {"three": [None, False]}],
        "obj": {"nested": {"deep": 1}},
    }
    assert seal_specialist_args(payload) == payload


def test_seal_rejects_reserved_and_injected_keys():
    for key in ("_deadline_monotonic", "idempotency_key", "user_id", "session_id"):
        with pytest.raises(SpecialistDispatchError) as exc_info:
            seal_specialist_args({key: "x"})
        assert exc_info.value.error_code == "specialist_reserved_argument"
