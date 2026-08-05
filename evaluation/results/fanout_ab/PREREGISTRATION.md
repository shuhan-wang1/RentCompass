# Experiment D — dimension fan-out / batch packing — PREREGISTRATION

**Frozen 2026-08-05 19:55 BST, before any measurement run.**
Product commit under test: `d7a7702` (branch `experiment/fanout-ab-20260805`).
Harness: `evaluation/results/_harness/ab_runner.py` (Experiment-D switch added in the same
commit as this file; nothing under `app/` is modified by the experiment).

Nothing below may be changed once the first measurement run starts. If the results are
uninteresting, this design still stands and any revision is recorded as a **second design**
with both results reported (GOAL §4.1).

---

## 1. What is being tested, and why this and not Experiment C

Experiment C asked whether fc_loop's **intra-batch concurrency** beat serial dispatch. That
is a settled **negative** result and is not being re-run:
`evaluation/results/parallel_tools_ab/analysis_C.json` and `negative_control_C.json` — the
retrieval-stage latency CI crossed zero in both the warm and the cold round, and the
negative control **failed** (16/60 warm, 18/60 cold paired runs executed a different number
of tools, because serialising changed the timing, which changed what the model planned
next). The reason it was empty is in `evaluation/results/EVAL_REPORT_20260804.md` §3: only
65 of 244 tool batches (26.6%) held ≥2 calls, and with a batch of one, intra-batch
concurrency is worth exactly zero.

The mechanism fc_loop actually has and legacy does not is **batch packing**:
`agent_loop._dimension_fanout_calls` (plan time) and `_completion_sweep_into_batch`
(answer time) put a read the user **cued** but the model did not request into a batch. The
comment next to `_dimension_fanout_cap` says so in the source: *"Intra-batch dispatch was
ALREADY fully concurrent … what was missing is putting more than one read INTO a batch."*
What that buys is a whole **LLM round-trip**, not milliseconds of dispatch.

**Framing hazard, stated so the report cannot fall into it:** legacy is **not** serial.
`evaluation/results/CV_METRICS.md` carries legacy's map-reduce fan-out ablation as a
`[SAFE]` positive result (retrieval-stage mean −57.1%, p95 −42.0%, negative control 0/48).
"fc parallel vs legacy serial" is a false contrast and will not be written.

### 1a. The two entry points are NOT the same trade, and are recorded separately

Read off the source before the design was frozen (`app/core/agent_loop.py`, symbol names,
not line numbers):

* **Plan time** — `agent_node` calls `_fanout_into_batch` when the model has *already*
  produced tool calls. The added reads are appended to that existing batch. Its own comment:
  *"it adds no LLM call ever — the hop was already happening."* This is the free ride.
* **Answer time** — `_completion_sweep_into_batch` runs when the model produced plain text
  with **no** tool calls. Attaching reads there **opens a batch that would not otherwise
  exist**, costing one more `execute_tools` hop and one more synthesis call. This is not
  free; it buys evidence the answer would otherwise have omitted, promised, or invented.

Collapsing the two would make a claim about the first out of measurements dominated by the
second. Every firing is therefore tagged with its call site (`fanout_sites`,
`fanout_added_plan_time`, `fanout_added_answer_time`).

---

## 2. Arms

Paired, per case, both arms back to back, same reconstructed input, same restored listing-
cache snapshot; arm order alternates by repeat (the runner already does this).

| arm | `FC_DIMENSION_FANOUT_MAX` | meaning |
|---|---|---|
| `fanout_on` | `3` | the product default — 3 = every row in `dimensions.DIMENSION_CUES`, i.e. no cap in practice |
| `fanout_off` | `0` | `_dimension_fanout_calls` returns `[]` at its first line |

Everything else is identical: `--arch fc_loop`, config `routed_models`, model
`deepseek-v4-flash` for every node, warm cache snapshot `warm_v3.sqlite3`.

**The switch was verified against the source and then empirically, before freezing.**
`_dimension_fanout_cap()` reads `os.getenv("FC_DIMENSION_FANOUT_MAX", "3")` on **every
call**, not at import; it has exactly **one** caller, `_dimension_fanout_calls`, whose first
statement is `if cap <= 0: return []`; and the answer-time sweep routes through that same
helper. So `0` disables **both** entry points. Smoke (2026-08-05 19:0x–19:5x BST, 3 cases ×
2 arms): every `fanout_off` run recorded `fanout_cap_observed = 0` and `fanout_fired = 0`;
every `fanout_on` run recorded `cap = 3`. Smoke output is kept at
`evaluation/results/fanout_ab/_smoke/`.

---

## 3. Population — frozen in `case_selection.json`

Source `evaluation/benchmark/cases.jsonl` (98 cases, sha256 recorded in that file).
**Rule:** a case qualifies when its `user_query` cues **≥2 distinct dimensions** under
`core.dimensions.cued_dimensions` — the product's own vocabulary function, not a look-alike.

Selected, **n = 8**: `E1, E3, E5, E6, E7, E9, E10, E11` (all `E_multi_constraint`).

Two wider rules were considered and rejected before freezing; both are recorded with their
reasons in `case_selection.json`. In short: widening to "≥1 cued dimension" (n=40) mostly
adds cases where the cued dimension **is** the case's own expected tool, so the model serves
it directly and the fan-out has nothing to add; and the "cued but not in `expected_tools`"
rule (n=13) is dominated by **negated** mentions ("no commute needed") and memory-recall
turns where the fan-out correctly declines for want of a derivable destination. Both would
have contributed guaranteed zeros to *both* arms.

**Known limitation, stated up front:** the cluster bootstrap resamples **cases**, so n = 8
is the effective resampling unit regardless of repeats. CIs will be wide and the study is
underpowered for small effects. Repeats are set to **12** to shrink *within-case* noise on
the paired discrete counters — not to inflate the bootstrap n. Per GOAL §4.2 this is
reported as a limitation, not fixed by widening the filter after the fact.

Runs: 8 cases × 2 arms × 12 repeats = **192 live runs** (cap 400).

---

## 4. Metrics

Every metric is computed from the per-run JSONL. Every ratio is reported with its
denominator and a bootstrap CI.

### Primary — benefit
**Dimension coverage.** Of the dimensions the message cued, the fraction that ended the turn
with actual tool evidence.

* cued = `core.dimensions.cued_dimensions(message)`, where `message` is the string the
  fan-out helper itself was handed (recorded per run as `dim_message_source =
  fanout_helper`), falling back to the case's `user_query` only if the helper never ran.
* served = complement of `agent_loop._unserved_cued_dimensions(message, artifacts)` — **the
  product's own function**, fed the run's **executed** tools. A look-alike matcher written
  in the harness would make "coverage" mean something other than what it claims (GOAL §3.4).
* Per-run: `dim_covered_n / dim_cued_n`. Aggregated as a paired per-case mean.

**Direction predicted: ON > OFF.**

### Primary — cost
`llm_calls` and `tool_batches`, paired, per run. **Direction predicted: ON ≤ OFF.**
These are discrete counters: far lower variance than latency and immune to network jitter,
which makes them the metrics most likely to reach significance here. *This is stated in
advance so a significant result on them is not read as a fishing expedition.*

### Secondary
End-to-end latency (`ab_wall_ms`, `turn_latency_ms`), `soft_wrapped` rate, `cost_usd`,
`tokens_in` / `tokens_out`.

### Negative controls — reported BEFORE any headline number
* **NC-a — the ON arm's batch count must not increase.** Paired `tool_batches`, ON − OFF.
  The packing claim is that reads ride an existing batch. Reported in two parts, because
  §1a says they are different trades: (i) the strict version as written here over all runs;
  (ii) restricted to runs whose only firings were **plan time**, which is the subset where
  "rides along for free" is actually claimed. If (i) fails, the free-ride framing is
  **refuted at the turn level** and will be reported as such — that is a legitimate result,
  not something to explain away.
* **NC-b — the fan-out must never touch a write or terminal tool.** `remember` and
  `ask_user` must not appear in `fanout_added_tools` in any run. The product asserts this
  (`_dimension_fanout_calls` refuses `side_effect == "write"`, `terminal`, and `ask_user`);
  this verifies it in recorded traces rather than trusting the assertion.
* **NC-c — switch integrity.** Every `fanout_off` run must record
  `fanout_cap_observed == 0` and `fanout_fired == 0`; every `fanout_on` run must record
  `cap == 3`. Any violation invalidates the whole comparison.

---

## 5. Analysis

* **Cluster bootstrap, resampling unit = CASE** (8 clusters), 10,000 resamples,
  `numpy.random.default_rng(20260805)`. Style matches
  `evaluation/results/parallel_tools_ab/analysis_C.json`.
* Paired statistic: per case, mean over repeats of (ON − OFF); the bootstrap resamples cases
  with replacement and recomputes the mean of those per-case differences.
* 95% percentile CI. **A CI crossing 0 is reported as "no significant difference observed"
  — never as "no difference", and never with a directional gloss.**
* Only paired runs where **both** arms succeeded (`ab_ok`) enter the paired analysis; the
  count of dropped pairs is reported.
* Outputs: `analysis_D.json`, `table_D.md`, `negative_control_D.json`, plus `runs.jsonl`.

## 6. Stopping rules

* No new request after **2026-08-06 07:00 BST**; the run carries a `--deadline` well inside
  that. Writing the report takes priority over completing the sweep.
* ≤400 live runs; ≤USD 5 total (Experiment C measured ≈$0.0006/run on E cases, so the
  binding constraint is wall clock, not money).
* 10 consecutive failures in the experiment → abort, record, go to the write-up.
* Per-request timeout 120 s; ≥300 ms between requests; external tool concurrency ≤2.
* Resumable: completed `ab_run_key`s are skipped on restart.

## 7. Declared in advance

* n = 8 cases is small; the CIs will be wide. Reported, not hidden.
* External retrieval (Overpass mirrors, listing scrapers) is flaky and was observed
  sidelining mirrors during smoke. Failures are counted and the affected results are marked
  as dependent on external availability (GOAL §4.3).
* If the premise turns out to be wrong — the switch does not really disable the fan-out, or
  coverage cannot be computed from the recorded fields — that finding is the deliverable and
  is written up as such (GOAL §4.4). The switch has already been verified in smoke; the
  coverage computation has already been demonstrated on smoke runs.
