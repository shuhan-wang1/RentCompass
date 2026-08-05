# Held-out v6 live-run status

The first attempt was aborted after an environment-loading mistake caused the clean
checkout to use the runner's offline placeholder key.  It is retained only as an abort
record and is not pooled with the completed run.

The formal rerun completed successfully:

- Frozen benchmark commit: `82274a1`
- Frozen product commit: `523745d`
- Cases: 180 (`evaluation/benchmark/holdout_v6/cases_holdout_v6.jsonl`)
- Static preflight: passed (exit 0)
- Clean-checkout identity: `82274a1`, `git-clean`
- Requests: 180/180; runner errors: 0; fixture violations: 0
- Runner summary: `runner_summary.json`
- Deterministic analysis: `analysis_summary.json`
- Raw/evidence records: `raw_runs.jsonl`, `grader_input.jsonl`

The completed run is valid for the frozen deterministic contracts, subject to the
case-level audit documented in `HOLDOUT_V6_REPORT.md`.  In particular, three retrieval
cases exposed a real product contract failure (missing structured eligible output; two
also lacked per-candidate commute evidence), while thirteen no-result task-completion
failures are evaluator marker false negatives and are not treated as product failures.
