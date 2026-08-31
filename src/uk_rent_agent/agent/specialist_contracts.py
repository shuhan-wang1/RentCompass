"""Typed manager/specialist boundaries for the experimental multi-agent runtime.

These contracts are deliberately independent from the legacy ``AgentState`` task-plan
dictionaries.  They define the data that may cross the manager/specialist boundary without
changing existing checkpoints or graph behaviour.

The models are frozen and reject unknown fields because this boundary is also a capability
boundary: specialists may return evidence, but they may not smuggle manager-owned state such
as memory, user interaction, or a final response into the graph update.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import AbstractSet, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


SpecialistRole = Literal["listings", "mobility", "area_evidence"]
SpecialistStatus = Literal["succeeded", "partial", "failed", "skipped"]
AnswerResponseType = Literal["answer", "clarification", "error"]

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    ),
]
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$",
    ),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]

MANAGER_ONLY_TOOLS = frozenset({"remember", "recall_memory", "ask_user"})
MAX_SPECIALIST_TASKS = 8
MAX_TOOL_INPUT_SCHEMA_BYTES = 256 * 1024
MAX_TASK_INPUT_BYTES = 64 * 1024
MAX_RESULT_DATA_BYTES = 32 * 1024
UNTRUSTED_SPECIALIST_TOOLS = frozenset(
    {
        "web_search",
        "search_properties",
        "get_property_details",
        "search_nearby_pois",
    }
)

# Exact, manager-owned capability catalog.  MappingProxyType freezes the role mapping and each
# value is a frozenset, so a caller cannot widen a specialist's authority in place.
SPECIALIST_TOOL_ALLOWLISTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "listings": frozenset({"search_properties", "get_property_details"}),
        "mobility": frozenset(
            {
                "calculate_commute",
                "calculate_commute_cost",
                "check_transport_cost",
                "get_transport_info",
            }
        ),
        "area_evidence": frozenset(
            {
                "check_safety",
                "compare_or_rank_areas",
                "get_weather",
                "search_nearby_pois",
                "web_search",
            }
        ),
    }
)


class SpecialistContractError(ValueError):
    """A live capability does not satisfy a specialist's read-only contract."""


class ToolSpecLike(Protocol):
    """Structural subset of ``core.tool_system.ToolSpec`` used at the src boundary.

    ``max_retries``/``retry_on_error``/``input_model_ref``/``output_model_ref`` are part of
    the security digest computed by ``core.specialist_runtime.tool_spec_security_digest``:
    the model-visible ``input_schema`` is snapshotted at Tool construction time and so
    cannot see a later ``input_model`` swap, and the retry policy decides how many times a
    pinned callable actually runs.  They are read with ``getattr(..., None)`` so a legacy
    spec object stays usable — it just digests as "unset" consistently on both sides.
    """

    name: str
    side_effect: str
    retry_safe: bool
    version: str
    terminal: bool
    input_schema: dict
    max_retries: int
    retry_on_error: bool
    input_model_ref: str
    output_model_ref: str


class _StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _canonical_json_bytes(value: Any, *, label: str, max_bytes: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        detached = json.loads(encoded)
        round_trip = json.dumps(
            detached,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SpecialistContractError(f"{label} must be finite JSON") from exc
    if encoded != round_trip:
        raise SpecialistContractError(f"{label} must survive a lossless JSON round trip")
    if len(encoded) > max_bytes:
        raise SpecialistContractError(f"{label} exceeds {max_bytes} bytes")
    return encoded


def tool_input_schema_digest(spec_or_schema: ToolSpecLike | Mapping[str, Any]) -> str:
    """Canonical digest of the model-visible input schema used in a tool grant."""
    schema = (
        spec_or_schema
        if isinstance(spec_or_schema, Mapping)
        else getattr(spec_or_schema, "input_schema", None)
    )
    if not isinstance(schema, Mapping):
        raise SpecialistContractError("live tool spec has no valid input schema")
    payload = _canonical_json_bytes(
        dict(schema),
        label="tool input schema",
        max_bytes=MAX_TOOL_INPUT_SCHEMA_BYTES,
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_input_schema_digest(input_schema: Mapping[str, Any]) -> str:
    """Compatibility alias accepting an input schema object directly."""
    return tool_input_schema_digest(input_schema)


class ReadOnlyToolGrant(_StrictContract):
    """Snapshot of a live tool capability granted by the manager.

    The literals make a write or terminal grant structurally invalid.  The live spec is still
    checked again immediately before dispatch so metadata or provider drift cannot turn this
    snapshot into ambient authority.
    """

    name: ToolName
    version: ShortText = "1"
    side_effect: Literal["none"] = "none"
    terminal: Literal[False] = False
    retry_safe: bool = True
    input_schema_digest: Sha256Digest

    @model_validator(mode="after")
    def _deny_manager_only_tools(self) -> "ReadOnlyToolGrant":
        if self.name in MANAGER_ONLY_TOOLS:
            raise ValueError(f"tool {self.name!r} is manager-only")
        return self


class SpecialistTask(_StrictContract):
    """One bounded, manager-authored specialist assignment."""

    schema_version: Literal["1"] = "1"
    task_id: Identifier
    parent_task_id: Identifier
    role: SpecialistRole
    objective: ShortText
    tools: tuple[ReadOnlyToolGrant, ...] = Field(min_length=1, max_length=16)
    depends_on: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=MAX_SPECIALIST_TASKS)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_local_invariants(self) -> "SpecialistTask":
        tool_names = [grant.name for grant in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("specialist task tool grants must be unique")

        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("specialist task dependencies must be unique")
        if self.task_id in self.depends_on:
            raise ValueError("specialist task cannot depend on itself")
        allowed = SPECIALIST_TOOL_ALLOWLISTS[self.role]
        wrong_role = [name for name in tool_names if name not in allowed]
        if wrong_role:
            raise ValueError(
                f"specialist task contains grants outside role {self.role!r}: {wrong_role!r}"
            )
        _canonical_json_bytes(
            self.inputs,
            label="specialist task inputs",
            max_bytes=MAX_TASK_INPUT_BYTES,
        )
        return self


class TaskPlan(_StrictContract):
    """A manager-owned, dependency-validated specialist plan.

    ``no_tools`` is explicit rather than inferred so a direct answer is observable and cannot
    accidentally acquire an extra specialist hop.  Version 1 permits a single delegation
    level: every specialist task is parented directly by ``root_task_id``.
    """

    schema_version: Literal["1"] = "1"
    plan_id: Identifier
    root_task_id: Identifier
    created_by: Literal["manager"] = "manager"
    no_tools: bool
    tasks: tuple[SpecialistTask, ...] = Field(default_factory=tuple, max_length=MAX_SPECIALIST_TASKS)

    @model_validator(mode="after")
    def _validate_plan(self) -> "TaskPlan":
        if self.no_tools != (len(self.tasks) == 0):
            raise ValueError("no_tools must be true if and only if tasks is empty")

        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("specialist task ids must be unique within a plan")

        known_ids = set(task_ids)
        if self.root_task_id in known_ids:
            raise ValueError("root task id must not equal a specialist task id")
        graph: dict[str, set[str]] = {}
        for task in self.tasks:
            if task.parent_task_id != self.root_task_id:
                raise ValueError(
                    f"task {task.task_id!r} must be parented by root task "
                    f"{self.root_task_id!r}"
                )
            unknown = set(task.depends_on) - known_ids
            if unknown:
                raise ValueError(
                    f"task {task.task_id!r} has unknown dependencies: {sorted(unknown)!r}"
                )
            graph[task.task_id] = set(task.depends_on)

        # Kahn's algorithm rejects every cycle (including a cycle hidden behind an otherwise
        # valid dependency chain) before any specialist can be dispatched.
        remaining = {task_id: set(deps) for task_id, deps in graph.items()}
        while remaining:
            ready = {task_id for task_id, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError("specialist task dependencies must form an acyclic graph")
            for task_id in ready:
                remaining.pop(task_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return self


class EvidenceRef(_StrictContract):
    """A compact pointer to evidence retained in the manager-owned artifact ledger."""

    schema_version: Literal["1"] = "1"
    evidence_id: Identifier
    task_id: Identifier
    tool_name: ToolName
    artifact_id: Identifier
    selector: Annotated[str, StringConstraints(strip_whitespace=True, max_length=512)] | None = None
    claim: ShortText
    source_uri: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_048)] | None = None
    tainted: bool = False

    @model_validator(mode="after")
    def _mark_web_evidence_untrusted(self) -> "EvidenceRef":
        if self.tool_name in UNTRUSTED_SPECIALIST_TOOLS and not self.tainted:
            raise ValueError(
                f"{self.tool_name} evidence must remain tainted for manager review"
            )
        return self


class SpecialistResult(_StrictContract):
    """Manager-facing specialist output; never a user-facing final answer."""

    schema_version: Literal["1"] = "1"
    task_id: Identifier
    parent_task_id: Identifier
    role: SpecialistRole
    status: SpecialistStatus
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4_000)] = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()
    error: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)] | None = None
    duration_ms: Annotated[float, Field(ge=0)] = 0.0

    @model_validator(mode="after")
    def _validate_result(self) -> "SpecialistResult":
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("specialist result evidence ids must be unique")
        mismatched = [item.evidence_id for item in self.evidence if item.task_id != self.task_id]
        if mismatched:
            raise ValueError(
                f"evidence must belong to result task {self.task_id!r}: {mismatched!r}"
            )

        if self.status == "succeeded":
            if not self.summary:
                raise ValueError("a succeeded specialist result requires a summary")
            if not self.evidence:
                raise ValueError("a succeeded specialist result requires evidence")
            if self.error is not None:
                raise ValueError("a succeeded specialist result cannot include an error")
        elif self.status == "partial":
            if not self.summary or not self.evidence:
                raise ValueError("a partial specialist result requires a summary and evidence")
            if self.error is None:
                raise ValueError("a partial specialist result requires an incompleteness reason")
        else:
            if self.error is None:
                raise ValueError(f"a {self.status} specialist result requires an error or reason")
            if self.evidence:
                raise ValueError(
                    f"a {self.status} specialist result cannot expose answer evidence; use partial"
                )
        _canonical_json_bytes(
            self.data,
            label="specialist result data",
            max_bytes=MAX_RESULT_DATA_BYTES,
        )
        return self


class AnswerContract(_StrictContract):
    """The sole user-facing answer boundary, owned by the root manager."""

    schema_version: Literal["1"] = "1"
    owner: Literal["manager"] = "manager"
    root_task_id: Identifier
    response_type: AnswerResponseType
    final_response: ShortText
    used_task_ids: tuple[Identifier, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    limitations: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def _validate_answer_evidence(self) -> "AnswerContract":
        if len(self.used_task_ids) != len(set(self.used_task_ids)):
            raise ValueError("answer task ids must be unique")

        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("answer evidence ids must be unique")

        used = set(self.used_task_ids)
        evidence_tasks = {item.task_id for item in self.evidence}
        unknown = evidence_tasks - used
        if unknown:
            raise ValueError(
                f"answer evidence refers to undeclared specialist tasks: {sorted(unknown)!r}"
            )
        missing = used - evidence_tasks
        if missing:
            raise ValueError(
                "every used specialist task requires supporting evidence: "
                f"{sorted(missing)!r}"
            )
        return self


def _index_live_specs(live_specs: Iterable[ToolSpecLike]) -> dict[str, ToolSpecLike]:
    indexed: dict[str, ToolSpecLike] = {}
    for spec in live_specs:
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not name:
            raise SpecialistContractError("live tool spec has no valid name")
        if name in indexed:
            raise SpecialistContractError(f"duplicate live tool spec for {name!r}")
        indexed[name] = spec
    return indexed


def _validate_live_read_only_spec(name: str, spec: ToolSpecLike) -> None:
    if getattr(spec, "name", None) != name:
        raise SpecialistContractError(
            f"live tool name mismatch: expected {name!r}, got {getattr(spec, 'name', None)!r}"
        )
    if name in MANAGER_ONLY_TOOLS:
        raise SpecialistContractError(f"tool {name!r} is manager-only")
    if getattr(spec, "side_effect", None) != "none":
        raise SpecialistContractError(f"tool {name!r} is not read-only")
    if getattr(spec, "terminal", None) is not False:
        raise SpecialistContractError(f"tool {name!r} is terminal and manager-only")
    if not isinstance(getattr(spec, "version", None), str) or not spec.version.strip():
        raise SpecialistContractError(f"tool {name!r} has no valid version")
    if not isinstance(getattr(spec, "retry_safe", None), bool):
        raise SpecialistContractError(f"tool {name!r} has no valid retry policy")
    tool_input_schema_digest(spec)


def grant_read_only_tools(
    requested_names: Iterable[str],
    *,
    live_specs: Iterable[ToolSpecLike],
    role_allowlist: AbstractSet[str],
) -> tuple[ReadOnlyToolGrant, ...]:
    """Create grants from live metadata and an exact manager-owned role allowlist.

    The caller must supply the role's allowlist; metadata saying "read-only" is not enough to
    grant a newly discovered plugin/MCP tool ambient specialist authority.
    """

    requested = tuple(requested_names)
    if not requested:
        raise SpecialistContractError("a specialist task requires at least one requested tool")
    if any(not isinstance(name, str) or not name for name in requested):
        raise SpecialistContractError("requested tool names must be non-empty strings")
    if len(requested) != len(set(requested)):
        raise SpecialistContractError("requested tool names must be unique")

    indexed = _index_live_specs(live_specs)
    grants: list[ReadOnlyToolGrant] = []
    for name in requested:
        if name not in role_allowlist:
            raise SpecialistContractError(f"tool {name!r} is not allowed for this specialist role")
        spec = indexed.get(name)
        if spec is None:
            raise SpecialistContractError(f"tool {name!r} is not present in the live registry")
        _validate_live_read_only_spec(name, spec)
        grants.append(
            ReadOnlyToolGrant(
                name=name,
                version=spec.version,
                retry_safe=spec.retry_safe,
                input_schema_digest=tool_input_schema_digest(spec),
            )
        )
    return tuple(grants)


def grant_read_only_tools_for_role(
    role: SpecialistRole,
    requested_names: Iterable[str],
    *,
    live_specs: Iterable[ToolSpecLike],
) -> tuple[ReadOnlyToolGrant, ...]:
    """Safest grant entry point: resolve the exact immutable allowlist by role."""

    try:
        allowlist = SPECIALIST_TOOL_ALLOWLISTS[role]
    except KeyError as exc:
        raise SpecialistContractError(f"unknown specialist role {role!r}") from exc
    return grant_read_only_tools(
        requested_names,
        live_specs=live_specs,
        role_allowlist=allowlist,
    )


def validate_read_only_dispatch(
    grant: ReadOnlyToolGrant,
    live_spec: ToolSpecLike,
    *,
    role_allowlist: AbstractSet[str],
) -> None:
    """Revalidate a grant against live metadata immediately before dispatch."""

    if grant.name not in role_allowlist:
        raise SpecialistContractError(
            f"tool {grant.name!r} is no longer allowed for this specialist role"
        )
    _validate_live_read_only_spec(grant.name, live_spec)
    if live_spec.version != grant.version:
        raise SpecialistContractError(
            f"tool {grant.name!r} version changed from {grant.version!r} "
            f"to {live_spec.version!r}"
        )
    if live_spec.retry_safe != grant.retry_safe:
        raise SpecialistContractError(
            f"tool {grant.name!r} retry policy changed after grant creation"
        )
    if tool_input_schema_digest(live_spec) != grant.input_schema_digest:
        raise SpecialistContractError(
            f"tool {grant.name!r} input schema changed after grant creation"
        )


def validate_read_only_dispatch_for_role(
    role: SpecialistRole,
    grant: ReadOnlyToolGrant,
    live_spec: ToolSpecLike,
) -> None:
    """Safest dispatch entry point: re-resolve role policy as well as live metadata."""

    try:
        allowlist = SPECIALIST_TOOL_ALLOWLISTS[role]
    except KeyError as exc:
        raise SpecialistContractError(f"unknown specialist role {role!r}") from exc
    validate_read_only_dispatch(grant, live_spec, role_allowlist=allowlist)


__all__ = [
    "AnswerContract",
    "AnswerResponseType",
    "EvidenceRef",
    "MANAGER_ONLY_TOOLS",
    "MAX_SPECIALIST_TASKS",
    "MAX_RESULT_DATA_BYTES",
    "MAX_TASK_INPUT_BYTES",
    "MAX_TOOL_INPUT_SCHEMA_BYTES",
    "ReadOnlyToolGrant",
    "SPECIALIST_TOOL_ALLOWLISTS",
    "SpecialistContractError",
    "SpecialistResult",
    "SpecialistRole",
    "SpecialistStatus",
    "SpecialistTask",
    "TaskPlan",
    "ToolSpecLike",
    "UNTRUSTED_SPECIALIST_TOOLS",
    "grant_read_only_tools",
    "grant_read_only_tools_for_role",
    "tool_input_schema_digest",
    "canonical_input_schema_digest",
    "validate_read_only_dispatch",
    "validate_read_only_dispatch_for_role",
]
