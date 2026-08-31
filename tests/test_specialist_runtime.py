from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from core.specialist_runtime import (
    ReadCall,
    SpecialistDispatchError,
    build_specialist_results,
    prepare_specialist_batch,
    revalidate_specialist_call,
    safe_turn_root_id,
    seal_specialist_args,
    specialist_eligible_role,
    tool_spec_security_digest,
)


@dataclass(frozen=True)
class Spec:
    name: str
    side_effect: str = "none"
    retry_safe: bool = True
    version: str = "1"
    terminal: bool = False
    input_schema: dict | None = None

    def __post_init__(self):
        if self.input_schema is None:
            object.__setattr__(
                self, "input_schema", {"type": "object", "properties": {}}
            )


def call(index, name, args=None):
    return ReadCall(
        index=index,
        tool_name=name,
        args=args or {},
        params_digest=f"{index + 1:016x}",
        tool_call_id=f"raw-call-{index}",
    )


def prepare(calls, specs):
    return prepare_specialist_batch(
        calls,
        live_specs=specs,
        root_task_id="root/user@example.test",
        run_id="run-secret",
        turn=1,
    )


def artifact(batch, index, **extra):
    item = batch.call(index)
    task = batch.task_for_index(index)
    value = {
        "artifact_id": item.artifact_id,
        "plan_id": batch.plan.plan_id,
        "task_id": item.task_id,
        "parent_task_id": task.parent_task_id,
        "agent_role": task.role,
        "tool": item.tool_name,
        "params_digest": item.params_digest,
        "success": True,
        "raw_data": {"fixture": True},
        "elapsed_ms": 4,
    }
    value.update(extra)
    return value


def test_one_task_per_role_and_manager_owned_calls_are_not_delegated():
    # UPDATED (audit K1/F2): ``web_search`` carrying ``sub_queries`` used to be exempt from
    # the boundary and dispatched unrestricted. A model-controlled argument must never be a
    # way OUT of the capability boundary, so it is now an ordinary area_evidence call.
    calls = [
        call(0, "search_properties", {"address": "private"}),
        call(1, "get_property_details"),
        call(2, "calculate_commute"),
        call(3, "get_weather"),
        call(4, "remember"),
        call(5, "web_search", {"sub_queries": ["a"]}),
    ]
    batch = prepare(calls, [Spec(item.tool_name) for item in calls])
    assert batch.eligible_indices == (0, 1, 2, 3, 5)
    assert [task.role for task in batch.plan.tasks] == [
        "listings",
        "mobility",
        "area_evidence",
    ]
    assert batch.call(4) is None
    assert batch.call(5).role == "area_evidence"
    assert batch.rejected == {}


def test_eligibility_predicate_is_shared_and_argument_independent():
    assert specialist_eligible_role("web_search", {"sub_queries": ["a"]}) == "area_evidence"
    assert specialist_eligible_role("web_search", None) == "area_evidence"
    assert specialist_eligible_role("remember", {}) is None
    assert specialist_eligible_role("recall_memory", {}) is None
    assert specialist_eligible_role("not_a_tool", {}) is None


def test_checkpoint_plan_contains_no_raw_args_or_source_ids():
    args = {"address": "private-address", "query": "private-query"}
    batch = prepare(
        [call(0, "search_properties", args)], [Spec("search_properties")]
    )
    payload = batch.plan.model_dump_json()
    assert "private-address" not in payload
    assert "private-query" not in payload
    assert "raw-call" not in payload
    assert "example.test" not in payload
    assert batch.call(0).args_snapshot() == args


@pytest.mark.parametrize(
    "root_task_id",
    [
        "turn:alice123/node:execute_tools:0",
        "turn:SW1A-1AA/node:execute_tools:0",
        "turn:07123456789/node:execute_tools:0",
    ],
)
def test_checkpoint_hashes_even_syntactically_valid_root_task_ids(root_task_id):
    batch = prepare_specialist_batch(
        [call(0, "get_weather", {"city": "London"})],
        live_specs=[Spec("get_weather")],
        root_task_id=root_task_id,
        run_id="run-secret",
        turn=1,
    )

    payload = batch.plan.model_dump_json()
    assert root_task_id not in payload
    assert batch.plan.root_task_id.startswith("manager:")
    assert all(
        task.parent_task_id == batch.plan.root_task_id
        for task in batch.plan.tasks
    )


def test_argument_snapshot_is_deep_detached_and_returned_fresh():
    args = {"nested": {"values": [1]}}
    batch = prepare([call(0, "get_weather", args)], [Spec("get_weather")])
    args["nested"]["values"].append(2)
    detached = batch.call(0).args_snapshot()
    detached["nested"]["values"].append(3)
    assert batch.call(0).args_snapshot() == {"nested": {"values": [1]}}
    with pytest.raises(TypeError):
        batch.calls_by_index[7] = batch.call(0)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_args_are_rejected(number):
    # UPDATED (audit K1): a per-call defect is now recorded as a per-call rejection instead
    # of aborting the batch. The call is still refused — it is simply not planned.
    batch = prepare([call(0, "get_weather", {"x": number})], [Spec("get_weather")])
    assert batch.call(0) is None
    assert batch.rejected[0] == "specialist_call_args_not_finite_json"
    assert batch.plan.tasks == ()


def test_reserved_runtime_or_memory_args_are_rejected():
    # UPDATED (audit K1): same defect, per-call radius.
    for key in ("_deadline_monotonic", "idempotency_key", "user_id", "final_response"):
        batch = prepare([call(0, "get_weather", {key: "x"})], [Spec("get_weather")])
        assert batch.call(0) is None
        assert batch.rejection_for_index(0) == "specialist_reserved_argument"


def test_security_digest_is_canonical_and_tracks_schema():
    one = Spec("get_weather", input_schema={"b": 2, "a": 1})
    two = Spec("get_weather", input_schema={"a": 1, "b": 2})
    assert tool_spec_security_digest(one) == tool_spec_security_digest(two)
    assert tool_spec_security_digest(one) != tool_spec_security_digest(
        replace(one, input_schema={"a": 2, "b": 2})
    )


@pytest.mark.parametrize(
    "drift",
    [
        Spec("get_weather", version="2"),
        Spec("get_weather", side_effect="write"),
        Spec("get_weather", terminal=True),
        Spec("get_weather", retry_safe=False),
        Spec("get_weather", input_schema={"required": ["x"]}),
    ],
)
def test_dispatch_fails_closed_on_all_security_drift(drift):
    batch = prepare([call(0, "get_weather")], [Spec("get_weather")])
    with pytest.raises(SpecialistDispatchError):
        revalidate_specialist_call(batch, 0, [drift])


class _HostileRepr:
    """A spec attribute whose own ``__repr__`` is attacker-controlled code."""

    def __repr__(self):
        raise SystemError("repr boom")


@pytest.mark.parametrize(
    "field", ["max_retries", "retry_on_error", "input_model_ref", "output_model_ref"]
)
def test_hostile_spec_attribute_cannot_escape_as_a_bare_exception(field):
    """Review R1/R3: the digest guard must not be the thing that crashes execute_tools.

    ``_digest_scalar`` reprs any non-scalar attribute, and a duck-typed spec (MCP wrapper,
    registry fallback, test fake) can put ANY object there.  The exception used to travel
    out of ``prepare_specialist_batch`` — which does not wrap this call — straight past the
    caller's ``except SpecialistDispatchError``.
    """

    class HostileSpec:
        name = "get_weather"
        side_effect = "none"
        retry_safe = True
        version = "1"
        terminal = False
        input_schema = {"type": "object", "properties": {}}
        max_retries = 1
        retry_on_error = False
        input_model_ref = "fixture"
        output_model_ref = "none"

    setattr(HostileSpec, field, _HostileRepr())
    spec = HostileSpec()

    with pytest.raises(SpecialistDispatchError) as digest_error:
        tool_spec_security_digest(spec)
    assert digest_error.value.error_code == "specialist_tool_spec_invalid"

    with pytest.raises(SpecialistDispatchError) as batch_error:
        prepare([call(0, "get_weather")], [spec])
    assert batch_error.value.error_code == "specialist_tool_spec_invalid"


def test_dispatch_accepts_exact_live_capability():
    specs = [Spec("get_weather")]
    batch = prepare([call(0, "get_weather")], specs)
    assert revalidate_specialist_call(batch, 0, specs) == batch.call(0)


def test_results_are_ledger_derived_and_listing_evidence_is_tainted():
    batch = prepare(
        [call(0, "search_properties"), call(1, "get_weather")],
        [Spec("search_properties"), Spec("get_weather")],
    )
    results = build_specialist_results(
        batch, [artifact(batch, 0), artifact(batch, 1)]
    )
    assert results[0].evidence[0].tainted is True
    assert results[1].evidence[0].tainted is False


def test_artifact_binding_and_duplicate_fail_closed_to_failed_result():
    batch = prepare([call(0, "get_weather")], [Spec("get_weather")])
    valid = artifact(batch, 0)
    mismatched = build_specialist_results(
        batch, [dict(valid, task_id="wrong")]
    )
    duplicated = build_specialist_results(batch, [valid, dict(valid)])
    assert mismatched[0].status == "failed"
    assert duplicated[0].status == "failed"
    assert not mismatched[0].evidence
    assert not duplicated[0].evidence


def test_success_without_manager_visible_data_is_not_evidence():
    batch = prepare([call(0, "get_weather")], [Spec("get_weather")])

    result = build_specialist_results(
        batch,
        [artifact(batch, 0, raw_data=None)],
    )[0]

    assert result.status == "failed"
    assert result.evidence == ()
    assert result.data["succeeded"] == 0


def test_pre_dispatch_turn_budget_exhaustion_is_skipped_not_failed():
    batch = prepare([call(0, "get_weather")], [Spec("get_weather")])

    result = build_specialist_results(
        batch,
        [
            artifact(
                batch,
                0,
                success=False,
                raw_data=None,
                error="turn tool budget exhausted",
                timed_out=True,
                elapsed_ms=0,
            )
        ],
    )[0]

    assert result.status == "skipped"
    assert result.evidence == ()
