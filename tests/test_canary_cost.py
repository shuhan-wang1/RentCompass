from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_cost.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("canary_cost_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cc = _load_module()


def _usage(
    model: str = "model-a",
    *,
    calls: int = 1,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read_tokens: int = 200,
) -> dict:
    metrics = {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    }
    return {**metrics, "models": {model: dict(metrics)}}


def _record(
    arch: str = "manager_v1",
    rollout_id: str | None = "rollout-1",
    *,
    status: str | None = "complete",
    usage: object = ...,
) -> dict:
    if usage is ...:
        usage = None if status == "no_llm_calls" else _usage()
    record = {
        "event": "canary.turn",
        "agent_arch": arch,
        "llm_usage_status": status,
        "llm_usage": usage,
    }
    if rollout_id is not None:
        record["rollout_id"] = rollout_id
    return record


def _prices(*, unverified: bool = False, include_model: bool = True) -> dict:
    return {
        "price_table_version": 7,
        "currency": "USD",
        "unit": "per_1m_tokens",
        "unverified": unverified,
        "models": (
            {
                "model-a": {
                    "input": 1.0,
                    "output": 2.0,
                    "cache_read": 0.5,
                }
            }
            if include_model
            else {}
        ),
    }


def _groups(report: dict) -> dict:
    return {
        (group["agent_arch"], group["rollout_id"]): group
        for group in report["groups"]
    }


def test_groups_by_exact_arch_and_rollout_and_prices_each_group():
    records = [
        _record("manager_v1", "rollout-a"),
        _record("manager_v1", "rollout-b", status="no_llm_calls"),
        _record(
            "fc_loop",
            "rollout-a",
            usage=_usage(input_tokens=2000, output_tokens=0, cache_read_tokens=0),
        ),
    ]

    report = cc.build_cost_report(records, _prices())
    groups = _groups(report)

    assert report["ok"] is True
    assert report["decision"] == "PROCEED"
    assert report["exit_code"] == 0
    assert set(groups) == {
        ("manager_v1", "rollout-a"),
        ("manager_v1", "rollout-b"),
        ("fc_loop", "rollout-a"),
    }
    assert groups[("manager_v1", "rollout-a")]["total_cost"] == pytest.approx(
        0.0021
    )
    assert groups[("fc_loop", "rollout-a")]["total_cost"] == pytest.approx(
        0.002
    )
    no_calls = groups[("manager_v1", "rollout-b")]
    assert no_calls["total_cost"] == 0
    assert no_calls["no_llm_call_turns"] == 1
    assert no_calls["unmeasured_turns"] == 0
    assert report["total_cost"] == pytest.approx(0.0041)


def test_no_llm_calls_is_zero_cost_not_unmeasured():
    usage = cc.sum_usage([_record(status="no_llm_calls")])
    result = cc.compute_cost(usage, _prices())

    assert usage["_no_llm_call_turns"] == {"count": 1}
    assert usage["_unmeasured_turns"] == {"count": 0}
    assert result["ok"] is True
    assert result["total_cost"] == 0
    assert result["no_llm_call_turns"] == 1


@pytest.mark.parametrize("status", ["partial", "not_instrumented", None, "bogus"])
def test_any_chargeable_turn_with_incomplete_usage_holds(status):
    report = cc.build_cost_report(
        [_record(status=status, usage=None)],
        _prices(),
    )

    assert report["ok"] is False
    assert report["decision"] == "HOLD"
    assert report["exit_code"] == 2
    assert report["total_cost"] is None
    assert report["unmeasured_turns"] == 1
    assert "incomplete LLM usage" in report["error"]


def test_complete_status_requires_consistent_usage_totals():
    inconsistent = _usage()
    inconsistent["input_tokens"] += 1

    report = cc.build_cost_report(
        [_record(usage=inconsistent)],
        _prices(),
    )

    assert report["exit_code"] == 2
    assert report["total_cost"] is None
    assert "does not match models total" in report["error"]


def test_no_llm_calls_with_usage_is_ambiguous_and_holds():
    report = cc.build_cost_report(
        [_record(status="no_llm_calls", usage=_usage())],
        _prices(),
    )

    assert report["exit_code"] == 2
    assert report["unmeasured_turns"] == 1
    assert "non-null llm_usage" in report["error"]


def test_unverified_prices_hold_even_with_legacy_override_flag():
    usage = cc.sum_usage([_record()])

    ordinary = cc.compute_cost(usage, _prices(unverified=True))
    provisional = cc.compute_cost(
        usage, _prices(unverified=True), allow_unverified=True
    )

    assert ordinary["exit_code"] == provisional["exit_code"] == 2
    assert ordinary["total_cost"] is provisional["total_cost"] is None
    assert "provisional_cost" not in ordinary
    assert provisional["provisional_cost"] == pytest.approx(0.0021)
    assert "unverified" in provisional["error"]


@pytest.mark.parametrize(
    "rate_patch",
    [
        None,
        {"input": 1.0, "output": 2.0, "cache_read": None},
        {"input": 1.0, "output": -1.0, "cache_read": 0.5},
    ],
)
def test_missing_or_invalid_model_rate_holds(rate_patch):
    prices = _prices(include_model=False)
    if rate_patch is not None:
        prices["models"]["model-a"] = rate_patch

    report = cc.build_cost_report([_record()], prices)

    assert report["exit_code"] == 2
    assert report["total_cost"] is None
    assert "no verified non-negative rate" in report["error"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("price_table_version", None, "price_table_version"),
        ("currency", "", "currency"),
        ("unit", "per_token", "unit"),
    ],
)
def test_invalid_price_table_metadata_holds(field, value, message):
    prices = _prices()
    prices[field] = value

    report = cc.build_cost_report([_record()], prices)

    assert report["exit_code"] == 2
    assert report["total_cost"] is None
    assert message in report["error"]


def test_missing_rollout_and_unknown_arch_are_not_canonicalised():
    records = [
        _record("MANAGER_V1", "rollout-1"),
        _record("manager_v1", None),
    ]

    report = cc.build_cost_report(records, _prices())
    groups = _groups(report)

    assert report["exit_code"] == 2
    assert ("MANAGER_V1", "rollout-1") in groups
    assert ("manager_v1", cc.MISSING_DIMENSION) in groups
    assert "unknown agent_arch" in report["error"]
    assert "missing rollout_id" in report["error"]


def test_empty_records_and_skipped_lines_hold():
    empty = cc.build_cost_report([], _prices())
    skipped = cc.build_cost_report(
        [_record(status="no_llm_calls")],
        _prices(),
        skipped=1,
    )

    assert empty["exit_code"] == skipped["exit_code"] == 2
    assert "no canary.turn records" in empty["error"]
    assert "unparseable input" in skipped["error"]


def test_old_cli_shape_still_works_and_emits_grouped_json(tmp_path, capsys):
    telemetry = tmp_path / "canary.jsonl"
    telemetry.write_text(
        json.dumps(_record("manager_v1", "rollout-cli")) + "\n",
        encoding="utf-8",
    )
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(_prices()), encoding="utf-8")

    code = cc.main([str(telemetry), "--prices", str(prices)])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["decision"] == "PROCEED"
    assert output["groups"][0]["agent_arch"] == "manager_v1"
    assert output["groups"][0]["rollout_id"] == "rollout-cli"


def test_cli_unverified_price_and_corrupt_line_return_hold(tmp_path, capsys):
    telemetry = tmp_path / "canary.jsonl"
    telemetry.write_text(
        json.dumps(_record()) + "\nnot-json\n",
        encoding="utf-8",
    )
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(_prices(unverified=True)),
        encoding="utf-8",
    )

    code = cc.main(
        [
            str(telemetry),
            "--prices",
            str(prices),
            "--allow-unverified",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["decision"] == "HOLD"
    assert output["total_cost"] is None
    assert output["skipped_lines"] == 1
    assert "provisional_cost" in output


def test_cli_missing_input_is_a_hold_not_a_traceback(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(_prices()), encoding="utf-8")

    code = cc.main([str(missing), "--prices", str(prices)])
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["decision"] == "HOLD"
    assert "no canary.turn records" in output["error"]
