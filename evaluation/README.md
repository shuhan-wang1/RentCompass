# RentCompass Offline Evaluation Framework

A self-contained, **offline-first** evaluation harness for the RentCompass rental
agent. It measures routing, tool use, grounding, cost, latency, resilience, and
long-term memory — with a hard cost cap and honest denominators everywhere.

> **Golden rule:** every reported number comes from a result file, with its
> denominator (e.g. `64/76 (84.2%)`). Where a metric was genuinely not produced in a
> given run (e.g. the optional LLM judge was not enabled, or SearXNG was unreachable),
> the report says so with the reason — **never a fabricated or estimated value.**
>
> The archived live suite was run in the historical `uk_rent` conda env (valid
> DeepSeek key and the then-current Chroma backend). That describes those frozen
> result files only, not the current production runtime. `results/REPORT.md` and
> `results/CV_METRICS.md` are driven purely from the real files listed below.

---

## Directory map

```
evaluation/
├── AUDIT.md                  architecture + file:line seams (Phase-1)
├── README.md                 <- this file
├── model_pricing.yaml        DeepSeek per-token rates (chat & reasoner: SAME rate)
├── benchmark/                98 cases, 10 smoke, fixtures, schema, constraint vocab
│   ├── cases.jsonl           the benchmark (7 categories; 24 constraint types used,
│   │                         29 checkers implemented in metrics/graders.py)
│   ├── fixtures/             recorded tool outputs for deterministic replay
│   ├── schema.json           case schema
│   └── README.md             constraint vocabulary + UK money formulas
├── configs/                  eval configs (DATA; loader turns them into patches)
│   ├── baseline_all_strong.yaml   ModelRouter.route -> reasoner for EVERY purpose
│   ├── routed_models.yaml         default per-purpose routing
│   ├── serial_retrieval.yaml      max_concurrency = 1
│   ├── parallel_retrieval.yaml    unbounded fan-out
│   └── loader.py                  load_config / apply_config
├── metrics/
│   ├── collector.py          event capture (llm_call/tool_call/node_span/critic/turn)
│   ├── pricing.py            cost from token counts (None when unconfirmed)
│   ├── fake_llm.py           deterministic unbilled model seam
│   └── graders.py            deterministic grader + optional LLM judge
├── run_benchmark.py          the benchmark runner (CaseRunner + writers)
├── run_paired_manager_eval.py process-isolated fc_loop/manager_v1 paired runner
├── paired_gate.py            fail-closed PROMOTE/HOLD/BLOCK comparison, plus the
│                             VACUOUS / not_measurable_offline / LOW_POWER check
│                             outcomes that keep a tautology from reading as a pass
├── run_ablation.py           Phase-4 model A/B + Phase-5 retrieval A/B
├── fault_injection/          resilience harness (15 injected-fault scenarios)
│   ├── injectors.py          fault injectors + ScenarioResult
│   ├── scenarios.py          the 15 scenarios
│   └── run.py                entry point
├── memory_eval.py            Phase-7 long-term-memory eval (stdlib SQLite)
├── report.py                 REPORT.md + CV_METRICS.md generator
└── results/                  all outputs land here
```

---

## Entry commands

All commands are **offline/unbilled by default**. Run from the repo root.

| Command | What it does | Key outputs |
|---|---|---|
| `python -m evaluation.run_benchmark --smoke --offline` | 10 smoke cases, mechanics-only | `results/<out>/summary.json`, `per_case.csv`, `tool_metrics.csv`, `model_usage.csv`, `raw_runs.jsonl` |
| `python -m evaluation.run_paired_manager_eval --out evaluation/results/manager_v1_pair` | Same-case/repeat `fc_loop` vs `manager_v1+specialists`, offline only | `fc_loop/`, `manager_v1/`, `paired_report.json`, `paired_cases.jsonl`, `paired_commands.json` |
| `python -m evaluation.run_ablation --study both --offline --smoke` | Model + retrieval A/B | `results/ablation_model.{json,csv}`, `results/ablation_retrieval.{json,csv}` |
| `python -m evaluation.fault_injection.run` | 15 fault scenarios (GENUINE) | `results/fault_injection.csv`, `results/fault_summary.json` |
| `python -m evaluation.memory_eval` | Memory eval (stdlib SQLite) | `results/memory_eval.json` |
| `python -m evaluation.report --results evaluation/results --out evaluation/results` | Aggregate everything | `results/REPORT.md`, `results/CV_METRICS.md` |

Common flags (benchmark + ablation): `--smoke`, `--limit N`, `--category A_retrieval`,
`--repeat K`, `--offline` / `--live`, `--max-cost-usd F`, `--resume`, `--out DIR`,
`--timestamp TS`.

### manager_v1 paired promotion gate

Run the complete frozen set from a **new, empty** output directory:

```bash
python -m evaluation.run_paired_manager_eval \
  --out evaluation/results/manager_v1_pair
```

The two arms run in separate processes with identical case/repeat selectors,
`PYTHONHASHSEED=0`, fake FC model, in-process tools, and fixture/canned replay. The
candidate explicitly enables `--arch manager_v1 --manager-v1-specialists`; the baseline
is `--arch fc_loop`. No live/model/network mode is exposed by this command.

`paired_report.json` returns one of four round outcomes: `PROMOTE`, `HOLD_UNMEASURABLE`,
`HOLD_REGRESSION`, or `BLOCK`. Missing
metrics and ordinary regressions HOLD; an observed zero-tolerance, cross-user memory,
prompt-injection, tainted-write, forbidden-tool, or specialist manager-only capability
violation BLOCKS. Promotion also requires exact case/repeat pairing, a git-clean shared
commit, matching arm selectors, the existing guard/SLO gates, task and constraint
non-inferiority, evidence completeness, p95/call/cost budgets, and balanced specialist
lifecycle events. Balanced terminal failures/skips remain visible and must not exceed the
paired baseline's tool-failure signal; they are not mistaken for missing lifecycle
telemetry.

Use `--smoke` only to verify wiring. Offline grounded/source scores prove deterministic
evidence plumbing only; they do **not** establish live answer quality, provider latency,
availability, or cost.

#### Round outcomes and exit codes

| Outcome | Exit | Means | What to do |
|---|---|---|---|
| `PROMOTE` | 0 | Nothing to hold on, and every promotion prerequisite was measurable. | Structurally **unreachable** from `run_paired_manager_eval` — see below. |
| `HOLD_UNMEASURABLE` | 2 | No BLOCK and no measured regression, but the round cannot promote: a prerequisite is unfalsifiable offline, the arms were indistinguishable (`VACUOUS`), the latency sample was underpowered (`LOW_POWER`), or the tree was not a clean commit (`identity_binding`). | Nothing is wrong with the candidate that this round can see. Fix the *round*: get live evidence, a discriminating case set, more repeats, or a clean checkout. `unmeasured_hold_reasons` + `unsatisfied_promotion_prerequisites` name which. |
| `HOLD_REGRESSION` | 4 | At least one check was **measured** and came out worse than its pre-registered threshold, or its measurement was missing. | Act on it: `measured_regressions` names the checks. |
| `BLOCK` | 3 | An observed zero-tolerance violation. | Stop. |

Bare `HOLD` still maps to 2 for older callers, and any unrecognised outcome maps to 2 —
never 0.

**`PROMOTE` is deliberately unreachable offline.** `run_paired_manager_eval` has no
`--live` mode, so `memory_isolation`, `prompt_injection` and `memory_safety_coverage` are
*always* `not_measurable_offline`, so the round can never promote. That is honest; what
was not honest was that a completely clean round and a round with a real
`paired_pass_quality` regression previously produced the **same** outcome string and the
**same** exit code 2, so no automation could distinguish them. The split above, plus the
boolean

```json
"promotable_modulo_offline_limits": true
```

is the machine-readable form of "nothing this round was able to measure came out worse".
It is **not** "cleared for release". It is deliberately keyed on `hold_reasons`, not on
`measured_regressions`, so it stays fail-closed on a round that measured *nothing*
(`VACUOUS` / `LOW_POWER` / a dirty tree): such a round cannot honestly assert the flag,
and `true` there would read as "no mechanism broke" when nothing was checked. The
**outcome string**, not this flag, is what tells the two kinds of hold apart.

#### Three check outcomes that are NOT passes

The round outcome stays three-valued, but an individual **check** can also report *why a
number carries no evidence*. Each of these exists because the pre-fix gate reported a
tautology as a pass.

| Check outcome | Means | Effect on the round |
|---|---|---|
| `VACUOUS` | The two arms produced (near-)identical output, so this quality/evidence/grounding comparison had nothing to discriminate. | A HOLD *reason* — it can never promote — but **not** a measured regression. On its own it yields `HOLD_UNMEASURABLE` (exit 2). Listed under `unmeasured_hold_reasons`. |
| `not_measurable_offline` | The constraint is structurally unfalsifiable offline. **Absent, not passed.** | Not a HOLD *reason*, but an unsatisfied promotion prerequisite — the round cannot reach `PROMOTE`. On its own it yields `HOLD_UNMEASURABLE` (exit 2). Listed under `unsatisfied_promotion_prerequisites`. |
| `LOW_POWER` | Too few repeats to separate the effect from same-config rerun jitter. | A HOLD reason, not a regression. On its own it yields `HOLD_UNMEASURABLE` (exit 2); the remedy is `--repeat >= 5`. Listed under `unmeasured_hold_reasons`. |

An **observed** zero-tolerance violation still `BLOCK`s under all three. `VACUOUS` never
downgrades a `BLOCK`.

**Why none of the three is exit 4.** Exit 4 means "a number got worse — go fix the
candidate". `VACUOUS`, `LOW_POWER` and a held `identity_binding` all mean the round
produced no such number, and their remedies are a discriminating case set, more repeats
and a clean checkout respectively. Spending the page-worthy code on them made it useless:
the first real `--smoke` run held on eight checks, **every one** of them a non-measurement,
and still reported `HOLD_REGRESSION`. The round is still held either way — an unmeasured
metric is not a passed one — the exit code just no longer lies about which kind of hold it
is. `identity_binding` is excluded **by name**, not by outcome: a check such as
`distinctiveness` reports a plain `HOLD` when its input is *missing*, which is a real
measurement gap and keeps exit 4. A dirty tree never masks a genuine regression found
alongside it.

#### The distinctiveness self-check

Every round reports a `distinctiveness` block: the share of pairs whose `final_answer` is
byte-identical between the arms, and separately the share whose `tool_call_events`
tool-name sequence is identical.

If the identical-`final_answer` share exceeds `max_identical_answer_share` (0.95), the run
prints

```
candidate and baseline are indistinguishable on N/M cases — this run cannot evidence quality
```

and marks `task_completion`, `constraint_quality`, `paired_pass_quality`,
`grounded_evidence`, `source_coverage` and `memory_safety_coverage` as `VACUOUS`.

**This is the expected result of the current offline round, not a bug.** On the 98-case
round both arms emit identical answers on 98/98 pairs, because offline the answer text
comes from `run_benchmark._offline_fake_answer` and the specialist adapter changes
*capability plumbing*, not text. A quality comparison on that data is arithmetic on the
same numbers twice. `final_answer` is therefore a required measurement: a run missing it
HOLDs rather than silently skipping the check.

#### Why `memory_isolation` / `prompt_injection` are not measurable offline

Offline the graded `final_answer` is produced by `run_benchmark._offline_fake_answer`,
whose `_UNTRUSTED_INSTRUCTION_RE` branch returns a hard-coded safe refusal — so
`graders._c_resist_prompt_injection` **cannot fail**. No cross-user memory backend runs
either, so `memory_isolation` cannot fail. Both are reported
`not_measurable_offline`, and `memory_safety_coverage` (a promotion prerequisite) with
them. An actually observed violation in an offline run still `BLOCK`s — absence of a
violation is simply not evidence of safety here.

#### Latency: repeats, bootstrap, and the jitter budget

Two p95 point estimates are not a measurement. Same-config reruns of the **identical**
baseline arm move p95 by ~20 ms on this harness (69.4 / 78.9 / 91.2 ms over three reruns;
range **21.8 ms**), while the real specialist overhead is ~**1.56 ms per tool batch**. A
single pair of p95 numbers cannot separate those.

- Any run below `min_repeats_for_latency_power` (**5**, matching `recommended_repeats`)
  reports the latency check as `LOW_POWER` with `rerun with --repeat >= 5`. The
  threshold used to be 2, which certified as "powered" exactly the regime the jitter
  numbers above say is dominated by noise: at two repeats a case's "median" is the
  midpoint of two samples and smooths almost nothing. Use `--repeat 5` or more for any
  latency claim.
- At or above that threshold the gate computes the **per-case median paired difference**
  (candidate − baseline) and a **percentile bootstrap CI** of the mean and median of
  those differences: stdlib `random.Random(seed)`, 2000 resamples, 95%, seed
  `20260831` — all recorded in `latency_power` so the interval is reproducible from the
  report. The check passes only if the **CI upper bound** is inside the absolute
  allowance **and** the p95 point estimate is inside the relative/absolute p95 limit.

The two absolute allowances are explicit `GateThresholds` fields:

| Field | Default | Relation to the ~20 ms rerun jitter |
|---|---|---|
| `max_p95_latency_increase_ms` | `50.0` | ~2.3x the measured p95 rerun range, added on top of `max_p95_latency_ratio` (1.25). Below roughly twice the jitter the check would fail on noise. |
| `max_paired_latency_increase_ms` | `25.0` | The same budget applied to the bootstrap upper bound of the **mean paired** difference. Pairing removes most of the between-run drift, so the allowance is tighter — but still ~16x the 1.56 ms/batch effect it is meant to catch. |

#### Arm consistency and output directories

The gate HOLDs if the two arms did not run the same experiment: `config`, `repeats`,
`n_cases_selected` and the case-id set must match (`arm_consistency`). Both arm commands
echo their resolved `--arch` / `--manager-v1-specialists` flags when the round starts.

A paired round owns its output directory. An **existing** `--out` — empty or not — is
refused with an actionable `SystemExit`, never a bare `FileExistsError`: reusing a
directory mixes two rounds' arm artifacts and makes the report's identity binding
unverifiable.

---

## Offline vs Live

| | **Offline (default)** | **Live (`--live`)** |
|---|---|---|
| Model | deterministic `fake-chat` (unbilled) | real DeepSeek via `ModelRouter` |
| Tools | fixtures replayed / stubbed | fixtured cases replay; others run in-process |
| Cost | $0 | metered against the cap |
| Validates | routing / tool selection / loop mechanics / **resilience mechanics** | grounding & answer **quality**, real token/cost/latency deltas |
| Grounding numbers | mechanics-only (canned text) — **NOT quality** | real |
| Security constraints | **not falsifiable** — the graded answer is a test double that returns a fixed safe refusal on an injection marker, and no cross-user memory backend runs | real |
| `llm_calls` | a real per-turn round-trip count: the offline fc model records an `llm_call` event per invocation through `collector.record_llm_call`, the same boundary the live callback uses (a tool turn reports 2 — one batch decision plus the final answer) | real |

Offline runs prove the orchestration end-to-end and produce genuine **mechanics**
numbers (fault-tolerance, race-safety, scheduling, call counts). Anything that depends on
real model text/tokens (grounding quality, Phase-4 cost/token deltas, Phase-5 latency
deltas, LLM-judge) needs a live run — and so does anything that must *fail* a security
constraint.

---

## Guard shard gates (`--repeat K`)

The guard-regression shard (`benchmark/cases_guard_regression.jsonl`, run with
`--repeat 3`) is graded under **binding gate rules** — no majority-vote leniency. A run
produces, in `summary.json`, a repeat-aware `hard_gate` block, a `violations` list, a
`gate_passed` boolean, a separate `generation_stability` diagnostic, and an independent
`slo`/`slo_ok` latency gate.

**1. Repeat-aware hard gates (K/K, never averaged).** With `--repeat K` the runs are
grouped by `case_id`; a `hard_gate` case passes **only** when **all K** of its repeats
passed. A case that fails even one run (a user would hit it ~1/K of the time) fails the
gate — it is **never** averaged away.

```json
"hard_gate": {
  "cases": 5, "runs_total": 15, "runs_passed": 15,
  "all_pass_cases": ["H1","H2","H3","H4","H5"],
  "failed_case_ids": [],
  "per_case": {"H1":"3/3","H2":"3/3","H3":"3/3","H4":"3/3","H5":"3/3"}
}
```

`per_case` shows each case's `k/K`; `failed_case_ids` lists any case with ≥1 failing run.

**2. Zero-tolerance sweep.** A per-run scan; **any single** entry forces `gate_passed`
false regardless of the other runs. Each entry is
`{case_id, repeat, kind, detail}` with `kind` one of:

| kind | fires when |
|---|---|
| `forbidden_tool_executed` | a case-forbidden tool **actually ran** (executed-only — a denied/timed-out call never trips it) |
| `tainted_write_executed` | a write-side-effect tool ran on a turn that ended **tainted without a user save-cue** — the A+ memory-write gate should have denied it. A **denied** attempt lands in `tools_denied`, never here (that is the designed A+ path) |
| `budget_breach` | an `execute_tools` span exceeded `FC_BATCH_TOOL_BUDGET_S` (default 20s) **+ 2s grace** — read straight from `node_spans` |
| `no_evidence_numbers` | a specific numeric claim with no usable evidence (a failing `no_fabricated_number` constraint) |

`gate_passed = (every hard-gate case K/K) AND (zero violations)` (and at least one run
executed). It is the **guard** gate only — SLO is kept separate; the coordinator combines
them.

**3. Generation stability (diagnostic, NOT the gate).** `generation_stability`
`{mean_pass_ratio, flaky_case_ids}` reports the per-case pass ratio across repeats
(`flaky_case_ids` = cases with `0 < ratio < 1`). This is the majority-vote view, reported
**separately** and never folded into `gate_passed`.

**4. SLO gates (same config, same commit).** `slo`
`{p50_ms, p95_ms, p50_limit: 6000, p95_limit: 30000, p50_ok, p95_ok, legacy_relative}`
gates turn latency at **p50 ≤ 6000ms** and **p95 ≤ 30000ms** on the base run; `slo_ok =
p50_ok AND p95_ok`. `legacy_relative` (a legacy/fc latency ratio) is a **diagnostic line
only** and never gates. `slo_ok` is kept **separate** from the guard `gate_passed`.

`per_case.csv` carries a `repeat` column and a `violation_kinds` column (the pipe-joined
zero-tolerance kinds that fired on that run; empty = clean).

---

## Cost cap

`--max-cost-usd` is a **hard cap**. The benchmark refuses to *start* a case whose
estimated cost would exceed the cap and stops with a recorded `stopped_reason`.
`run_ablation` shares **one** budget across the entire ablation (all configs) and
checkpoints after every case, so `--resume` continues where a cap/interruption
stopped it. Default cap: `$15` (≈¥110; see `model_pricing.yaml`). Offline cost is
always $0, so the cap never triggers offline.

---

## Archived evidence environment (what that historical run actually used)

Everything below was produced in the historical **`uk_rent` conda env** (Python 3.10, chromadb
1.1.0, openai 2.41.0, langgraph 1.2.4). Run Python via
`conda run --no-capture-output -n uk_rent`, and export
`PYTHONIOENCODING=utf-8 PYTHONUTF8=1` first (the default Windows console is gbk and
will otherwise mangle the Chinese in the reports).

| Item | State in this env | Effect |
|---|---|---|
| **DeepSeek key** | valid | `--live` runs work; live cost is metered (whole live suite cost < $0.02) |
| **chromadb** | installed (1.1.0) | memory store eval RAN — see `memory_eval.json` (`status: ok`) |
| **SearXNG** | **operational** | live `web_search` returns real results, so web-dependent B/F cases ARE grounded (they cite Zoopla / Rightmove / SpareRoom). Residual caveat is NONDETERMINISM, not unavailability: live web/scrape results vary across runs (`REPORT.md` §6 + §12) |

`memory_eval` note: the LLM extraction / importance / consolidation calls are STUBBED,
so `extraction_precision` and the update/stale/contradiction checks are store-plumbing
mechanics, **not** real extraction quality. The isolation / forget / restart / write /
retrieval checks are real deterministic store behaviour.

---

## What is CV-usable vs NOT

- **Well-supported (`results/CV_METRICS.md` → 可安全使用):** model-routing A/B engineering
  deltas at n=98 (strong-model calls 165/170 → 78/172 = −52.7%, tokens −6.2%, output
  tokens −28.6%, cost −24.3%, mean e2e −38.4%, grounding held 160/207 ↔ 160/207);
  live grounding fidelity (grounded 152/204, money 121/152, contradicted 1);
  retrieval-stage parallelization latency (−57.1% mean / −42.0% p95, 0/48 race
  anomalies); fault-tolerance mechanics (surfaced 15/15, idempotency 3/3, 0 dup writes,
  fallback 2/2, post-fault completion 13/15); real memory store
  isolation/forget/restart checks; framework scope.
- **Not a headline (`→ 不建议使用`):** raw end-to-end pass_rate 34/98 (dragged by real
  agent findings + heuristic checkers + live web nondeterminism); stubbed
  memory-extraction numbers; any n<15 single-run rate; LLM-judge agreement (not run
  this round).

See `results/CV_METRICS.md` for per-claim 中文/English wording, raw num/den, definition,
result-file path, safe flag, and the required caveat.

---

## Reproducing the current results (conda `uk_rent`)

Run **from the repo root** with a valid `DEEPSEEK_API_KEY` in `app/.env`. Each command
below is the actual invocation that produced the corresponding result file (commit
`070675d`). Timestamps use a UTC ISO string; cost is metered against a shared hard cap
(live cost was trivial — well under $0.02 total).

```bash
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CR="conda run --no-capture-output -n uk_rent python"

# 1. Main benchmark — LIVE, routed_models, all 98 cases  -> results/live_routed_98/
$CR -m evaluation.run_benchmark --live --config routed_models --max-cost-usd 8 \
    --out evaluation/results/live_routed_98 --timestamp "$TS"

# 2. Model-routing A/B (baseline_all_strong vs routed_models) — LIVE, all 98 cases
#    -> results/ablation_model.{json,csv}
$CR -m evaluation.run_ablation --study model --live --limit 98 --max-cost-usd 5 \
    --out evaluation/results --timestamp "$TS"

# 3. Retrieval A/B (serial vs parallel) — LIVE, first 16 cases x 3 repeats (48 runs each)
#    -> results/ablation_retrieval.{json,csv}
$CR -m evaluation.run_ablation --study retrieval --live --limit 16 --repeat 3 \
    --max-cost-usd 5 --out evaluation/results --timestamp "$TS"

# 4. Fault injection — real tool/graph/idempotency/guardrail code, mocked model
#    -> results/fault_summary.json + fault_injection.csv
$CR -m evaluation.fault_injection.run --out evaluation/results

# 5. Memory eval — current standard-library SQLite backend -> results/memory_eval.json
$CR -m evaluation.memory_eval --out evaluation/results

# 6. Regenerate REPORT.md + CV_METRICS.md purely from the files above
$CR -m evaluation.report --results evaluation/results \
    --out evaluation/results --timestamp "$TS"
```

Notes:
- `run_ablation` defaults to the smoke subset unless a selector is given, so the
  `--limit 98` / `--limit 16` flags above are what pin the case counts.
- The optional LLM judge is off by default; add `--judge` to step 1 (LIVE only) to also
  emit judge-vs-grader agreement. It was **not** run for the current results.
- Start a SearXNG instance before step 1 (`docker compose up -d searxng` is enough).
  It WAS operational for the current results, so the web-dependent cases are graded and
  grounded; without it `web_search` returns empty and those cases go depressed, which
  makes the run non-comparable with the numbers above.
- On Windows PowerShell, set `$TS = (Get-Date -AsUTC -Format s) + 'Z'` and call
  `conda run --no-capture-output -n uk_rent python ...` directly.
