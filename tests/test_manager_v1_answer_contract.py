"""Phase 3 / deliverable 2 — the AnswerContract at the answer boundary.

``format_output_fc`` is the last node before the response leaves the graph, so it is the
only place that can record what actually shipped: the final text, the response type after
every card formatter and the memory-contract composer have run, the specialist tasks whose
evidence supports it, and one limitation line per task that produced none.

The contract is an OBSERVABILITY artefact with a hard rule attached: it may never cost the
user a turn.  A contract that fails validation is stored as an explicit
``{"valid": false, ...}`` marker and the answer ships unchanged.
"""

from __future__ import annotations

import json

import pytest

import core.specialist_runtime as specialist_runtime
from core.agent_loop import build_fc_nodes
from core.tool_system import ToolRegistry
from uk_rent_agent.agent.specialist_contracts import AnswerContract
from uk_rent_agent.agent.state import create_initial_state
from uk_rent_agent.tools.idempotency import IdempotencyStore


ROOT_TASK_ID = "manager:0123456789abcdef01234567"
LISTINGS_TASK_ID = "plan:0123456789abcdef01234567/listings"
MOBILITY_TASK_ID = "plan:0123456789abcdef01234567/mobility"


def _nodes(tmp_path, *, specialist_dispatch=True):
    registry = ToolRegistry(IdempotencyStore(tmp_path / "idempotency.sqlite3"))
    return build_fc_nodes(registry, specialist_dispatch=specialist_dispatch)


def _plan():
    return {
        "schema_version": "1",
        "plan_id": "plan:0123456789abcdef01234567",
        "root_task_id": ROOT_TASK_ID,
        "created_by": "manager",
        "no_tools": False,
        "tasks": [
            {
                "schema_version": "1",
                "task_id": MOBILITY_TASK_ID,
                "parent_task_id": ROOT_TASK_ID,
                "role": "mobility",
                "objective": "Collect manager-requested mobility evidence",
                "tools": [
                    {
                        "schema_version": "1",
                        "name": "calculate_commute",
                        "version": "1",
                        "side_effect": "none",
                        "terminal": False,
                        "retry_safe": True,
                        "input_schema_digest": f"sha256:{'0' * 64}",
                    }
                ],
                "depends_on": [],
                "inputs": {},
            }
        ],
    }


def _results():
    return [
        {
            "schema_version": "1",
            "task_id": LISTINGS_TASK_ID,
            "parent_task_id": ROOT_TASK_ID,
            "role": "listings",
            "status": "succeeded",
            "summary": "1 of 1 specialist calls returned evidence",
            "data": {},
            "evidence": [
                {
                    "schema_version": "1",
                    "evidence_id": "evidence:0123456789abcdef01234567",
                    "task_id": LISTINGS_TASK_ID,
                    "tool_name": "search_properties",
                    "artifact_id": "artifact:0123456789abcdef01234567",
                    "selector": None,
                    "claim": "search_properties returned manager-visible evidence",
                    "source_uri": None,
                    "tainted": True,
                }
            ],
            "error": None,
            "duration_ms": 12.5,
        },
        {
            "schema_version": "1",
            "task_id": MOBILITY_TASK_ID,
            "parent_task_id": ROOT_TASK_ID,
            "role": "mobility",
            "status": "failed",
            "summary": "",
            "data": {},
            "evidence": [],
            "error": "specialist task produced no reliable evidence",
            "duration_ms": 3.0,
        },
    ]


def _state(*, response_type="answer", final_response="Three listings in Camden.",
           with_specialists=True):
    state = create_initial_state(
        "find me a room in camden",
        extracted_context={"current_message": "find me a room in camden",
                           "reply_language": "en"},
        request_id="request-1",
    )
    state["final_response"] = final_response
    state["response_type"] = response_type
    if with_specialists:
        state["manager_task_plans"] = [_plan()]
        state["specialist_results"] = _results()
    return state


def test_answer_contract_records_the_shipped_answer_and_is_json_plain(tmp_path):
    out = _nodes(tmp_path)["format_output_fc"](_state())
    contract = out["answer_contract"]

    assert json.loads(json.dumps(contract)) == contract
    assert AnswerContract.model_validate(contract).owner == "manager"
    assert contract["root_task_id"] == ROOT_TASK_ID
    assert contract["response_type"] == "answer"
    assert contract["final_response"] == out["final_response"]
    assert contract["used_task_ids"] == [LISTINGS_TASK_ID]
    assert [ref["tool_name"] for ref in contract["evidence"]] == ["search_properties"]
    assert contract["limitations"] == [
        "mobility: calculate_commute evidence unavailable (tool error)"
    ]


def test_the_fc_path_writes_no_answer_contract(tmp_path):
    out = _nodes(tmp_path, specialist_dispatch=False)["format_output_fc"](_state())

    assert "answer_contract" not in out


def test_a_turn_with_no_specialist_plan_still_gets_a_contract(tmp_path):
    out = _nodes(tmp_path)["format_output_fc"](_state(with_specialists=False))
    contract = out["answer_contract"]

    assert contract["used_task_ids"] == []
    assert contract["evidence"] == []
    assert contract["limitations"] == []
    # No plan root exists, so the contract falls back to the opaque turn root.
    assert contract["root_task_id"].startswith("turn:")


@pytest.mark.parametrize(
    "state_response_type,expected",
    [
        ("answer", "answer"),
        ("search", "answer"),
        ("clarification", "clarification"),
        ("error", "error"),
    ],
)
def test_response_type_maps_onto_the_contract_vocabulary(
    tmp_path, state_response_type, expected
):
    out = _nodes(tmp_path)["format_output_fc"](
        _state(response_type=state_response_type)
    )

    assert out["response_type"] == state_response_type
    assert out["answer_contract"]["response_type"] == expected


def test_an_invalid_contract_never_costs_the_user_the_turn(tmp_path, monkeypatch):
    def explode(**kwargs):
        raise specialist_runtime.SpecialistDispatchError("answer_contract_exploded")

    monkeypatch.setattr(specialist_runtime, "build_answer_contract", explode)
    out = _nodes(tmp_path)["format_output_fc"](_state())

    assert out["final_response"] == "Three listings in Camden."
    assert out["response_type"] == "answer"
    assert out["answer_contract"] == {
        "valid": False,
        "error_code": "answer_contract_exploded",
        # The limitations survive the failure: they are the half the response layer wants.
        "limitations": ["mobility: calculate_commute evidence unavailable (tool error)"],
    }


def test_an_empty_answer_is_recorded_as_an_invalid_contract(tmp_path):
    out = _nodes(tmp_path)["format_output_fc"](_state(final_response=""))

    assert out["answer_contract"]["valid"] is False
    assert out["answer_contract"]["error_code"] == "answer_contract_invalid"


def test_the_contract_channel_is_reset_between_turns():
    assert create_initial_state("hello")["answer_contract"] == {}
