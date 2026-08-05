# Experiment D — dimension fan-out / batch packing

Preregistration: `evaluation/results/fanout_ab/PREREGISTRATION.md`. Difference convention: **ON - OFF (positive = the fan-out arm is higher)**.
Bootstrap: 10000 resamples, unit = case, seed 20260805, 95% percentile.

## Negative controls (reported first)

| control | result |
|---|---|
| NC-c switch integrity | PASS — 96 OFF runs at cap 0 with zero firings, 96 ON runs at cap 3 |
| NC-b read tools only | PASS — 179 fan-out additions across 192 runs; histogram {'calculate_commute': 60, 'check_safety': 47, 'search_nearby_pois': 72}; violations 0 |
| NC-a no extra batches | PASS — paired tool_batches ON-OFF = -0.281 [-0.583, -0.031] over 96 pairs |

Plan-time-only pairs: 72; pairs with an answer-time firing: 0. Restricted to plan-time-only pairs, paired tool_batches ON-OFF = -0.389 [-0.764, -0.069] (n_cases=6).

## Per arm

| metric | fanout_on | fanout_off |
|---|---|---|
| runs ok | 96 | 96 |
| dimension coverage (covered/cued) | 204/252 = 0.810 | 138/252 = 0.548 |
| llm_calls mean | 2.54 | 2.99 |
| tool_batches mean | 1.28 | 1.56 |
| tools executed mean | 3.78 | 2.67 |
| e2e wall ms p50 | 9159 | 8093 |
| e2e wall ms p95 | 51752 | 66031 |
| soft-wrapped runs | 1 | 2 |
| cost USD total | 0.0545 | 0.0541 |
| tokens in / out | 2614421 / 73831 | 3015657 / 77374 |
| runs where the fan-out fired | 72 | 0 |
| firings plan-time / answer-time | 72 / 0 | 0 / 0 |

Usable pairs: **96** over 8 cases (192/192 runs ok, 0 failed, 0 pairs dropped).

## Paired contrasts (ON − OFF), cluster bootstrap over cases

| metric | point | 95% CI | pairs | cases | verdict |
|---|---|---|---|---|---|
| dimension_coverage_ratio | 0.2292 | [0.0382, 0.4410] | 96 | 8 | ON higher |
| dimensions_covered_count | 0.69 | [0.11, 1.32] | 96 | 8 | ON higher |
| llm_calls | -0.45 | [-0.94, -0.09] | 96 | 8 | ON lower |
| tool_batches | -0.28 | [-0.58, -0.03] | 96 | 8 | ON lower |
| n_tools_executed | 1.11 | [0.15, 2.30] | 96 | 8 | ON higher |
| wall_ms | -5818.79 | [-15247.60, -563.91] | 96 | 8 | ON lower |
| turn_latency_ms | -5861.76 | [-15350.02, -577.04] | 96 | 8 | ON lower |
| cost_usd | 0.0000 | [-0.0001, 0.0001] | 96 | 8 | no significant difference observed |
| tokens_in | -4179.54 | [-11135.11, 1028.07] | 96 | 8 | no significant difference observed |
| tokens_out | -36.91 | [-155.88, 65.70] | 96 | 8 | no significant difference observed |
| soft_wrapped | -0.0104 | [-0.0312, 0.0000] | 96 | 8 | no significant difference observed |

A CI that includes 0 means **no significant difference was observed** — it is not evidence that there is no difference, and no direction is read off it.
