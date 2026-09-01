"""Focused offline tests for the experimental manager/specialist boundary."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from uk_rent_agent.agent.specialist_contracts import (
    AnswerContract,
    EvidenceRef,
    MAX_SPECIALIST_TASKS,
    ReadOnlyToolGrant,
    SPECIALIST_TOOL_ALLOWLISTS,
    SpecialistContractError,
    SpecialistResult,
    SpecialistTask,
    TaskPlan,
    canonical_input_schema_digest,
    grant_read_only_tools,
    grant_read_only_tools_for_role,
    validate_read_only_dispatch,
    validate_read_only_dispatch_for_role,
)


ROOT_TASK_ID = "root-1"


@dataclass(frozen=True)
class _Spec:
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


def _grant(name: str = "search_properties", *, version: str = "1") -> ReadOnlyToolGrant:
    return ReadOnlyToolGrant(
        name=name,
        version=version,
        input_schema_digest=canonical_input_schema_digest(
            {"type": "object", "properties": {}}
        ),
    )


def _task(
    task_id: str,
    *,
    parent_task_id: str = ROOT_TASK_ID,
    role: str = "listings",
    tool: str = "search_properties",
    depends_on: tuple[str, ...] = (),
) -> SpecialistTask:
    return SpecialistTask(
        task_id=task_id,
        parent_task_id=parent_task_id,
        role=role,
        objective=f"Collect evidence for {task_id}",
        tools=(_grant(tool),),
        depends_on=depends_on,
        inputs={"query": f"synthetic query for {task_id}"},
    )


def _evidence(
    *,
    evidence_id: str = "evidence-1",
    task_id: str = "task-1",
    tool_name: str = "search_properties",
    tainted: bool | None = None,
) -> EvidenceRef:
    if tainted is None:
        tainted = tool_name in {
            "search_properties",
            "get_property_details",
            "web_search",
            "search_nearby_pois",
        }
    return EvidenceRef(
        evidence_id=evidence_id,
        task_id=task_id,
        tool_name=tool_name,
        artifact_id=f"artifact-{evidence_id}",
        selector="/recommendations/0",
        claim="The tool returned one synthetic listing.",
        source_uri="fixture://synthetic-listing",
        tainted=tainted,
    )


def test_valid_dependency_plan_round_trips_and_is_frozen():
    plan = TaskPlan(
        plan_id="plan-1",
        root_task_id=ROOT_TASK_ID,
        no_tools=False,
        tasks=(
            _task("listings-1"),
            _task(
                "mobility-1",
                role="mobility",
                tool="calculate_commute",
                depends_on=("listings-1",),
            ),
        ),
    )

    assert TaskPlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.created_by == "manager"
    with pytest.raises(ValidationError, match="frozen"):
        plan.no_tools = True


def test_direct_answer_plan_is_explicit_and_has_no_specialist_hop():
    plan = TaskPlan(
        plan_id="plan-direct",
        root_task_id=ROOT_TASK_ID,
        no_tools=True,
    )

    assert plan.tasks == ()
    assert plan.no_tools is True


@pytest.mark.parametrize(
    ("no_tools", "tasks"),
    [
        (False, ()),
        (True, (_task("task-1"),)),
    ],
)
def test_no_tools_must_match_whether_the_plan_has_tasks(no_tools, tasks):
    with pytest.raises(ValidationError, match="if and only if"):
        TaskPlan(
            plan_id="plan-invalid",
            root_task_id=ROOT_TASK_ID,
            no_tools=no_tools,
            tasks=tasks,
        )


def test_plan_rejects_more_than_eight_tasks():
    tasks = tuple(_task(f"task-{index}") for index in range(MAX_SPECIALIST_TASKS + 1))

    with pytest.raises(ValidationError, match="at most 8"):
        TaskPlan(
            plan_id="plan-too-wide",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=tasks,
        )


def test_plan_rejects_duplicate_task_ids():
    with pytest.raises(ValidationError, match="ids must be unique"):
        TaskPlan(
            plan_id="plan-duplicate",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=(
                _task("task-1"),
                _task("task-1", role="area_evidence", tool="get_weather"),
            ),
        )


def test_plan_rejects_root_id_reused_as_a_specialist_task_id():
    with pytest.raises(ValidationError, match="root task id must not equal"):
        TaskPlan(
            plan_id="plan-self-parent",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=(_task(ROOT_TASK_ID),),
        )


def test_plan_rejects_unknown_dependencies():
    with pytest.raises(ValidationError, match="unknown dependencies"):
        TaskPlan(
            plan_id="plan-unknown-dependency",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=(_task("task-1", depends_on=("missing-task",)),),
        )


def test_plan_rejects_dependency_cycles_before_dispatch():
    with pytest.raises(ValidationError, match="acyclic graph"):
        TaskPlan(
            plan_id="plan-cycle",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=(
                _task("task-a", depends_on=("task-b",)),
                _task("task-b", depends_on=("task-a",)),
            ),
        )


def test_plan_rejects_a_non_root_parent_in_v1():
    with pytest.raises(ValidationError, match="must be parented by root task"):
        TaskPlan(
            plan_id="plan-nested",
            root_task_id=ROOT_TASK_ID,
            no_tools=False,
            tasks=(_task("task-1", parent_task_id="another-parent"),),
        )


@pytest.mark.parametrize(
    "depends_on",
    [
        ("task-1",),
        ("task-0", "task-0"),
    ],
)
def test_specialist_task_rejects_self_or_duplicate_dependencies(depends_on):
    expected = "itself" if depends_on == ("task-1",) else "unique"
    with pytest.raises(ValidationError, match=expected):
        _task("task-1", depends_on=depends_on)


def test_specialist_task_rejects_duplicate_grants_and_manager_owned_fields():
    grant = _grant()
    with pytest.raises(ValidationError, match="tool grants must be unique"):
        SpecialistTask(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            objective="Find listings",
            tools=(grant, grant),
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecialistTask(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            objective="Find listings",
            tools=(grant,),
            memory_context="private manager memory",
        )


@pytest.mark.parametrize("name", ["remember", "recall_memory", "ask_user"])
def test_manager_only_tools_cannot_be_read_only_grants(name):
    with pytest.raises(ValidationError, match="manager-only"):
        _grant(name)


def test_read_only_grant_literals_reject_write_and_terminal_authority():
    with pytest.raises(ValidationError):
        ReadOnlyToolGrant(
            name="save_note", side_effect="write",
            input_schema_digest=canonical_input_schema_digest({}),
        )
    with pytest.raises(ValidationError):
        ReadOnlyToolGrant(
            name="search_properties", terminal=True,
            input_schema_digest=canonical_input_schema_digest({}),
        )


def test_specialist_role_allowlists_are_deeply_immutable_and_manager_only_free():
    assert set(SPECIALIST_TOOL_ALLOWLISTS) == {"listings", "mobility", "area_evidence"}
    assert all(
        not ({"remember", "recall_memory", "ask_user"} & tools)
        for tools in SPECIALIST_TOOL_ALLOWLISTS.values()
    )
    with pytest.raises(TypeError):
        SPECIALIST_TOOL_ALLOWLISTS["listings"] = frozenset({"remember"})
    with pytest.raises(AttributeError):
        SPECIALIST_TOOL_ALLOWLISTS["listings"].add("remember")


def test_role_first_grant_entry_uses_exact_frozen_policy():
    grants = grant_read_only_tools_for_role(
        "mobility",
        ["calculate_commute"],
        live_specs=(_Spec("calculate_commute", version="3"),),
    )
    assert grants == (_grant("calculate_commute", version="3"),)

    with pytest.raises(SpecialistContractError, match="not allowed"):
        grant_read_only_tools_for_role(
            "mobility",
            ["search_properties"],
            live_specs=(_Spec("search_properties"),),
        )


def test_grant_read_only_tools_uses_live_metadata_and_preserves_order():
    specs = (
        _Spec("calculate_commute", version="3", retry_safe=False),
        _Spec("search_properties", version="2"),
    )

    grants = grant_read_only_tools(
        ["search_properties", "calculate_commute"],
        live_specs=specs,
        role_allowlist=frozenset({"search_properties", "calculate_commute"}),
    )

    assert [grant.name for grant in grants] == ["search_properties", "calculate_commute"]
    assert [grant.version for grant in grants] == ["2", "3"]
    assert grants[1].retry_safe is False


@pytest.mark.parametrize(
    ("requested", "specs", "allowlist", "message"),
    [
        (["save_note"], (_Spec("save_note", side_effect="write"),), {"save_note"}, "not read-only"),
        (["ask_user"], (_Spec("ask_user", terminal=True),), {"ask_user"}, "manager-only"),
        (["recall_memory"], (_Spec("recall_memory"),), {"recall_memory"}, "manager-only"),
        (["missing"], (), {"missing"}, "not present"),
        (["web_search"], (_Spec("web_search"),), {"get_weather"}, "not allowed"),
        (["web_search", "web_search"], (_Spec("web_search"),), {"web_search"}, "unique"),
    ],
)
def test_grant_read_only_tools_fails_closed(requested, specs, allowlist, message):
    with pytest.raises(SpecialistContractError, match=message):
        grant_read_only_tools(
            requested,
            live_specs=specs,
            role_allowlist=frozenset(allowlist),
        )


def test_grant_read_only_tools_rejects_duplicate_live_specs():
    with pytest.raises(SpecialistContractError, match="duplicate live tool spec"):
        grant_read_only_tools(
            ["web_search"],
            live_specs=(_Spec("web_search"), _Spec("web_search")),
            role_allowlist=frozenset({"web_search"}),
        )


@pytest.mark.parametrize(
    ("live_spec", "allowlist", "message"),
    [
        (_Spec("get_weather"), {"web_search"}, "name mismatch"),
        (_Spec("web_search", side_effect="write"), {"web_search"}, "not read-only"),
        (_Spec("web_search", terminal=True), {"web_search"}, "terminal"),
        (_Spec("web_search", version="2"), {"web_search"}, "version changed"),
        (_Spec("web_search"), {"get_weather"}, "no longer allowed"),
    ],
)
def test_dispatch_revalidates_name_policy_and_live_metadata(live_spec, allowlist, message):
    grant = _grant("web_search")

    with pytest.raises(SpecialistContractError, match=message):
        validate_read_only_dispatch(
            grant,
            live_spec,
            role_allowlist=frozenset(allowlist),
        )


def test_role_first_dispatch_reloads_exact_policy_and_live_version():
    grant = _grant("calculate_commute", version="4")
    assert validate_read_only_dispatch_for_role(
        "mobility",
        grant,
        _Spec("calculate_commute", version="4"),
    ) is None

    with pytest.raises(SpecialistContractError, match="no longer allowed"):
        validate_read_only_dispatch_for_role(
            "listings",
            grant,
            _Spec("calculate_commute", version="4"),
        )


def test_dispatch_accepts_an_unchanged_live_read_only_spec():
    grant = _grant("calculate_commute", version="4")

    assert validate_read_only_dispatch(
        grant,
        _Spec("calculate_commute", version="4"),
        role_allowlist=frozenset({"calculate_commute"}),
    ) is None


@pytest.mark.parametrize("tool_name", ["web_search", "search_nearby_pois"])
def test_external_evidence_must_remain_tainted_for_manager_review(tool_name):
    with pytest.raises(ValidationError, match="must remain tainted"):
        _evidence(tool_name=tool_name, tainted=False)

    assert _evidence(tool_name=tool_name, tainted=True).tainted is True


def test_successful_specialist_result_is_evidence_bound_and_round_trips():
    result = SpecialistResult(
        task_id="task-1",
        parent_task_id=ROOT_TASK_ID,
        role="listings",
        status="succeeded",
        summary="One synthetic listing was returned.",
        data={"listing_count": 1},
        evidence=(_evidence(),),
        duration_ms=12.5,
    )

    assert SpecialistResult.model_validate_json(result.model_dump_json()) == result


def test_specialist_result_rejects_evidence_from_another_task():
    with pytest.raises(ValidationError, match="must belong to result task"):
        SpecialistResult(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            status="succeeded",
            summary="Found something.",
            evidence=(_evidence(task_id="task-2"),),
        )


def test_specialist_result_rejects_duplicate_evidence_and_final_answer_injection():
    evidence = _evidence()
    with pytest.raises(ValidationError, match="evidence ids must be unique"):
        SpecialistResult(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            status="succeeded",
            summary="Found something.",
            evidence=(evidence, evidence),
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecialistResult(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            status="failed",
            error="provider unavailable",
            final_response="A specialist must not answer the user.",
        )


@pytest.mark.parametrize(
    ("status", "summary", "evidence", "error", "message"),
    [
        ("succeeded", "Found data.", (), None, "requires evidence"),
        ("succeeded", "Found data.", (_evidence(),), "unexpected", "cannot include an error"),
        ("partial", "Partial data.", (_evidence(),), None, "incompleteness reason"),
        ("failed", "", (), None, "requires an error or reason"),
        ("skipped", "", (_evidence(),), "dependency failed", "use partial"),
    ],
)
def test_specialist_result_status_semantics(status, summary, evidence, error, message):
    with pytest.raises(ValidationError, match=message):
        SpecialistResult(
            task_id="task-1",
            parent_task_id=ROOT_TASK_ID,
            role="listings",
            status=status,
            summary=summary,
            evidence=evidence,
            error=error,
        )


def test_manager_can_answer_directly_without_specialist_evidence():
    answer = AnswerContract(
        root_task_id=ROOT_TASK_ID,
        response_type="answer",
        final_response="This is a direct manager answer.",
    )

    assert answer.owner == "manager"
    assert answer.used_task_ids == ()
    assert answer.evidence == ()


def test_answer_contract_is_manager_only():
    with pytest.raises(ValidationError):
        AnswerContract(
            owner="listings",
            root_task_id=ROOT_TASK_ID,
            response_type="answer",
            final_response="Not allowed.",
        )


def test_answer_requires_evidence_for_every_declared_specialist_source():
    with pytest.raises(ValidationError, match="requires supporting evidence"):
        AnswerContract(
            root_task_id=ROOT_TASK_ID,
            response_type="answer",
            final_response="Unsupported specialist answer.",
            used_task_ids=("task-1",),
        )

    with pytest.raises(ValidationError, match="undeclared specialist tasks"):
        AnswerContract(
            root_task_id=ROOT_TASK_ID,
            response_type="answer",
            final_response="Mismatched evidence.",
            used_task_ids=("task-2",),
            evidence=(_evidence(task_id="task-1"),),
        )


def test_answer_with_specialist_evidence_round_trips():
    answer = AnswerContract(
        root_task_id=ROOT_TASK_ID,
        response_type="answer",
        final_response="The manager synthesized one supported result.",
        used_task_ids=("task-1",),
        evidence=(_evidence(),),
        limitations=("The source was a synthetic fixture.",),
    )

    assert AnswerContract.model_validate_json(answer.model_dump_json()) == answer


def test_legacy_agent_state_task_plan_remains_a_plain_list():
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("Synthetic query")
    legacy_task = {
        "id": "legacy-1",
        "index": 0,
        "tool": "web_search",
        "params": {"query": "synthetic"},
        "depends_on": [],
    }
    state["task_plan"] = [legacy_task]

    assert state["task_plan"] == [legacy_task]
