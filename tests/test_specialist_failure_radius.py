"""Audit K1 / K10 / seal: the blast radius of ONE defective specialist call.

Before these fixes a single per-call defect — a hallucinated ``user_id``, a leading
underscore, a non-finite number, an oversize payload — escaped ``prepare_specialist_batch``
and made the caller deny EVERY role-mapped read in the turn.  One bad argument on
``check_safety`` zeroed out the whole turn's retrieval while the fc path answered normally.
"""

from __future__ import annotations

import pytest

from core import turn_observations
from core.agent_loop import build_fc_nodes
from core.specialist_runtime import (
    MAX_CALL_ARGS_BYTES,
    ReadCall,
    SpecialistDispatchError,
    prepare_specialist_batch,
)
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)


LONE_SURROGATE = "Lon" + chr(0xD800) + "don"


class _Spec:
    """Minimal live ToolSpec stand-in (structural, like the runtime's other fixtures)."""

    def __init__(self, name):
        self.name = name
        self.side_effect = "none"
        self.retry_safe = True
        self.version = "1"
        self.terminal = False
        self.input_schema = {"type": "object", "properties": {}}
        self.max_retries = 1
        self.retry_on_error = False
        self.input_model_ref = f"fixture.{name}"
        self.output_model_ref = "none"


def _call(index, name, args=None):
    return ReadCall(
        index=index,
        tool_name=name,
        args={} if args is None else args,
        params_digest=f"{index + 1:016x}",
        tool_call_id=f"raw-call-{index}",
    )


def _prepare(calls):
    return prepare_specialist_batch(
        calls,
        live_specs=[_Spec(item.tool_name) for item in calls],
        root_task_id="turn:request-1",
        run_id="run-1",
        turn=0,
    )


# ── unit level ────────────────────────────────────────────────────────────────


def test_one_unsnapshotable_call_does_not_deny_the_rest_of_the_batch():
    """Audit missing-test #1."""
    batch = _prepare(
        [
            _call(0, "search_properties", {"area": "Camden"}),
            _call(1, "calculate_commute", {"from_address": "A", "to_address": "B"}),
            # The hallucinated reserved key: the manager LLM invented a `user_id`.
            _call(2, "check_safety", {"address": "Camden", "user_id": "u-1"}),
            _call(3, "get_weather", {"city": "London"}),
        ]
    )

    assert batch.rejected == {2: "specialist_reserved_argument"}
    assert batch.eligible_indices == (0, 1, 3)
    # The defective call's ROLE SIBLING keeps its grant and its plan membership.
    assert batch.call(3).role == "area_evidence"
    assert {task.role for task in batch.plan.tasks} == {
        "listings",
        "mobility",
        "area_evidence",
    }
    assert batch.call(2) is None
    assert batch.rejection_for_index(2) == "specialist_reserved_argument"
    assert batch.rejection_for_index(3) is None


@pytest.mark.parametrize(
    ("args", "expected_code"),
    [
        pytest.param({"city": LONE_SURROGATE}, "specialist_call_args_not_finite_json",
                     id="lone-surrogate"),
        pytest.param({"x": float("inf")}, "specialist_call_args_not_finite_json", id="inf"),
        pytest.param({"x": float("nan")}, "specialist_call_args_not_finite_json", id="nan"),
        pytest.param({"nested": {1: "a"}}, "specialist_call_args_not_json_native",
                     id="int-dict-key"),
        pytest.param({"x": ("a", "b")}, "specialist_call_args_not_json_native", id="tuple"),
        pytest.param({"x": {"a", "b"}}, "specialist_call_args_not_json_native", id="set"),
        pytest.param({"x": b"bytes"}, "specialist_call_args_not_json_native", id="bytes"),
        pytest.param({"x": "y" * (MAX_CALL_ARGS_BYTES + 8)},
                     "specialist_call_args_too_large", id="oversize"),
        pytest.param({"_private": "x"}, "specialist_reserved_argument", id="underscore"),
        pytest.param({"session_id": "s"}, "specialist_reserved_argument", id="reserved"),
    ],
)
def test_prepare_never_raises_for_a_per_call_defect(args, expected_code):
    """Audit missing-test #4: only SpecialistDispatchError semantics ever escape.

    ``"Lon\\ud800don"`` is the K10 case: ``json.dumps(ensure_ascii=False)`` accepts a lone
    surrogate and only the UTF-8 measurement fails, which used to raise a raw
    ``UnicodeEncodeError`` straight through every ``except SpecialistDispatchError`` and
    crash the execute_tools node.
    """
    batch = _prepare([_call(0, "get_weather", args), _call(1, "check_safety", {"address": "A"})])

    assert batch.rejected == {0: expected_code}
    # The healthy sibling in the SAME role is untouched.
    assert batch.eligible_indices == (1,)
    assert [task.role for task in batch.plan.tasks] == ["area_evidence"]


@pytest.mark.parametrize(
    "value", [LONE_SURROGATE, float("inf"), float("nan"), {"a"}, b"b"]
)
def test_canonical_json_only_ever_raises_specialist_dispatch_error(value):
    """K10: no raw encoder exception escapes, whatever the encoder chokes on."""
    from core.specialist_runtime import _canonical_json

    with pytest.raises(SpecialistDispatchError):
        _canonical_json({"x": value}, label="probe", max_bytes=MAX_CALL_ARGS_BYTES)


@pytest.mark.parametrize(
    "value",
    [LONE_SURROGATE, float("inf"), float("nan"), {1: "a"}, ("a",), {"a"}, b"b"],
)
def test_seal_only_ever_raises_specialist_dispatch_error(value):
    """The SNAPSHOT boundary catches the coercions ``_canonical_json`` cannot see.

    ``{1: "a"}`` and ``("a",)`` survive a JSON round trip unchanged as ``{"1": "a"}`` and
    ``["a"]``, so ``encoded != round_trip`` is blind to them — the explicit JSON-native
    check is what makes the sealed args a faithful copy (audit seal/F5).
    """
    from core.specialist_runtime import seal_specialist_args

    with pytest.raises(SpecialistDispatchError):
        seal_specialist_args({"x": value})


def test_batch_level_defects_still_deny_the_whole_eligible_set():
    """Fail-closed is preserved exactly where the audit says it must be."""
    with pytest.raises(SpecialistDispatchError) as duplicate:
        _prepare([_call(0, "get_weather"), _call(0, "check_safety", {"address": "A"})])
    assert duplicate.value.error_code == "specialist_duplicate_call_index"

    with pytest.raises(SpecialistDispatchError) as turn_invalid:
        prepare_specialist_batch(
            [_call(0, "get_weather")],
            live_specs=[_Spec("get_weather")],
            root_task_id="turn:request-1",
            run_id="run-1",
            turn=-1,
        )
    assert turn_invalid.value.error_code == "specialist_turn_invalid"

    with pytest.raises(SpecialistDispatchError) as too_wide:
        _prepare([_call(i, "get_weather") for i in range(33)])
    assert too_wide.value.error_code == "specialist_batch_too_wide"


# ── end to end through execute_tools ──────────────────────────────────────────


def test_hallucinated_reserved_arg_denies_only_its_own_call(tmp_path, monkeypatch):
    """The audit's PoC scenario, driven through the real execute_tools node."""
    executed = []
    denials = []
    monkeypatch.setattr(
        turn_observations,
        "note_specialist_call_denied",
        lambda **fields: denials.append(fields),
        raising=False,
    )

    def probe(name):
        async def run(**kwargs):
            executed.append(name)
            return {"tool": name, "kwargs": sorted(kwargs)}

        return run

    registry = _registry(
        tmp_path,
        [
            _tool("get_property_details", probe("get_property_details"),
                  parameters=_schema("url")),
            _tool("calculate_commute", probe("calculate_commute"),
                  parameters=_schema("from_address", "to_address")),
            _tool("check_safety", probe("check_safety"),
                  parameters=_schema("address", "user_id")),
            _tool("get_weather", probe("get_weather"), parameters=_schema("city")),
        ],
    )
    state = _execute(
        build_fc_nodes(registry, specialist_dispatch=True),
        _state(
            [
                _tc("get_property_details", {"url": "fixture://listing"}, "c1"),
                _tc("calculate_commute", {"from_address": "A", "to_address": "B"}, "c2"),
                _tc("check_safety", {"address": "Camden", "user_id": "u-1"}, "c3"),
                _tc("get_weather", {"city": "London"}, "c4"),
            ]
        ),
    )

    assert sorted(executed) == [
        "calculate_commute", "get_property_details", "get_weather",
    ]
    by_tool = {item["tool"]: item for item in state["tool_artifacts"]}
    denied = by_tool["check_safety"]
    assert denied["denied"] is True
    assert denied["success"] is False
    # LEDGER-only reason.
    assert denied["specialist_error_code"] == "specialist_reserved_argument"
    # A rejected call is not a plan member, so it carries no task metadata.
    assert "plan_id" not in denied

    for name in ("get_property_details", "calculate_commute", "get_weather"):
        assert by_tool[name]["success"] is True
        assert "specialist_error_code" not in by_tool[name]
    # Same-role sibling really did run under the boundary, not on the manager path.
    assert by_tool["get_weather"]["agent_role"] == "area_evidence"

    tool_message = next(
        message for message in state["messages"] if message.name == "check_safety"
    )
    # The stable code is never model-visible.
    assert "specialist_reserved_argument" not in tool_message.content
    assert "denied" in tool_message.content

    assert denials == [
        {"tool": "check_safety", "error_code": "specialist_reserved_argument"}
    ]

    # The three healthy calls still produced usable specialist evidence.
    statuses = {result["role"]: result["status"] for result in state["specialist_results"]}
    assert statuses == {
        "listings": "succeeded",
        "mobility": "succeeded",
        "area_evidence": "succeeded",
    }


def test_denied_call_is_counted_without_unbalancing_the_task_lifecycle(tmp_path):
    """A denial is one refused DISPATCH, not a task transition.

    The task it happened inside (``area_evidence``, whose other call is healthy) must still
    reach its own terminal status, or the turn-end invariants the release gate enforces
    would not balance.
    """
    async def probe(**kwargs):
        return {"ok": True}

    registry = _registry(
        tmp_path,
        [
            _tool("check_safety", probe, parameters=_schema("address", "user_id")),
            _tool("get_weather", probe, parameters=_schema("city")),
        ],
    )
    state = _state(
        [
            _tc("check_safety", {"address": "Camden", "user_id": "u-1"}, "c1"),
            _tc("get_weather", {"city": "London"}, "c2"),
        ]
    )

    turn_observations.begin_turn()
    try:
        state = _execute(build_fc_nodes(registry, specialist_dispatch=True), state)
        trace = turn_observations.specialist_snapshot()
    finally:
        turn_observations.end_turn()

    assert trace["denied_calls"] == 1
    assert trace["planned"] == trace["started"] == trace["completed"] == 1
    assert trace["partial"] == trace["failed"] == trace["skipped"] == 0
    assert trace["started"] == trace["completed"] + trace["partial"] + trace["failed"]
    assert trace["skipped"] <= trace["planned"] - trace["started"]
    assert state["specialist_results"][0]["status"] == "succeeded"


def test_missing_per_call_denial_hook_is_tolerated(tmp_path, monkeypatch):
    """Engineer B's counter may not exist yet; its absence must not break a turn."""
    monkeypatch.delattr(
        turn_observations, "note_specialist_call_denied", raising=False
    )

    async def weather(**kwargs):
        return {"weather": kwargs.get("city")}

    registry = _registry(
        tmp_path, [_tool("get_weather", weather, parameters=_schema("city", "user_id"))]
    )
    state = _execute(
        build_fc_nodes(registry, specialist_dispatch=True),
        _state([_tc("get_weather", {"city": "London", "user_id": "u"}, "c1")]),
    )

    artifact = state["tool_artifacts"][0]
    assert artifact["denied"] is True
    assert artifact["specialist_error_code"] == "specialist_reserved_argument"


def test_fc_path_is_unaffected_by_a_reserved_argument(tmp_path):
    """The same call on specialist_dispatch=False keeps its historical behaviour."""
    executed = []

    async def weather(**kwargs):
        executed.append(kwargs.get("city"))
        return {"weather": kwargs.get("city")}

    registry = _registry(
        tmp_path, [_tool("get_weather", weather, parameters=_schema("city", "user_id"))]
    )
    state = _execute(
        build_fc_nodes(registry, specialist_dispatch=False),
        _state([_tc("get_weather", {"city": "London", "user_id": "u"}, "c1")]),
    )

    assert executed == ["London"]
    assert state["tool_artifacts"][0]["success"] is True
    assert "specialist_error_code" not in state["tool_artifacts"][0]


def test_unplanned_but_eligible_read_fails_closed(tmp_path, monkeypatch):
    """An eligible call that is not a plan member is denied, never run unrestricted."""
    executed = []

    async def weather(**kwargs):
        executed.append(kwargs.get("city"))
        return {"weather": "sunny"}

    registry = _registry(tmp_path, [_tool("get_weather", weather, parameters=_schema("city"))])
    nodes = build_fc_nodes(registry, specialist_dispatch=True)

    import core.specialist_runtime as specialist_runtime

    real_prepare = specialist_runtime.prepare_specialist_batch

    def prepare_without_calls(calls, **kwargs):
        batch = real_prepare(calls, **kwargs)
        # Simulate a plan that lost this call between preparation and dispatch.
        return type(batch)(
            plan=batch.plan,
            calls_by_index={},
            _plan_json=batch._plan_json,
            rejected={},
        )

    monkeypatch.setattr(
        specialist_runtime, "prepare_specialist_batch", prepare_without_calls
    )
    state = _execute(nodes, _state([_tc("get_weather", {"city": "London"}, "c1")]))

    assert executed == []
    artifact = state["tool_artifacts"][0]
    assert artifact["denied"] is True
    assert artifact["specialist_error_code"] == "specialist_call_not_planned"
