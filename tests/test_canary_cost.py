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


def test_unknown_arch_is_not_canonicalised_and_holds():
    records = [
        _record("MANAGER_V1", "rollout-1"),
        _record("manager_v1", "rollout-1"),
    ]

    report = cc.build_cost_report(records, _prices())
    groups = _groups(report)

    assert report["exit_code"] == 2
    assert ("MANAGER_V1", "rollout-1") in groups
    assert ("manager_v1", "rollout-1") in groups
    assert "unknown agent_arch" in report["error"]


def test_records_without_a_rollout_id_are_costed_not_held_forever():
    """rollout_id only exists on edge-labelled traffic.

    Every historical turn and every direct-pool smoke turn has none, so treating
    its absence as a malformed identity made `decision=HOLD, total_cost=None` the
    permanent answer for the entire cost history — the script could never cost the
    records it was written to cost. Absence is now a NOTE and a named bucket.
    """
    report = cc.build_cost_report(
        [_record("manager_v1", None), _record("manager_v1", None)], _prices()
    )
    groups = _groups(report)

    assert report["exit_code"] == 0, report.get("error")
    assert report["decision"] != "HOLD"
    assert report["total_cost"] is not None
    assert ("manager_v1", cc.UNLABELLED_ROLLOUT) in groups
    unlabelled = groups[("manager_v1", cc.UNLABELLED_ROLLOUT)]
    assert unlabelled["total_cost"] is not None
    assert unlabelled["records"] == 2
    # Reported, but never as a blocking issue.
    assert any("no rollout_id" in note for note in report["notes"])
    assert not any("rollout_id" in issue for issue in (report.get("issues") or []))


def test_a_malformed_rollout_id_still_holds():
    """Absent and unparseable are different facts. Only one of them is a defect."""
    report = cc.build_cost_report([_record("manager_v1", "not a rollout id!")],
                                  _prices())
    groups = _groups(report)

    assert report["exit_code"] == 2
    assert ("manager_v1", cc.INVALID_DIMENSION) in groups
    assert "invalid rollout_id" in report["error"]


def test_labelled_and_unlabelled_records_are_never_folded_together():
    report = cc.build_cost_report(
        [_record("manager_v1", "rollout-1"), _record("manager_v1", None)],
        _prices(),
    )
    groups = _groups(report)

    assert ("manager_v1", "rollout-1") in groups
    assert ("manager_v1", cc.UNLABELLED_ROLLOUT) in groups
    assert groups[("manager_v1", "rollout-1")]["records"] == 1
    assert groups[("manager_v1", cc.UNLABELLED_ROLLOUT)]["records"] == 1


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


# --------------------------------------------------------------------------- #
# An unlabelled EDGE record is a defect, not history.                          #
# --------------------------------------------------------------------------- #

def _edge(rollout_id: str | None, **usage_kwargs) -> dict:
    record = _record("manager_v1", rollout_id)
    record["traffic_source"] = "edge"
    if usage_kwargs:
        record["llm_usage"] = _usage(**usage_kwargs)
    return record


def test_an_edge_record_without_a_rollout_id_blocks_instead_of_joining_history():
    """The trusted edge is what stamps `rollout_id`; `canary_report` already treats
    a labelled-edge record without one as a contract violation. Here the same record
    was silently folded into `<none>` with a note asserting it was "pre-rollout or
    direct-traffic" — a provenance nobody checked.

    The consequence, with one mislabelled 999k-token edge turn among 50 historical
    direct turns: the report printed `decision=PROCEED`, `r-1: $0.00076`, and hid
    this rollout's real spend inside `<none>: $0.399`.
    """
    records = [
        _edge("rollout-1"),
        _edge(None, input_tokens=999_000),
        *[_record("manager_v1", None) for _ in range(3)],
    ]
    report = cc.build_cost_report(records, _prices())
    groups = _groups(report)

    assert ("manager_v1", cc.UNLABELLED_EDGE_ROLLOUT) in groups
    unlabelled_edge = groups[("manager_v1", cc.UNLABELLED_EDGE_ROLLOUT)]
    assert unlabelled_edge["records"] == 1
    assert unlabelled_edge["decision"] == "HOLD"
    assert any("edge" in issue for issue in unlabelled_edge["issues"])

    # The historical bucket now holds ONLY the direct records...
    assert groups[("manager_v1", cc.UNLABELLED_ROLLOUT)]["records"] == 3
    # ...and the mislabelled turn's spend is not inside it.
    assert (groups[("manager_v1", cc.UNLABELLED_ROLLOUT)]["usage"]["model-a"]
            ["input_tokens"] == 3000)
    assert (groups[("manager_v1", cc.UNLABELLED_EDGE_ROLLOUT)]["usage"]["model-a"]
            ["input_tokens"] == 999_000)

    assert report["decision"] == "HOLD"
    assert report["exit_code"] == cc.EXIT_HOLD


def test_direct_and_unlabelled_traffic_is_still_costed_not_refused():
    """The permanent-HOLD defect this bucket was created to fix must stay fixed:
    every historical turn and every direct :5001/:5002 smoke turn has no
    `rollout_id`, and refusing to cost them made the cost history uncostable."""
    report = cc.build_cost_report(
        [_record("manager_v1", None), {**_record("manager_v1", None),
                                       "traffic_source": "direct"}],
        _prices(),
    )
    group = _groups(report)[("manager_v1", cc.UNLABELLED_ROLLOUT)]

    assert report["decision"] == "PROCEED"
    assert group["total_cost"] is not None
    assert group["issues"] == []
    assert group["notes"], "reported, not silently costed"


def test_the_unlabelled_note_describes_rather_than_asserts_provenance():
    report = cc.build_cost_report([_record("manager_v1", None)], _prices())
    note = " ".join(_groups(report)[("manager_v1", cc.UNLABELLED_ROLLOUT)]["notes"])

    assert "no rollout_id on these records" in note
    assert "pre-rollout" not in note, (
        "the script cannot know these predate the rollout; it only knows the field "
        "is absent and the traffic is not labelled edge")


def test_a_malformed_rollout_id_still_holds_on_either_traffic_source():
    """Absent is history; MALFORMED is a defect. Collapsing the two is what put
    `decision=HOLD, total_cost=None` on the entire cost history in the first place."""
    for source in ("direct", "edge"):
        record = _record("manager_v1", "rollout id with spaces")
        record["traffic_source"] = source
        report = cc.build_cost_report([record], _prices())
        groups = _groups(report)
        assert ("manager_v1", cc.INVALID_DIMENSION) in groups, source
        assert report["exit_code"] == cc.EXIT_HOLD, source
