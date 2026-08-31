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
import hmac
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
    """Opaque handle minted by the trusted in-process ToolRegistry.

    Everything that decides WHAT the pinned callable receives (``input_model``), WHAT is
    returned (``output_model``) and HOW MANY TIMES it runs (``max_retries`` /
    ``retry_on_error``) is captured here as well, so a post-grant mutation of the otherwise
    identical ``Tool`` object cannot reshape an already-authorised dispatch (audit K7).
    """

    provider_identity: int
    tool_name: str
    tool_identity: int
    tool: Any
    tool_callable_identity: int
    tool_callable: Callable[..., Any]
    spec_digest: str
    input_model: Any = None
    input_model_identity: int = 0
    output_model: Any = None
    output_model_identity: int = 0
    max_retries: int = 1
    retry_on_error: bool = False


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
    #: index -> stable ``SpecialistDispatchError.error_code`` for calls this batch refused
    #: to seal.  A rejected call is NOT a member of any task; it must be denied
    #: individually by the caller while every other call — including its role siblings —
    #: dispatches normally (audit K1).
    rejected: Mapping[int, str] = MappingProxyType({})

    def call(self, index: int) -> PreparedSpecialistCall | None:
        return self.calls_by_index.get(index)

    def rejection_for_index(self, index: int) -> str | None:
        """Return the stable error code for a per-call rejection, if any."""
        return self.rejected.get(index)

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
    """Canonical JSON or a stable dispatch error — never a raw encoder exception.

    The UTF-8 measurement is inside the ``try`` on purpose: a lone surrogate
    (``"Lon\\ud800don"``) survives ``json.dumps(ensure_ascii=False)`` and only fails when
    the resulting ``str`` is encoded.  With that call outside the handler the raw
    ``UnicodeEncodeError`` escaped every ``except SpecialistDispatchError`` in the runtime
    and crashed the execute_tools node (audit K10).
    """
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
        oversize = len(encoded.encode("utf-8")) > max_bytes
    except SpecialistDispatchError:
        raise
    except Exception as exc:
        # TypeError / ValueError / UnicodeError / RecursionError are the expected
        # families; anything a hostile ``__eq__``/``__hash__``/``default`` raises is the
        # same class of defect and must fail closed with the same stable code.
        raise SpecialistDispatchError(f"{label}_not_finite_json") from exc
    if encoded != round_trip:
        raise SpecialistDispatchError(f"{label}_lossy_json")
    if oversize:
        raise SpecialistDispatchError(f"{label}_too_large")
    return encoded


_MAX_ARGS_DEPTH = 32


def _assert_json_native(value: Any, *, label: str) -> None:
    """Reject anything a JSON round trip would silently COERCE rather than lose.

    ``_canonical_json``'s ``encoded != round_trip`` guard cannot see a coercion that is
    idempotent: ``{1: "a"}`` encodes to ``{"1":"a"}`` and re-encodes identically, and a
    ``tuple`` becomes a ``list`` the same way.  The specialist boundary promises the tool
    receives the manager's exact arguments, so the sealed snapshot must contain only
    JSON-native values with ``str`` keys (audit K-seal / F5).
    """
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_ARGS_DEPTH:
            raise SpecialistDispatchError(f"{label}_too_deep")
        if node is None or isinstance(node, (str, bool)):
            continue
        if isinstance(node, int):
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                raise SpecialistDispatchError(f"{label}_not_finite_json")
            continue
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str) or isinstance(key, bool):
                    raise SpecialistDispatchError(f"{label}_not_json_native")
                stack.append((item, depth + 1))
            continue
        if isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))
            continue
        # tuple, set, bytes, datetime, pydantic model, numpy scalar, ...
        raise SpecialistDispatchError(f"{label}_not_json_native")


_SAFE_TURN_ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def safe_turn_root_id(request_id: Any) -> str | None:
    """``turn:<request_id>`` for a well-formed id, an opaque hash otherwise.

    ``observability.new_request_id`` accepts a client-supplied id, so the turn root can
    inherit arbitrary text (newlines, ``/node:`` lookalikes, 4 KB of it).  A root id is a
    trace label that ends up in execution contexts and log lines, so an unrecognised shape
    is hashed rather than propagated (audit K8).  Returns ``None`` when there is no id.
    """
    raw = str(request_id or "").strip()
    if not raw:
        return None
    if _SAFE_TURN_ROOT_RE.fullmatch(raw):
        return f"turn:{raw}"
    return f"turn:h:{hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:16]}"


def _checkpoint_digest_factory(run_id: Any) -> Callable[[str], str]:
    """Per-run keyed masking of the manager params digest (audit K8)."""
    key = hashlib.sha256(
        b"rentcompass-specialist:" + str(run_id or "").encode("utf-8", "surrogatepass")
    ).digest()

    def mask(digest: str) -> str:
        return hmac.new(
            key, str(digest or "").encode("utf-8", "surrogatepass"), hashlib.sha256
        ).hexdigest()[:16]

    return mask


def validation_fanout_task_id(*, plan_id: str | None, root_task_id: str) -> str:
    """Stable task id for the post-search commute fan-out (audit F5).

    These calls are real specialist work but they are NOT members of the immutable
    ``TaskPlan``: they are discovered from a search RESULT, after the plan was sealed.
    They therefore get a deterministic id derived from the plan/root rather than a task
    entry, and they produce no ``SpecialistResult``.
    """
    return _stable_id(
        "task", "commute-validation-v1", str(plan_id or ""), str(root_task_id)
    )


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = _canonical_json(parts, label="specialist_id", max_bytes=MAX_BATCH_ARGS_BYTES)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def tool_spec_security_digest(spec: ToolSpecLike) -> str:
    """Pin every security-relevant ToolSpec field, including its input schema.

    ``input_schema`` is the MODEL-VISIBLE schema (``Tool.parameters``), computed once in
    ``Tool.__init__``.  Swapping ``Tool.input_model`` afterwards therefore left this digest
    unchanged while the tool started validating — and accepting — attacker-shaped arguments
    (audit K7, PoC-confirmed).  ``input_model_ref``/``output_model_ref`` are recomputed live
    from the models themselves, and ``max_retries``/``retry_on_error`` are pinned because
    they decide how many times the pinned callable actually runs.

    ``spec`` is duck-typed: an MCP wrapper, a registry fallback adapter or a test double can
    put ANY object behind these attribute names.  Every failure mode of reading them is
    therefore mapped onto one stable ``SpecialistDispatchError`` code — a bare exception here
    would cross ``prepare_specialist_batch`` (which does not wrap this call) and crash
    ``execute_tools`` itself, which is precisely the class of failure the boundary exists to
    contain (review R1/R3).
    """
    try:
        payload = {
            # The five original fields stay RAW: a non-JSON value there already fails closed
            # in ``_canonical_json`` with a stable code, and digesting its ``repr`` instead
            # would be a weakening, not a fix.
            "name": getattr(spec, "name", None),
            "version": getattr(spec, "version", None),
            "side_effect": getattr(spec, "side_effect", None),
            "terminal": getattr(spec, "terminal", None),
            "retry_safe": getattr(spec, "retry_safe", None),
            "max_retries": _digest_scalar(getattr(spec, "max_retries", None)),
            "retry_on_error": _digest_scalar(getattr(spec, "retry_on_error", None)),
            "input_model_ref": _digest_scalar(getattr(spec, "input_model_ref", None)),
            "output_model_ref": _digest_scalar(getattr(spec, "output_model_ref", None)),
            "input_schema_digest": tool_input_schema_digest(spec),
        }
        encoded = _canonical_json(payload, label="tool_spec", max_bytes=MAX_CALL_ARGS_BYTES)
    except SpecialistDispatchError:
        raise
    except Exception as exc:
        raise SpecialistDispatchError("specialist_tool_spec_invalid") from exc
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _digest_scalar(value: Any) -> Any:
    """Keep a hostile/exotic spec attribute from breaking the digest itself.

    ``repr()`` is attacker-reachable code: a duck-typed spec whose ``__repr__`` raises used to
    propagate that exception straight out of the digest, past
    ``prepare_specialist_batch`` and past every ``except SpecialistDispatchError`` in the
    caller (review R1/R3).  A value that cannot even describe itself cannot be PINNED either
    — two such objects would digest identically — so this fails closed with a stable code
    rather than minting a grant over an unpinnable capability.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    try:
        return f"repr:{value!r}"
    except Exception as exc:
        raise SpecialistDispatchError("specialist_tool_spec_invalid") from exc


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


def specialist_eligible_role(
    tool_name: str, args: Mapping[str, Any] | None = None
) -> SpecialistRole | None:
    """THE single predicate for "this call belongs to a specialist role".

    ``_eligible`` and the caller-side denial set used to disagree: the caller only asked
    ``specialist_role_for_tool``, so a ``web_search`` carrying ``sub_queries`` was exempt on
    the happy path and denied on the failure path (audit K1/F2).  The ``sub_queries``
    exemption is gone — a model-controlled argument must never be able to steer a call OUT
    of the capability boundary and back onto unrestricted manager dispatch.

    ``args`` is accepted (and currently unused) so a future arg-sensitive rule has exactly
    one place to live; it is deliberately never a reason to LEAVE the boundary.
    """
    name = str(tool_name or "")
    role = _ROLE_BY_TOOL.get(name)
    if role is None or name in MANAGER_ONLY_TOOLS:
        return None
    return role


def _eligible(call: ReadCall) -> SpecialistRole | None:
    return specialist_eligible_role(call.tool_name, getattr(call, "args", None))


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
    args = dict(call.args)
    _assert_json_native(args, label="specialist_call_args")
    args_json = _canonical_json(
        args, label="specialist_call_args", max_bytes=MAX_CALL_ARGS_BYTES
    )
    if not isinstance(json.loads(args_json), dict):
        raise SpecialistDispatchError("specialist_call_args_invalid")
    return args_json, call_id


def seal_specialist_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the exact sealing rules of a planned call to a single ad-hoc call.

    Used by the post-search commute fan-out (audit F5), whose calls are driven by SCRAPED
    listing text and therefore need the same reserved-key, JSON-native and size checks as a
    manager-planned call even though they are not members of the immutable ``TaskPlan``.
    """
    if not isinstance(args, Mapping):
        raise SpecialistDispatchError("specialist_call_args_invalid")
    keys = tuple(args)
    if any(not isinstance(key, str) or key.startswith("_") for key in keys):
        raise SpecialistDispatchError("specialist_reserved_argument")
    if _RESERVED_INPUT_KEYS.intersection(keys):
        raise SpecialistDispatchError("specialist_reserved_argument")
    payload = dict(args)
    _assert_json_native(payload, label="specialist_call_args")
    sealed = json.loads(
        _canonical_json(
            payload, label="specialist_call_args", max_bytes=MAX_CALL_ARGS_BYTES
        )
    )
    if not isinstance(sealed, dict):
        raise SpecialistDispatchError("specialist_call_args_invalid")
    return sealed


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
    rejected: dict[int, str] = {}
    total_args = 0
    for call in call_list:
        role = _eligible(call)
        if role is None:
            continue
        try:
            args_json, call_id = _snapshot_call(call)
        except SpecialistDispatchError as exc:
            # PER-CALL failure radius (audit K1).  One hallucinated ``user_id`` on
            # ``check_safety`` used to abort the whole batch, and the caller then denied
            # every role-mapped read in the turn.  The defective call is recorded and
            # excluded here; its siblings — including siblings in the SAME role — keep
            # their grants and dispatch normally.  Only BATCH-level defects below still
            # deny the whole eligible set.
            index = call.index
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                rejected[index] = exc.error_code
            continue
        total_args += len(args_json.encode("utf-8"))
        if total_args > MAX_BATCH_ARGS_BYTES:
            raise SpecialistDispatchError("specialist_batch_args_too_large")
        buckets.setdefault(role, []).append((call, args_json, call_id))

    # Execution-context IDs can be syntactically valid while still embedding a
    # client-controlled request ID, email address, postcode, or other source
    # identifier. Checkpoint only an opaque, deterministic boundary ID.
    #
    # ``turn`` is deliberately NOT in this seed (audit K8): the root task is the TURN's
    # root, so seeding it per super-step minted eight different "roots" for one request
    # and nothing downstream could join them.  ``plan_id`` below still carries ``turn``,
    # so per-super-step uniqueness is unaffected.
    safe_root_task_id = _stable_id(
        "manager", "root-task-v1", str(run_id), str(root_task_id)
    )
    # The manager-owned params digest is an unsalted truncated hash of the tool arguments.
    # Checkpointing it verbatim turned the SQLite checkpoint into an offline oracle for a
    # user address (audit K8, PoC-confirmed).  Everything PERSISTED (plan_id, task inputs,
    # tool_call_id, artifact_id) is seeded from a per-run keyed digest instead; the raw
    # value stays in memory on PreparedSpecialistCall so _artifact_matches still binds the
    # ledger entry written by the fc artifact writer.
    checkpoint_digest = _checkpoint_digest_factory(run_id)
    plan_seed = [
        str(run_id),
        int(turn),
        str(root_task_id),
        [
            [call.index, call.tool_name, checkpoint_digest(call.params_digest), call_id]
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
                    checkpoint_digest(call.params_digest),
                    hashlib.sha256(args_json.encode("utf-8")).hexdigest(),
                ),
                "params_digest": checkpoint_digest(call.params_digest),
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
                    "artifact",
                    plan_id,
                    task_id,
                    call.index,
                    checkpoint_digest(call.params_digest),
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
        rejected=MappingProxyType(dict(rejected)),
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
    """Fallback duration derived from the ledger.

    ``elapsed_ms`` is NOT a latency measurement for every artifact shape: an abandoned
    read carries the batch-window constant and a denied/never-dispatched call carries 0
    (audit K-duration).  The caller passes measured wall clock via ``duration_ms_by_task``
    whenever it has one; this remains the fallback for tasks it never started.
    """
    values: list[float] = []
    for artifact in artifacts:
        value = artifact.get("elapsed_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return max(values, default=0.0)


def _measured_duration(
    wall_clock_ms: Any, matched: Iterable[Mapping[str, Any]]
) -> float:
    """Prefer the scheduler's measured wall clock over ledger ``elapsed_ms``."""
    if isinstance(wall_clock_ms, (int, float)) and not isinstance(wall_clock_ms, bool):
        value = float(wall_clock_ms)
        if math.isfinite(value) and value >= 0:
            return value
    return _duration(matched)


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
    *,
    duration_ms_by_task: Mapping[str, float] | None = None,
) -> tuple[SpecialistResult, ...]:
    """Derive typed results solely from manager-minted artifact references."""
    measured = duration_ms_by_task or {}
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
            duration_ms=_measured_duration(measured.get(task.task_id), matched),
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
    "safe_turn_root_id",
    "seal_specialist_args",
    "specialist_eligible_role",
    "specialist_role_for_tool",
    "tool_spec_security_digest",
    "validation_fanout_task_id",
]
