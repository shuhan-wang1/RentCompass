# Phase 2.1 A/B result packages — 2026-07-19

This directory is the **in-repo home for the round-3 A/B result packages** of the
harness-migration Phase 2 (`docs/harness_migration_design.md`). It is intentionally a
**stub**: the coordinator runs the paid `--live` A/B (legacy vs fc_loop) and copies the
resulting packages here. Nothing in this directory is fabricated — no numbers are written
by any implementation agent.

## What a package contains

Each `--out` dir produced by `python -m evaluation.run_benchmark` emits a reproducible
package. Copy the following per arch (drop `events.jsonl` — it stays out of git):

```
phase2_ab_2026-07-19/
  legacy/
    summary.json          # aggregate metrics + node_latency_by_kind + slowest_cases
    per_case.csv          # case_id, category, arch, passed, route_matched, hard_gate,
                          #   llm_calls, tool_batches, latency_ms, cost_usd, failed_constraints
    per_case_detail.csv   # grounding rates + constraint tallies (companion)
    manifest.json         # argv, env (AGENT_ARCH/DEEPSEEK_STRICT/config), git_commit,
                          #   timestamp, case-file SHA256, events.jsonl SHA256 (digest only)
    tool_metrics.csv
    model_usage.csv
    raw_runs.jsonl
  fc_loop/
    ...same files...
```

`manifest.json` pins the exact command, code commit, and the SHA256 of the case file and
the (uncommitted) event log, so each package is reproducible and integrity-checkable from
the committed tree alone. `events.jsonl` is deliberately **not** committed; its digest in
`manifest.json` is enough to verify a package against a re-run.

## Producing the round-3 packages

```
# fc_loop
python -m evaluation.run_benchmark \
  --cases evaluation/benchmark/cases_guard_regression.jsonl --live --arch fc_loop \
  --out evaluation/results/phase2_ab_2026-07-19/fc_loop

# legacy baseline (H1/H13 expected to fail per the legacy asymmetry — see benchmark/README.md)
python -m evaluation.run_benchmark \
  --cases evaluation/benchmark/cases_guard_regression.jsonl --live --arch legacy \
  --out evaluation/results/phase2_ab_2026-07-19/legacy
```

Then `git add` everything **except** the two `events.jsonl` files.
