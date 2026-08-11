from __future__ import annotations

import json
from pathlib import Path

import pytest

from uk_rent_agent.evals import evidence_v7 as ev


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8")


def _ref(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": ev.sha256_file(path)}


def _build_package(tmp_path: Path, *, live_value: float = 1.0,
                   violation: str | None = None, missing_repeat: bool = False,
                   run_drift: bool = False) -> tuple[Path, Path, Path, dict, dict]:
    repo = tmp_path / "repo"
    package = repo / "package"
    package.mkdir(parents=True)
    prompt = repo / "prompt.txt"
    model_policy = repo / "model-policy.json"
    tool_policy = repo / "tool-policy.json"
    evaluator = repo / "evaluator.py"
    case_schema = repo / "case.schema.json"
    fixture_manifest = repo / "fixtures.json"
    capture_protocol = repo / "capture.md"
    for path, text in (
        (prompt, "system prompt v7\n"),
        (model_policy, "{}\n"),
        (tool_policy, "{}\n"),
        (evaluator, "# frozen evaluator\n"),
        (case_schema, "{}\n"),
        (fixture_manifest, "{}\n"),
        (capture_protocol, "fresh capture protocol\n"),
    ):
        path.write_text(text, encoding="utf-8")

    kinds = sorted(ev.REQUIRED_ZERO_TOLERANCE | {"dsml_or_tool_markup_leak"})
    case_files: dict[str, Path] = {}
    run_files: dict[str, Path] = {}
    case_rows: dict[str, list[dict]] = {}
    for track, prefix in (("deterministic_fixture", "DET"), ("live_freshness", "LIVE")):
        rows = []
        for number in range(1, 5):
            rows.append({
                "schema_version": ev.CASE_SCHEMA_VERSION,
                "case_id": f"HO7-{prefix}-{number:03d}",
                "track": track,
                "semantic_cluster": "search" if number <= 2 else "safety",
                "template_cluster": f"template-{number}",
                "metric_eligibility": ["quality"],
                "zero_tolerance_probes": kinds,
                "user_query": f"case {number}",
                "fixture": "fixture.json" if track == "deterministic_fixture" else None,
                "freshness_oracle": ({"kind": "source_revisit"}
                                     if track == "live_freshness" else None),
            })
        case_path = package / track / "cases.jsonl"
        _write_jsonl(case_path, rows)
        case_files[track], case_rows[track] = case_path, rows

    prereg = {
        "schema_version": ev.PREREG_SCHEMA_VERSION,
        "status": "frozen",
        "identity": {"product_sha": "1" * 40, "capture_sha": "2" * 40,
                     "evaluator_sha": "3" * 40},
        "arch": "fc_loop",
        "image_digest": "sha256:" + "4" * 64,
        "prompts": [{"prompt_id": "uk_rent.fc_loop.system", "version": "7.0.0",
                     **_ref(repo, prompt)}],
        "model_policy": {"policy_id": "routing", "version": "7.0.0",
                         **_ref(repo, model_policy)},
        "tool_policy": {"policy_id": "tools", "version": "7.0.0",
                        **_ref(repo, tool_policy)},
        "evaluator_files": [_ref(repo, evaluator)],
        "tracks": {
            "deterministic_fixture": {
                "data_mode": "closed_fixture", "repeats": 3,
                "case_set": _ref(repo, case_files["deterministic_fixture"]),
                "case_schema": _ref(repo, case_schema),
                "fixture_manifest": _ref(repo, fixture_manifest),
                "pool_with_other_track": False,
            },
            "live_freshness": {
                "data_mode": "live_capture", "repeats": 3,
                "case_set": _ref(repo, case_files["live_freshness"]),
                "case_schema": _ref(repo, case_schema),
                "capture_protocol": _ref(repo, capture_protocol),
                "max_capture_age_hours": 24,
                "pool_with_other_track": False,
            },
        },
        "cluster_bootstrap": {
            "method": "nested_semantic_template_percentile",
            "semantic_field": "semantic_cluster", "template_field": "template_cluster",
            "n_resamples": 2000, "seed": 17, "confidence": 0.95,
        },
        "release_floors": [
            {"track": track, "metric": "quality", "min_point": 0.9,
             "min_ci_low": 0.8, "min_cases": 4, "min_template_clusters": 4,
             "min_semantic_clusters": 2, "require_every_case_all_repeats": False}
            for track in ev.TRACKS
        ],
        "zero_tolerance": {
            kind: {"max_count": 0, "min_probe_cases": 1} for kind in kinds
        },
    }
    prereg_path = package / "PREREGISTRATION.json"
    _write_json(prereg_path, prereg)
    binding = ev.run_binding_from_prereg(prereg)
    for track in ev.TRACKS:
        runs = []
        for case in case_rows[track]:
            for repeat in range(1, 4):
                if missing_repeat and track == "live_freshness" and repeat == 3 and case is case_rows[track][0]:
                    continue
                current_binding = dict(binding)
                if run_drift and track == "deterministic_fixture" and repeat == 1 and case is case_rows[track][0]:
                    current_binding["product_sha"] = "f" * 40
                runs.append({
                    "schema_version": ev.RUN_SCHEMA_VERSION,
                    "case_id": case["case_id"], "track": track, "repeat": repeat,
                    "semantic_cluster": case["semantic_cluster"],
                    "template_cluster": case["template_cluster"],
                    "binding": current_binding,
                    "track_contract_sha256": ev.canonical_sha256(prereg["tracks"][track]),
                    "metrics": {"quality": live_value if track == "live_freshness" else 1.0},
                    "violations": ([{"kind": violation, "detail": "probe caught it"}]
                                   if violation and track == "deterministic_fixture" and
                                   repeat == 1 and case is case_rows[track][0] else []),
                    **({"capture_evidence": {
                        "captured_at": "2026-08-11T10:00:00+00:00",
                        "source_observations": [{
                            "source_id": f"source-{case['case_id']}",
                            "source_url": "https://example.invalid/listing",
                            "retrieved_at": "2026-08-11T10:00:00+00:00",
                            "verified_at": "2026-08-11T11:00:00+00:00",
                            "availability": "current",
                            "provenance": "direct_source_revisit",
                        }],
                    }} if track == "live_freshness" else {}),
                })
        run_path = package / track / "runs.jsonl"
        _write_jsonl(run_path, runs)
        run_files[track] = run_path

    input_hashes = {
        track: {"cases_sha256": ev.sha256_file(case_files[track]),
                "runs_sha256": ev.sha256_file(run_files[track])}
        for track in ev.TRACKS
    }
    report = ev.build_release_report(
        prereg,
        {track: ev.load_jsonl(case_files[track]) for track in ev.TRACKS},
        {track: ev.load_jsonl(run_files[track]) for track in ev.TRACKS},
        input_hashes=input_hashes,
    )
    report_path = package / "release_report.json"
    _write_json(report_path, report)
    manifest = {
        "schema_version": ev.MANIFEST_SCHEMA_VERSION,
        "status": "complete",
        "preregistration": {"path": "PREREGISTRATION.json",
                            "sha256": ev.sha256_file(prereg_path)},
        "binding": ev.binding_from_prereg(prereg),
        "artifacts": [
            {"role": "cases", "track": track,
             "path": case_files[track].relative_to(package).as_posix(),
             "sha256": ev.sha256_file(case_files[track])}
            for track in ev.TRACKS
        ] + [
            {"role": "runs", "track": track,
             "path": run_files[track].relative_to(package).as_posix(),
             "sha256": ev.sha256_file(run_files[track])}
            for track in ev.TRACKS
        ] + [{"role": "release_report", "track": "all", "path": "release_report.json",
              "sha256": ev.sha256_file(report_path)}],
    }
    manifest_path = package / "manifest.json"
    _write_json(manifest_path, manifest)
    return repo, package, prereg_path, prereg, manifest


def test_unbound_template_is_inspectable_but_never_release_eligible():
    root = Path(__file__).resolve().parents[1]
    path = root / "evaluation/benchmark/holdout_v7/PREREGISTRATION.template.json"
    template = ev.load_json(path)
    assert ev.validate_preregistration(template, root, require_frozen=False) == []
    errors = ev.validate_preregistration(template, root, require_frozen=True)
    assert any("not frozen" in error for error in errors)


def test_schemas_are_valid_draft_2020_12_documents():
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[1] / "evaluation/benchmark/holdout_v7"
    for path in sorted(root.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    schema = json.loads((root / "preregistration.schema.json").read_text(encoding="utf-8"))
    template = json.loads((root / "PREREGISTRATION.template.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(template)


def test_generated_package_records_match_the_published_schemas(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    repo, package, prereg_path, _, _ = _build_package(tmp_path)
    schema_root = (Path(__file__).resolve().parents[1] /
                   "evaluation/benchmark/holdout_v7")

    def validator(name: str):
        schema = json.loads((schema_root / name).read_text(encoding="utf-8"))
        return jsonschema.Draft202012Validator(schema)

    validator("preregistration.schema.json").validate(ev.load_json(prereg_path))
    validator("run_manifest.schema.json").validate(ev.load_json(package / "manifest.json"))
    validator("release_report.schema.json").validate(ev.load_json(package / "release_report.json"))
    case_validator = validator("case.schema.json")
    run_validator = validator("run_record.schema.json")
    for track in ev.TRACKS:
        for row in ev.load_jsonl(package / track / "cases.jsonl"):
            case_validator.validate(row)
        for row in ev.load_jsonl(package / track / "runs.jsonl"):
            run_validator.validate(row)


def test_complete_sealed_package_passes_and_recomputes_report(tmp_path):
    repo, package, prereg_path, _, _ = _build_package(tmp_path)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "PASS"
    assert result["exit_code"] == ev.EXIT_PASS
    assert set(result["report"]["tracks"]) == set(ev.TRACKS)


def test_declared_artifact_hash_mismatch_holds_gate(tmp_path):
    repo, package, prereg_path, _, manifest = _build_package(tmp_path)
    manifest["artifacts"][0]["sha256"] = "0" * 64
    _write_json(package / "manifest.json", manifest)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    assert result["exit_code"] == ev.EXIT_HOLD
    assert any("sha256 mismatch" in reason for reason in result["reasons"])


def test_missing_declared_artifact_holds_gate(tmp_path):
    repo, package, prereg_path, _, _ = _build_package(tmp_path)
    (package / "live_freshness/runs.jsonl").unlink()
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    assert any("does not exist" in reason for reason in result["reasons"])


def test_identity_drift_and_missing_third_repeat_are_hold_not_reduced_denominator(tmp_path):
    repo, package, prereg_path, _, _ = _build_package(
        tmp_path, run_drift=True, missing_repeat=True)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    joined = " ".join(result["report"]["integrity_errors"])
    assert "run binding differs" in joined
    assert "repeats are [1, 2], expected [1, 2, 3]" in joined


def test_live_and_fixture_tracks_are_not_pooled(tmp_path):
    repo, package, prereg_path, _, _ = _build_package(tmp_path, live_value=0.0)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "BLOCK"
    floors = {(row["track"], row["metric"]): row for row in result["report"]["release_floors"]}
    assert floors[("deterministic_fixture", "quality")]["status"] == "passed"
    assert floors[("live_freshness", "quality")]["status"] == "failed"


@pytest.mark.parametrize("kind", sorted(ev.REQUIRED_ZERO_TOLERANCE))
def test_each_required_zero_tolerance_category_blocks(tmp_path, kind):
    repo, package, prereg_path, _, _ = _build_package(tmp_path, violation=kind)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "BLOCK"
    assert result["exit_code"] == ev.EXIT_BLOCK
    assert result["report"]["zero_tolerance"][kind]["count"] == 1


def test_tampered_stored_report_holds_even_when_manifest_hash_is_updated(tmp_path):
    repo, package, prereg_path, _, manifest = _build_package(tmp_path)
    report_path = package / "release_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["decision"] = "BLOCK"
    report["exit_code"] = 3
    _write_json(report_path, report)
    manifest["artifacts"][-1]["sha256"] = ev.sha256_file(report_path)
    _write_json(package / "manifest.json", manifest)
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    assert "fresh deterministic recomputation" in result["reasons"][0]


def test_nested_bootstrap_macro_averages_semantic_and_template_clusters():
    cases = {
        "a": {"semantic_cluster": "s1", "template_cluster": "t1"},
        "b": {"semantic_cluster": "s1", "template_cluster": "t1"},
        "c": {"semantic_cluster": "s1", "template_cluster": "t2"},
        "d": {"semantic_cluster": "s2", "template_cluster": "t3"},
    }
    # s1 = mean(template t1 mean(1,1), template t2 mean(0)) = .5; s2 = 1; macro = .75.
    result = ev.nested_cluster_bootstrap(
        {"a": 1.0, "b": 1.0, "c": 0.0, "d": 1.0}, cases,
        n_resamples=2000, seed=7)
    assert result["point"] == pytest.approx(0.75)
    assert result["n_cases"] == 4
    assert result["n_template_clusters"] == 3
    assert result["n_semantic_clusters"] == 2
    assert result["n_draws"] == 2000


def test_console_gate_cli_misuse_never_returns_success():
    assert ev.cli_gate([]) == ev.EXIT_USAGE


def test_seal_manifest_hashes_real_files_and_generated_manifest_gates(tmp_path):
    repo, package, prereg_path, _, _ = _build_package(tmp_path)
    out = package / "sealed.json"
    ev.seal_manifest(
        prereg_path,
        package,
        [
            "cases:deterministic_fixture=deterministic_fixture/cases.jsonl",
            "runs:deterministic_fixture=deterministic_fixture/runs.jsonl",
            "cases:live_freshness=live_freshness/cases.jsonl",
            "runs:live_freshness=live_freshness/runs.jsonl",
            "release_report:all=release_report.json",
        ],
        out,
    )
    result = ev.gate_evidence_package(prereg_path, out, repo_root=repo,
                                      package_root=package)
    assert result["decision"] == "PASS"


def test_live_capture_outside_freshness_window_is_hold(tmp_path):
    repo, package, prereg_path, prereg, manifest = _build_package(tmp_path)
    runs_path = package / "live_freshness/runs.jsonl"
    runs = ev.load_jsonl(runs_path)
    runs[0]["capture_evidence"]["source_observations"][0]["verified_at"] = (
        "2026-08-13T10:00:01+00:00")
    _write_jsonl(runs_path, runs)
    cases = {track: ev.load_jsonl(package / track / "cases.jsonl") for track in ev.TRACKS}
    run_rows = {track: ev.load_jsonl(package / track / "runs.jsonl") for track in ev.TRACKS}
    hashes = {track: {
        "cases_sha256": ev.sha256_file(package / track / "cases.jsonl"),
        "runs_sha256": ev.sha256_file(package / track / "runs.jsonl"),
    } for track in ev.TRACKS}
    _write_json(package / "release_report.json",
                ev.build_release_report(prereg, cases, run_rows, input_hashes=hashes))
    ev.seal_manifest(
        prereg_path, package,
        ["cases:deterministic_fixture=deterministic_fixture/cases.jsonl",
         "runs:deterministic_fixture=deterministic_fixture/runs.jsonl",
         "cases:live_freshness=live_freshness/cases.jsonl",
         "runs:live_freshness=live_freshness/runs.jsonl",
         "release_report:all=release_report.json"],
        package / "manifest.json")
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    assert any("freshness window" in error
               for error in result["report"]["integrity_errors"])


def test_track_contract_hash_drift_is_hold(tmp_path):
    repo, package, prereg_path, prereg, _ = _build_package(tmp_path)
    runs_path = package / "deterministic_fixture/runs.jsonl"
    runs = ev.load_jsonl(runs_path)
    runs[0]["track_contract_sha256"] = "0" * 64
    _write_jsonl(runs_path, runs)
    cases = {track: ev.load_jsonl(package / track / "cases.jsonl") for track in ev.TRACKS}
    run_rows = {track: ev.load_jsonl(package / track / "runs.jsonl") for track in ev.TRACKS}
    hashes = {track: {
        "cases_sha256": ev.sha256_file(package / track / "cases.jsonl"),
        "runs_sha256": ev.sha256_file(package / track / "runs.jsonl"),
    } for track in ev.TRACKS}
    _write_json(package / "release_report.json",
                ev.build_release_report(prereg, cases, run_rows, input_hashes=hashes))
    ev.seal_manifest(
        prereg_path, package,
        ["cases:deterministic_fixture=deterministic_fixture/cases.jsonl",
         "runs:deterministic_fixture=deterministic_fixture/runs.jsonl",
         "cases:live_freshness=live_freshness/cases.jsonl",
         "runs:live_freshness=live_freshness/runs.jsonl",
         "release_report:all=release_report.json"],
        package / "manifest.json")
    result = ev.gate_evidence_package(prereg_path, package / "manifest.json",
                                      repo_root=repo, package_root=package)
    assert result["decision"] == "HOLD"
    assert any("track contract hash differs" in error
               for error in result["report"]["integrity_errors"])
