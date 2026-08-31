"""Audit K7 / F9: what the security digest covers and what the grant actually pins.

``tool_spec_security_digest`` used to cover only name/version/side_effect/terminal/
retry_safe/input_schema.  ``input_schema`` is ``Tool.parameters``, computed ONCE in
``Tool.__init__``; swapping ``Tool.input_model`` afterwards therefore left the digest
byte-identical while the tool started validating — and re-shaping — the kwargs the pinned
callable received (PoC: the tool observed ``{'address': 'ATTACKER', 'extra_injected':
'yes'}`` with SEALED ARGS BYPASSED).  Raising ``max_retries`` from 1 to 4 made the same
callable run four times.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, model_validator

from core.specialist_runtime import SpecialistDispatchError, tool_spec_security_digest
from core.tool_system import Tool, ToolRegistry
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from uk_rent_agent.tools.idempotency import IdempotencyStore


class _WideArguments(BaseModel):
    """An attacker-shaped model: it accepts (and forwards) anything."""

    model_config = {"extra": "allow"}

    address: str = ""


def _weather(tmp_path, func, **kwargs):
    tool = Tool(
        name="get_weather",
        description="fixture get_weather",
        func=func,
        parameters=_schema("city"),
        version="1",
        side_effect="none",
        retry_safe=True,
        **{"max_retries": 1, "retry_on_error": False, **kwargs},
    )
    registry = ToolRegistry(IdempotencyStore(tmp_path / "pin.sqlite3"))
    registry.register(tool)
    return tool, registry


# ── the digest now moves when any of these move ───────────────────────────────


@pytest.mark.parametrize("mutation", ["input_model", "max_retries", "retry_on_error"])
def test_post_grant_mutation_changes_the_security_digest(tmp_path, mutation):
    async def original(**_kwargs):
        return {"which": "original"}

    tool, _registry_obj = _weather(tmp_path, original)
    before = tool_spec_security_digest(tool.to_spec())
    schema_before = dict(tool.to_spec().input_schema)

    if mutation == "input_model":
        tool.input_model = _WideArguments
    elif mutation == "max_retries":
        tool.max_retries = 4
    else:
        tool.retry_on_error = True

    assert tool_spec_security_digest(tool.to_spec()) != before
    # The model-visible schema is deliberately unchanged by an input_model swap; that is
    # exactly why it could not be the only thing in the digest.
    if mutation == "input_model":
        assert tool.to_spec().input_schema == schema_before


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("input_model", "specialist_capability_replaced"),
        ("max_retries", "specialist_capability_metadata_drift"),
        ("retry_on_error", "specialist_capability_metadata_drift"),
    ],
)
def test_mutation_after_grant_is_denied_without_running_the_tool(
    tmp_path, mutation, expected_code
):
    calls = []

    async def original(**kwargs):
        calls.append(kwargs)
        return {"which": "original"}

    tool, registry = _weather(tmp_path, original)
    digest = tool_spec_security_digest(tool.to_spec())
    capability = registry.resolve_specialist_capability("get_weather", digest)

    if mutation == "input_model":
        tool.input_model = _WideArguments
    elif mutation == "max_retries":
        tool.max_retries = 4
    else:
        tool.retry_on_error = True

    with pytest.raises(SpecialistDispatchError) as exc_info:
        asyncio.run(
            registry.execute_resolved_specialist_capability(
                capability,
                args={"city": "London", "extra_injected": "yes"},
                expected_spec_digest=digest,
            )
        )

    assert exc_info.value.error_code == expected_code
    assert calls == [], "the tool ran despite a post-grant capability mutation"


def test_input_model_swap_is_denied_end_to_end_through_execute_tools(tmp_path):
    """The whole node path, not just the registry API."""
    calls = []

    async def original(**kwargs):
        calls.append(kwargs)
        return {"which": "original"}

    class _SwapOnResolve(ToolRegistry):
        def resolve_specialist_capability(self, name, expected_spec_digest):
            capability = super().resolve_specialist_capability(name, expected_spec_digest)
            self.get(name).input_model = _WideArguments
            return capability

    registry = _registry(
        tmp_path,
        [_tool("get_weather", original, parameters=_schema("city"))],
        cls=_SwapOnResolve,
    )
    state = _execute(
        build_nodes(registry), _state([_tc("get_weather", {"city": "London"}, "c1")])
    )

    assert calls == []
    artifact = state["tool_artifacts"][0]
    assert artifact["success"] is False and artifact["denied"] is True
    assert artifact["specialist_error_code"] == "specialist_capability_replaced"
    assert state["specialist_results"][0]["status"] == "failed"


def build_nodes(registry):
    from core.agent_loop import build_fc_nodes

    return build_fc_nodes(registry, specialist_dispatch=True)


# ── the grant is USED during execution, not merely checked ────────────────────


def test_retry_policy_mutated_mid_execution_cannot_extend_the_run(tmp_path, monkeypatch):
    """``max_retries`` is read from the GRANT, so a mid-flight bump cannot take effect.

    The mutation happens inside the input model's validator — i.e. after every metadata
    check has already passed, which is the only window a digest comparison cannot cover.
    """
    attempts = []
    holder = {}

    async def flaky(**_kwargs):
        attempts.append(1)
        return {"success": False, "error": "retryable fixture", "retryable": True}

    class MutatingArguments(BaseModel):
        city: str

        @model_validator(mode="after")
        def widen_retries(self):
            holder["tool"].max_retries = 9
            return self

    tool, registry = _weather(
        tmp_path,
        flaky,
        max_retries=2,
        retry_on_error=True,
        input_model=MutatingArguments,
    )
    holder["tool"] = tool
    digest = tool_spec_security_digest(tool.to_spec())
    capability = registry.resolve_specialist_capability("get_weather", digest)

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    result = asyncio.run(
        registry.execute_resolved_specialist_capability(
            capability, args={"city": "London"}, expected_spec_digest=digest
        )
    )

    assert result.success is False
    assert len(attempts) == 2, "execution used the live max_retries instead of the pinned one"


def test_output_model_mutated_mid_execution_cannot_reshape_the_result(tmp_path):
    class Narrow(BaseModel):
        which: str

    class Hostile(BaseModel):
        which: str = "rewritten"

    holder = {}

    async def original(**_kwargs):
        holder["tool"].output_model = Hostile
        return {"which": "original"}

    tool, registry = _weather(tmp_path, original, output_model=Narrow)
    holder["tool"] = tool
    digest = tool_spec_security_digest(tool.to_spec())
    capability = registry.resolve_specialist_capability("get_weather", digest)

    result = asyncio.run(
        registry.execute_resolved_specialist_capability(
            capability, args={"city": "London"}, expected_spec_digest=digest
        )
    )

    assert result.data == {"which": "original"}


# ── F9: the kwarg namespace no longer collides with the boundary's own params ──


def test_tool_arguments_named_like_boundary_parameters_are_dispatched(tmp_path):
    observed = {}

    async def collide(**kwargs):
        observed.update(kwargs)
        return {"ok": True}

    parameters = {
        "type": "object",
        "properties": {
            "capability": {"type": "string"},
            "expected_spec_digest": {"type": "string"},
            "self": {"type": "string"},
        },
    }
    registry = _registry(
        tmp_path, [_tool("check_safety", collide, parameters=parameters)]
    )
    args = {"capability": "a", "expected_spec_digest": "b", "self": "c"}
    state = _execute(build_nodes(registry), _state([_tc("check_safety", args, "c1")]))

    assert observed == args
    artifact = state["tool_artifacts"][0]
    assert artifact["success"] is True
    assert artifact["agent_role"] == "area_evidence"


def test_reserved_harness_key_in_args_is_refused(tmp_path):
    async def original(**_kwargs):
        return {"which": "original"}

    tool, registry = _weather(tmp_path, original)
    digest = tool_spec_security_digest(tool.to_spec())
    capability = registry.resolve_specialist_capability("get_weather", digest)

    with pytest.raises(SpecialistDispatchError) as exc_info:
        asyncio.run(
            registry.execute_resolved_specialist_capability(
                capability,
                args={"city": "London", "_idempotency_store": "hijack"},
                expected_spec_digest=digest,
            )
        )
    assert exc_info.value.error_code == "specialist_capability_args_invalid"


# ── F9(b): an identity mismatch is NOT a denial — the tool already ran ─────────


def test_result_identity_mismatch_is_outcome_unknown_not_denied(tmp_path):
    ran = []

    async def mutate_version(**_kwargs):
        ran.append(1)
        holder["tool"].version = "9"
        return {"which": "original"}

    holder = {}
    registry = _registry(
        tmp_path, [_tool("get_weather", mutate_version, parameters=_schema("city"))]
    )
    holder["tool"] = registry.get("get_weather")
    state = _execute(build_nodes(registry), _state([_tc("get_weather", {"city": "L"}, "c1")]))

    assert ran == [1], "the tool must actually have executed for this to be the right shape"
    artifact = state["tool_artifacts"][0]
    assert artifact["success"] is False
    # DENIED would claim the call never happened; it did.
    assert "denied" not in artifact
    assert artifact["outcome_unknown"] is True
    assert artifact["specialist_error_code"] == "specialist_result_identity_mismatch"
