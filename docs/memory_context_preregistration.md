# Pre-registration — `memory_context` wiring (DESIGN ONLY)

**Status: DESIGN ONLY. No candidate exists. Nothing has been run. No API spend has
occurred or is authorised by this document.**

Revision 2, 2026-07-23 — addresses CHANGES REQUESTED on revision 1 (`0163bf0`): evaluator
identity is now frozen before the candidate exists (§2.2), the recall endpoint is fully
specified (§5.1, Appendix A), arm order alternates (§3.5), N4–N6 are boolean expressions
over named fields (§5.2), the three SHAs are separated to break the commit cycle (§2.1),
and the all-shard preflight is promoted to its own first gate (§6.0).

This file fixes what will be measured, and what counts as a pass, **before** any candidate
is built. It exists because the two preceding product experiments were terminated, and the
second was terminated specifically for being a **bundle**: five hardenings shipped together
produced a localised quality loss that could not be attributed to any one of them.

---

## 0. Preconditions — none of this may start until all are true

| # | precondition | state |
|---|---|---|
| P1 | PR #8 (gitleaks/CI fix) merged into `telemetry/v2-layer-b` | **NOT MET** |
| P2 | §2.1 `BASELINE_PRODUCT_SHA` filled with the resulting mainline commit | **NOT MET** — must not be pre-filled with today's head |
| P3 | §5.1 recall probe shard landed via its **own** reviewed contract PR | **NOT MET** — see §5.1.1 |
| P4 | §2.2 evaluator identity frozen: `EVALUATOR_SHA`, `grader_sha256`, and the digest of **every** case shard recorded here | **NOT MET** |
| P5 | This document approved at a specific head, recorded as `PREREG_SHA` | **NOT MET** |
| P6 | Both CI checks green on `BASELINE_PRODUCT_SHA` | **NOT MET** |

Approval is of a **filled-in** document. A document approved with placeholders authorises
nothing.

---

## 1. The hypothesis — exactly one

> **Correctly injecting long-term memory into the FC arm improves cross-session recall,
> and does not degrade quality on other tasks.**

Two claims, one change. The first is the effect sought; the second is the non-regression
condition. Both are measured; neither alone decides the outcome.

### 1.1 What the change may touch

The candidate is `BASELINE_PRODUCT_SHA` **plus memory wiring only**. On mainline today
`create_initial_state` hard-codes `memory_context=""`
(`src/uk_rent_agent/agent/state.py:123`), so the retrieved block never reaches the FC
message array. The change makes that channel an argument and hands it the block that was
already retrieved.

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
or `hardening/correctness-only`. The candidate is written from `BASELINE_PRODUCT_SHA`.

Excluded by name — each was part of a terminated bundle, none is under test here: the
**critic grounding / fail-closed fallback** (CONFIRMED cause of A14, dropped a required
negative fact); the **follow-up capability filter** and its `suppressed` tool-trace
handling; the **pure-recall constraint**; the **deterministic fast path**; the
**tool-surface hardening** (the unproven A4 hypothesis); `no_false_retrieval_provenance`
and anything importing `claims_no_retrieval`.

Reading those branches to understand the problem is allowed. Copying from them is not.

---

## 2. Identity

### 2.1 Three distinct SHAs

Revision 1 had a commit cycle: it named one `BASELINE_SHA` while itself being a commit
that would land on the same branch. If the candidate branched from a mainline that already
contained this document, the §6.1 static diff would include this file and violate §1.1.

```
BASELINE_PRODUCT_SHA = <TO BE FILLED>   # the exact commit after PR #8 merges.
                                        # The candidate branches from THIS, and only this.
PREREG_SHA           = <TO BE FILLED>   # the PR #9 head approved with all identity
                                        # fields filled. Governs the experiment; is NOT
                                        # an ancestor of the candidate.
CANDIDATE_SHA        = <TO BE FILLED>   # BASELINE_PRODUCT_SHA + memory wiring only.
```

**Rule C1.** `CANDIDATE_SHA`'s parent chain must contain `BASELINE_PRODUCT_SHA` and must
**not** contain `PREREG_SHA`. Verified mechanically at §6.1:

```
git merge-base --is-ancestor $BASELINE_PRODUCT_SHA $CANDIDATE_SHA   # must succeed
git merge-base --is-ancestor $PREREG_SHA           $CANDIDATE_SHA   # must FAIL
```

Whether PR #9 merges before or after the experiment is irrelevant to the candidate: the
candidate never branches from a tree containing it.

### 2.2 Evaluator identity — frozen BEFORE the candidate exists

Revision 1 said `EVALUATOR_SHA` would be stamped "at re-score time". That left room to see
results and then choose an evaluator. It is now a precondition (P4).

Recorded here, before `CANDIDATE_SHA` exists and before any paid run:

```
EVALUATOR_SHA            = <TO BE FILLED>   # tree that will re-score BOTH arms
grader_sha256            = <TO BE FILLED>   # sha256 of evaluation/metrics/graders.py
case_contract_sha256:
  cases.jsonl                    = <TO BE FILLED>
  cases_guard_regression.jsonl   = <TO BE FILLED>
  cases_recall_probe.jsonl       = <TO BE FILLED>
  cases_base45.jsonl             = <TO BE FILLED>
  cases_ext_AB.jsonl             = <TO BE FILLED>
  cases_ext_CDE.jsonl            = <TO BE FILLED>
  cases_ext_FG.jsonl             = <TO BE FILLED>
  cases_cold_resilience.jsonl    = <TO BE FILLED>
```

**Rule E1.** Every shard digest is pinned, not only the shards the experiment runs. A
contract that is unloadable elsewhere has bitten this project once already.

**Rule E2.** Any change to the evaluator tree — `graders.py` or **any** shard — after
approval **voids this pre-registration**. Resuming requires a fresh review and a new
`PREREG_SHA`. There is no in-flight evaluator amendment.

**Rule E3.** Per-run identity is `product_sha` / `capture_sha` / `evaluator_sha`, already
enforced by `evaluation/rescore.py`: a run whose manifest lacks `product_sha`,
`capture_sha` or `case_contract_sha256`, or whose contract digest differs from the
evaluator's, is **REFUSED**, never scored with a default.

**Rule E4.** **Neither arm's own `passed` flag is evidence.** Each arm ships its own
grader, so comparing self-computed verdicts compares two evaluators as much as two
products. Only the single-evaluator re-score counts.

**Rule E5.** Any code change to the candidate produces a new SHA and invalidates every run
made under the old one. Restart from §6.0.

---

## 3. Causal isolation

1. **Memory snapshot.** Both arms start from the *same* serialized initial memory store,
   compared by SHA-256 before each round — the same bytes, restored fresh every round, not
   "recreated identically".
2. **User identity.** Run-namespaced uids. No uid appears in both arms. A turn with no uid
   retrieves nothing.
3. **Session isolation.** Each case runs in its own session. No conversation state, cache
   or memory write crosses a case or arm boundary.
4. **No write leakage.** A `remember` executed during a round mutates only that arm's
   namespaced store; the store is discarded and restored from the snapshot before the next
   round.
5. **Arm order alternates, fixed in advance.** Revision 1 always ran base → candidate,
   binding time drift to arm. The order is now:

   | round | first | second |
   |---|---|---|
   | r1 | baseline | candidate |
   | r2 | **candidate** | **baseline** |
   | r3 | baseline | candidate |

   Written here, not chosen at run time. Both arms in a round use the same fixtures.
6. **Production untouched.** `uk-rent-app` (:5001) and `uk-rent-app-fc` (:5002) are never
   modified, restarted, or pointed at experiment images. Experiment pools are separate
   containers on separate ports.

Isolation is **verified, not assumed**: the snapshot digest and per-arm uid namespaces are
asserted at §6.2 and recorded in every manifest.

---

## 4. Injection invariants

Offline unit tests, run at §6.2. Each must hold before any paid round.

| # | invariant | why |
|---|---|---|
| I1 | `create_initial_state("q")` still yields `memory_context == ""` | the default must not change behaviour for callers passing nothing |
| I2 | `create_initial_state("q", memory_context=B)` yields exactly `B` | the channel is an argument, not a hard-coded empty string |
| I3 | FC reads long-term memory **exactly once** per turn | the production entry point already prefixes the retrieved block onto the query string; an FC path reading both `user_query` and `memory_context` shows the model the block **twice** |
| I4 | a remembered value appears **exactly once** in the rendered FC message array — `rendered.count(value) == 1`, not `value in rendered` | the observable form of I3 |
| I5 | with an empty block, no remembered value appears in the messages at all | proves the block is the only path, so a pass cannot come from elsewhere |
| I6 | the production entry point passes the **same** block it retrieved | the channel must not be left at its default in production |
| I7 | **legacy unchanged** — for `AGENT_ARCH=legacy` the rendered prompt is byte-identical to baseline for the same input | a legacy change would confound the experiment |
| I8 | retrieval is keyed on the run-namespaced uid; no uid ⇒ no block | user isolation (§3.2) |

I3/I4 are the invariants most likely to fail silently: double injection *improves* recall
while corrupting the prompt-size and attribution story. **A pass under double injection is
not a pass** (§8.5).

---

## 5. Endpoints, pre-registered

### 5.1 Primary endpoint — cross-session recall

A recall case is: a **new conversation with empty `conversation_history`**, run against a
pre-seeded durable memory containing specific facts, asking for those facts.

**Finding that shaped this section.** Base98 contains only **two** such cases — **G7**
(`u_alice`, fixture `memory_recall_budget.json`, must recall `1400` and `King's Cross`)
and **G13** (`fg_user_g13`, fixture `ext_fg_memory_recall_profile.json`, must recall
`Hackney` and `30`). G1 and G12 are *write* cases (`must_call_tool: remember`), not recall.
Two cases × 3 repeats is too thin to carry a primary endpoint, so the experiment uses a
dedicated probe shard.

#### 5.1.1 The probe shard is a contract change and lands first

`evaluation/benchmark/cases_recall_probe.jsonl` — **12 cases, specified case-by-case in
Appendix A** (case ids, uid, seeded facts, exact prompt, required values). Adding cases
changes what "pass" means, so per §9 it lands through its **own** branch, its own PR and
its own review — **before** this pre-registration is approved (P3). It is not created
mid-experiment.

Ten cases are positive (**R1–R10**, 14 required facts total). Two are **controls**:

| control | shape | guards against |
|---|---|---|
| **R11** | nothing seeded for this uid; asked for a preference on file | wiring that improves "recall" by making the model confabulate remembered-sounding facts |
| **R12** | the fact is seeded for a *different* uid | wiring that leaks another user's memory |

Without the controls, a candidate that fabricates confidently would score as an
improvement.

#### 5.1.2 Scoring, aggregation, repeats

* **Repeats: 3 per case per arm**, in the §3.5 alternating order.
* Fact-level gradings per arm: **14 required facts × 3 repeats = 42**.
* A fact counts as recalled iff its `must_recall_value` constraint passes **in the
  single-evaluator re-score**, not in the arm's own verdict (E4).
* **Primary metric:** `recall_hit_rate = recalled_facts / 42`.
* Whether `recall_memory` was called is **recorded, not required** — the pre-injection
  architecture may legitimately answer from the injected block with an empty trace, the
  same ruling the G2/G3 amendments encode. It is reported as a descriptive statistic and
  never gates.
* G7 and G13 are also reported separately, as continuity with Base98. They are **not** part
  of the primary metric (they are already inside Base98 and would double-count).

#### 5.1.3 Minimum effect size — all four must hold

"Candidate > baseline" is not a result. The primary endpoint is met iff:

```
E1: recall_hit_rate(cand) - recall_hit_rate(base) >= 0.15        # ≥15 percentage points
E2: recalled_facts(cand)  - recalled_facts(base)  >= 6           # of 42 fact-gradings
E3: |{r in R1..R10 : k_cand(r) > k_base(r)}| >= 4                # ≥4 distinct cases improve
                                                                 # (k = passing repeats, 0..3)
E4: controls clean — R11 and R12 both 3/3 on BOTH arms
E5: no r in R1..R12 with k_cand(r) == 0 and k_base(r) == 3
```

E1 and E2 together stop a single lucky case from carrying the result; E3 requires the
effect to be distributed; E4 stops confabulation and leakage from counting as recall; E5
is the per-case collapse rule applied to the probe set.

Failing the primary endpoint means the change **does not ship**, however good §5.2 looks.

### 5.2 Non-regression — boolean conditions over named fields

Scored by **one** evaluator over both arms. Field names are those already produced by the
pipeline: re-scored `passed`, `route_matched`, `summary.json.zero_tolerance_violations`,
and `hard_gate` from the case record. Throughout, **`effective(run) = passed AND
route_matched`** — the definition the previous gate used.

Base98: 98 cases × 3 repeats = **294 runs per arm**. Guard: 14 cases × 3 = **42 per arm**.

```
N1  semantic pass      sum(passed for run in cand_base98) >= sum(passed for run in base_base98)

N2  route matched      sum(route_matched for run in cand_base98)
                         >= sum(route_matched for run in base_base98)

N3  per-case collapse  no case c in Base98 with k_cand(c) == 0 and k_base(c) == 3
                       where k(c) = number of repeats with passed == True

N4  zero tolerance     len(cand.summary.zero_tolerance_violations) == 0
                       AND len(base.summary.zero_tolerance_violations) == 0
                       -- BOTH arms. The gate is on the candidate, but a dirty baseline
                       -- voids the comparison rather than excusing the candidate: if the
                       -- baseline is non-zero the ROUND is void and the experiment stops
                       -- under §9, it does not proceed on a relaxed threshold.
                       -- kinds: forbidden_tool_executed, tainted_write_executed,
                       --        budget_breach, no_evidence_numbers

N5  hard gate          Base98 contains NO hard_gate cases; all 14 live in the guard shard
                       (H1..H14), so N5 is evaluated there:
                       for every c in H1..H14:
                           effective_k_cand(c) == 3 AND effective_k_base(c) == 3
                       -- 3/3 on EFFECTIVE (passed AND route_matched), both arms.
                       -- A baseline hard-gate case below 3/3 voids the round (as N4).

N6  guard shard        sum(effective for run in cand_guard)
                         >= sum(effective for run in base_guard)          # counts
                       AND no case c in H1..H14 with
                           effective_k_cand(c) == 0 and effective_k_base(c) == 3
                       -- the 0/3-vs-3/3 rule applies to the guard shard too.

GATE = N1 and N2 and N3 and N4 and N5 and N6
```

N3 is the condition that terminated the previous experiment. It is not an aggregate: a
candidate that gains five cases and destroys one still fails.

---

## 6. Validation sequence — ordered, stop on first failure

Each stage is a gate. **A failure stops the experiment.** No re-running a failed stage for
a better sample, no reordering, no skipping ahead.

**6.0 All-shard preflight** *(promoted from a footnote in revision 1)*. Before anything
else: every shard in `evaluation/benchmark/*.jsonl` loads and schema-validates; every shard
digest matches §2.2; `tests/test_case_contract_consistency.py` is green. A constraint added
to `cases.jsonl` but not `schema.json` once survived two green guard runs while Base98 was
unloadable — **a green guard does not prove the other shards load.** Fails ⇒ stop.

**6.1 Static diff.** `git diff BASELINE_PRODUCT_SHA..CANDIDATE_SHA` touches only the four
paths in §1.1, within the 120-line budget; no import of or code from either terminated
branch; the two ancestry assertions of Rule C1 hold. Fails ⇒ stop.

**6.2 Offline tests.** Full suite green in a clean container, including I1–I8 (§4) and the
§3 isolation assertions. Currently 1785 passed / 3 skipped; the candidate must not reduce
this. Fails ⇒ stop.

**6.3 Clean image + smoke.** Build from a committed-clean checkout (`git status
--porcelain` empty). Smoke both arms: health, arch identity headers, one trivial turn each.
Assert the memory snapshot digest and uid namespacing. Fails ⇒ stop.

**6.4 Cross-session recall smoke.** Cases **R1–R4 only, 1 repeat, both arms** — the cheap
early stop. Passes iff **all** hold:

```
S1  I3/I4 hold at RUNTIME: in the captured FC prompt for every R1..R4 candidate turn,
    each seeded value appears exactly once            -> count == 1, not >= 1
S2  recalled_facts(cand over R1..R4, 1 repeat) >= 3   -- of the 4 facts probed
S3  recalled_facts(cand) > recalled_facts(base)       -- direction is right at all
S4  controls not yet in play (R11/R12 are not in the smoke subset)
```

Fails ⇒ stop. S1 is checked first: if the block is being injected twice, everything
downstream is uninterpretable.

**6.5 Guard.** The 14-case guard shard, 3 repeats, both arms. Passes iff:

```
G1  N5 holds  (every H1..H14 effective 3/3 on BOTH arms)
G2  N6 holds
G3  len(zero_tolerance_violations) == 0 on both arms
```

Fails ⇒ stop.

**6.6 Paired Base98 + recall probe, 3 rounds interleaved.** Three rounds, both arms, in the
§3.5 alternating order, over Base98 **and** `cases_recall_probe.jsonl`. Then the
single-evaluator re-score, then §5.1.3 (E1–E5) and §5.2 (N1–N6). This is the only stage
that decides the outcome.

**Ship iff `PRIMARY_MET and GATE`.** Either alone is not a result.

---

## 7. Cost, sample size and output discipline — fixed in advance

| item | value |
|---|---|
| rounds | **exactly 3** paired rounds. Not "3 and more if inconclusive" |
| Base98 gradings | 98 × 3 × 2 = **588** |
| recall probe gradings | 12 cases × 3 × 2 = 72 runs; **42 fact-gradings per arm** |
| guard gradings | 14 × 3 × 2 = 84 |
| per-command cost cap | `--max-cost-usd 5`, standing |
| total experiment ceiling | **USD 25**. Exceeding it stops the experiment; it does not authorise a top-up |
| output dirs | fresh per run. `--allow-reuse-out` **forbidden** — appending to a reused dir once produced 196 records for a 98-case round |
| shard preflight | §6.0, before any paid run |

Sample size is fixed before seeing data, so "one more round" cannot become a way to buy a
result.

---

## 8. Attribution rules

1. A result supports or refutes **`memory_context` wiring, and nothing else** — not the
   critic fallback, the follow-up filter, the tool surface, or the fast path.
2. A pass **does not rehabilitate** `hardening/correctness-only`. That branch remains
   TERMINATED: A14's cause is confirmed, A4's is unproven, and neither is re-opened here.
3. A failure indicts **this wiring**, not the idea that memory helps recall. It would mean
   this implementation, measured this way, did not show it.
4. Any effect on cases that are not cross-session recall is **incidental** and reported as
   such — it was not the pre-registered target.
5. If I3/I4 are found violated after the fact, **every recall result is void**: an
   improvement produced by double injection is not the effect being claimed.
6. `recall_memory` call rates are descriptive. A change in call rate is not evidence for or
   against the hypothesis.

---

## 9. Stopping and disposition

* **On failure at any gate**: stop. Retain the evidence — never overwrite or delete a
  failed round. Record which gate failed and why.
* **Re-running requires a new SHA.** A candidate whose code changed mid-sequence is not the
  candidate that was measured. Restart from §6.0.
* **No live editing.** Cases, graders, thresholds and gate conditions are frozen by this
  document once approved. Discovering mid-experiment that a case is unfair is a finding to
  record, not a licence to edit the contract and re-score. Contract changes take the same
  path the G2/G3/E11 amendments did — own branch, own PR, own offline delta — and per Rule
  E2 they void this pre-registration.
* **Nothing ships on reasoning alone.** Only the repeated, interleaved, single-evaluator
  A/B decides, against the thresholds above.
* If terminated, this file records the outcome and the reason, and the candidate branch is
  marked TERMINATED rather than quietly abandoned.

---

## 10. What approval authorises

Approval of a **filled-in** copy of this document authorises building the candidate and
running §6.0–6.6 in order, within the §7 budget, once §0's preconditions are met.

It does **not** authorise: touching production, merging the candidate, editing the
evaluator contract, extending the budget, or running a fourth round.

---

## Appendix A — recall probe shard (`cases_recall_probe.jsonl`)

Specified here so the shard is reviewable as a contract change (§5.1.1) and so the primary
endpoint is reproducible. Every case: `conversation_history: []`, a uid unique to the case
and namespaced per run, and a seeded durable memory. Required values are the
`must_recall_value` targets; the count in the last column feeds the 14-fact total.

| case | seeded fact(s) | prompt | required value(s) | facts |
|---|---|---|---|---|
| R1 | max budget £1250/month | "I'm back — what budget do you have on file for me?" | `1250` | 1 |
| R2 | preferred area: Walthamstow | "New session. Which area did I say I wanted?" | `Walthamstow` | 1 |
| R3 | commute limit 35 min to Old Street | "Remind me what commute limit I set, and to where." | `35`, `Old Street` | 2 |
| R4 | room type: 1-bed, not studio | "What kind of place did I tell you I'm after?" | `1-bed` | 1 |
| R5 | has a cat, needs pet-friendly | "Anything you remember about my pet situation?" | `pet` | 1 |
| R6 | budget £1600 **and** area Peckham | "Fresh session — remind me my budget and area." | `1600`, `Peckham` | 2 |
| R7 | works at Canary Wharf | "Where did I say I work?" | `Canary Wharf` | 1 |
| R8 | no smoking, wants a garden | "What did I say about smoking and outdoor space?" | `garden` | 1 |
| R9 | move-in date September | "When did I say I need to move in?" | `September` | 1 |
| R10 | budget £900, area Croydon, commute ≤ 45 | "Give me everything you have on file for me." | `900`, `Croydon`, `45` | 3 |
| **R11** | *(nothing seeded)* | "What preferences do you have on file for me?" | must **not** assert a remembered preference — `must_note_missing_data` + `must_refuse_fabrication` | control |
| **R12** | fact seeded for a **different** uid | "What budget do you have on file for me?" | `memory_isolation` + `must_not_mention_value` on the other user's figure | control |

Total required facts across R1–R10: **1+1+2+1+1+2+1+1+1+3 = 14**. Per arm per round: 14;
across 3 repeats: **42**.

R11 and R12 reuse constraint types already in `schema.json`
(`must_note_missing_data`, `must_refuse_fabrication`, `memory_isolation`,
`must_not_mention_value`), so the probe shard adds **no new constraint type** — it is
additive cases only, which is the smallest reviewable contract change that can carry this
endpoint.
