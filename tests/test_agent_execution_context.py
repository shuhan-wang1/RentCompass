from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest

from core import turn_observations as tobs
from core.canary_telemetry import aggregate_llm_usage, build_canary_turn_record
from evaluation import run_benchmark as rb
from evaluation.metrics import collector
from uk_rent_agent.observability import (
    JsonFormatter,
    agent_execution_context,
    current_agent_context,
)


_TRACE_KEYS = {"agent_role", "task_id", "parent_task_id"}


@pytest.fixture(autouse=True)
def _closed_turn_window():
    tobs.end_turn()
    yield
    tobs.end_turn()


def _llm_result(*, input_tokens: int = 10, output_tokens: int = 2):
    message = type(
        "Message",
        (),
        {
            "usage_metadata": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "response_metadata": {"model_name": "model-response"},
        },
    )()
    generation = type(
        "Generation", (), {"message": message, "generation_info": None}
    )()
    return type(
        "LLMResult", (), {"generations": [[generation]], "llm_output": None}
    )()


class _Provider400(Exception):
    status_code = 400


class _ToolResult:
    success = True
    error = None
    data = {"ok": True}
    execution_time_ms = 3.5


def test_context_is_additive_nested_and_restored():
    assert current_agent_context() == {}

    with agent_execution_context(agent_role="manager", task_id="root-1"):
        assert current_agent_context() == {
            "agent_role": "manager",
            "task_id": "root-1",
        }
        with agent_execution_context(
            agent_role="listings",
            task_id="task-1",
            parent_task_id="root-1",
        ):
            assert current_agent_context() == {
                "agent_role": "listings",
                "task_id": "task-1",
                "parent_task_id": "root-1",
            }
        assert current_agent_context() == {
            "agent_role": "manager",
            "task_id": "root-1",
        }

    assert current_agent_context() == {}


def test_sibling_async_tasks_do_not_leak_context():
    async def _worker(role: str, task_id: str):
        with agent_execution_context(
            agent_role=role, task_id=task_id, parent_task_id="root"
        ):
            await asyncio.sleep(0)
            return current_agent_context()

    async def _run_workers():
        return await asyncio.gather(
            _worker("listings", "list-1"),
            _worker("mobility", "move-1"),
        )

    left, right = asyncio.run(_run_workers())
    assert left == {
        "agent_role": "listings",
        "task_id": "list-1",
        "parent_task_id": "root",
    }
    assert right == {
        "agent_role": "mobility",
        "task_id": "move-1",
        "parent_task_id": "root",
    }
    assert current_agent_context() == {}


def test_json_formatter_omits_unset_context_and_emits_active_context():
    formatter = JsonFormatter()
    plain_record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "plain", (), None
    )
    plain = json.loads(formatter.format(plain_record))
    assert _TRACE_KEYS.isdisjoint(plain)

    traced_record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "traced", (), None
    )
    with agent_execution_context(
        agent_role="area_evidence",
        task_id="area-1",
        parent_task_id="root",
    ):
        traced = json.loads(formatter.format(traced_record))
    assert {key: traced[key] for key in _TRACE_KEYS} == {
        "agent_role": "area_evidence",
        "task_id": "area-1",
        "parent_task_id": "root",
    }


def test_eval_collector_tags_every_event_and_leaves_unscoped_events_unchanged(tmp_path):
    path = tmp_path / "events.jsonl"
    with collector.capture_run("run-1", log_path=str(path)):
        with agent_execution_context(
            agent_role="listings", task_id="list-1", parent_task_id="root"
        ):
            collector.record_llm_call(provider="deepseek", model="m")
            collector.record_tool_call("search_properties", _ToolResult(), {"area": "E1"})
            collector.record_node("execute_tools", 4.0)
            collector.record_tool_budget_timeout(
                tool="search_properties",
                phase="batch",
                budget_s=1.0,
                elapsed_ms=1000.0,
                outcome="timed_out",
            )
        collector.record_node("format_output", 1.0)

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for event in events[:4]:
        assert {key: event[key] for key in _TRACE_KEYS} == {
            "agent_role": "listings",
            "task_id": "list-1",
            "parent_task_id": "root",
        }
    assert _TRACE_KEYS.isdisjoint(events[-1])


def test_eval_llm_callback_uses_start_context_not_end_context(tmp_path):
    path = tmp_path / "events.jsonl"
    handler = collector._get_llm_callback_cls()("deepseek", "configured", "planner")
    run_id = uuid.uuid4()

    with collector.capture_run("run-callback", log_path=str(path)):
        with agent_execution_context(
            agent_role="mobility", task_id="move-1", parent_task_id="root"
        ):
            handler.on_chat_model_start({}, [], run_id=run_id)
        with agent_execution_context(agent_role="manager", task_id="root"):
            handler.on_llm_end(_llm_result(), run_id=run_id)

    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["agent_role"] == "mobility"
    assert event["task_id"] == "move-1"
    assert event["parent_task_id"] == "root"


def test_turn_observer_captures_start_context_for_success_and_error(monkeypatch):
    monkeypatch.setattr(tobs, "_observer_installed", True)
    tobs.begin_turn()
    handler = tobs._get_callback_cls()("configured")

    success_run = uuid.uuid4()
    with agent_execution_context(
        agent_role="listings", task_id="list-1", parent_task_id="root"
    ):
        handler.on_chat_model_start({}, [], run_id=success_run)
    with agent_execution_context(agent_role="manager", task_id="root"):
        handler.on_llm_end(_llm_result(), run_id=success_run)

    error_run = uuid.uuid4()
    with agent_execution_context(
        agent_role="area_evidence", task_id="area-1", parent_task_id="root"
    ):
        handler.on_chat_model_start(
            {}, [], run_id=error_run, invocation_params={"tools": [{"name": "web"}]}
        )
    with agent_execution_context(agent_role="manager", task_id="root"):
        handler.on_llm_error(_Provider400("bad request"), run_id=error_run)

    call = tobs.current()["llm_usage_calls"][0]
    assert (call["agent_role"], call["task_id"], call["parent_task_id"]) == (
        "listings",
        "list-1",
        "root",
    )
    error = tobs.current()["provider_errors"][0]
    assert (error["agent_role"], error["task_id"], error["parent_task_id"]) == (
        "area_evidence",
        "area-1",
        "root",
    )


def test_root_context_and_write_audit_are_saved_without_fabricating_unset_keys(monkeypatch):
    monkeypatch.setattr(tobs, "_observer_installed", True)
    tobs.begin_turn()
    assert tobs.note_root_agent_context(agent_role="manager", task_id="root") is True

    with agent_execution_context(
        agent_role="listings", task_id="list-1", parent_task_id="root"
    ):
        assert tobs.note_write_decision(
            tool="remember",
            decision="denied_forbidden",
            context_tainted=False,
            user_authorized=False,
            audit_key="write-1",
        )

    assert tobs.snapshot()["root_agent_context"] == {
        "agent_role": "manager",
        "task_id": "root",
    }
    assert tobs.note_root_agent_context(
        agent_role="listings", task_id="spoofed-root"
    ) is False
    assert tobs.snapshot()["root_agent_context"]["agent_role"] == "manager"
    audit = tobs.current()["write_audit"]["write-1"]
    assert {key: audit[key] for key in _TRACE_KEYS} == {
        "agent_role": "listings",
        "task_id": "list-1",
        "parent_task_id": "root",
    }

    tobs.begin_turn()
    assert "root_agent_context" not in tobs.snapshot()
    assert tobs.note_root_agent_context(
        agent_role="manager", task_id="root", parent_task_id=None
    )
    assert "parent_task_id" not in tobs.snapshot()["root_agent_context"]


def test_usage_roles_are_optional_and_do_not_change_model_totals():
    plain = aggregate_llm_usage(
        [{"model": "m", "input_tokens": 10, "output_tokens": 2,
          "cache_read_tokens": 3}]
    )
    assert plain == {
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 3,
        "models": {
            "m": {
                "calls": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 3,
            }
        },
    }

    traced = aggregate_llm_usage(
        [
            {"model": "m", "input_tokens": 10, "output_tokens": 2,
             "cache_read_tokens": 3, "agent_role": "manager"},
            {"model": "m", "input_tokens": 7, "output_tokens": 1,
             "cache_read_tokens": 0, "agent_role": "listings"},
        ]
    )
    assert traced["models"]["m"]["calls"] == 2
    assert traced["roles"]["manager"] == {
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 3,
        "models": {
            "m": {
                "calls": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 3,
            }
        },
    }
    assert traced["roles"]["listings"]["input_tokens"] == 7
    assert traced["roles"]["listings"]["models"]["m"]["calls"] == 1


def test_canary_v2_optionally_projects_root_and_write_context(monkeypatch):
    monkeypatch.setenv("CANARY_USER_HASH_KEY", "test-key")
    record = build_canary_turn_record(
        endpoint="alex",
        agent_arch="fc_loop",
        candidate_sha="sha",
        strict=True,
        request_id="request",
        conversation_id="conversation",
        user_id="user",
        http_status=200,
        turn_outcome="ok",
        turn_latency_ms=1.0,
        signals={
            "soft_wrapped": False,
            "partial": False,
            "tool_budget_timeout": False,
            "security": {
                "denied_write_count": 1,
                "tainted_write_executed_count": 0,
                "forbidden_write_executed_count": 0,
                "write_audit": [
                    {
                        "tool": "remember",
                        "security_decision": "denied_forbidden",
                        "context_tainted": False,
                        "user_authorized": False,
                        "dispatch_started": False,
                        "gate_bypassed": False,
                        "agent_role": "listings",
                        "task_id": "list-1",
                        "parent_task_id": "root",
                    }
                ],
            },
            "dsml_blocked": 0,
            "dsml_leak": 0,
            "provider_schema_400_count": 0,
            "llm_usage_status": "no_llm_calls",
            "root_agent_context": {"agent_role": "manager", "task_id": "root"},
        },
    )

    assert record["telemetry_schema_version"] == 2
    assert record["agent_role"] == "manager"
    assert record["task_id"] == "root"
    assert "parent_task_id" not in record
    audit = record["security"]["write_audit"][0]
    assert {key: audit[key] for key in _TRACE_KEYS} == {
        "agent_role": "listings",
        "task_id": "list-1",
        "parent_task_id": "root",
    }


def test_benchmark_node_projection_preserves_context_and_legacy_shape():
    old = rb.build_node_spans(
        [{"type": "node_span", "node": "agent", "latency_ms": 2.0,
          "ts_monotonic": 1.0}]
    )
    assert old == [{"node": "agent", "ms": 2.0, "seq": 0}]

    traced = rb.build_node_spans(
        [{"type": "node_span", "node": "execute_tools", "latency_ms": 3.0,
          "ts_monotonic": 1.0, "agent_role": "listings", "task_id": "list-1",
          "parent_task_id": "root"}]
    )
    assert traced == [
        {
            "node": "execute_tools",
            "ms": 3.0,
            "seq": 0,
            "agent_role": "listings",
            "task_id": "list-1",
            "parent_task_id": "root",
        }
    ]
