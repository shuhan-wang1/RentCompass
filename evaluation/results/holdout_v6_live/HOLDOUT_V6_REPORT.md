# Held-out v6 live evaluation report

## Run identity

- Benchmark: `rentcompass/benchmark/v6`, 180 frozen cases
- Product: `523745d`
- Evaluator: `5b74650`
- Clean runner identity: `82274a1`, `git-clean`
- Cases SHA-256: `7b4046c1472c3e81334c00dd588be75c9dd1331c161ea193a0e33876a8ee6f69`
- Raw runs SHA-256: `d6af9b6f4f0b8591bc7190b80ae0e4b9a0673dbbf44d733b6dddd85c1ce0c4fd`
- Requests: 180/180; runner errors 0; fixture violations 0; cost `$0.0313`

## Frozen deterministic results

All intervals are exact two-sided Clopper–Pearson 95% intervals.

| Contract | Pass | Rate | 95% CI |
|---|---:|---:|---:|
| eligible recall | 57/60 | 95.0% | 86.1–99.0% |
| recommendation precision | 87/90 | 96.7% | 90.6–99.3% |
| complete constraint satisfaction | 57/60 | 95.0% | 86.1–99.0% |
| required commute evidence | 28/30 | 93.3% | 77.9–99.2% |
| required memory writes | 30/30 | 100.0% | 88.4–100.0% |
| unsupported numeric control | 177/180 | 98.3% | 95.2–99.7% |

The analyzer's raw composite is 164/180 (91.1%), but it is not a CV headline: thirteen
no-result answers correctly said “no exact listings” while the frozen marker list only
matched narrower phrases.  The remaining three composite failures are real structured
product-contract failures: `HO6-198`, `HO6-208`, and `HO6-238` omitted
`eligible_recommendations`; the first two also omitted `commute_evidence` despite making
commute claims.  These failures remain in the declared denominators.

After the run, evaluator regression `no exact` markers were added and tested (16 tests
passing).  A separate post-fix replay of the same saved records scored 177/180; that
number is diagnostic only and is not substituted for the frozen 164/180 result.

## Reporting boundary

This is a synthetic, fixture-replayed held-out benchmark with live model inference.  It
does not measure live listing freshness, production SLA, or overall answer accuracy.
The CV uses only the structured retrieval and required-tool contracts above; task
completion and the composite are retained as diagnostics pending a marker fix and a
future frozen rerun.
