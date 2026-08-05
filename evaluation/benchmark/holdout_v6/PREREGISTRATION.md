# Held-out v6 preregistration

This set is frozen before any formal v6 model request.  The product under test is
commit `523745d` (evidence-preserving multi-tool orchestration, deterministic rent
conversion and side-effect/provenance guards).  The evaluator and v6 preflight are
frozen at commit `5b74650`.

The set contains 180 deterministic cases: 90 retrieval-hard (30 per-listing commute,
30 ordinary retrieval and 30 no-result), 30 calculations, 30 memory writes and 30
clarifications.  Case and fixture identities, queries, addresses and prices are
mechanically non-overlapping with base98 and held-out v2–v5.  The v5 run remains a
diagnostic postmortem and is not pooled with v6.

Every case declares its applicable metrics before execution.  The static gate requires
at least 30 observations for every primary metric, each required-tool contract, and
each of the seven hard-constraint slots.  Missing runs, malformed payloads, duplicate
listing IDs and runner errors remain failures in their declared denominator.

Clarification cases deliberately separate two layers: a user-facing focused question
is the task-completion criterion; an internal `ask_user` dispatch is not silently used
as a proxy for that criterion.  v6 clarification oracles carry
`accept_text_question: true`.  A text-only question must not follow a successful search
or expose a listing payload.  Tool-side effects may be reported separately as
diagnostics, but are not part of the clarification task-completion denominator.

Primary metrics are the deterministic contracts implemented in
`evaluation/results/_harness/holdout_v3_metrics.py`: eligible recall, recommendation
precision, complete constraint satisfaction, required-tool completion, unsupported
numeric control and task completion.  Rates are reported as raw `k/n` with exact
Clopper–Pearson 95% intervals.  Any LLM blind review is supplemental only.

The preflight command, run before the first request, is:

```bash
python3 evaluation/results/_harness/holdout_v3_preflight.py \
  --schema-version rentcompass/benchmark/v6 \
  --cases evaluation/benchmark/holdout_v6/cases_holdout_v6.jsonl \
  --fixtures evaluation/benchmark/holdout_v6/fixtures \
  --compare-cases evaluation/benchmark/cases.jsonl \
  --compare-cases evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl \
  --compare-cases evaluation/benchmark/holdout_v3/cases_holdout_v3.jsonl \
  --compare-cases evaluation/benchmark/holdout_v4/cases_holdout_v4.jsonl \
  --compare-cases evaluation/benchmark/holdout_v5/cases_holdout_v5.jsonl \
  --out evaluation/benchmark/holdout_v6/preflight_report.json
```

Exit 0 is necessary but not sufficient: the formal run must use a clean checkout,
the closed-fixture guard, one monitored process and no resume/concurrent runner.  No
metric is added to the CV unless its denominator and contract remain valid after the
formal run and case-level audit.
