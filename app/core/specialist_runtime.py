"""Deterministic, capability-scoped runtime for manager_v1 specialists.

The manager's already policy-approved function-call batch is projected into one
SpecialistTask per role. This module never calls a model or a tool. It snapshots
JSON inputs, mints/revalidates exact read-only grants, creates content-free IDs,
and derives typed evidence from the manager-owned artifact ledger.

Actual scheduling stays in core.agent_loop so its mature timeout, abandonment,
budget, cancellation and request-order semantics remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

from pydantic import ValidationError

from uk_rent_agent.agent.specialist_contracts import (
    EvidenceRef,
    MANAGER_ONLY_TOOLS,
    SPECIALIST_TOOL_ALLOWLISTS,
    UNTRUSTED_SPECIALIST_TOOLS,
    ReadOnlyToolGrant,
    SpecialistContractError,
    SpecialistResult,
    SpecialistRole,
    SpecialistTask,
    TaskPlan,
    ToolSpecLike,
    grant_read_only_tools_for_role,
    tool_input_schema_digest,
    validate_read_only_dispatch_for_role,
)


MAX_CALL_ARGS_BYTES = 64 * 1024
MAX_BATCH_ARGS_BYTES = 128 * 1024
MAX_PLAN_BYTES = 64 * 1024
MAX_RESULTS_BYTES = 128 * 1024
MAX_SPECIALIST_CALLS = 32
_PARAMS_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
_RESERVED_INPUT_KEYS = frozenset(
    {
        "idempotency_key",
        "user_id",
        "session_id",
        "memory_context",
        "final_response",
        "agent_state",
        "state",
        "store",
        "checkpointer",
    }
)


def _role_index() -> Mapping[str, SpecialistRole]:
    indexed: dict[str, SpecialistRole] = {}
    for role, names in SPECIALIST_TOOL_ALLOWLISTS.items():
        for name in names:
            if name in indexed:
                raise RuntimeError(f"specialist tool {name!r} belongs to multiple roles")
            indexed[name] = role  # type: ignore[assignment]
    if MANAGER_ONLY_TOOLS.intersection(indexed):
        raise RuntimeError("manager-only tools cannot appear in a specialist allowlist")
    return MappingProxyType(indexed)


_ROLE_BY_TOOL = _role_index()


class SpecialistDispatchError(SpecialistContractError):
    """Stable fail-closed runtime error safe to expose as an error code."""

    def __init__(self, error_code: str):
        self.error_code = str(error_code or "specialist_dispatch_failed")
        super().__init__(self.error_code)


@dataclass(frozen=True)
class ReadCall:
    """Manager-authored call after injection, de-duplication and policy checks."""

    index: int
    tool_name: str
    args: Mapping[str, Any]
    params_digest: str
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ResolvedSpecialistCapability:
    """Opaque handle minted by the trusted in-process ToolRegistry."""

    provider_identity: int
    tool_name: str
    tool_identity: int
    tool: Any
    tool_callable_identity: int
    tool_callable: Callable[..., Any]
    spec_digest: str


@dataclass(frozen=True)
class PreparedSpecialistCall:
    index: int
    tool_name: str
    params_digest: str
    tool_call_id: str
    task_id: str
    role: SpecialistRole
    grant: ReadOnlyToolGrant
    artifact_id: str
    spec_digest: str
    _args_json: str

    @property
    def args(self) -> dict[str, Any]:
        """Return a fresh detached JSON object on every access."""
        value = json.loads(self._args_json)
        if not isinstance(value, dict):
            raise SpecialistDispatchError("specialist_args_snapshot_invalid")
        return value

    def args_snapshot(self) -> dict[str, Any]:
        return self.args


@dataclass(frozen=True)
class PreparedSpecialistBatch:
    plan: TaskPlan
    calls_by_index: Mapping[int, PreparedSpecialistCall]
    _plan_json: str

    def call(self, index: int) -> PreparedSpecialistCall | None:
        return self.calls_by_index.get(index)

    @property
    def eligible_indices(self) -> tuple[int, ...]:
        return tuple(self.calls_by_index)

    def task_for_index(self, index: int) -> SpecialistTask:
        call = self.call(index)
        if call is None:
            raise SpecialistDispatchError("specialist_call_not_planned")
        for task in self.plan.tasks:
            if task.task_id == call.task_id:
                return task
        raise SpecialistDispatchError("specialist_task_missing")

    def grant_for_index(self, index: int) -> ReadOnlyToolGrant:
        call = self.call(index)
        if call is None:
            raise SpecialistDispatchError("specialist_call_not_planned")
        return call.grant

    def artifact_id_for_index(self, index: int) -> str:
        call = self.call(index)
        if call is None:
            raise SpecialistDispatchError("specialist_call_not_planned")
        return call.artifact_id

    def validated_plan(self) -> TaskPlan:
        try:
            return TaskPlan.model_validate_json(self._plan_json)
        except (ValidationError, ValueError) as exc:
            raise SpecialistDispatchError("specialist_plan_snapshot_invalid") from exc


def specialist_role_for_tool(tool_name: str) -> SpecialistRole | None:
    return _ROLE_BY_TOOL.get(str(tool_name or ""))


def _canonical_json(value: Any, *, label: str, max_bytes: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        detached = json.loads(encoded)
        round_trip = json.dumps(
            detached,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SpecialistDispatchError(f"{label}_not_finite_json") from exc
    if encoded != round_trip:
        raise SpecialistDispatchError(f"{label}_lossy_json")
    if len(encoded.encode("utf-8")) > max_bytes:
        raise SpecialistDispatchError(f"{label}_too_large")
    return encoded


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = _canonical_json(parts, label="specialist_id", max_bytes=MAX_BATCH_ARGS_BYTES)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def tool_spec_security_digest(spec: ToolSpecLike) -> str:
    """Pin every security-relevant ToolSpec field, including its input schema."""
    payload = {
        "name": getattr(spec, "name", None),
        "version": getattr(spec, "version", None),
        "side_effect": getattr(spec, "side_effect", None),
        "terminal": getattr(spec, "terminal", None),
        "retry_safe": getattr(spec, "retry_safe", None),
        "input_schema_digest": tool_input_schema_digest(spec),
    }
    encoded = _canonical_json(payload, label="tool_spec", max_bytes=MAX_CALL_ARGS_BYTES)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _index_specs(live_specs: Iterable[ToolSpecLike]) -> dict[str, ToolSpecLike]:
    indexed: dict[str, ToolSpecLike] = {}
    try:
        specs = tuple(live_specs)
    except Exception as exc:
        raise SpecialistDispatchError("specialist_live_specs_unavailable") from exc
    for spec in specs:
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not name or name in indexed:
            raise SpecialistDispatchError("specialist_live_specs_invalid")
        indexed[name] = spec
    return indexed


def _eligible(call: ReadCall) -> SpecialistRole | None:
    role = specialist_role_for_tool(call.tool_name)
    if role is None or call.tool_name in MANAGER_ONLY_TOOLS:
        return None
    if call.tool_name == "web_search":
        try:
            if call.args.get("sub_queries"):
                return None
        except Exception as exc:
            raise SpecialistDispatchError("specialist_web_search_args_invalid") from exc
    return role


def _snapshot_call(call: ReadCall) -> tuple[str, str]:
    if isinstance(call.index, bool) or not isinstance(call.index, int) or call.index < 0:
        raise SpecialistDispatchError("specialist_call_index_invalid")
    if not isinstance(call.tool_name, str) or not call.tool_name:
        raise SpecialistDispatchError("specialist_tool_name_invalid")
    if not isinstance(call.args, Mapping):
        raise SpecialistDispatchError("specialist_call_args_invalid")
    keys = tuple(call.args)
    if any(not isinstance(key, str) or key.startswith("_") for key in keys):
        raise SpecialistDispatchError("specialist_reserved_argument")
    if _RESERVED_INPUT_KEYS.intersection(keys):
        raise SpecialistDispatchError("specialist_reserved_argument")
    if (
        not isinstance(call.params_digest, str)
        or _PARAMS_DIGEST_RE.fullmatch(call.params_digest) is None
    ):
        raise SpecialistDispatchError("specialist_params_digest_invalid")
    call_id = call.tool_call_id or f"call_{call.index}"
    if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
        raise SpecialistDispatchError("specialist_tool_call_id_invalid")
    args_json = _canonical_json(
        dict(call.args), label="specialist_call_args", max_bytes=MAX_CALL_ARGS_BYTES
    )
    if not isinstance(json.loads(args_json), dict):
        raise SpecialistDispatchError("specialist_call_args_invalid")
    return args_json, call_id


def prepare_specialist_batch(
    calls: Iterable[ReadCall],
    *,
    live_specs: Iterable[ToolSpecLike],
    root_task_id: str,
    run_id: str,
    turn: int,
) -> PreparedSpecialistBatch:
    """Group eligible calls by role and mint immutable manager-owned grants."""
    call_list = tuple(calls)
    if len(call_list) > MAX_SPECIALIST_CALLS:
        raise SpecialistDispatchError("specialist_batch_too_wide")
    if len(call_list) != len({call.index for call in call_list}):
        raise SpecialistDispatchError("specialist_duplicate_call_index")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise SpecialistDispatchError("specialist_turn_invalid")
    specs = _index_specs(live_specs)
    buckets: OrderedDict[
        SpecialistRole, list[tuple[ReadCall, str, str]]
    ] = OrderedDict()
    total_args = 0
    for call in call_list:
        role = _eligible(call)
        if role is None:
            continue
        args_json, call_id = _snapshot_call(call)
        total_args += len(args_json.encode("utf-8"))
        if total_args > MAX_BATCH_ARGS_BYTES:
            raise SpecialistDispatchError("specialist_batch_args_too_large")
        buckets.setdefault(role, []).append((call, args_json, call_id))

    # Execution-context IDs can be syntactically valid while still embedding a
    # client-controlled request ID, email address, postcode, or other source
    # identifier. Checkpoint only an opaque, deterministic boundary ID.
    safe_root_task_id = _stable_id(
        "manager", "root-task-v1", str(run_id), turn, str(root_task_id)
    )
    plan_seed = [
        str(run_id),
        int(turn),
        str(root_task_id),
        [
            [call.index, call.tool_name, call.params_digest, call_id]
            for entries in buckets.values()
            for call, _args_json, call_id in entries
        ],
    ]
    plan_id = _stable_id("plan", plan_seed)
    tasks: list[SpecialistTask] = []
    prepared: dict[int, PreparedSpecialistCall] = {}
    for role, entries in buckets.items():
        tool_names = tuple(dict.fromkeys(call.tool_name for call, _a, _cid in entries))
        try:
            grants = grant_read_only_tools_for_role(
                role, tool_names, live_specs=tuple(specs.values())
            )
        except (SpecialistContractError, ValidationError, ValueError) as exc:
            raise SpecialistDispatchError("specialist_grant_invalid") from exc
        grant_by_name = {grant.name: grant for grant in grants}
        task_id = f"{plan_id}/{role}"
        metadata_calls = [
            {
                "index": call.index,
                "tool": call.tool_name,
                "tool_call_id": _stable_id(
                    "call",
                    str(run_id),
                    turn,
                    str(root_task_id),
                    call.index,
                    call_id,
                    call.params_digest,
                    hashlib.sha256(args_json.encode("utf-8")).hexdigest(),
                ),
                "params_digest": call.params_digest,
            }
            for call, args_json, call_id in entries
        ]
        try:
            task = SpecialistTask(
                task_id=task_id,
                parent_task_id=safe_root_task_id,
                role=role,
                objective=f"Collect manager-requested {role} evidence",
                tools=grants,
                inputs={"calls": metadata_calls},
            )
        except (ValidationError, ValueError, SpecialistContractError) as exc:
            raise SpecialistDispatchError("specialist_task_invalid") from exc
        tasks.append(task)
        for call, args_json, call_id in entries:
            spec = specs.get(call.tool_name)
            if spec is None:
                raise SpecialistDispatchError("specialist_live_spec_missing")
            spec_digest = tool_spec_security_digest(spec)
            safe_call_id = next(
                item["tool_call_id"]
                for item in metadata_calls
                if item["index"] == call.index
            )
            prepared[call.index] = PreparedSpecialistCall(
                index=call.index,
                tool_name=call.tool_name,
                params_digest=call.params_digest,
                tool_call_id=safe_call_id,
                task_id=task_id,
                role=role,
                grant=grant_by_name[call.tool_name],
                artifact_id=_stable_id(
                    "artifact", plan_id, task_id, call.index, call.params_digest
                ),
                spec_digest=spec_digest,
                _args_json=args_json,
            )

    try:
        plan = TaskPlan(
            plan_id=plan_id,
            root_task_id=safe_root_task_id,
            no_tools=not tasks,
            tasks=tuple(tasks),
        )
        plan_json = plan.model_dump_json()
        _canonical_json(
            plan.model_dump(mode="json"),
            label="specialist_plan",
            max_bytes=MAX_PLAN_BYTES,
        )
        plan = TaskPlan.model_validate_json(plan_json)
    except (ValidationError, ValueError, SpecialistContractError) as exc:
        raise SpecialistDispatchError("specialist_plan_invalid") from exc
    return PreparedSpecialistBatch(
        plan=plan,
        calls_by_index=MappingProxyType(dict(prepared)),
        _plan_json=plan_json,
    )


def revalidate_specialist_call(
    prepared: PreparedSpecialistBatch,
    index: int,
    live_specs: Iterable[ToolSpecLike],
) -> PreparedSpecialistCall:
    """Revalidate plan, role policy and the complete live ToolSpec before dispatch."""
    if not isinstance(prepared, PreparedSpecialistBatch):
        raise SpecialistDispatchError("specialist_batch_invalid")
    call = prepared.call(index)
    if call is None:
        raise SpecialistDispatchError("specialist_call_not_planned")
    plan = prepared.validated_plan()
    task = next((item for item in plan.tasks if item.task_id == call.task_id), None)
    if task is None or task.role != call.role:
        raise SpecialistDispatchError("specialist_task_identity_changed")
    grant = next((item for item in task.tools if item.name == call.tool_name), None)
    if grant is None or grant != call.grant:
        raise SpecialistDispatchError("specialist_grant_changed")
    spec = _index_specs(live_specs).get(call.tool_name)
    if spec is None:
        raise SpecialistDispatchError("specialist_live_spec_missing")
    try:
        validate_read_only_dispatch_for_role(task.role, grant, spec)
    except (SpecialistContractError, ValidationError, ValueError) as exc:
        raise SpecialistDispatchError("specialist_live_spec_rejected") from exc
    if tool_spec_security_digest(spec) != call.spec_digest:
        raise SpecialistDispatchError("specialist_capability_metadata_drift")
    return call


def _duration(artifacts: Iterable[Mapping[str, Any]]) -> float:
    values: list[float] = []
    for artifact in artifacts:
        value = artifact.get("elapsed_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return max(values, default=0.0)


def _artifact_matches(
    artifact: Mapping[str, Any],
    *,
    prepared: PreparedSpecialistBatch,
    call: PreparedSpecialistCall,
) -> bool:
    return (
        artifact.get("artifact_id") == call.artifact_id
        and artifact.get("plan_id") == prepared.plan.plan_id
        and artifact.get("task_id") == call.task_id
        and artifact.get("agent_role") == call.role
        and artifact.get("parent_task_id") == prepared.plan.root_task_id
        and artifact.get("tool") == call.tool_name
        and artifact.get("params_digest") == call.params_digest
    )


def build_specialist_results(
    prepared: PreparedSpecialistBatch,
    artifacts: Iterable[Mapping[str, Any]],
) -> tuple[SpecialistResult, ...]:
    """Derive typed results solely from manager-minted artifact references."""
    plan = prepared.validated_plan()
    ledger = [item for item in artifacts if isinstance(item, Mapping)]
    by_artifact: dict[str, list[Mapping[str, Any]]] = {}
    for artifact in ledger:
        artifact_id = artifact.get("artifact_id")
        if isinstance(artifact_id, str):
            by_artifact.setdefault(artifact_id, []).append(artifact)

    results: list[SpecialistResult] = []
    for task in plan.tasks:
        task_calls = [
            call
            for call in prepared.calls_by_index.values()
            if call.task_id == task.task_id
        ]
        matched: list[Mapping[str, Any]] = []
        invalid_ledger = False
        for call in task_calls:
            candidates = by_artifact.get(call.artifact_id, [])
            if len(candidates) != 1 or not _artifact_matches(
                candidates[0], prepared=prepared, call=call
            ):
                invalid_ledger = True
                break
            matched.append(candidates[0])

        evidence: list[EvidenceRef] = []
        if not invalid_ledger:
            for call, artifact in zip(task_calls, matched):
                reliable = (
                    artifact.get("success") is True
                    and artifact.get("raw_data") is not None
                    and not artifact.get("denied")
                    and not artifact.get("timed_out")
                    and not artifact.get("abandoned")
                    and not artifact.get("outcome_unknown")
                )
                if not reliable:
                    continue
                evidence.append(
                    EvidenceRef(
                        evidence_id=_stable_id("evidence", call.artifact_id),
                        task_id=task.task_id,
                        tool_name=call.tool_name,
                        artifact_id=call.artifact_id,
                        claim=f"{call.tool_name} returned manager-visible evidence",
                        tainted=call.tool_name in UNTRUSTED_SPECIALIST_TOOLS,
                    )
                )

        total = len(task_calls)
        succeeded = len(evidence)
        if invalid_ledger:
            status = "failed"
            summary = ""
            error = "specialist artifact validation failed"
            evidence = []
        elif succeeded == total and total:
            status = "succeeded"
            summary = f"{succeeded} of {total} specialist calls returned evidence"
            error = None
        elif succeeded:
            status = "partial"
            summary = f"{succeeded} of {total} specialist calls returned evidence"
            error = "one or more specialist calls were incomplete"
        else:
            # A task skipped before dispatch has two existing FC artifact shapes:
            # soft-budget exhaustion is ``denied=True``; cumulative turn-budget
            # exhaustion is ``timed_out=True, elapsed_ms=0``.  Neither started a
            # provider call, so both are a specialist ``skipped`` outcome.  Actual
            # timeouts/abandonments remain failures.
            skipped = bool(matched) and all(
                (
                    artifact.get("denied")
                    or (
                        artifact.get("timed_out")
                        and artifact.get("elapsed_ms") == 0
                        and not artifact.get("abandoned")
                        and not artifact.get("outcome_unknown")
                    )
                )
                and any(
                    marker in str(artifact.get("error") or "").lower()
                    for marker in ("budget exhausted", "time budget")
                )
                for artifact in matched
            )
            status = "skipped" if skipped else "failed"
            summary = ""
            error = (
                "specialist task was not started because the turn budget was exhausted"
                if skipped
                else "specialist task produced no reliable evidence"
            )
        result = SpecialistResult(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            role=task.role,
            status=status,
            summary=summary,
            data={
                "call_count": total,
                "succeeded": succeeded,
                "artifact_ids": [call.artifact_id for call in task_calls],
            },
            evidence=tuple(evidence),
            error=error,
            duration_ms=_duration(matched),
        )
        results.append(SpecialistResult.model_validate_json(result.model_dump_json()))

    _canonical_json(
        [item.model_dump(mode="json") for item in results],
        label="specialist_results",
        max_bytes=MAX_RESULTS_BYTES,
    )
    return tuple(results)


build_specialist_results_from_artifacts = build_specialist_results


__all__ = [
    "MAX_BATCH_ARGS_BYTES",
    "MAX_CALL_ARGS_BYTES",
    "MAX_PLAN_BYTES",
    "MAX_RESULTS_BYTES",
    "MAX_SPECIALIST_CALLS",
    "PreparedSpecialistBatch",
    "PreparedSpecialistCall",
    "ReadCall",
    "ResolvedSpecialistCapability",
    "SpecialistDispatchError",
    "build_specialist_results",
    "build_specialist_results_from_artifacts",
    "prepare_specialist_batch",
    "revalidate_specialist_call",
    "specialist_role_for_tool",
    "tool_spec_security_digest",
]
