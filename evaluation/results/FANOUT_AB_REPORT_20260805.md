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

<!-- RESULTS-SECTION -->

