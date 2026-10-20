import asyncio

from evaluation.verdicts import benchmark_verdict
from evaluation.fault_injection.injectors import ScenarioResult
from evaluation.fault_injection.run import _aggregate
from evaluation.fault_injection.scenarios import (
    ScenarioContext, scenario_13_partial_worker_failure, scenario_14_critic_raises,
)


def summary(**updates):
    result = {
        "n_runs": 10, "n_cases_selected": 10, "repeats": 1,
        "passed": {"rate": 0.7}, "gate_passed": True, "slo_ok": True,
        "offline": True, "hard_gate": {"cases": 0},
    }
    result.update(updates)
    return result


def test_guard_success_does_not_hide_failed_scored_checks():
    verdict = benchmark_verdict(summary())
    assert verdict["mechanism_passed"] is True
    assert verdict["scored_checks_passed"] is False
    assert verdict["exit_code"] == 1
    assert verdict["quality_evidence"] == "not_measurable_offline"
    assert "not_evaluated" in verdict["release_evidence"]


def test_explicit_diagnostic_threshold_and_incomplete_runs():
    assert benchmark_verdict(summary(), min_pass_rate=0.7)["exit_code"] == 0
    assert benchmark_verdict(summary(n_runs=9), min_pass_rate=0.7)["exit_code"] == 1
    assert benchmark_verdict(summary(slo_ok=False), min_pass_rate=0.7)["exit_code"] == 1


def test_fault_gate_rejects_silent_failure_without_harness_exception():
    failure = ScenarioResult("x", "silent", "fault", fault_surfaced=False)
    report = _aggregate([failure], "test")
    assert report["harness_errors"] == 0
    assert report["gate_passed"] is False
    assert report["failures"] == [{"scenario_id": "x", "reason": "fault_not_surfaced"}]
    assert _aggregate([], "test")["gate_passed"] is False


def test_fault_scenarios_exercise_current_batch_and_final_critic(tmp_path):
    async def run():
        context = ScenarioContext(tmp_path)
        return await scenario_13_partial_worker_failure(context), await scenario_14_critic_raises(context)

    for result in asyncio.run(run()):
        assert result.task_completed_after_fault
        assert result.fault_surfaced
        assert not result.produced_ungrounded_answer_after_fault
        assert result.error is None
