# FANOUT A/B REPORT — 2026-08-05

Unattended run against `GOAL_UNATTENDED_FANOUT_20260805.md`.
Branch `experiment/fanout-ab-20260805`, forked from `778714a` (`origin/main`, clean tree).
Nothing pushed; `main` untouched; no deploy, no pool switch, no production restart.

Timeline: `PROGRESS.log`, session block starting `2026-08-05 18:41 BST`.

**Every number below carries the file it came from.** Where a claim rests on source code it
cites the **symbol**, never a line number.

---

## Phase 1 — two deterministic fixes

Both were done red → change → green, and neither adds a failure to the full suite.

### Baseline and final suite state

| | failed | passed | skipped |
|---|---|---|---|
| baseline (`778714a`, per the GOAL) | 38 | 3347 | — |
| after both fixes | **29** | **3364** | 3 |

Command: `CANARY_LOG_PATH=<writable tmp> pytest tests/ -q -p no:randomly`.
Source: `PROGRESS.log` 19:20 BST entry.

The arithmetic reconciles exactly and shows **no new failure**: 3385 baseline tests + 8 new
tests = 3393 = 3364 + 29. Passed rose by 17 = 8 new + 9 previously-failing now green.

Two remaining failures looked suspicious enough to check individually rather than assume
(`tests/test_dimension_fanout.py::test_sweep_does_not_fire_when_the_turn_tool_budget_is_spent`
and `tests/test_tool_budgets.py::test_retuned_defaults`). Running the same files against
`778714a`'s `app/` and against the fixed `app/` gives **identical** results — they are the
pre-existing cross-test env leak the GOAL's baseline already accounts for, not fallout.

The `CANARY_LOG_PATH` trap in the GOAL is real and was honoured throughout: `.runtime/logs/`
is root-owned, and without the redirect a large block of tests fails on `PermissionError`.

---

### Fix A — `is_loop_synthesis` bypassed the structured-contract mount

**Symptom** (`evaluation/results/holdout_v6_live/raw_runs.jsonl`): of 180 recorded runs,
57 carry an eight-key `tool_data`, 120 carry none, and exactly **3** carry a three-key
subset — `HO6-198`, `HO6-208`, `HO6-238`. All three are `E_multi_constraint`. All three
therefore scored zero on the three structured-contract metrics.

**It is not the model skipping tools.** From the same file: `HO6-198` executed
`calculate_commute` **8** times, `HO6-208` **7** times. The evidence was fetched; the
formatter dropped it.

**Root cause, confirmed against source, not against a summary.** In
`app/core/langgraph_agent.py::_make_format_output_node`:

1. `is_loop_synthesis` is a plain `if`, so when it is true the whole
   `elif tool_name == 'search_properties'` chain below it is skipped — no
   `validate_search_payload`, no structured fields.
2. Inside it, the post-search dimension fan-out sub-branch (guarded on
   `plan_origin == _PLAN_ORIGIN_DIMENSIONS`) hand-built a three-key dict:
   `recommendations`, `search_criteria`, `area_recommendations`.
3. **And it runs *after* the artifact-ledger recovery**, which had already built the correct
   eight-key payload from `tool_artifacts` — so the subset did not merely fail to mount the
   contract, it *overwrote* a correct one.

Two facts were verified before changing anything:

* `evaluation/results/holdout_v6_live/manifest.json` records `"arch": "legacy"`, so
  `langgraph_agent.py` — not `agent_loop.py` — is the module that produced these runs.
* A repo-wide grep confirms this was the only producer of that three-key shape under `app/`;
  fc_loop's `agent_loop.py::format_output_fc_node` mounts all eight.

**Fix.** Extracted `langgraph_agent::_structured_search_tool_data(payload, prefs,
commute_evidence)` and routed **all three** listings paths through it — the ledger recovery,
the dimension fan-out, and the plain `search_properties` branch. One contract mount instead
of three copies that can drift. The prose stays the model's synthesis; only `tool_data`
changes.

**Regression test** — `tests/test_holdout_v6_dimension_contract_mount.py`, 8 tests. It is
built from primary sources only: the archived runs supply the symptom and the case list, the
frozen fixtures (`evaluation/benchmark/holdout_v6/fixtures/ho6_<n>_search.json`) supply the
listings and the commute records, and the expected eight-key contract is read **off the 57
runs that got it** rather than hard-coded from memory. No live call is needed.

Red → green, demonstrated by reverting `app/core/langgraph_agent.py` to `778714a` and back:
**5 failed / 3 passed** before, **8 passed** after.

> **Premise correction (GOAL §4.4, recorded rather than smoothed over).** The GOAL's
> acceptance criterion was "eight keys + non-empty `commute_evidence`" for all three cases.
> That holds for `HO6-198` and `HO6-208` only. `HO6-238`'s `user_query` carries **no commute
> constraint** — its `hard_constraint_slots` has no `commute` entry and its fixture contains a
> search record only — so an empty `commute_evidence` is the *correct* result there. The same
> is true of 29 of the 57 already-passing eight-key runs. The test asserts the eight-key shape
> for all three and non-empty evidence for the two commute-constrained cases.

---

### Fix B — `standalone_rent_conversion` intercepted B12

**Spec** (the two red tests named in the GOAL):
`tests/test_tool_policy_dispatch.py::test_b12_search_properties_never_dispatches` and
`tests/test_deposit_boundary.py::test_b12_still_reaches_the_model`. Both were confirmed
failing before any edit.

B12 is *"I'm looking at a £380/week studio. What'll it cost me all-in per month, including
bills and council tax?"* — `core.tool_policy.standalone_rent_conversion` matched it and
short-circuited the whole turn deterministically. A weekly→monthly conversion cannot answer a
bills-inclusive question, so the user would have received the rent conversion presented as the
answer to a question about total cost.

**Fix.** Narrowed by reusing the module's **existing** `_NON_DERIVABLE_COST_RE` (bills,
council tax, all-in, utilities, service charge, 账单, 市政税, …) — the same gate
`statutory_money_answer` already applies. One definition of "not derivable from a rent", not
two. The ÷4.33 fix itself is untouched and
`tests/test_agent_contract_guards.py::test_weekly_to_monthly_uses_52_over_12_and_penny_rounding`
stays green.

**Blast radius, measured rather than asserted.** Both versions of the module were imported
side by side and every case's verdict compared:

| corpus | intercepted before | intercepted after | verdicts changed |
|---|---|---|---|
| `evaluation/benchmark/cases.jsonl` (98) | 2 | 1 | **1 — B12, the target** |
| `evaluation/benchmark/holdout_v6/cases_holdout_v6.jsonl` (180) | 30 | 30 | **0** |

Source: `PROGRESS.log` 19:20 BST entry.

#### Recommended supplementary cases for `evaluation/benchmark/holdout_v6/`

**Advisory only — the frozen dataset was not modified.**

Measured from the primary source: all **30/30** `B_money` cases in
`cases_holdout_v6.jsonl` collapse to **exactly one** template once the amounts are masked —

> `Using the specified conversion weekly × N ÷ N, calculate the monthly GBP equivalent of £N per week.`

That template *is* this interceptor's positive class. v6 therefore tests only that the
interceptor fires when it should, and structurally cannot detect the B12 failure mode: a
correct conversion **embedded in a question the conversion does not answer**. This is why a
180-case held-out set scored the interceptor as healthy while it was silently taking over
B12-shaped turns.

Suggested additions (negative and boundary classes, not more of the positive one):

1. **Conversion + non-derivable cost.** B12's shape: a stated weekly rent plus bills /
   council tax / "all-in". Expected: the model answers, converts the rent, and explicitly
   refuses to invent the rest. This is the class with **zero** coverage today.
2. **Conversion + a second amount.** A weekly rent plus a stated holding deposit or agency
   fee, where picking the wrong figure to price is a silent wrong answer.
3. **Conversion + a beyond-arithmetic ask.** "…and how do I get the deposit back?" — the
   deterministic template answers none of it.
4. **Negated / hypothetical conversion.** "I don't need the monthly figure, just tell me
   whether £380/week is normal for a studio in Zone 2." The conversion cue is present, the
   conversion is not the ask.
5. **Genuine pure conversions with varied surface form** — imperative, question, Chinese,
   embedded in a sentence — so the positive class stops being one template repeated 30 times.
6. **Month→week direction.** The current 30 are all week→month; the other branch of
   `standalone_rent_conversion` is untested by v6.

---

## Phase 2 — Experiment D: dimension fan-out / batch packing

Preregistration: `evaluation/results/fanout_ab/PREREGISTRATION.md` (frozen before the first
measurement run). Case selection: `evaluation/results/fanout_ab/case_selection.json`.
Raw runs: `evaluation/results/fanout_ab/main/runs.jsonl`. Analysis:
`evaluation/results/fanout_ab/{analysis_D.json, table_D.md, negative_control_D.json}`.

### Why this experiment, and the framing hazard

Experiment C's question — does fc_loop's **intra-batch concurrency** beat serial dispatch —
is a settled negative and was **not** re-run. From
`evaluation/results/parallel_tools_ab/analysis_C.json` and `negative_control_C.json`: the
retrieval-stage CI crossed zero in both the warm and the cold round, and the negative control
**failed** (16/60 warm and 18/60 cold paired runs executed a different number of tools —
serialising changed the timing, which changed what the model planned next). The reason it was
empty is in `evaluation/results/EVAL_REPORT_20260804.md` §3 and
`evaluation/results/parallel_tools_ab/case_selection.json`: only **65 of 244** tool batches
(26.6%) held ≥2 calls, and with a batch of one, intra-batch concurrency is worth exactly zero.

The mechanism fc_loop actually has and legacy does not is **batch packing** —
`agent_loop::_dimension_fanout_calls` and `agent_loop::_completion_sweep_into_batch` put a
read the user *cued* but the model did not request into a batch. What that buys is a whole
LLM round-trip.

**Framing hazard, stated so this report does not fall into it: legacy is not serial.**
`evaluation/results/CV_METRICS.md` carries legacy's map-reduce fan-out ablation as a `[SAFE]`
positive result (retrieval-stage mean −57.1%, p95 −42.0%, negative control 0/48). "fc parallel
vs legacy serial" is a false contrast and is not claimed anywhere here.

### The switch, verified before it was relied on

`FC_DIMENSION_FANOUT_MAX`, read by `agent_loop::_dimension_fanout_cap`. Confirmed at the
source: it calls `os.getenv` on **every** invocation (not at import), it has exactly **one**
caller — `_dimension_fanout_calls`, whose first statement is `if cap <= 0: return []` — and
the answer-time sweep routes through that same helper. So `0` disables **both** entry points.
Then confirmed empirically in smoke (`evaluation/results/fanout_ab/_smoke/runs.jsonl`): every
`fanout_off` run recorded `cap = 0` with zero firings; every `fanout_on` run recorded `cap = 3`.

### The two entry points are different trades and are recorded separately

Read off `app/core/agent_loop.py` before the design was frozen:

* **Plan time** (`agent_node`): the model has already produced tool calls; the added reads are
  appended to that existing batch. Its own comment says *"it adds no LLM call ever — the hop
  was already happening."* This is the free ride.
* **Answer time** (`_completion_sweep_into_batch`): the model produced plain text with **no**
  tool calls, so attaching reads **opens a batch that would not otherwise exist**. It costs an
  extra `execute_tools` hop plus a synthesis call, and buys evidence the answer would otherwise
  have omitted, promised, or invented.

Collapsing the two would let a claim about the first be supported by measurements dominated by
the second. Every firing is therefore tagged with its call site.

### Method

* Harness: the **existing** `evaluation/results/_harness/ab_runner.py`, extended in place with
  `--fanout-max` / `--fanout-max-arm` and the arm names `fanout_on` / `fanout_off`, following
  the `--serial-tools-arm` pattern already there. The cap is set **in process, per arm**.
  **Nothing under `app/` was changed for the experiment.**
* Arms: `fanout_on` (`FC_DIMENSION_FANOUT_MAX=3`, the product default) vs `fanout_off` (`=0`).
  Everything else identical: `--arch fc_loop`, config `routed_models`, `deepseek-v4-flash`
  at every node, warm cache snapshot `warm_v3.sqlite3`, arm order alternating by repeat.
* Population: cases from `evaluation/benchmark/cases.jsonl` whose `user_query` cues ≥2
  distinct dimensions under **the product's own** `core.dimensions.cued_dimensions`.
  **n = 8**: `E1, E3, E5, E6, E7, E9, E10, E11`. 12 repeats × 2 arms = **192 planned runs**.
* Dimension coverage reuses the product's own functions — `core.dimensions.cued_dimensions`
  and `agent_loop::_unserved_cued_dimensions` — never a look-alike matcher, so the metric is
  the thing it claims to be.
* CIs: cluster bootstrap with the **case** as the resampling unit, 10,000 resamples, seed
  `20260805`, 95% percentile.

### Execution

192/192 runs succeeded, 0 failures, **96 usable pairs** over 8 cases, 0 pairs dropped.
Ran 19:09:29 → 20:06:06 BST. Total cost **USD 0.1086** against a USD 5 cap.
Source: `evaluation/results/fanout_ab/analysis_D.json`.

### Negative controls — reported before any headline number

All three **pass**. Source: `evaluation/results/fanout_ab/negative_control_D.json`.

| control | statement | result |
|---|---|---|
| **NC-c** switch integrity | every OFF run at `cap == 0` with zero firings; every ON run at `cap == 3` | **PASS** — 96/96 and 96/96, no violations |
| **NC-b** read tools only | `remember` and `ask_user` must never appear in `fanout_added_tools` | **PASS** — 179 additions (`search_nearby_pois` 72, `calculate_commute` 60, `check_safety` 47), **0** violations |
| **NC-a** no extra batches | paired `tool_batches` (ON − OFF) must not be significantly positive | **PASS** — −0.281, CI [−0.583, −0.031] over 96 pairs |

NC-a did not merely fail to increase: the ON arm used **significantly fewer** batches.
Distribution over the 96 pairs: 27 where ON used fewer, 62 equal, 7 where ON used more.
Restricted to the 72 plan-time-only pairs the effect is larger, −0.389, CI [−0.764, −0.069]
(6 cases).

### Primary results

Difference convention **ON − OFF**; cluster bootstrap over cases, 10,000 resamples, seed
`20260805`, 95% percentile CI. Source: `evaluation/results/fanout_ab/table_D.md`.

| metric | ON | OFF | paired diff | 95% CI | verdict |
|---|---|---|---|---|---|
| **dimension coverage** | **204/252 = 81.0%** | **138/252 = 54.8%** | **+0.229** | [+0.038, +0.441] | ON higher |
| runs at full coverage | 72/96 | 34/96 | — | — | — |
| **`llm_calls`** (mean) | **2.54** | **2.99** | **−0.45** | [−0.94, −0.09] | ON lower |
| **`tool_batches`** (mean) | **1.28** | **1.56** | **−0.28** | [−0.58, −0.03] | ON lower |
| tools executed (mean) | 3.78 | 2.67 | +1.11 | [+0.15, +2.30] | ON higher |

**Both halves of the two-sided claim hold on this population.** Coverage went up while
`llm_calls` and `tool_batches` went *down* — the added reads did not buy evidence with an
extra round-trip, they rode a batch that was already being dispatched, and the turn then
needed fewer follow-up hops. As preregistered, the discrete counters are where significance
was expected: they are low-variance and immune to network jitter, and that is where it landed.

### Secondary results

| metric | ON | OFF | paired diff | 95% CI | verdict |
|---|---|---|---|---|---|
| e2e wall ms (mean diff) | p50 9,159 / p95 51,752 | p50 8,093 / p95 66,031 | −5,819 | [−15,248, −564] | ON lower |
| `cost_usd` | 0.0545 total | 0.0541 total | +0.0000 | [−0.0001, +0.0001] | **no significant difference observed** |
| `tokens_in` | 2,614,421 | 3,015,657 | −4,180 | [−11,135, +1,028] | **no significant difference observed** |
| `tokens_out` | 73,831 | 77,374 | −36.9 | [−155.9, +65.7] | **no significant difference observed** |
| soft-wrapped | 1/96 | 2/96 | −0.0104 | [−0.0312, 0.0000] | **no significant difference observed** |

**The latency result must not be quoted as a speedup.** The paired *median* difference is
**−40 ms** and only **51 of 96** pairs were faster — a coin flip. ON's *p50 is higher*
(9,159 vs 8,093 ms). The significant mean difference is entirely **tail-driven**: p95 51,752
vs 66,031 ms. The honest statement is "the fan-out arm has a shorter tail", not "the fan-out
arm is faster".

### Three findings that qualify the headline

**1. The answer-time completion sweep never fired — this experiment does not test it.**
All **72** firings across all 192 runs were **plan-time** (`agent_node`);
`_completion_sweep_into_batch` fired **zero** times. The preregistration split NC-a precisely
because the two entry points are different trades, and the answer-time cell came back empty.
So this result supports the **free-ride** mechanism only. The branch that *can* open a batch
that would not otherwise exist is **untested here**, and no claim is made about it.

**2. One fifth of the fan-out's additions never ran.** 143 of 179 additions (79.9%) actually
executed; the other 36 were **denied at the execute-time gate**. The fan-out proposes, and the
same policy that judges the model's own calls can still refuse. On **E7** and **E10** that was
*100%* of the additions, which is why both sit at coverage 0.000 in **both** arms. Coverage is
counted from **executed** tools, so the measured gain is already net of this — it is
conservative, not inflated.

**3. Two of the eight cases contribute nothing, for a good reason.** On **E3** and **E9** the
fan-out never fired (0/12 repeats): the model requested every cued dimension itself, and both
arms sat at coverage 1.000. The effect is carried by E1, E5, E6, E10 and E11. Per-case means:

| case | coverage ON | coverage OFF | `llm_calls` ON | OFF | `tool_batches` ON | OFF | repeats fired |
|---|---|---|---|---|---|---|---|
| E1 | 1.000 | 0.694 | 2.17 | 4.17 | 1.08 | 2.25 | 12/12 |
| E3 | 1.000 | 1.000 | 2.00 | 2.00 | 1.00 | 1.00 | 0/12 |
| E5 | 1.000 | 0.861 | 4.83 | 5.08 | 2.83 | 2.75 | 12/12 |
| E6 | 1.000 | 0.361 | 2.00 | 2.25 | 1.00 | 1.17 | 12/12 |
| E7 | 0.000 | 0.000 | 2.00 | 2.00 | 1.00 | 1.00 | 12/12 |
| E9 | 1.000 | 1.000 | 3.33 | 3.25 | 1.33 | 1.25 | 0/12 |
| E10 | 0.000 | 0.000 | 2.00 | 2.67 | 1.00 | 1.58 | 12/12 |
| E11 | 1.000 | 0.250 | 2.00 | 2.50 | 1.00 | 1.50 | 12/12 |

### External-tool availability (GOAL §4.3)

Tool-call failure rates differ by arm: ON **52/410 (12.7%)**, OFF **21/290 (7.2%)**. This is
expected rather than alarming — the ON arm attempts *more* calls, and the extra ones are
concentrated in the flakiest class (`search_nearby_pois` / Overpass, whose mirrors were
observed being sidelined during the run). It still means the two arms did not face identical
external conditions, so it is disclosed here and in the caveats. It does not inflate the
coverage result, which counts only executed tools. Also: `tools_denied` ON 40 / OFF 15;
`tools_timed_out` ON 16 / OFF 35; `budget_timeout_events` ON 16 / OFF 35.

### Limitations

* **n = 8 cases.** The bootstrap resamples cases, so 8 is the effective unit no matter how
  many repeats. Every CI here is wide, and three of the eight cases (E3, E9, plus E7 as a
  structural zero) carry no signal. This was declared in the preregistration, not discovered
  afterwards.
* **Warm cache.** Every run restores the same `warm_v3.sqlite3` snapshot, so latency figures
  are snapshot-relative, not cold-start.
* **Single population.** `E_multi_constraint` only; nothing here generalises to categories
  that cue fewer than two dimensions.
* **The coverage denominator counts negated mentions.** `core.dimensions.cued_dimensions`
  fires on the *word*, so E7's *"no commute worries"* still counts `commute` as cued and
  unservable. `dimensions.py` documents this as deliberate fail-safe behaviour. It caps the
  achievable coverage rate, identically in both arms — a conservative bias, not a confound.
* **Preregistration deviation, recorded not hidden:** the prereg named
  `numpy.random.default_rng`; the analysis reuses the repo's existing
  `analyze.cluster_bootstrap` (stdlib `random.Random`) with the preregistered seed 20260805
  and 10,000 resamples. Estimator, resampling unit and interval are unchanged. Reuse was
  preferred over a duplicate bootstrap.

---

## Suggested CV entries

**Advisory only — `evaluation/results/CV_METRICS.md` was NOT modified.** Paste manually.

### [SAFE-WITH-SCOPE] Batch packing: dimension coverage up while LLM round-trips go down (paired A/B, 192 live runs)

- **中文 CV 表述**: 在 8 例多维度请求上做配对 A/B（每臂 12 次重复，共 192 次 live 请求，0 失败）：把「用户提到但模型没请求的读取」打包进同一批次后，维度覆盖率从 **138/252 (54.8%)** 升到 **204/252 (81.0%)**（配对差 **+0.229**，bootstrap 95% CI +0.038…+0.441），同时 LLM 调用数**下降** 0.45 次/回合（CI −0.94…−0.09）、工具批次数下降 0.28（CI −0.58…−0.03）。三项阴性对照全部通过。
- **English CV statement**: A paired A/B on 8 multi-dimension requests (12 repeats per arm, 192 live runs, 0 failures) shows that packing a *cued but unrequested* read into an existing tool batch raises dimension coverage from **138/252 (54.8%)** to **204/252 (81.0%)** — paired difference **+0.229**, bootstrap 95% CI +0.038…+0.441 — while **reducing** LLM calls by 0.45 per turn (CI −0.94…−0.09) and tool batches by 0.28 (CI −0.58…−0.03). All three negative controls passed.
- **Raw data (num/den)**: coverage 138/252 → 204/252; runs at full coverage 34/96 → 72/96; llm_calls mean 2.99 → 2.54 (−0.45, CI −0.94…−0.09); tool_batches mean 1.56 → 1.28 (−0.28, CI −0.58…−0.03); tools executed 2.67 → 3.78 (+1.11, CI +0.15…+2.30); e2e mean −5,819 ms (CI −15,248…−564) but paired **median −40 ms**, 51/96 pairs faster, p50 8,093 → 9,159, p95 66,031 → 51,752; cost_usd +0.0000 (CI −0.0001…+0.0001, **crosses 0**); tokens_in −4,180 (CI −11,135…+1,028, **crosses 0**); soft-wrapped 2/96 → 1/96 (CI **crosses 0**); 192/192 runs ok; total cost USD 0.1086
- **Metric definition**: Paired per-case A/B on the fc_loop graph (`app/core/agent_loop.py::build_fc_graph`), arms differing **only** in `FC_DIMENSION_FANOUT_MAX` (3 vs 0), set in process per arm; no product code changed. "Cued dimension" and "unserved dimension" are the product's own `core.dimensions.cued_dimensions` and `agent_loop::_unserved_cued_dimensions`, fed the run's **executed** tools — not a look-alike matcher. Population frozen before the run: cases in `evaluation/benchmark/cases.jsonl` cueing ≥2 distinct dimensions (n=8). CIs are cluster bootstrap, CASE as resampling unit, 10,000 resamples, seed `20260805`.
- **Result file**: `evaluation/results/fanout_ab/analysis_D.json` (+ `table_D.md`, `negative_control_D.json`, raw `main/runs.jsonl`); design frozen in `evaluation/results/fanout_ab/PREREGISTRATION.md`; write-up in `evaluation/results/FANOUT_AB_REPORT_20260805.md` §Phase 2.
- **Safe to use**: YES, within the scope below
- **Required caveat**: **n = 8 cases** — the bootstrap resamples cases, so every CI is wide; 2 of the 8 (E3, E9) never triggered the mechanism and 2 more (E7, E10) sit at coverage 0.000 in both arms because their fanned-out reads were denied at the execute-time gate. `E_multi_constraint` only. Measured on branch `experiment/fanout-ab-20260805`: product code `d7a7702` (i.e. `778714a` plus this session's two Phase 1 fixes, neither of which touches the fc_loop path exercised here — Fix A is in the legacy formatter and Fix B only affects rent-conversion turns), harness `6bad6d9`. **Not** a deployed build; the serving pools were untouched. Warm-cache relative (`warm_v3.sqlite3` restored per run), so latency is not a cold-start figure. **Never quote this as a latency win**: the paired median is −40 ms and 51/96 pairs favoured ON — the significant mean is tail-only, so say "shorter tail (p95 66.0 s → 51.8 s)", never "faster". Cost, tokens and soft-wrap CIs cross 0 → 未观察到显著差异. **Do not describe this as "fc parallel vs legacy serial"** — legacy is not serial, and its own map-reduce fan-out is a separate `[SAFE]` positive result in this file. The two arms hit different external-tool failure rates (12.7% vs 7.2%, because the ON arm attempts more of the flakiest tool class), so treat the latency and token figures as availability-dependent; the coverage figure counts executed tools only and is unaffected.

### [SAFE] Mechanism finding: only the plan-time entry point was exercised (n=192 runs)

- **中文 CV 表述**: 全部 192 次运行中，72 次维度扩展**全部**发生在 plan 阶段（`agent_node`，模型已经在开批次，扩展不额外花一次 LLM 往返）；answer 阶段的 `_completion_sweep_into_batch` **一次都没触发**。因此该实验只验证了「搭顺风车」这一条机制。
- **English CV statement**: Across all 192 runs, every one of the 72 fan-out firings happened at **plan time** (`agent_node`, where the model is already opening a batch, so the expansion costs no extra LLM round-trip); the answer-time `_completion_sweep_into_batch` fired **zero** times. The result therefore validates the free-ride mechanism only.
- **Raw data (num/den)**: firings plan-time 72/72, answer-time 0/72; runs where the fan-out fired 72/96 (ON arm); additions that actually executed 143/179 (79.9%), the remaining 36 denied at the execute-time gate; added-tool histogram `search_nearby_pois` 72, `calculate_commute` 60, `check_safety` 47, `remember`/`ask_user` 0
- **Metric definition**: Each call into `agent_loop::_dimension_fanout_calls` was tagged with its call site by walking the stack in the harness; the product function itself was wrapped, not reimplemented, so what is recorded is literally what the fan-out returned.
- **Result file**: `evaluation/results/fanout_ab/negative_control_D.json`, raw `main/runs.jsonl`
- **Safe to use**: YES, as a mechanism/engineering finding
- **Required caveat**: State it as scope, not as a property of the product — the answer-time sweep exists and is reachable; this 8-case population simply never took that branch, so **no claim about it is supported either way**. The "no extra batches" control passing is therefore evidence about plan-time packing only.

### [AVOID] "The dimension fan-out makes turns faster"

- **Why**: The paired median latency difference is **−40 ms** on 96 pairs and only 51/96 pairs favoured the fan-out arm; the fan-out arm's **p50 is higher** (9,159 vs 8,093 ms). The significant mean (−5,819 ms) is produced by the tail alone.
- **Say instead**: "shorter latency tail (p95 66.0 s → 51.8 s); median unchanged."
- **Result file**: `evaluation/results/fanout_ab/analysis_D.json` → `paired_contrasts.wall_ms`, plus the per-arm `wall_ms` percentiles in `table_D.md`.

---

## What is NOT done, skipped, or should not be trusted

**Not done / out of scope**

1. **The answer-time completion sweep is unmeasured.** Zero firings in 192 runs. Anything
   about `_completion_sweep_into_batch` — including whether it opens batches that would not
   otherwise exist — remains an open question. It is the obvious next experiment.
2. **No legacy arm.** Experiment D is fc_loop vs fc_loop. It says nothing about how batch
   packing compares to legacy's dimension follow-up wave.
3. **Fix A was not re-validated live.** Per the GOAL, the regression is asserted against the
   frozen v6 fixtures and archived runs; held-out v6 was not re-run, so the *scored* effect on
   the three structured-contract metrics is inferred from the contract shape, not remeasured.
4. **The supplementary v6 cases are a written recommendation only.** The frozen dataset was
   not touched, and the six suggested classes have not been authored or piloted.
5. **`CV_METRICS.md` was not modified**, per the hard boundary. The entries above are drafts
   for manual review.
6. **No second design was run.** The frozen design produced a usable result, so GOAL §4.1's
   "second design" path was never entered.

**Trust these with reservations**

7. **Every CI here rests on 8 clusters.** Treat all interval widths as indicative. The
   coverage and counter results are directionally robust (they replicate across 12 repeats and
   across 5 of the 8 cases); the latency result is not — see the `[AVOID]` entry.
8. **Arm-asymmetric external-tool failure (12.7% vs 7.2%).** Mechanically explained, but the
   two arms did not face identical external conditions. Latency and token figures inherit this;
   coverage does not.
9. **Per-run cost figures come from the harness's own price table**, the same one Experiment A
   used. The USD contrast is not significant anyway, so nothing rests on it.
10. **The `p95` values in this report are the analysis file's percentile method.** An ad-hoc
    recomputation with a different index convention gave 52,716 rather than 51,752 ms for the
    ON arm. Quote `analysis_D.json` / `table_D.md`, and do not mix percentile conventions.

**Known-good but worth restating**

11. Phase 1's suite comparison is a *count* reconciliation against a baseline supplied by the
    GOAL, not a re-run of `778714a`'s full suite. The two failures that looked new were
    individually A/B-tested against `778714a` and are identical there.
12. The timestamps in the first block of this session's `PROGRESS.log` entries run up to ~45
    minutes ahead of the real clock; a correction entry at 19:12 BST anchors the true timeline
    to the runner's own ISO stamps. Nothing was rewritten.
13. **`PROGRESS.log` is not in git.** The repo's `.gitignore` excludes `*.log` and the file has
    never been committed in this repository's history, so the timeline lives on disk at the
    repo root rather than in the branch. The per-run JSONL *is* committed (force-added past the
    `evaluation/results/**/*.jsonl` ignore rule), matching what Experiment C did.

---

## Artifacts

| deliverable | path | in git |
|---|---|---|
| local commits, not pushed | branch `experiment/fanout-ab-20260805` | yes |
| Fix A + regression test | `app/core/langgraph_agent.py`, `tests/test_holdout_v6_dimension_contract_mount.py` | yes |
| Fix B | `app/core/tool_policy.py` (spec tests already in `tests/`) | yes |
| preregistration | `evaluation/results/fanout_ab/PREREGISTRATION.md` | yes |
| frozen case selection | `evaluation/results/fanout_ab/case_selection.json` | yes |
| analysis | `evaluation/results/fanout_ab/{analysis_D.json, table_D.md, negative_control_D.json}` | yes |
| per-run JSONL | `evaluation/results/fanout_ab/main/runs.jsonl` (+ `events_shard0`, `grader_input`) | yes (force-added) |
| smoke run | `evaluation/results/fanout_ab/_smoke/runs.jsonl` | yes (force-added) |
| harness changes | `evaluation/results/_harness/{ab_runner.py, analyze_d_fanout.py}` | yes |
| this report | `evaluation/results/FANOUT_AB_REPORT_20260805.md` | yes |
| timeline | `PROGRESS.log` (repo root) | **no — `.gitignore` `*.log`** |

Boundary compliance: the only files touched under `app/` are the two the GOAL named
(`core/langgraph_agent.py`, `core/tool_policy.py`). `deploy/**`, every `.tex`,
`fact-ledger.md` and `evaluation/results/CV_METRICS.md` are untouched. Nothing was pushed, no
PR was opened, `main` was not modified, no deploy or pool switch was run, and the benchmark
ran as an independent process against a temp state root — never against a serving pool. No
credential material appears in any artifact (verified by scanning all of them for the live key
and for key-shaped strings).

