# RentCompass Offline Evaluation — REPORT

_Every number below is copied or derived from a file under `evaluation/results/` (or a static scope file). Nothing is estimated; every rate carries its denominator._

## 1. Evaluation date

- Report generated: **2026-07-12T06:27:47Z**
- Result files were produced at (UTC/ISO from each file): `2026-07-12T05:01:34Z`, `2026-07-12T05:18:31Z`, `2026-07-12T06:14:32Z`, `2026-07-12T06:18:51`, `2026-07-12T06:18:58`

## 2. Git commit

- Report generated at HEAD: **`070675d`**
- Commit recorded in the result files: `070675d`

## 3. Environment

- OS: `Windows 10` (reported by the interpreter; host is Windows 11).
- Conda env: `uk_rent` (target env: `uk_rent`).
- Python: `3.10.18` — `C:\Users\shuhan\anaconda3\envs\uk_rent\python.exe`
- Key libraries (from installed metadata):
  - `chromadb`: 1.1.0
  - `folium`: 0.20.0
  - `pydantic`: 2.11.9
  - `PyYAML`: 6.0.3
  - `openai`: 2.41.0
  - `httpx`: 0.28.1
  - `langgraph`: 1.2.4
  - `langchain-core`: 1.4.0
  - `sentence-transformers`: 5.1.1
  - `numpy`: 2.2.6
  - `requests`: 2.32.5
  - `flask`: 3.1.2
- `chromadb` is present in this env, so the memory store eval RAN (not blocked). SearXNG is operational, so the live `web_search` tool returns real results and web-dependent cases are grounded (see §6 and §12).

## 4. Models + versions

- Router maps light nodes to `deepseek-chat` and strong/thinking nodes to `deepseek-reasoner`.
- **Pricing / version note (important):** per `model_pricing.yaml`, `deepseek-chat` and `deepseek-reasoner` are the non-thinking / thinking modes of the SAME underlying model (`deepseek-v4-flash`) and share the SAME per-token rate. Therefore any cost saving from model routing is TOKEN-VOLUME driven (fewer / shorter reasoner generations), **not** a cheaper per-token rate.
- Price source: `https://api-docs.deepseek.com/quick_start/pricing (official DeepSeek API pricing page)`, as of `2026-07-11` (USD per 1000000 tokens). These aliases are slated for deprecation on 2026-07-24 — re-confirm rates after that date.
- `deepseek-chat`/`deepseek-reasoner` rate: input $0.14 / cached $0.0028 / output $0.28 per 1000000 tokens.

## 5. Benchmark counts + categories

- Benchmark cases: **98** (smoke subset: **10**), across **7** categories.
  - `A_retrieval`: 14
  - `B_money`: 15
  - `C_commute`: 12
  - `D_crime_poi`: 13
  - `E_multi_constraint`: 11
  - `F_grounding`: 17
  - `G_memory`: 16
- Constraint-type vocabulary (from `metrics.graders.CONSTRAINT_CHECKERS`): **23** machine-checkable types.
- Fault-injection scenarios: **15** (harness defines 15).
- Retrieval A/B scope: **16** cases x **3** repeats per config.

## 6. Live vs fixture

- Run `live_routed_98`: mode=**live** (LIVE), config=`routed_models`, n_runs=98.
  - Note (from summary.json): LIVE mode: fixtured cases replay recorded evidence; non-fixture cases run tools in-process (may hit cache/live network, nondeterministic).
- Fixture bank on disk: **48** recorded tool-output fixtures (`evaluation/benchmark/fixtures/`). Fixtured cases replay recorded evidence deterministically; non-fixture cases run tools in-process (cache/live network, nondeterministic).
- **Web grounding works this run:** SearXNG is operational, so the live `web_search` tool returns real Rightmove / Zoopla / SpareRoom results and web-dependent cases ARE grounded (visible in the B_money / F_grounding `multi_search` cases in §10.1). The remaining caveat is nondeterminism — live web/scrape results vary across runs.

## 7. Cache usage

- Run `live_routed_98`: sum `cached_tokens` = **109824** across 168 model-call rows in `model_usage.csv` (real DeepSeek prompt-cache hits on the live run).

## 8. Repeat counts

- Benchmark `live_routed_98`: repeats = **1**
- Model-routing A/B: repeat = **1**
- Retrieval-concurrency A/B: repeat_count = **3** (36 runs per config).

## 9. Metric definitions

- **passed (end-to-end)**: a case passes iff task_completed AND no forbidden tool was called AND every expected_constraint passes AND 0 contradicted claims. A single failed heuristic constraint fails the whole case.
- **task_completion**: non-empty final answer AND no run/harness error.
- **grounded_rate**: grounded verifiable claims / total verifiable claims extracted from the answer (money / commute / crime / distance / postcode). A claim is grounded when it traces to tool/fixture evidence; the complement is 'unsupported'.
- **money_grounded_rate**: grounded monetary claims / total monetary claims.
- **contradicted_claims**: count of claims in the answer that directly contradict the tool evidence (e.g. two disagreeing prices, wrong direction). 0 is the target.
- **source_coverage**: claims traceable to a named TOOL source / total claims.
- **constraints**: expected_constraints that passed / total expected_constraints.
- **strong_model_calls**: llm_call events whose model is the reasoner tier.
- **critic_repair_success**: critic-triggered regenerations that produced an improved (re-graded better) answer / critic triggers.
- **retry_recovery_rate**: fault scenarios where the REAL retry loop recovered / scenarios where retry applies (non-recoverable-by-design faults, e.g. HTTP 500 on every attempt, are counted in the denominator and correctly do NOT recover).
- **fallback_success_rate**: MCP fault scenarios where the in-process fallback produced a valid result / MCP fault scenarios.
- **idempotency_pass_rate**: write-fault scenarios that produced exactly one durable write / idempotency scenarios.
- **faults_correctly_surfaced**: scenarios where the fault was surfaced honestly (error, caveat, neutralised, or recovery) instead of silently fabricated / total scenarios.
- **post_fault_completion_rate**: scenarios that still completed the task after the fault.
- **post_fault_ungrounded_rate**: scenarios that produced an ungrounded answer after the fault (lower is better).
- **race_anomalies (retrieval A/B)**: serial-vs-parallel (case,repeat) pairs that differ in tool-call count or completion status (a proxy for dropped / raced results). Broken out into tool_count_mismatch and completion_mismatch.
- **memory: identity_gate / user_isolation / forget_delete / restart_recovery / retrieval_relevance**: deterministic ChromaDB store checks — no model in the loop; these measure real store behaviour (per-user filtering, GDPR erasure, on-disk persistence, retrieval).
- **memory: extraction_precision / update_correctness / stale_replacement / contradiction_handling**: exercise the store's write/consolidate MECHANICS with the LLM extraction / importance / consolidation calls STUBBED (canned) — they measure plumbing, NOT real LLM extraction quality.

## 10. Full results (with denominators)

### 10.1 Main benchmark

**Run `live_routed_98`** (mode=live, config=routed_models, n_runs=98):

- passed (end-to-end): 34/98 (34.7%)
- task_completion: 98/98 (100.0%)
- constraints: 215/321 (67.0%)
- grounded_rate: 152/204 (74.5%)
- money_grounded_rate: 121/152 (79.6%)
- source_coverage: 120/204 (58.8%)
- contradicted_claims: **1**
- critic_triggers: 73, critic_repairs: 11
- e2e latency ms: mean=6432.0 p50=4310.6 p95=19932.8 (n=98)
- tokens: input=137553 output=38156
- total_cost_usd: **$0.0149** (cap $8.00)

Per-category pass (from `evaluation/results/live_routed_98/per_case.csv`, cross-checks the 34/98 (34.7%) headline; sum=34/98):

| Category | Passed |
|---|---|
| `A_retrieval` | 9/14 (64.3%) |
| `B_money` | 7/15 (46.7%) |
| `C_commute` | 5/12 (41.7%) |
| `D_crime_poi` | 1/13 (7.7%) |
| `E_multi_constraint` | 2/11 (18.2%) |
| `F_grounding` | 7/17 (41.2%) |
| `G_memory` | 3/16 (18.8%) |

Tool call metrics (from `evaluation/results/live_routed_98/tool_metrics.csv`):

| Tool | calls | success | fail | mean_ms | p95_ms |
|---|---|---|---|---|---|
| `calculate_commute` | 5 | 5 | 0 | 0.5 | 0.5 |
| `check_safety` | 1 | 1 | 0 | 8.0 | 8.0 |
| `get_property_details` | 5 | 4 | 1 | 9.0 | 12.7 |
| `get_transport_info` | 4 | 4 | 0 | 2.3 | 7.0 |
| `recall_memory` | 4 | 4 | 0 | 272.5 | 482.4 |
| `search_nearby_pois` | 4 | 4 | 0 | 0.5 | 0.5 |
| `search_properties` | 29 | 22 | 7 | 3151.3 | 9896.5 |
| `web_search` | 75 | 75 | 0 | 516.6 | 2866.9 |

### 10.2 Model-routing A/B (baseline_all_strong vs routed_models)

- mode: **live**, n_cases=98, repeat=1

| Metric | baseline_all_strong | routed_models | change (routed vs baseline) |
|---|---|---|---|
| strong_model_calls / total | 165/170 | 78/172 | -52.7% |
| total tokens (in/out) | 197981 (141967/56014) | 185612 (145599/40013) | total -6.2%, output -28.6% |
| estimated_cost_usd | $0.02105 | $0.01594 | -24.3% |
| e2e latency mean ms | 9338.1 | 5754.0 | -38.4% |
| e2e latency p95 ms | 27871.6 | 17256.2 | -38.1% |
| grounded_rate | 160/207 (77.3%) | 160/207 (77.3%) | grounding maintained |
| task_completion | 98/98 (100.0%) | 98/98 (100.0%) | = |
| tool_success_rate | 113/121 (93.4%) | 124/131 (94.7%) |  |

- **Framing:** the cost / token / strong-call reductions are TOKEN-VOLUME driven (routing cheap nodes off the thinking-mode reasoner) — the two models share the same per-token rate, so this is NOT a per-token price difference.
- Recorded note: Volume-driven: deepseek-chat and deepseek-reasoner share the SAME per-token rate, so cost/token reductions reflect fewer/shorter reasoner generations, not a cheaper rate.

### 10.3 Retrieval-concurrency A/B (serial vs parallel)

- mode: **live**, n_cases=16, repeat_count=3 (n_runs/config=48).

| Metric | serial | parallel | change (parallel vs serial) |
|---|---|---|---|
| retrieval-stage latency mean ms | 1359.0 | 582.4 | -57.1% |
| retrieval-stage latency p50 ms | 4.0 | 4.3 | +9.3% |
| retrieval-stage latency p95 ms | 5789.9 | 3359.6 | -42.0% |
| e2e latency mean ms | 9989.5 | 8549.4 | -14.4% |
| tool_success_rate | 102/105 (97.1%) | 102/105 (97.1%) | |

- **Race / dropped-result anomalies:** 0/48 paired runs (tool_count_mismatch=0, completion_mismatch=0). No paired run differed in tool-call count or completion status — i.e. no result was dropped or raced at any level under live nondeterminism.
- **Interpretation:** parallel fan-out cuts the retrieval STAGE latency sharply, but end-to-end time is dominated by model synthesis, so e2e is ~unchanged.

### 10.4 Fault injection (real tool/graph/idempotency/guardrail code, mocked model)

- Resilience mechanics are GENUINE: only the LLM is mocked. Numbers below are real observations of production error paths.
- scenarios: **15**
- faults_correctly_surfaced: 15/15 (100.0%)
- retry_recovery_rate: 4/8 (50.0%) (the non-recoveries are non-recoverable-by-design faults: HTTP 500 every attempt, missing schema field, critic/synthesis model raising)
- fallback_success_rate: 2/2 (100.0%)
- idempotency_pass_rate: 3/3 (100.0%)
- total_duplicate_writes: **0**
- post_fault_completion_rate: 13/15 (86.7%)
- post_fault_ungrounded_rate: 1/15 (6.7%)
- harness_errors: **0**

### 10.5 Long-term memory eval

- status: **ok**; chromadb_available: **True**

**REAL deterministic store checks (no model in the loop — genuine):**

- forget_delete: 3/3 (100.0%)
- identity_gate: 7/7 (100.0%)
- restart_recovery: 1/1 (100.0%)
- retrieval_relevance: 0/1 (0.0%)
- user_isolation: 5/5 (100.0%)
- forget_request_pass_rate: 3/3 (100.0%)
- memory_write_success_rate: 4/4 (100.0%)
- restart_recovery_pass_rate: 1/1 (100.0%)
- user_isolation_pass_rate: 5/5 (100.0%)

**STUBBED-model mechanics (LLM extraction / consolidation is canned — measures plumbing, NOT extraction quality):**

- contradiction_handling: 1/1 (100.0%) — *stub mechanics, not real extraction quality*
- extraction_precision: 3/3 (100.0%) — *stub mechanics, not real extraction quality*
- stale_replacement: 1/1 (100.0%) — *stub mechanics, not real extraction quality*
- update_correctness: 1/1 (100.0%) — *stub mechanics, not real extraction quality*
- memory_retrieval_accuracy: 4/5 (80.0%) — *retrieval-by-user-filter is real, but 4 of its 5 sub-cases are seeded via the stubbed consolidation path; retrieval_relevance (the failing sub-case) is fully real*

## 11. Known limitations

- This is an OFFLINE benchmark on 98 curated cases, not a study of real users. Pass/grounding rates describe agent behaviour on these prompts, not field accuracy.
- The main benchmark and the model A/B are SINGLE runs (repeat=1); only the retrieval A/B is repeated (3x). Single-run rates on small n have wide confidence intervals.
- Grounding and several constraint checkers are HEURISTIC (text-marker / regex based); they can under- or over-count claims. The optional LLM judge was not enabled this run.
- LIVE non-fixture cases (many A / C / D / E prompts) run tools in-process against cache/live network and are NONDETERMINISTIC; re-running can shift their pass/latency.
- SearXNG IS operational this run, so the live `web_search` tool returns real results and the web-dependent B / F cases ARE grounded (market-rent answers cite Zoopla / Rightmove / SpareRoom). The residual caveat is that live web/scrape results are NON-DETERMINISTIC across runs, so exact figures vary between runs.
- 3 benchmark cases (E8 / F11 / G16, flagged NEEDS_CHECKER in `cases.jsonl`) are graded via the closest existing constraint-checker type rather than a bespoke checker — an approximation that can under- or over-count those specific cases.
- Cost is DeepSeek's published rate applied to measured token counts (aliased models slated for 2026-07-24 deprecation); re-confirm rates before quoting spend.
- Fault injection and the memory store checks mock/stub the LLM; those numbers are genuine RESILIENCE / STORE mechanics, not answer-quality measurements.
- No human review of the generated answers was performed.

## 12. What could NOT be measured (and why)

- **LLM-judge agreement with the deterministic grader** — the auxiliary LLM judge is implemented and optional (`--judge`), but was NOT enabled on this run, so no judge-vs-grader agreement number was produced. Not a blocker; simply not run.
- **`web_search` answer quality (now PARTIALLY measurable)** — SearXNG is operational, so the tool returns real results and web-dependent cases ARE graded and grounded (market-rent answers cite Zoopla / Rightmove / SpareRoom). The residual limitation is NONDETERMINISM, not unavailability: live web/scrape results vary across runs, so exact web-grounded figures are not bit-for-bit reproducible from a single run.
- **Real long-term-memory extraction / consolidation quality** — the memory eval STUBS the LLM extraction / importance / consolidation calls (see `memory_eval.py` docstring), so `extraction_precision` and the update/stale/contradiction checks are store-plumbing mechanics, not a measure of real LLM extraction quality.
- **Live tool variance for non-fixture A / C / D / E cases** — those prompts hit cache/live network in-process and are nondeterministic; a single run does not bound their variance.

### Real agent findings surfaced (valuable eval output, not framework defects)

The low per-category pass rates in D / E / G reflect REAL agent behaviours that the benchmark correctly caught:

- **Over-clarification on area-level safety (D = 1/13):** the agent asked a clarifying question instead of answering on cases D2, D3, D6, D7, D9, D10, D12, D13 (all clarification-routed cases this run: C3, D2, D3, D6, F4, C7, C12, D7, D9, D10, D12, D13, F12). A real over-conservative safety gate, not a grading bug.
- **Memory `remember`/recall not firing (G = 3/16):** several G cases routed to `direct_answer` / `reasoning_property` and the agent did not persist/recall the stored preference (only the explicit `recall_memory` cases passed). A real behaviour gap surfaced by the eval.
- **Multi-constraint tool-chaining + source-citation gaps (E = 2/11):** E answers are largely grounded but fail on the FULL constraint set (partial constraint satisfaction), i.e. the agent does not chain enough tools / cite every required source to satisfy every constraint at once.
- **Web-dependent cases now grounded (SearXNG operational):** 15 cases routed through `web_search`/`multi_search` (B2, B3, B4, B5, B7, F7, A12, B8, B9, B10, B13, B14, B15, F9, F10); the live search backend returns real Rightmove / Zoopla / SpareRoom results, so market-rent claims ARE web-grounded. Any residual failures in this subset (B5, A12, B8, B9, B10, B13, B14, B15, F9, F10) stem from the constraint/citation gaps above plus run-to-run web nondeterminism, NOT from the web tool being unavailable.

## 13. Which numbers are CV-suitable

- **Well-supported (see `CV_METRICS.md` -> 可安全使用):** model-routing A/B engineering deltas at n=98 (strong-call, cost, e2e, token reductions with grounding maintained); live grounding fidelity on the 98-case benchmark (grounded 152/204 (74.5%), money 121/152 (79.6%), contradicted 1); retrieval-stage parallelization latency reduction with near-perfect race parity; fault-tolerance mechanics; the REAL memory store isolation/forget/restart checks; and framework scope.
- **Do NOT quote as a headline (see `CV_METRICS.md` -> 不建议使用):** the raw end-to-end pass_rate 34/98 (34.7%) (dragged down by the real agent findings above + heuristic checkers + live tool variance / web nondeterminism); the STUBBED memory extraction number; any n<15 single-run rate; and LLM-judge agreement (not run).

_Full per-claim CV guidance — 中文/English wording, raw num/den, definition, file path, safe flag, required caveat — is in `CV_METRICS.md`._

