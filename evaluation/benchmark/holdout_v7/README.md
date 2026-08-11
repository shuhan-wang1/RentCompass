# Production `fc_loop` v7 evaluation evidence

v7 replaces the v6 legacy-only run as the release-evidence contract. The tracked
`PREREGISTRATION.template.json` is intentionally **UNBOUND** and cannot pass the gate;
it is a protocol template, not a claim that a live evaluation happened.

## Freeze before capture

Copy the template to a new evidence package as `PREREGISTRATION.json`, replace every
`UNBOUND`, set `status` to `frozen`, and hash each referenced file:

```bash
python -m uk_rent_agent.evals.evidence_v7 hash path/to/file
python -m uk_rent_agent.evals.evidence_v7 validate-prereg \
  path/to/package/PREREGISTRATION.json --repo-root .
```

The freeze is invalid unless all three full identities, the OCI digest, every prompt
ID/version/hash, model policy, tool policy, evaluator source set, both case sets and the
track-specific fixture/protocol artifact exist and hash correctly. A branch, image tag,
short SHA, or `latest` is not an identity.

## Two non-poolable tracks

- `deterministic_fixture`: closed fixtures, no live-tool fallback. It measures repeatable
  behavioral contracts but makes no freshness claim.
- `live_freshness`: fresh source capture under `LIVE_FRESHNESS_PROTOCOL.md`. It measures
  current availability/provenance and makes no deterministic-fixture claim.

Every case carries both `semantic_cluster` and `template_cluster`; every case is repeated
at least three times. Analysis first averages repeats per case, then cases per template,
then templates per semantic cluster. Its percentile interval resamples semantic clusters
and template clusters with replacement. Raw runs are never treated as independent.

## Analyze, seal and gate

The capture runner must emit case and run JSONL matching `case.schema.json` and
`run_record.schema.json`. Analysis is offline and performs no model or network calls:

```bash
python -m uk_rent_agent.evals.evidence_v7 analyze \
  path/to/package/PREREGISTRATION.json \
  --cases deterministic_fixture=path/to/package/deterministic/cases.jsonl \
  --cases live_freshness=path/to/package/live/cases.jsonl \
  --runs deterministic_fixture=path/to/package/deterministic/runs.jsonl \
  --runs live_freshness=path/to/package/live/runs.jsonl \
  --out path/to/package/release_report.json
```

Seal `manifest.json` from the actual package files. Optional capture logs/grader
inputs/tool traces must also be declared if retained:

```bash
python -m uk_rent_agent.evals.evidence_v7 seal-manifest \
  path/to/package/PREREGISTRATION.json --package-root path/to/package \
  --artifact cases:deterministic_fixture=deterministic/cases.jsonl \
  --artifact runs:deterministic_fixture=deterministic/runs.jsonl \
  --artifact cases:live_freshness=live/cases.jsonl \
  --artifact runs:live_freshness=live/runs.jsonl \
  --artifact release_report:all=release_report.json \
  --out path/to/package/manifest.json
```

Then run the real release gate:

```bash
uk-rent-eval-gate path/to/package/PREREGISTRATION.json \
  path/to/package/manifest.json --repo-root . --package-root path/to/package
```

Exit codes are `0 PASS`, `2 HOLD` (missing/inconsistent/insufficient evidence), `3 BLOCK`
(floor or zero-tolerance breach), and `64` for CLI misuse. The gate verifies every hash,
recomputes the report from the declared case/run artifacts, and rejects a stored report
that differs byte-for-structure from recomputation.

The preregistered template floors include 100% per-repeat hard-contract success,
deterministic task/tool/numeric floors, live listing-freshness/provenance/timestamp floors,
and zero tolerance for privacy-deletion failure, obeying untrusted prompt instructions,
unsupported numeric claims, forbidden/tainted writes, and DSML/tool-markup leakage.
