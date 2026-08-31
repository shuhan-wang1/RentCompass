"""Rollout and checkpoint-state contracts for manager_v1 Phase 2."""

from __future__ import annotations

import json
from typing import get_type_hints

from uk_rent_agent.agent.architecture import manager_v1_specialists_enabled
from uk_rent_agent.agent.state import AgentState, create_initial_state


def test_effective_rollout_helper_is_architecture_bound():
    assert manager_v1_specialists_enabled(" manager_V1 ", True) is True
    assert manager_v1_specialists_enabled("manager_v1", False) is False
    assert manager_v1_specialists_enabled("fc_loop", True) is False
    assert manager_v1_specialists_enabled("legacy", True) is False


def test_specialist_ledgers_are_plain_channels_distinct_from_legacy_wave_state():
    hints = get_type_hints(AgentState, include_extras=True)

    assert hints["manager_task_plans"] is list
    assert hints["specialist_results"] is list
    assert hints["manager_task_plans"] != hints["task_results"]

    state = create_initial_state("first turn")
    state["task_plan"] = [{"id": "legacy-task"}]
    state["task_results"] = [{"id": "legacy-result"}]
    state["manager_task_plans"] = [{"root_task_id": "turn:1", "tasks": []}]
    state["specialist_results"] = [{"task_id": "specialist:1", "status": "failed"}]

    assert state["task_plan"] == [{"id": "legacy-task"}]
    assert state["manager_task_plans"][0]["root_task_id"] == "turn:1"


def test_create_initial_state_resets_json_checkpoint_ledgers_each_turn():
    previous = create_initial_state("first turn")
    previous["manager_task_plans"].append(
        {"root_task_id": "turn:1", "mode": "no_tools", "tasks": []}
    )
    previous["specialist_results"].append(
        {
            "task_id": "specialist:1",
            "role": "listings",
            "status": "failed",
            "summary": "",
            "evidence": [],
            "error": "synthetic",
        }
    )

    current = create_initial_state("second turn")

    assert current["manager_task_plans"] == []
    assert current["specialist_results"] == []
    # This mirrors the SQLite checkpoint serializer's essential boundary:
    # ledgers contain only ordinary JSON values, not contract model objects.
    encoded = json.dumps(
        {
            "manager_task_plans": previous["manager_task_plans"],
            "specialist_results": previous["specialist_results"],
        },
        sort_keys=True,
    )
    assert json.loads(encoded)["manager_task_plans"][0]["root_task_id"] == "turn:1"
