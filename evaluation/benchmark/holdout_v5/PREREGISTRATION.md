# Held-out v5 preregistration

Frozen before formal v5 cases or model requests.  Product under test is commit
`1c4e42bd82c9cc6be9230365b5d288c6739816fe` on `main`; the earlier v2 held-out result is
historical evidence only and is not reused after that product change.

The v3 operational attempt is also excluded: overlapping runner processes produced duplicate case executions before any outcome analysis. v4 is a completely fresh set and mechanically excludes v3 identifiers, queries, addresses and prices.

The v4 operational attempt is also excluded: fixtured cases could fall through to unbound live tools. v5 uses the closed-fixture guard and mechanically excludes v4 identifiers, queries, addresses and prices.

The formal set has 180 cases: 90 retrieval-hard (30 with an explicit per-listing commute
contract, 30 without, and 30 no-result), 30 calculation, 30 memory-write and 30
clarification.  The no-result prompts still contain user housing conditions, so calling
them retrieval-soft would conceal their hard-constraint semantics; this correction is made
before any v5 case or request exists.  A sacrificial pilot, if used, is stored separately
and never enters this set, its freeze record, or its denominator.

Each case declares `metric_eligibility` before execution.  Every headline metric and each
of the two required-tool contracts has at least 30 cases.  A missing run, malformed
structured output, missing listing ID, duplicate listing ID or runner error is a FAIL in
the declared denominator.  No case may be reclassified N/A after output is observed.

Primary deterministic metrics, implemented in the versioned deterministic metrics
harness `evaluation/results/_harness/holdout_v3_metrics.py` (the module name is historical;
the v5 schema and metric contract are passed explicitly):

1. **Eligible recall** — every fixture-truth eligible `eval_listing_id` appears in the
   product's `tool_data.eligible_recommendations`.
2. **Recommendation precision / false-positive control** — no ID in that collection is
   fixture-excluded, unknown, foreign or duplicated.
3. **Complete constraint satisfaction** — every offered eligible listing independently
   passes every stated frozen hard constraint; a nonempty truth set must produce an offer.
4. **Required-tool completion** — 30 commute cases require successful evidence bound to
   every retrieved listing; 30 memory cases require a successful `remember` side effect.
5. **Unsupported numeric control** — every currency or minutes claim in the answer is
   traceable to user input, frozen fixture/tool evidence or the frozen weekly/monthly
   formula.  Plausible invented market figures fail.
6. **Task completion** — the per-case machine-readable `completion_oracle` succeeds.
   Retrieval requires the exact eligible set (or a no-result acknowledgement); calculation,
   memory-write and clarification each have their own frozen oracle.

The composite case-success rate requires every metric declared applicable by that case to
pass.  Rates carry raw `k/n` and a Clopper-Pearson exact 95% interval; no bootstrap result
is quoted at a boundary.  Any LLM blind review is supplemental diagnostic evidence only,
not a primary metric or CV input.

All seven hard-constraint slots (budget, bedroom count, room type, area, move-in date,
property feature, commute) have at least 30 satisfaction-trap cases.  The first six occur
in every non-empty retrieval-hard case; commute occurs in the 30 per-listing contract cases.

Static preflight is required before the first formal request:

```bash
python3 evaluation/results/_harness/holdout_v3_preflight.py \
  --schema-version rentcompass/benchmark/v5 \
  --cases evaluation/benchmark/holdout_v5/cases_holdout_v5.jsonl \
  --fixtures evaluation/benchmark/holdout_v5/fixtures \
  --compare-cases evaluation/benchmark/cases.jsonl \
  --compare-cases evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl \
  --compare-cases evaluation/benchmark/holdout_v3/cases_holdout_v3.jsonl \
  --compare-cases evaluation/benchmark/holdout_v4/cases_holdout_v4.jsonl \
  --out evaluation/benchmark/holdout_v5/preflight_report.json
```

Exit 0 is necessary, not sufficient: it proves the static contract and quotas, while the
author audit records the human checks of factual correctness, novelty and fixture logic.
