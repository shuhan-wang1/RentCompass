# Pre-registration — `memory_context` wiring (DESIGN ONLY)

**Status: DESIGN ONLY. No candidate exists. Nothing has been run. No API spend has
occurred or is authorised by this document.**

This file fixes what will be measured, and what will count as a pass, **before** any
candidate is built. It exists because the two preceding product experiments were
terminated, and the second was terminated specifically for being a **bundle**: five
hardenings shipped together produced a localised quality loss that could not be attributed
to any one of them. This experiment tests exactly one change.

Written 2026-07-23, against mainline `32454d3`. **PR #8 (the gitleaks/CI fix) is OPEN and
NOT merged** — it is a CI hygiene change and is not part of this experiment.

---

## 0. Preconditions — none of this may start until all are true

| # | precondition | state |
|---|---|---|
| P1 | PR #8 merged into `telemetry/v2-layer-b` | **NOT MET** (open, `mergedAt: null`) |
| P2 | §2 `BASELINE_SHA` filled in with the resulting mainline SHA | **NOT MET** — must not be pre-filled with today's head |
| P3 | This document reviewed and approved | pending |
| P4 | Both CI checks green on the baseline commit | pending |

`BASELINE_SHA` is deliberately left unfilled. Writing today's `32454d3` into it now would
pin a baseline that the experiment will not actually run against.

---

## 1. The hypothesis — exactly one

> **Correctly injecting long-term memory into the FC arm improves cross-session recall,
> and does not degrade quality on other tasks.**

Two claims, one change. The first is the effect being sought; the second is the
non-regression condition. Both are measured; neither alone decides the outcome.

### 1.1 What the change may touch

The candidate is `BASELINE_SHA` **plus memory wiring only**. On mainline today
`create_initial_state` hard-codes `memory_context=""`
(`src/uk_rent_agent/agent/state.py:123`), so the retrieved block never reaches the FC
message array. The change makes that channel an argument and hands it the block that was
already retrieved.

Permitted change surface — anything outside it fails the static gate in §6.1:

| file | permitted change |
|---|---|
| `src/uk_rent_agent/agent/state.py` | `memory_context` becomes a parameter of `create_initial_state`, defaulting to `""` |
| `app/core/agent_loop.py` | FC path reads `memory_context` into its message array |
| `app/app.py` | production entry point passes the block it already retrieved |
| `tests/` | tests for the above |

**Hard budget: ≤ 120 changed lines across the three product files.** The same wiring on
the terminated branch sat inside a 429-line `agent_loop.py` diff that also carried the
critic fallback. If the candidate approaches this budget, it has stopped being one change.

### 1.2 Explicitly forbidden

No code may be cherry-picked, ported, or re-derived from `fastpath/deterministic-phase1`
or `hardening/correctness-only`. The candidate is written from `BASELINE_SHA`.

Specifically excluded — each was part of a terminated bundle and none is under test here:

* the **critic grounding / fail-closed fallback** — CONFIRMED to have caused A14 by
  dropping a required negative fact. Must not appear in any form.
* the **follow-up capability filter** and its `suppressed` tool-trace handling
* the **pure-recall constraint**
* the **deterministic fast path**
* the **tool-surface hardening** (the unproven A4 hypothesis)
* `no_false_retrieval_provenance` and anything importing `claims_no_retrieval`

Reading those branches to understand the problem is allowed. Copying from them is not.

---

## 2. Identity layer

Every run records all three, per the measurement infrastructure merged in PR #6:

| field | value |
|---|---|
| `product_sha` | baseline arm: `BASELINE_SHA`; candidate arm: `CANDIDATE_SHA` |
| `capture_sha` | the tree that recorded the run — equals `product_sha` unless a measurement-only probe is added, in which case it differs and the probe is allowlisted |
| `evaluator_sha` | **one** evaluator for both arms, stamped at re-score time, never by the arm itself |
| `grader_sha256` | digest of `evaluation/metrics/graders.py` in the evaluator tree |
| `case_contract_sha256` | digest of the case shard; must be identical across both arms |

```
BASELINE_SHA  = <TO BE FILLED after PR #8 merges — the resulting telemetry/v2-layer-b tip>
CANDIDATE_SHA = <TO BE FILLED when the candidate is built>
EVALUATOR_SHA = <TO BE FILLED at re-score time>
```

Rules:

* A run whose manifest lacks `product_sha`, `capture_sha` or `case_contract_sha256`, or
  whose contract digest differs from the evaluator's, is **REFUSED**, not scored with a
  default. This is already enforced by `evaluation/rescore.py`.
* **Neither arm's own `passed` flag is evidence.** Each arm ships its own grader, so
  comparing self-computed verdicts compares two evaluators as much as two products. Only
  the single-evaluator re-score counts.
* Any code change to the candidate produces a **new SHA** and invalidates every run made
  under the old one. Restart from §6.3.

---

## 3. Causal isolation

The experiment claims a causal effect of one change, so everything else must be held
byte-identical.

1. **Memory snapshot.** Both arms start from the *same* serialized initial memory store,
   compared by SHA-256 before each round. Not "recreated identically" — the same bytes,
   restored fresh for every round.
2. **User identity.** Each arm uses run-namespaced user ids. No uid appears in both arms.
   A turn with no uid must retrieve nothing.
3. **Session isolation.** Each case runs in its own session. No conversation state, cache
   or memory write crosses a case boundary or an arm boundary.
4. **No writes leak between arms.** Any `remember` executed during a round mutates only
   that arm's namespaced store, and the store is discarded and restored from the snapshot
   before the next round.
5. **Interleaving.** Rounds run `base r1 → cand r1 → base r2 → cand r2 → base r3 → cand
   r3`, so drift in external services (TfL, OSM, police.uk, the model endpoint) hits both
   arms roughly equally. Both arms in a round use the same fixtures.
6. **Production is untouched.** `uk-rent-app` (:5001) and `uk-rent-app-fc` (:5002) are
   never modified, restarted or pointed at experiment images. Experiment pools are separate
   containers on separate ports.

Isolation is **verified, not assumed**: the snapshot digest and the per-arm uid namespaces
are asserted in the smoke gate (§6.3) and recorded in every manifest.

---

## 4. Injection invariants

These are the mechanism-level assertions. They are offline unit tests, run at §6.2, and
each must hold before any paid round begins.

| # | invariant | why |
|---|---|---|
| I1 | `create_initial_state("q")` still yields `memory_context == ""` | the default must not change behaviour for callers that pass nothing |
| I2 | `create_initial_state("q", memory_context=B)` yields exactly `B` | the channel is an argument, not a hard-coded empty string |
| I3 | **FC reads long-term memory exactly once per turn** | the production entry point already prefixes the retrieved block onto the query string; if FC reads both `user_query` and `memory_context`, the model sees the block **twice** |
| I4 | a remembered value appears **exactly once** in the rendered FC message array | the observable form of I3 — assert `rendered.count(value) == 1`, not merely `value in rendered` |
| I5 | with an empty block, no remembered value appears in the messages at all | proves the block is the only path, so a pass cannot come from elsewhere |
| I6 | the production entry point passes the **same** block it retrieved | the channel must not be left at its default in production |
| I7 | **legacy behaviour is unchanged** — for `AGENT_ARCH=legacy`, the rendered prompt is byte-identical to baseline for the same input | the experiment is about FC; a legacy change would confound it |
| I8 | retrieval is keyed on the run-namespaced uid; no uid ⇒ no block | user isolation (§3.2) |

I3/I4 are the invariants most likely to fail silently, because double injection *improves*
recall while corrupting the prompt-size and attribution story. A pass under double
injection is not a pass.

---

## 5. Success criteria, pre-registered

### 5.1 Recall (the effect being sought)

A recall case is: a **new conversation with empty `conversation_history`**, run against a
pre-seeded long-term memory containing a specific fact, asking for that fact.

* **PASS** = the answer accurately reproduces the stored fact.
* Whether `recall_memory` was called is **recorded but not required**. The pre-injection
  architecture can legitimately answer from the injected block with an empty tool trace —
  this is the same ruling the G2/G3 case amendments encode.
* Accuracy is judged by `must_recall_value` on the stored value, not by tool trace.

Pre-registered threshold: **candidate strictly greater than baseline on cross-session
recall cases, over 3 repeats.** No improvement means the hypothesis is not supported, and
the change does not ship regardless of how the non-regression conditions look.

### 5.2 Non-regression (the condition)

Scored by **one** evaluator over both arms, on Base98 (98 cases × 3 repeats × 2 arms).

| # | condition | threshold |
|---|---|---|
| N1 | semantic pass | candidate **≥** baseline |
| N2 | route matched | candidate **≥** baseline |
| N3 | per-case collapse | **no case** at candidate 0/3 while baseline 3/3 |
| N4 | zero-tolerance violations | exactly **0** |
| N5 | hard-gate cases | all pass |
| N6 | guard shard | no regression vs baseline |

N3 is the condition that terminated the previous experiment. It is not an aggregate: a
candidate that gains five cases and destroys one still fails.

---

## 6. Validation sequence — ordered, stop on first failure

Each stage is a gate. **A failure stops the experiment.** No re-running a failed stage
hoping for a better sample, no reordering, no skipping ahead.

**6.1 Static diff.** `git diff BASELINE_SHA..CANDIDATE_SHA` touches only the four paths in
§1.1 and stays within the 120-line budget. No import of, or code from, either terminated
branch. Fails ⇒ stop.

**6.2 Offline tests.** Full suite green in a clean container, including the eight
invariants of §4. Currently 1785 passed / 3 skipped; the candidate must not reduce this.
Fails ⇒ stop.

**6.3 Clean image + smoke.** Build from a committed-clean checkout (`git status
--porcelain` empty). Smoke both arms: health, arch identity headers, one trivial turn each.
Assert the memory snapshot digest and the uid namespacing of §3. Fails ⇒ stop.

**6.4 Cross-session recall smoke.** The §5.1 shape, a handful of cases, both arms. This is
the first stage that can support the hypothesis — and the cheapest place to find that it
does not. Fails ⇒ stop.

**6.5 Guard.** The 14-case guard shard, both arms. Note the standing scar: **a green guard
does not prove the other shards load.** §7's preflight is what proves that, and it runs
first. Fails ⇒ stop.

**6.6 Paired Base98, 3 × 3 interleaved.** Three rounds, both arms, interleaved per §3.5.
Then a single-evaluator re-score and the §5.2 gate. This is the only stage that decides
the outcome.

---

## 7. Cost, sample size and output discipline — fixed in advance

| item | value |
|---|---|
| rounds | **exactly 3** paired rounds. Not "3 and more if inconclusive" |
| Base98 gradings | 98 × 3 × 2 = **588** |
| guard gradings | 14 × 3 × 2 = 84 |
| per-command cost cap | `--max-cost-usd 5`, standing |
| total experiment ceiling | **USD 25**. Exceeding it stops the experiment; it does not authorise a top-up |
| output dirs | fresh per run. `--allow-reuse-out` is **forbidden** — appending to a reused dir once produced 196 records for a 98-case round |
| shard preflight | **every** benchmark shard must load and schema-validate before any paid round starts |
| cross-shard contract | `tests/test_case_contract_consistency.py` green, so a case means the same thing in every shard |

Sample size is fixed before seeing data, so "one more round" cannot become a way to buy a
result.

---

## 8. Attribution rules

1. A result supports or refutes **`memory_context` wiring, and nothing else.** It says
   nothing about the critic fallback, the follow-up filter, the tool surface or the fast
   path.
2. A pass **does not rehabilitate** `hardening/correctness-only`. That branch remains
   TERMINATED. The A14 cause is confirmed and the A4 cause remains unproven; neither is
   re-opened by this experiment.
3. A failure indicts **this wiring**, not the hypothesis that memory helps recall. It
   would mean this implementation, measured this way, did not show it.
4. Any effect observed on cases that are not cross-session recall is **incidental** and is
   reported as such — it was not the pre-registered target.
5. If I3/I4 turn out to be violated after the fact, every recall result is void: an
   improvement produced by double injection is not the effect being claimed.

---

## 9. Stopping and disposition

* **On failure at any gate**: stop. Retain the evidence — never overwrite or delete a
  failed round. Record which gate failed and why.
* **Re-running requires a new SHA.** A candidate whose code changed mid-sequence is no
  longer the candidate that was measured. Restart from §6.1.
* **No live editing.** Cases, graders, thresholds and gate conditions are frozen by this
  document once approved. Discovering mid-experiment that a case is unfair is a finding to
  record, not a licence to edit the contract and rescore. Contract changes go through the
  same review path the G2/G3/E11 amendments did — their own branch, their own PR, their own
  offline delta.
* **Nothing ships on reasoning alone.** Only the repeated, interleaved, single-evaluator
  A/B decides, against the thresholds written above.
* If the experiment is terminated, this file is updated with the outcome and the reason,
  and the candidate branch is marked TERMINATED rather than quietly abandoned.

---

## 10. What approval of this document authorises

Approval authorises **building the candidate and running §6.1–6.6 in order**, within the
§7 budget, once §0's preconditions are met.

It does **not** authorise: touching production, merging the candidate, editing the
evaluator contract, extending the budget, or running a fourth round.
