# Held-out v5 result

## Validity

- Frozen product commit: 1c4e42bd82c9cc6be9230365b5d288c6739816fe.
- Fresh synthetic held-out set: 180 cases, with zero verbatim-query overlap against base98 and held-out v2/v3/v4.
- Static preflight passed before the first request.
- Formal run: 180 runs for 180 unique case IDs; 0 missing, duplicate, or runner-error records.
- Cases SHA-256: 196bf65309a26162eafa6661de8d1e269968a0319925fcf0c7e345e26c040584.

All product outputs were evaluated only against frozen fixtures. The closed-fixture boundary denied 25 unrecorded tool requests; no live-tool fallback occurred.

## Pre-registered deterministic results

| Metric | Result | Exact 95% CI |
| --- | ---: | ---: |
| Composite case success | 133/180 (73.9%) | 66.8%–80.1% |
| Eligible-listing recall | 50/60 (83.3%) | 71.5%–91.7% |
| Recommendation precision | 80/90 (88.9%) | 80.5%–94.5% |
| Complete constraint satisfaction | 50/60 (83.3%) | 71.5%–91.7% |
| Required-tool completion | 50/60 (83.3%) | 71.5%–91.7% |
| Unsupported numeric control | 170/180 (94.4%) | 90.0%–97.3% |
| Task completion | 133/180 (73.9%) | 66.8%–80.1% |
| Commute contract | 20/30 (66.7%) | 47.2%–82.7% |
| Memory-write contract | 30/30 (100.0%) | 88.4%–100.0% |

Intervals are Clopper–Pearson exact binomial intervals.

## Findings

1. Product: commute orchestration remains incomplete. Ten commute retrieval cases (HO5-003/006/009/012/015/018/021/024/027/030) already had frozen structured listings and per-listing commute evidence. The agent nevertheless requested unrecorded property-detail or commute-cost tools, then returned that the commute could not be verified. These cases fail recall, precision, constraint satisfaction, required-tool completion, numeric control, and task completion.
2. Product/product-contract mismatch: weekly-to-monthly calculation. Twenty-nine of 30 calculation cases were declined as out of scope, despite the frozen request explicitly being a rent conversion and the product having a monthly_from_weekly implementation. This needs a product routing/contract decision before it can be evaluated as successful functionality.
3. Evaluator contract defect: clarification. Six clarification cases asked an appropriate text question but did not invoke the internal ask_user tool. The v5 oracle treats that tool call as mandatory, so these six task-completion failures are not evidence of a user-facing product failure. The clarification oracle must be repaired before a new overall-completion rate is reported.
4. No CV quality claim. The overall result is mixed and the primary aggregate includes the known clarification-oracle mismatch. No new quality metric from v5 is added to CV_METRICS.md. The existing v2 claims are historical, scoped claims and are not updated by this run.

## Reporting correction

After the formal run, a bug was found in the bisection direction used only to compute non-boundary Clopper–Pearson interval endpoints. It did not affect cases, fixtures, predicates, pass/fail counts, or denominators. The helper was corrected and given a non-boundary regression test before this report was produced.

## Artifacts

- summary.json and per_case_metrics.jsonl are deterministic analyses of the saved raw records.
- raw_runs.jsonl and grader_input.jsonl are retained with SHA-256 hashes in summary.json.
