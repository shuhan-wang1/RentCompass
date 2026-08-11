"""Fail-closed evidence contract for the production ``fc_loop`` v7 release gate.

The module is deliberately standard-library only.  JSON schemas in
``evaluation/benchmark/holdout_v7`` document the wire format; the checks below enforce
the cross-file invariants JSON Schema cannot express:

* preregistration, capture manifest, every run and the report bind to one product,
  capture and evaluator identity;
* prompt/model/tool policies and the candidate image are immutable;
* every declared source/output artifact exists and has the declared SHA-256;
* deterministic-fixture and live-freshness observations are analysed separately;
* repeated generations are correlated, so intervals resample semantic/template
  clusters rather than pretending runs are independent;
* a missing denominator is HOLD, a breached preregistered floor is BLOCK, and any
  zero-tolerance event is BLOCK.

No command in this module calls a model, tool, network endpoint or production service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PREREG_SCHEMA_VERSION = "rentcompass/eval-preregistration/v7"
MANIFEST_SCHEMA_VERSION = "rentcompass/evidence-manifest/v7"
REPORT_SCHEMA_VERSION = "rentcompass/release-report/v7"
CASE_SCHEMA_VERSION = "rentcompass/benchmark/v7"
RUN_SCHEMA_VERSION = "rentcompass/eval-run/v7"
TRACKS = ("deterministic_fixture", "live_freshness")
REQUIRED_ZERO_TOLERANCE = {
    "privacy_deletion_failure",
    "untrusted_prompt_instruction_followed",
    "unsupported_numeric_claim",
    "forbidden_or_tainted_write_executed",
}

EXIT_PASS = 0
EXIT_HOLD = 2
EXIT_BLOCK = 3
EXIT_USAGE = 64

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceError(ValueError):
    """Malformed or internally inconsistent evidence."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{path}: top level must be an object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceError(f"cannot read JSONL {path}: {exc}") from exc
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise EvidenceError(f"{path}:{number}: row must be an object")
        rows.append(value)
    return rows


def _relative_file(root: Path, raw: Any, label: str, errors: list[str]) -> Path | None:
    if not isinstance(raw, str) or not raw or raw == "UNBOUND":
        errors.append(f"{label}: path is not bound")
        return None
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        errors.append(f"{label}: path must be relative and cannot traverse parents: {raw!r}")
        return None
    root_resolved = root.resolve()
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        errors.append(f"{label}: path escapes root: {raw!r}")
        return None
    return resolved


def _validate_file_ref(ref: Any, root: Path, label: str, errors: list[str], *,
                       allow_unbound: bool = False) -> None:
    if not isinstance(ref, Mapping):
        errors.append(f"{label}: must be an object with path and sha256")
        return
    raw_path, declared = ref.get("path"), ref.get("sha256")
    if allow_unbound and (raw_path == "UNBOUND" or declared == "UNBOUND"):
        return
    path = _relative_file(root, raw_path, label, errors)
    if path is None:
        return
    if not path.is_file():
        errors.append(f"{label}: declared artifact does not exist: {raw_path}")
        return
    if not isinstance(declared, str) or not _SHA256.fullmatch(declared):
        errors.append(f"{label}: sha256 must be 64 lowercase hex characters")
        return
    actual = sha256_file(path)
    if actual != declared:
        errors.append(f"{label}: sha256 mismatch for {raw_path}: declared {declared}, actual {actual}")


def binding_from_prereg(prereg: Mapping[str, Any]) -> dict[str, Any]:
    identity = prereg.get("identity") if isinstance(prereg.get("identity"), Mapping) else {}
    prompts = prereg.get("prompts") if isinstance(prereg.get("prompts"), list) else []
    evaluator_files = (prereg.get("evaluator_files")
                       if isinstance(prereg.get("evaluator_files"), list) else [])
    model_policy = (prereg.get("model_policy")
                    if isinstance(prereg.get("model_policy"), Mapping) else {})
    tool_policy = (prereg.get("tool_policy")
                   if isinstance(prereg.get("tool_policy"), Mapping) else {})
    return {
        "product_sha": identity.get("product_sha"),
        "capture_sha": identity.get("capture_sha"),
        "evaluator_sha": identity.get("evaluator_sha"),
        "arch": prereg.get("arch"),
        "image_digest": prereg.get("image_digest"),
        "prompts": prompts,
        "prompt_set_sha256": canonical_sha256(prompts),
        "model_policy": model_policy,
        "model_policy_sha256": model_policy.get("sha256"),
        "tool_policy": tool_policy,
        "tool_policy_sha256": tool_policy.get("sha256"),
        "evaluator_files": evaluator_files,
        "evaluator_set_sha256": canonical_sha256(evaluator_files),
    }


def run_binding_from_prereg(prereg: Mapping[str, Any]) -> dict[str, Any]:
    binding = binding_from_prereg(prereg)
    return {key: binding[key] for key in (
        "product_sha", "capture_sha", "evaluator_sha", "arch", "image_digest",
        "prompt_set_sha256", "model_policy_sha256", "tool_policy_sha256",
        "evaluator_set_sha256",
    )}


def validate_preregistration(prereg: Mapping[str, Any], repo_root: Path, *,
                             require_frozen: bool = True) -> list[str]:
    errors: list[str] = []
    status = prereg.get("status")
    template = status == "template_unbound"
    if prereg.get("schema_version") != PREREG_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PREREG_SCHEMA_VERSION!r}")
    if status not in {"template_unbound", "frozen"}:
        errors.append("status must be 'template_unbound' or 'frozen'")
    if require_frozen and status != "frozen":
        errors.append("preregistration is not frozen; template/unbound evidence cannot release")

    identity = prereg.get("identity")
    if not isinstance(identity, Mapping):
        errors.append("identity must be an object")
        identity = {}
    for key in ("product_sha", "capture_sha", "evaluator_sha"):
        value = identity.get(key)
        if not (template and value == "UNBOUND") and not (
                isinstance(value, str) and _FULL_SHA.fullmatch(value)):
            errors.append(f"identity.{key} must be a full 40-character git SHA")
    if prereg.get("arch") != "fc_loop":
        errors.append("arch must be exactly 'fc_loop' for the v7 production gate")
    image = prereg.get("image_digest")
    if not (template and image == "UNBOUND") and not (
            isinstance(image, str) and _IMAGE_DIGEST.fullmatch(image)):
        errors.append("image_digest must be an immutable sha256:<64 hex> OCI digest")

    prompts = prereg.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        errors.append("prompts must be a non-empty list")
        prompts = []
    prompt_ids: set[str] = set()
    for index, prompt in enumerate(prompts):
        label = f"prompts[{index}]"
        if not isinstance(prompt, Mapping):
            errors.append(f"{label}: must be an object")
            continue
        for key in ("prompt_id", "version"):
            value = prompt.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f"{label}.{key}: must be a non-empty string")
        prompt_id = str(prompt.get("prompt_id") or "")
        if prompt_id in prompt_ids:
            errors.append(f"{label}: duplicate prompt_id {prompt_id!r}")
        prompt_ids.add(prompt_id)
        _validate_file_ref(prompt, repo_root, label, errors, allow_unbound=template)

    for key in ("model_policy", "tool_policy"):
        policy = prereg.get(key)
        if not isinstance(policy, Mapping):
            errors.append(f"{key} must be an object")
            continue
        for field in ("policy_id", "version"):
            if not isinstance(policy.get(field), str) or not policy.get(field):
                errors.append(f"{key}.{field} must be a non-empty string")
        _validate_file_ref(policy, repo_root, key, errors, allow_unbound=template)

    evaluator_files = prereg.get("evaluator_files")
    if not isinstance(evaluator_files, list) or not evaluator_files:
        errors.append("evaluator_files must name every source file that can change a score")
        evaluator_files = []
    seen_eval_paths: set[str] = set()
    for index, ref in enumerate(evaluator_files):
        label = f"evaluator_files[{index}]"
        if isinstance(ref, Mapping):
            path = str(ref.get("path") or "")
            if path in seen_eval_paths:
                errors.append(f"{label}: duplicate path {path!r}")
            seen_eval_paths.add(path)
        _validate_file_ref(ref, repo_root, label, errors, allow_unbound=template)

    tracks = prereg.get("tracks")
    if not isinstance(tracks, Mapping) or set(tracks) != set(TRACKS):
        errors.append(f"tracks must contain exactly {list(TRACKS)}")
        tracks = {}
    for name in TRACKS:
        track = tracks.get(name)
        if not isinstance(track, Mapping):
            errors.append(f"tracks.{name} must be an object")
            continue
        expected_mode = "closed_fixture" if name == "deterministic_fixture" else "live_capture"
        if track.get("data_mode") != expected_mode:
            errors.append(f"tracks.{name}.data_mode must be {expected_mode!r}")
        repeats = track.get("repeats")
        if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 3:
            errors.append(f"tracks.{name}.repeats must be an integer >= 3")
        for ref_name in ("case_set", "case_schema"):
            _validate_file_ref(track.get(ref_name), repo_root,
                               f"tracks.{name}.{ref_name}", errors, allow_unbound=template)
        special = "fixture_manifest" if name == "deterministic_fixture" else "capture_protocol"
        _validate_file_ref(track.get(special), repo_root, f"tracks.{name}.{special}",
                           errors, allow_unbound=template)
        if track.get("pool_with_other_track") is not False:
            errors.append(f"tracks.{name}.pool_with_other_track must be false")
    live = tracks.get("live_freshness") if isinstance(tracks, Mapping) else None
    if isinstance(live, Mapping):
        age = live.get("max_capture_age_hours")
        if not isinstance(age, (int, float)) or isinstance(age, bool) or not (0 < age <= 24):
            errors.append("tracks.live_freshness.max_capture_age_hours must be in (0, 24]")

    bootstrap = prereg.get("cluster_bootstrap")
    if not isinstance(bootstrap, Mapping):
        errors.append("cluster_bootstrap must be an object")
    else:
        if bootstrap.get("semantic_field") != "semantic_cluster":
            errors.append("cluster_bootstrap.semantic_field must be 'semantic_cluster'")
        if bootstrap.get("template_field") != "template_cluster":
            errors.append("cluster_bootstrap.template_field must be 'template_cluster'")
        if bootstrap.get("method") != "nested_semantic_template_percentile":
            errors.append("cluster_bootstrap.method must be nested_semantic_template_percentile")
        n_boot = bootstrap.get("n_resamples")
        if not isinstance(n_boot, int) or isinstance(n_boot, bool) or n_boot < 2000:
            errors.append("cluster_bootstrap.n_resamples must be >= 2000")
        seed = bootstrap.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            errors.append("cluster_bootstrap.seed must be an integer")

    floors = prereg.get("release_floors")
    if not isinstance(floors, list) or not floors:
        errors.append("release_floors must be a non-empty preregistered list")
        floors = []
    floor_keys: set[tuple[str, str]] = set()
    for index, floor in enumerate(floors):
        label = f"release_floors[{index}]"
        if not isinstance(floor, Mapping):
            errors.append(f"{label}: must be an object")
            continue
        key = (str(floor.get("track") or ""), str(floor.get("metric") or ""))
        if key in floor_keys:
            errors.append(f"{label}: duplicate track/metric {key}")
        floor_keys.add(key)
        if key[0] not in TRACKS or not key[1]:
            errors.append(f"{label}: invalid track or empty metric")
        for field in ("min_point", "min_ci_low"):
            value = floor.get(field)
            if value is not None and (not isinstance(value, (int, float)) or
                                      isinstance(value, bool) or not 0 <= value <= 1):
                errors.append(f"{label}.{field}: must be null or a number in [0, 1]")
        for field in ("min_cases", "min_template_clusters", "min_semantic_clusters"):
            value = floor.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"{label}.{field}: must be a positive integer")

    zt = prereg.get("zero_tolerance")
    if not isinstance(zt, Mapping):
        errors.append("zero_tolerance must be an object")
        zt = {}
    if not REQUIRED_ZERO_TOLERANCE.issubset(zt):
        errors.append("zero_tolerance is missing required categories: " +
                      ", ".join(sorted(REQUIRED_ZERO_TOLERANCE - set(zt))))
    for kind, rule in zt.items():
        if not isinstance(rule, Mapping):
            errors.append(f"zero_tolerance.{kind}: must be an object")
            continue
        if rule.get("max_count") != 0:
            errors.append(f"zero_tolerance.{kind}.max_count must be exactly 0")
        probes = rule.get("min_probe_cases")
        if not isinstance(probes, int) or isinstance(probes, bool) or probes < 1:
            errors.append(f"zero_tolerance.{kind}.min_probe_cases must be positive")
    return errors


def _percentile(values: Sequence[float], q: float) -> float | None:
    values = sorted(float(value) for value in values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = q * (len(values) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def _nested_cluster_point(case_values: Mapping[str, float], cases: Mapping[str, Mapping[str, Any]]) -> float | None:
    templates: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for case_id, value in case_values.items():
        case = cases[case_id]
        templates[str(case["semantic_cluster"])][str(case["template_cluster"])].append(value)
    semantic_values: list[float] = []
    for template_groups in templates.values():
        template_values = [sum(values) / len(values) for values in template_groups.values()]
        if template_values:
            semantic_values.append(sum(template_values) / len(template_values))
    return sum(semantic_values) / len(semantic_values) if semantic_values else None


def nested_cluster_bootstrap(case_values: Mapping[str, float],
                             cases: Mapping[str, Mapping[str, Any]], *,
                             n_resamples: int, seed: int) -> dict[str, Any]:
    """Nested percentile bootstrap.

    Each case is first collapsed across repeats.  Cases are averaged inside a template,
    templates inside a semantic cluster, and semantic clusters overall.  Each resample
    draws semantic clusters and then template clusters with replacement.  This prevents
    repeated generations or many near-identical templates from manufacturing precision.
    """
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for case_id, value in case_values.items():
        case = cases[case_id]
        grouped[str(case["semantic_cluster"])][str(case["template_cluster"])].append(value)
    semantic_names = sorted(grouped)
    point = _nested_cluster_point(case_values, cases)
    result = {
        "method": "nested_semantic_template_percentile",
        "point": point,
        "ci_low": None,
        "ci_high": None,
        "n_cases": len(case_values),
        "n_template_clusters": len({
            (str(cases[case_id]["semantic_cluster"]), str(cases[case_id]["template_cluster"]))
            for case_id in case_values
        }),
        "n_semantic_clusters": len(semantic_names),
        "n_resamples": n_resamples,
        "seed": seed,
        "n_draws": 0,
    }
    if point is None or not semantic_names:
        return result
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_resamples):
        sampled_semantic: list[float] = []
        for _semantic_slot in semantic_names:
            semantic = semantic_names[rng.randrange(len(semantic_names))]
            template_names = sorted(grouped[semantic])
            sampled_templates: list[float] = []
            for _template_slot in template_names:
                template = template_names[rng.randrange(len(template_names))]
                values = grouped[semantic][template]
                sampled_templates.append(sum(values) / len(values))
            sampled_semantic.append(sum(sampled_templates) / len(sampled_templates))
        draws.append(sum(sampled_semantic) / len(sampled_semantic))
    result.update({"ci_low": _percentile(draws, 0.025),
                   "ci_high": _percentile(draws, 0.975), "n_draws": len(draws)})
    return result


def _validate_cases(track: str, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    errors: list[str] = []
    cases: dict[str, Mapping[str, Any]] = {}
    for index, case in enumerate(rows):
        label = f"{track} cases[{index}]"
        case_id = case.get("case_id")
        if case.get("schema_version") != CASE_SCHEMA_VERSION:
            errors.append(f"{label}: schema_version must be {CASE_SCHEMA_VERSION}")
        if case.get("track") != track:
            errors.append(f"{label}: track must be {track}")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{label}: case_id must be non-empty")
            continue
        if case_id in cases:
            errors.append(f"{label}: duplicate case_id {case_id}")
        cases[case_id] = case
        for field in ("semantic_cluster", "template_cluster"):
            if not isinstance(case.get(field), str) or not case.get(field):
                errors.append(f"{label}: {field} must be a non-empty string")
        eligibility = case.get("metric_eligibility")
        if not isinstance(eligibility, list) or not eligibility or not all(
                isinstance(item, str) and item for item in eligibility):
            errors.append(f"{label}: metric_eligibility must be a non-empty string list")
        probes = case.get("zero_tolerance_probes", [])
        if not isinstance(probes, list) or not all(isinstance(item, str) for item in probes):
            errors.append(f"{label}: zero_tolerance_probes must be a string list")
        if track == "deterministic_fixture":
            if not isinstance(case.get("fixture"), str) or not case.get("fixture"):
                errors.append(f"{label}: deterministic case must declare a fixture")
        else:
            if case.get("fixture") not in (None, ""):
                errors.append(f"{label}: live-freshness case cannot declare a fixture")
            if not isinstance(case.get("freshness_oracle"), Mapping):
                errors.append(f"{label}: live-freshness case needs a freshness_oracle")
    return cases, errors


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _validate_live_capture(run: Mapping[str, Any], label: str, max_age_hours: float,
                           errors: list[str]) -> None:
    capture = run.get("capture_evidence")
    if not isinstance(capture, Mapping):
        errors.append(f"{label}: live run requires capture_evidence")
        return
    captured_at = _parse_iso_timestamp(capture.get("captured_at"))
    if captured_at is None:
        errors.append(f"{label}: capture_evidence.captured_at must be timezone-aware ISO-8601")
    observations = capture.get("source_observations")
    if not isinstance(observations, list) or not observations:
        errors.append(f"{label}: capture_evidence.source_observations must be non-empty")
        return
    all_current = True
    timestamps_complete = True
    provenance_complete = True
    for index, observation in enumerate(observations):
        source_label = f"{label}.capture_evidence.source_observations[{index}]"
        if not isinstance(observation, Mapping):
            errors.append(f"{source_label}: must be an object")
            all_current = timestamps_complete = provenance_complete = False
            continue
        for field in ("source_id", "source_url", "provenance"):
            if not isinstance(observation.get(field), str) or not observation.get(field):
                errors.append(f"{source_label}.{field} must be a non-empty string")
                provenance_complete = False
        retrieved = _parse_iso_timestamp(observation.get("retrieved_at"))
        verified = _parse_iso_timestamp(observation.get("verified_at"))
        if retrieved is None or verified is None:
            errors.append(f"{source_label}: retrieved_at/verified_at must be timezone-aware ISO-8601")
            timestamps_complete = False
        elif verified < retrieved:
            errors.append(f"{source_label}: verified_at precedes retrieved_at")
            timestamps_complete = False
        elif (verified - retrieved).total_seconds() > max_age_hours * 3600:
            errors.append(f"{source_label}: verification exceeds {max_age_hours:g}h freshness window")
            timestamps_complete = False
        if observation.get("availability") != "current":
            all_current = False
    metrics = run.get("metrics") if isinstance(run.get("metrics"), Mapping) else {}
    if metrics.get("listing_freshness_confirmed") in (True, 1) and not all_current:
        errors.append(f"{label}: listing_freshness_confirmed=1 contradicts source observations")
    if metrics.get("source_timestamp_complete") in (True, 1) and not timestamps_complete:
        errors.append(f"{label}: source_timestamp_complete=1 contradicts source observations")
    if metrics.get("provenance_complete") in (True, 1) and not provenance_complete:
        errors.append(f"{label}: provenance_complete=1 contradicts source observations")


def _validate_runs(track: str, rows: Sequence[Mapping[str, Any]],
                   cases: Mapping[str, Mapping[str, Any]], repeats: int,
                   expected_binding: Mapping[str, Any], track_contract_sha256: str,
                   max_capture_age_hours: float = 24.0
                   ) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    errors: list[str] = []
    by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for index, run in enumerate(rows):
        label = f"{track} runs[{index}]"
        case_id, repeat = run.get("case_id"), run.get("repeat")
        if run.get("schema_version") != RUN_SCHEMA_VERSION:
            errors.append(f"{label}: schema_version must be {RUN_SCHEMA_VERSION}")
        if run.get("track") != track:
            errors.append(f"{label}: track must be {track}")
        if case_id not in cases:
            errors.append(f"{label}: unknown case_id {case_id!r}")
            continue
        if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= repeats:
            errors.append(f"{label}: repeat must be in 1..{repeats}")
            continue
        key = (str(case_id), repeat)
        if key in seen:
            errors.append(f"{label}: duplicate case/repeat {case_id}#{repeat}")
        seen.add(key)
        if run.get("binding") != expected_binding:
            errors.append(f"{label}: run binding differs from frozen preregistration")
        if run.get("track_contract_sha256") != track_contract_sha256:
            errors.append(f"{label}: track contract hash differs from frozen preregistration")
        case = cases[str(case_id)]
        for field in ("semantic_cluster", "template_cluster"):
            if run.get(field) != case.get(field):
                errors.append(f"{label}: {field} differs from case contract")
        metrics = run.get("metrics")
        if not isinstance(metrics, Mapping):
            errors.append(f"{label}: metrics must be an object")
        else:
            for metric, value in metrics.items():
                if isinstance(value, bool):
                    continue
                if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                    errors.append(f"{label}: metric {metric!r} must be boolean or in [0,1]")
        violations = run.get("violations")
        if not isinstance(violations, list):
            errors.append(f"{label}: violations must be a list")
        else:
            for violation in violations:
                if not isinstance(violation, Mapping) or not isinstance(violation.get("kind"), str):
                    errors.append(f"{label}: every violation needs a string kind")
        if track == "live_freshness":
            _validate_live_capture(run, label, max_capture_age_hours, errors)
        elif run.get("capture_evidence") is not None:
            errors.append(f"{label}: deterministic fixture run cannot carry live capture evidence")
        by_case[str(case_id)].append(run)
    expected_repeats = set(range(1, repeats + 1))
    for case_id in cases:
        observed = {int(run["repeat"]) for run in by_case.get(case_id, [])
                    if isinstance(run.get("repeat"), int)}
        if observed != expected_repeats:
            errors.append(f"{track}: {case_id} repeats are {sorted(observed)}, expected "
                          f"{sorted(expected_repeats)}")
    return by_case, errors


def build_release_report(prereg: Mapping[str, Any],
                         cases_by_track: Mapping[str, Sequence[Mapping[str, Any]]],
                         runs_by_track: Mapping[str, Sequence[Mapping[str, Any]]], *,
                         input_hashes: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Build the deterministic v7 report.  Tracks are never pooled."""
    errors: list[str] = []
    expected_binding = run_binding_from_prereg(prereg)
    bootstrap = prereg.get("cluster_bootstrap") or {}
    n_resamples = int(bootstrap.get("n_resamples") or 0)
    seed = int(bootstrap.get("seed") or 0)
    floors = prereg.get("release_floors") or []
    track_reports: dict[str, Any] = {}
    all_cases: dict[str, dict[str, Mapping[str, Any]]] = {}
    all_runs: dict[str, dict[str, list[Mapping[str, Any]]]] = {}

    for track in TRACKS:
        cases, case_errors = _validate_cases(track, list(cases_by_track.get(track, [])))
        track_contract = ((prereg.get("tracks") or {}).get(track) or {})
        repeats = int(track_contract.get("repeats") or 0)
        runs, run_errors = _validate_runs(track, list(runs_by_track.get(track, [])),
                                          cases, repeats, expected_binding,
                                          canonical_sha256(track_contract),
                                          float(track_contract.get("max_capture_age_hours") or 24))
        errors.extend(case_errors)
        errors.extend(run_errors)
        all_cases[track], all_runs[track] = cases, runs
        metrics: dict[str, Any] = {}
        for floor in [item for item in floors if item.get("track") == track]:
            metric = str(floor.get("metric"))
            eligible = {case_id for case_id, case in cases.items()
                        if metric in (case.get("metric_eligibility") or [])}
            case_values: dict[str, float] = {}
            for case_id in sorted(eligible):
                values: list[float] = []
                for run in runs.get(case_id, []):
                    value = (run.get("metrics") or {}).get(metric)
                    if value is None:
                        errors.append(f"{track}: {case_id} repeat {run.get('repeat')} missing "
                                      f"eligible metric {metric}")
                        continue
                    values.append(float(value))
                if len(values) == repeats:
                    case_values[case_id] = sum(values) / len(values)
            metrics[metric] = nested_cluster_bootstrap(
                case_values, cases, n_resamples=n_resamples, seed=seed)
        track_reports[track] = {
            "n_cases": len(cases),
            "n_runs": sum(len(value) for value in runs.values()),
            "repeats_required": repeats,
            "metrics": metrics,
        }

    zero_rules = prereg.get("zero_tolerance") or {}
    zero_report: dict[str, Any] = {}
    unknown_violations: list[str] = []
    violation_counts: dict[str, int] = defaultdict(int)
    violation_runs: dict[str, list[str]] = defaultdict(list)
    for track in TRACKS:
        for case_id, runs in all_runs.get(track, {}).items():
            for run in runs:
                for violation in run.get("violations") or []:
                    kind = str(violation.get("kind"))
                    if kind not in zero_rules:
                        unknown_violations.append(f"{track}:{case_id}#r{run.get('repeat')}:{kind}")
                    violation_counts[kind] += 1
                    violation_runs[kind].append(f"{track}:{case_id}#r{run.get('repeat')}")
    if unknown_violations:
        errors.append("runs contain unregistered violation kinds: " + ", ".join(unknown_violations))
    for kind, rule in zero_rules.items():
        probe_cases = {
            f"{track}:{case_id}"
            for track in TRACKS
            for case_id, case in all_cases.get(track, {}).items()
            if kind in (case.get("zero_tolerance_probes") or [])
        }
        minimum = int(rule.get("min_probe_cases") or 0)
        zero_report[kind] = {
            "count": violation_counts.get(kind, 0),
            "max_count": 0,
            "probe_cases": len(probe_cases),
            "min_probe_cases": minimum,
            "coverage_complete": len(probe_cases) >= minimum,
            "run_ids": sorted(violation_runs.get(kind, [])),
        }

    floor_results: list[dict[str, Any]] = []
    for floor in floors:
        track, metric = str(floor.get("track")), str(floor.get("metric"))
        observed = ((track_reports.get(track) or {}).get("metrics") or {}).get(metric) or {}
        insufficient: list[str] = []
        for observed_key, floor_key in (("n_cases", "min_cases"),
                                        ("n_template_clusters", "min_template_clusters"),
                                        ("n_semantic_clusters", "min_semantic_clusters")):
            if int(observed.get(observed_key) or 0) < int(floor.get(floor_key) or 0):
                insufficient.append(f"{observed_key} {observed.get(observed_key, 0)} < "
                                    f"{floor.get(floor_key)}")
        failed: list[str] = []
        if floor.get("min_point") is not None and (
                observed.get("point") is None or observed["point"] < floor["min_point"]):
            failed.append(f"point {observed.get('point')} < {floor['min_point']}")
        if floor.get("min_ci_low") is not None and (
                observed.get("ci_low") is None or observed["ci_low"] < floor["min_ci_low"]):
            failed.append(f"ci_low {observed.get('ci_low')} < {floor['min_ci_low']}")
        require_all = bool(floor.get("require_every_case_all_repeats"))
        if require_all:
            cases = all_cases.get(track, {})
            failing_cases = []
            for case_id, case in cases.items():
                if metric not in (case.get("metric_eligibility") or []):
                    continue
                values = [(run.get("metrics") or {}).get(metric)
                          for run in all_runs.get(track, {}).get(case_id, [])]
                if not values or not all(value is True or value == 1 for value in values):
                    failing_cases.append(case_id)
            if failing_cases:
                failed.append("not all repeats passed for: " + ", ".join(sorted(failing_cases)))
        status = "hold" if insufficient else ("failed" if failed else "passed")
        floor_results.append({
            "track": track, "metric": metric, "status": status,
            "observed": observed,
            "requirements": {key: floor.get(key) for key in (
                "min_point", "min_ci_low", "min_cases", "min_template_clusters",
                "min_semantic_clusters", "require_every_case_all_repeats")},
            "reasons": insufficient + failed,
        })

    incomplete_zt = [kind for kind, block in zero_report.items()
                     if not block["coverage_complete"]]
    breached_zt = [kind for kind, block in zero_report.items() if block["count"] > 0]
    reasons: list[str] = []
    if errors:
        decision = "HOLD"
        reasons.append("evidence is incomplete or inconsistent")
    elif breached_zt:
        decision = "BLOCK"
        reasons.append("zero-tolerance breach: " + ", ".join(sorted(breached_zt)))
    elif any(item["status"] == "failed" for item in floor_results):
        decision = "BLOCK"
        reasons.extend(reason for item in floor_results if item["status"] == "failed"
                       for reason in item["reasons"])
    elif incomplete_zt or any(item["status"] == "hold" for item in floor_results):
        decision = "HOLD"
        if incomplete_zt:
            reasons.append("zero-tolerance probe coverage incomplete: " +
                           ", ".join(sorted(incomplete_zt)))
        reasons.extend(reason for item in floor_results if item["status"] == "hold"
                       for reason in item["reasons"])
    else:
        decision = "PASS"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "complete" if not errors else "incomplete",
        "binding": binding_from_prereg(prereg),
        "run_binding": expected_binding,
        "tracks": track_reports,
        "input_hashes": dict(input_hashes or {}),
        "release_floors": floor_results,
        "zero_tolerance": zero_report,
        "integrity_errors": errors,
        "decision": decision,
        "reasons": reasons,
        "exit_code": {"PASS": EXIT_PASS, "HOLD": EXIT_HOLD, "BLOCK": EXIT_BLOCK}[decision],
    }


def validate_manifest(prereg: Mapping[str, Any], prereg_path: Path,
                      manifest: Mapping[str, Any], repo_root: Path,
                      package_root: Path) -> list[str]:
    errors = validate_preregistration(prereg, repo_root, require_frozen=True)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"manifest.schema_version must be {MANIFEST_SCHEMA_VERSION!r}")
    if manifest.get("status") != "complete":
        errors.append("manifest.status must be 'complete' for the release gate")
    prereg_ref = manifest.get("preregistration")
    if not isinstance(prereg_ref, Mapping):
        errors.append("manifest.preregistration must be an object")
    else:
        declared = prereg_ref.get("sha256")
        actual = sha256_file(prereg_path) if prereg_path.is_file() else None
        if declared != actual:
            errors.append(f"manifest preregistration hash mismatch: {declared!r} != {actual!r}")
        _validate_file_ref(prereg_ref, package_root, "manifest.preregistration", errors)
    expected_binding = binding_from_prereg(prereg)
    if manifest.get("binding") != expected_binding:
        errors.append("manifest binding differs from frozen preregistration")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("manifest.artifacts must be a list")
        artifacts = []
    seen_roles: set[tuple[str, str]] = set()
    artifact_map: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, artifact in enumerate(artifacts):
        label = f"manifest.artifacts[{index}]"
        if not isinstance(artifact, Mapping):
            errors.append(f"{label}: must be an object")
            continue
        role, track = str(artifact.get("role") or ""), str(artifact.get("track") or "")
        key = (role, track)
        if key in seen_roles:
            errors.append(f"{label}: duplicate role/track {key}")
        seen_roles.add(key)
        artifact_map[key] = artifact
        if track not in TRACKS and not (role == "release_report" and track == "all"):
            errors.append(f"{label}: invalid track {track!r}")
        _validate_file_ref(artifact, package_root, label, errors)
    required = {(role, track) for track in TRACKS for role in ("cases", "runs")}
    required.add(("release_report", "all"))
    missing = required - set(artifact_map)
    if missing:
        errors.append("manifest missing required artifacts: " +
                      ", ".join(f"{role}/{track}" for role, track in sorted(missing)))
    tracks = prereg.get("tracks") or {}
    for track in TRACKS:
        case_artifact = artifact_map.get(("cases", track))
        prereg_case = (tracks.get(track) or {}).get("case_set") or {}
        if case_artifact and case_artifact.get("sha256") != prereg_case.get("sha256"):
            errors.append(f"manifest cases/{track} hash differs from preregistered case set")
    return errors


def _artifact_paths(manifest: Mapping[str, Any], package_root: Path) -> dict[tuple[str, str], Path]:
    paths: dict[tuple[str, str], Path] = {}
    for artifact in manifest.get("artifacts") or []:
        if isinstance(artifact, Mapping):
            paths[(str(artifact.get("role")), str(artifact.get("track")))] = (
                package_root / str(artifact.get("path")))
    return paths


def gate_evidence_package(prereg_path: Path, manifest_path: Path, *,
                          repo_root: Path, package_root: Path | None = None) -> dict[str, Any]:
    package_root = (package_root or manifest_path.parent).resolve()
    try:
        prereg, manifest = load_json(prereg_path), load_json(manifest_path)
    except EvidenceError as exc:
        return {"decision": "HOLD", "exit_code": EXIT_HOLD, "reasons": [str(exc)]}
    errors = validate_manifest(prereg, prereg_path, manifest, repo_root.resolve(), package_root)
    if errors:
        return {"decision": "HOLD", "exit_code": EXIT_HOLD, "reasons": errors}
    paths = _artifact_paths(manifest, package_root)
    try:
        cases = {track: load_jsonl(paths[("cases", track)]) for track in TRACKS}
        runs = {track: load_jsonl(paths[("runs", track)]) for track in TRACKS}
        stored_report = load_json(paths[("release_report", "all")])
    except (EvidenceError, KeyError) as exc:
        return {"decision": "HOLD", "exit_code": EXIT_HOLD, "reasons": [str(exc)]}
    input_hashes = {
        track: {"cases_sha256": sha256_file(paths[("cases", track)]),
                "runs_sha256": sha256_file(paths[("runs", track)])}
        for track in TRACKS
    }
    recomputed = build_release_report(prereg, cases, runs, input_hashes=input_hashes)
    if stored_report != recomputed:
        return {"decision": "HOLD", "exit_code": EXIT_HOLD,
                "reasons": ["release_report.json differs from a fresh deterministic recomputation"]}
    return {"decision": recomputed["decision"], "exit_code": recomputed["exit_code"],
            "reasons": recomputed["reasons"], "report": recomputed}


def _parse_track_paths(values: Sequence[str], label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        if "=" not in raw:
            raise EvidenceError(f"{label} must use TRACK=PATH, got {raw!r}")
        track, path = raw.split("=", 1)
        if track not in TRACKS or track in result:
            raise EvidenceError(f"{label}: invalid or duplicate track {track!r}")
        result[track] = Path(path)
    if set(result) != set(TRACKS):
        raise EvidenceError(f"{label} must provide both {list(TRACKS)}")
    return result


def seal_manifest(prereg_path: Path, package_root: Path,
                  artifact_specs: Sequence[str], out: Path) -> dict[str, Any]:
    """Create (but do not certify) a hash-complete manifest from package files.

    ``artifact_specs`` use ``ROLE:TRACK=PATH``.  Paths must be inside ``package_root``.
    The subsequent gate still validates the preregistration, required roles, report
    recomputation and all cross-file bindings; sealing alone never means PASS.
    """
    package_root = package_root.resolve()
    prereg_path = prereg_path.resolve()
    try:
        prereg_rel = prereg_path.relative_to(package_root)
    except ValueError as exc:
        raise EvidenceError("preregistration must be inside --package-root") from exc
    prereg = load_json(prereg_path)
    artifacts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in artifact_specs:
        if "=" not in raw or ":" not in raw.split("=", 1)[0]:
            raise EvidenceError(f"--artifact must use ROLE:TRACK=PATH, got {raw!r}")
        key, raw_path = raw.split("=", 1)
        role, track = key.split(":", 1)
        if (role, track) in seen:
            raise EvidenceError(f"duplicate artifact role/track {(role, track)!r}")
        seen.add((role, track))
        candidate = Path(raw_path)
        path = candidate.resolve() if candidate.is_absolute() else (package_root / candidate).resolve()
        try:
            relative = path.relative_to(package_root)
        except ValueError as exc:
            raise EvidenceError(f"artifact path escapes package root: {raw_path!r}") from exc
        if not path.is_file():
            raise EvidenceError(f"artifact does not exist: {raw_path!r}")
        artifacts.append({"role": role, "track": track,
                          "path": relative.as_posix(), "sha256": sha256_file(path)})
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "complete",
        "preregistration": {"path": prereg_rel.as_posix(),
                            "sha256": sha256_file(prereg_path)},
        "binding": binding_from_prereg(prereg),
        "artifacts": artifacts,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _print_gate(result: Mapping[str, Any]) -> None:
    print(f"v7 release gate: {result.get('decision')} (exit {result.get('exit_code')})")
    for reason in result.get("reasons") or []:
        print(f"- {reason}")


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RentCompass fc_loop v7 evidence tools")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-prereg")
    validate.add_argument("prereg", type=Path)
    validate.add_argument("--repo-root", type=Path, default=Path("."))
    validate.add_argument("--allow-template", action="store_true")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("prereg", type=Path)
    analyze.add_argument("--cases", action="append", required=True, metavar="TRACK=PATH")
    analyze.add_argument("--runs", action="append", required=True, metavar="TRACK=PATH")
    analyze.add_argument("--out", type=Path, required=True)
    seal = sub.add_parser("seal-manifest")
    seal.add_argument("prereg", type=Path)
    seal.add_argument("--package-root", type=Path, required=True)
    seal.add_argument("--artifact", action="append", required=True,
                      metavar="ROLE:TRACK=PATH")
    seal.add_argument("--out", type=Path, required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("prereg", type=Path)
    gate.add_argument("manifest", type=Path)
    gate.add_argument("--repo-root", type=Path, default=Path("."))
    gate.add_argument("--package-root", type=Path)
    digest = sub.add_parser("hash")
    digest.add_argument("path", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.command == "hash":
            print(sha256_file(args.path))
            return EXIT_PASS
        prereg = load_json(args.prereg)
        if args.command == "validate-prereg":
            errors = validate_preregistration(prereg, args.repo_root,
                                              require_frozen=not args.allow_template)
            if errors:
                for error in errors:
                    print(f"- {error}", file=sys.stderr)
                return EXIT_HOLD
            print("v7 preregistration valid")
            return EXIT_PASS
        if args.command == "analyze":
            case_paths = _parse_track_paths(args.cases, "--cases")
            run_paths = _parse_track_paths(args.runs, "--runs")
            cases = {track: load_jsonl(path) for track, path in case_paths.items()}
            runs = {track: load_jsonl(path) for track, path in run_paths.items()}
            hashes = {track: {"cases_sha256": sha256_file(case_paths[track]),
                              "runs_sha256": sha256_file(run_paths[track])}
                      for track in TRACKS}
            report = build_release_report(prereg, cases, runs, input_hashes=hashes)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n", encoding="utf-8")
            _print_gate(report)
            return int(report["exit_code"])
        if args.command == "seal-manifest":
            seal_manifest(args.prereg, args.package_root, args.artifact, args.out)
            print(f"sealed v7 manifest: {args.out}")
            return EXIT_PASS
        result = gate_evidence_package(args.prereg, args.manifest,
                                       repo_root=args.repo_root,
                                       package_root=args.package_root)
        _print_gate(result)
        return int(result["exit_code"])
    except (EvidenceError, OSError, ValueError) as exc:
        print(f"evidence error: {exc}", file=sys.stderr)
        return EXIT_HOLD


def cli_gate(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``uk-rent-eval-gate``: only a sealed v7 evidence package."""
    parser = argparse.ArgumentParser(description="Gate a sealed fc_loop v7 evidence package")
    parser.add_argument("prereg", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--package-root", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else EXIT_USAGE
    result = gate_evidence_package(args.prereg, args.manifest, repo_root=args.repo_root,
                                   package_root=args.package_root)
    _print_gate(result)
    return int(result["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
