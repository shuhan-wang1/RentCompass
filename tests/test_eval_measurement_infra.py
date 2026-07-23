"""Measurement infrastructure: identity, evidence persistence, preflight, reuse guard.

Landed as PURE evaluation infrastructure (2026-07-23 ruling) after two candidate branches
were terminated. It carries no product, case-contract or critic/filter behaviour change —
only the machinery that makes a paired A/B trustworthy:

  * three-layer identity (product / capture / evaluator) so a measurement probe can never
    masquerade as the product under test;
  * per-run grader-input persistence, so any arm can be re-scored later;
  * single-evaluator re-scoring with identity refusal, evidence-digest verification and
    run_id de-duplication;
  * a preflight that validates EVERY benchmark shard, not just the one being run;
  * refusing a non-empty output dir by default.

Each check below exists because the corresponding failure actually happened.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import results_package as rp
from evaluation import run_benchmark as rb


# ── three-layer identity ─────────────────────────────────────────────
def _manifest(tmp_path, **env):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "node_span"}\n', encoding="utf-8")
    return rp.build_manifest(
        argv=["python", "-m", "evaluation.run_benchmark"], arch="fc_loop",
        config="routed_models", timestamp="2026-07-23T00:00:00",
        case_file=case_file, events_log=events, mode="live",
        git_commit=lambda: "capture1", git_dirty=lambda: False)


def test_capture_tree_does_not_masquerade_as_the_product(tmp_path, monkeypatch):
    """The whole point: a probe commit on top of a baseline must report the BASELINE as
    the product and itself as the capture tree."""
    monkeypatch.setenv("PRODUCT_SHA", "baseline0")
    m = _manifest(tmp_path)
    assert m["product_sha"] == "baseline0"
    assert m["capture_sha"] == "capture1"
    assert m["capture_is_product"] is False


def test_product_defaults_to_the_running_tree_when_not_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    m = _manifest(tmp_path)
    assert m["product_sha"] == "capture1" and m["capture_sha"] == "capture1"
    assert m["capture_is_product"] is True


def test_evaluator_is_null_until_a_rescore_stamps_it(tmp_path, monkeypatch):
    """A tree's own verdicts are not the gate, so it must not claim to be the evaluator."""
    monkeypatch.delenv("EVALUATOR_SHA", raising=False)
    assert _manifest(tmp_path)["evaluator_sha"] is None


def test_manifest_pins_grader_and_case_contract_digests(tmp_path):
    m = _manifest(tmp_path)
    assert m["grader_sha256"] and m["case_contract_sha256"]


# ── preflight: EVERY shard, not just the one being run ───────────────
def test_preflight_validates_every_shard_in_the_repo():
    """The failure this prevents: a constraint added to cases.jsonl but not to
    schema.json survived two green guard runs, because the guard uses a different
    shard — while the Base98 contract was unloadable the whole time."""
    assert rb.validate_all_shards() == []


def test_preflight_reports_a_bad_shard(tmp_path):
    (tmp_path / "cases_broken.jsonl").write_text(
        json.dumps({"case_id": "X1", "expected_constraints": [{"type": "not_a_real_type"}]}) + "\n",
        encoding="utf-8")
    problems = rb.validate_all_shards(tmp_path)
    assert problems and "cases_broken.jsonl" in problems[0]


def test_preflight_flags_an_empty_benchmark_dir(tmp_path):
    assert rb.validate_all_shards(tmp_path) == [f"no benchmark shards found in {tmp_path}"]


# ── output-dir reuse guard ───────────────────────────────────────────
def test_fresh_or_missing_out_dir_is_allowed(tmp_path):
    rb.guard_output_dir(tmp_path / "nope")          # missing
    (tmp_path / "empty").mkdir()
    rb.guard_output_dir(tmp_path / "empty")          # present but empty


def test_non_empty_out_dir_is_refused(tmp_path):
    (tmp_path / "grader_input.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rb.guard_output_dir(tmp_path)
    assert "non-empty output dir" in str(exc.value)


def test_reuse_can_be_opted_into_explicitly(tmp_path, capsys):
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    rb.guard_output_dir(tmp_path, allow_reuse=True)
    assert "APPENDS" in capsys.readouterr().out


# ── the re-scorer's refusals ─────────────────────────────────────────
def _run_dir(tmp_path, name, manifest, records):
    d = tmp_path / name
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if records is not None:
        (d / "grader_input.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return d


def _rec(case_id="A1", run_id="A1#r1", passed=True):
    import hashlib
    ev = [{"tool": "search_properties", "success": True, "error": None, "data": {"x": 1}}]
    blob = json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str)
    return {"run_id": run_id, "case_id": case_id, "repeat": 1,
            "raw_evidence_sha256": hashlib.sha256(blob.encode()).hexdigest(),
            "evidence": ev,
            "grader_input": {"final_answer": "ok", "tools_called": ["search_properties"],
                             "tool_call_events": [], "route": None, "user_texts": [],
                             "reference_calculations": None, "error": None,
                             "reconstructed_context": None, "history_texts": []},
            "scored_passed": passed, "scored_route_matched": True}


def test_rescore_refuses_a_run_without_persisted_evidence(tmp_path):
    from evaluation import rescore
    d = _run_dir(tmp_path, "old", {"product_sha": "p", "capture_sha": "p"}, None)
    r = rescore.rescore_dir(d, {}, None)
    assert r["rescorable"] is False and "predates evidence persistence" in r["reason"]


def test_rescore_refuses_a_missing_identity(tmp_path):
    from evaluation import rescore
    d = _run_dir(tmp_path, "noident", {"case_contract_sha256": "abc"}, [_rec()])
    r = rescore.rescore_dir(d, {}, None)
    assert r["rescorable"] is False and "IDENTITY REFUSED" in r["reason"]
    assert "product_sha" in r["reason"]


def test_rescore_refuses_a_contract_mismatch(tmp_path):
    """Two arms scored against different contracts are not a paired comparison."""
    from evaluation import rescore
    d = _run_dir(tmp_path, "mismatch",
                 {"product_sha": "p", "capture_sha": "c", "case_contract_sha256": "aaa"},
                 [_rec()])
    r = rescore.rescore_dir(d, {}, None, expected_contract="bbb")
    assert r["rescorable"] is False and "did not score the same contract" in r["reason"]
