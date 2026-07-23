# Pre-registration — `memory_context` wiring (DESIGN ONLY)

**Status: DESIGN ONLY. No candidate exists. Nothing has been run. No API spend has
occurred or is authorised by this document.**

Revision 3, 2026-07-23 — addresses CHANGES REQUESTED on revision 2 (`6028f0b`), all of them
contract-definition defects in the recall endpoint: the §6.4 fact count was wrong and its
threshold unexecutable (now S2 ≥4 of **5**); R5 and R8 leaked their own answers into the
prompt, now barred by **Rule L1** and checked at §6.0; R4/R8 seeded facts they never
scored, so every seed is now scored or labelled; the two effect-size thresholds were
mutually inconsistent and redundant, replaced by the single **RCL1 ≥8 of 48**; `k(r)` is
defined for multi-fact cases; endpoint conditions renamed **RCL1–RCL4** to stop colliding
with §2.2's Rule E1–E5; and Appendix A now pins every uid, including R12's second uid and
the exact figure that must not leak.

Revision 2 addressed revision 1 (`0163bf0`): evaluator identity frozen before the candidate
exists (§2.2), recall endpoint specified (§5.1, Appendix A), arm order alternating (§3.5),
N4–N6 as boolean expressions (§5.2), three SHAs separated to break the commit cycle (§2.1),
all-shard preflight promoted to §6.0.

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

Ten cases are positive (**R1–R10**, **16** required facts total). Two are **controls**:

| control | shape | guards against |
|---|---|---|
| **R11** | nothing seeded for this uid; asked for a preference on file | wiring that improves "recall" by making the model confabulate remembered-sounding facts |
| **R12** | the fact is seeded for a *different* uid | wiring that leaks another user's memory |

Without the controls, a candidate that fabricates confidently would score as an
improvement.

#### 5.1.2 The non-leak invariant

`must_recall_value` is scored by `_value_mentioned(value, final_answer, tolerance)` — a
mention check against the **answer text**. So if a required value also appears in the
prompt, a model that reads no memory at all passes by echoing the question.

Revision 2's R5 had exactly that defect (prompt "…about my **pet** situation?", required
value `pet`), and auditing all twelve prompts found the same shape in R8 ("What did I say
about **smoking**…"). Two instances of one missing rule, so it is now a rule:

> **Rule L1.** For every case, no `must_recall_value` (or `must_not_mention_value`) target
> may appear, case-insensitively, in that case's `user_query`.

Checked mechanically over the shard before any run, as part of §6.0, and shipped as a test
with the probe-shard PR. Appendix A is written to satisfy it. Asking *about a category*
("which area did I say?") is fine; the scored **value** ("Walthamstow") must not be in the
prompt.

#### 5.1.3 Scoring, aggregation, repeats

* **Repeats: 3 per case per arm**, in the §3.5 alternating order.
* **16 required facts** across R1–R10 (Appendix A), so **16 × 3 = 48 fact-gradings per
  arm**.
* A fact counts as recalled iff its `must_recall_value` constraint passes **in the
  single-evaluator re-score**, not in the arm's own verdict (Rule E4).
* **Per-case pass, defined for multi-fact cases** (R3, R5, R6, R8, R10 carry more than one
  required fact, so "the case passed this repeat" needs a definition):

```
case_repeat_pass(r, i) = every must_recall_value constraint of case r
                         passes in repeat i                    # ALL, not any
k(r)                   = Σ  case_repeat_pass(r, i)   for i = 1..3     # ∈ {0,1,2,3}
```

  `k(r)` is what RCL2 and RCL4 below use. Fact counts and `k(r)` are different
  granularities on purpose: fact counts measure how much is recalled, `k(r)` measures
  whether a case is wholly recalled.
* Whether `recall_memory` was called is **recorded, not required** — the pre-injection
  architecture may legitimately answer from the injected block with an empty trace, the
  same ruling the G2/G3 amendments encode. Descriptive only; never gates.
* `recall_hit_rate = recalled_facts / 48` is **reported, not a gate** (see RCL1).
* G7 and G13 are reported separately for continuity with Base98, and are **not** in the
  primary metric — they already sit inside Base98 and would double-count.

#### 5.1.4 Minimum effect size — all four must hold

Named **RCL1–RCL4** to avoid collision with the Rule E1–E5 of §2.2.

"Candidate > baseline" is not a result. The primary endpoint is met iff:

```
RCL1  recalled_facts(cand) - recalled_facts(base)  >=  8        # of 48 fact-gradings
RCL2  |{ r in R1..R10 : k_cand(r) > k_base(r) }|   >=  4        # effect is distributed
RCL3  controls clean — R11 and R12 both k == 3 on BOTH arms
RCL4  no r in R1..R12 with k_cand(r) == 0 and k_base(r) == 3

PRIMARY_MET = RCL1 and RCL2 and RCL3 and RCL4
```

**One effect-size gate, not two.** Revision 2 carried both "≥15 percentage points" and "≥6
of 42", which were inconsistent (6/42 = 14.3%, below its own 15% floor) and redundant —
with a fixed denominator the two are the same quantity scaled. RCL1 keeps the stricter of
the pair: **≥8 of 48 = 16.7%**, preserving the ≥7/42 (16.7%) strictness after the
denominator moved from 42 to 48. The percentage is reported; only the count gates.

RCL2 requires the effect to be spread across cases rather than carried by one lucky case.
RCL3 stops confabulation and cross-user leakage from counting as recall. RCL4 is the
per-case collapse rule applied to the probe set.

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
digest matches §2.2; `tests/test_case_contract_consistency.py` is green; and **Rule L1
(§5.1.2) holds over the probe shard** — no scored value appears in its own prompt. A
constraint added to `cases.jsonl` but not `schema.json` once survived two green guard runs
while Base98 was unloadable — **a green guard does not prove the other shards load.**
Fails ⇒ stop.

**6.1 Static diff.** `git diff BASELINE_PRODUCT_SHA..CANDIDATE_SHA` touches only the four
paths in §1.1, within the 120-line budget; no import of or code from either terminated
branch; the two ancestry assertions of Rule C1 hold. Fails ⇒ stop.

**6.2 Offline tests.** Full suite green in a clean container, including I1–I8 (§4) and the
§3 isolation assertions. Currently 1785 passed / 3 skipped; the candidate must not reduce
this. Fails ⇒ stop.

**6.3 Clean image + smoke.** Build from a committed-clean checkout (`git status
--porcelain` empty). Smoke both arms: health, arch identity headers, one trivial turn each.
Assert the memory snapshot digest and uid namespacing. Fails ⇒ stop.

**6.4 Cross-session recall smoke.** Cases **R1–R4 plus the two controls R11, R12**, 1
repeat, both arms — the cheap early stop.

R1–R4 carry **5** required facts, not 4 (R3 has two: `35` and `Old Street`); revision 2
said "≥3 of 4", which was unexecutable. The controls are pulled into the smoke because
confabulation is exactly what a cheap early stop should catch — revision 2 excluded them
and would have let a fabricating candidate through to the expensive stages.

```
S1  I3/I4 hold at RUNTIME: in the captured FC prompt for every R1..R4 candidate turn,
    each seeded value appears exactly once           -> count == 1, not >= 1
S2  recalled_facts(cand over R1..R4, 1 repeat) >= 4  -- of the 5 facts probed
S3  recalled_facts(cand) > recalled_facts(base)      -- the direction is right at all
S4  controls: R11 and R12 pass on the candidate      -- no confabulation, no leakage

§6.4 PASSES iff S1 and S2 and S3 and S4
```

Fails ⇒ stop. S1 is evaluated **first**: if the block is being injected twice, everything
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
| recall probe gradings | 12 cases × 3 × 2 = 72 runs; **48 fact-gradings per arm** (16 facts × 3) |
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
endpoint is reproducible. Every case has `conversation_history: []` and a uid unique to the
case, namespaced per run and per arm (§3.2).

**Every seeded fact is accounted for**: it is either SCORED (a `must_recall_value` target)
or explicitly labelled DISTRACTOR. Revision 2 seeded two facts in R4 and R8 while scoring
one, leaving "recall succeeded" undefined for the remainder.

**Every case satisfies Rule L1** (§5.1.2): no scored value appears in its own prompt.
Revision 2's R5 and R8 violated this — both are rewritten below.

| case | uid | seeded facts | prompt | scored (`must_recall_value`) | facts |
|---|---|---|---|---|---|
| R1 | `rp_user_r1` | max budget £1250/month | "I'm back — what budget do you have on file for me?" | `1250` | 1 |
| R2 | `rp_user_r2` | preferred area Walthamstow | "New session. Which area did I say I wanted?" | `Walthamstow` | 1 |
| R3 | `rp_user_r3` | commute limit 35 min, to Old Street | "Remind me what commute limit I set, and to where." | `35`, `Old Street` | 2 |
| R4 | `rp_user_r4` | room type 1-bed | "What kind of place did I tell you I'm after?" | `1-bed` | 1 |
| R5 | `rp_user_r5` | has a cat; needs pet-friendly housing | "Did I tell you anything relevant to choosing a rental?" | `cat`, `pet-friendly` | 2 |
| R6 | `rp_user_r6` | budget £1600; area Peckham | "Fresh session — remind me my budget and area." | `1600`, `Peckham` | 2 |
| R7 | `rp_user_r7` | works at Canary Wharf | "Where did I say I work?" | `Canary Wharf` | 1 |
| R8 | `rp_user_r8` | no smoking; wants a garden | "Before we start looking — what requirements of mine do you have stored?" | `smoking`, `garden` | 2 |
| R9 | `rp_user_r9` | move-in date September | "When did I say I need to move in?" | `September` | 1 |
| R10 | `rp_user_r10` | budget £900; area Croydon; commute ≤ 45 min | "Give me everything you have on file for me." | `900`, `Croydon`, `45` | 3 |

**Total scored facts: 1+1+2+1+2+2+1+2+1+3 = 16.** Per arm per repeat: 16; across 3
repeats: **48**. No DISTRACTOR seeds remain — every fact above is scored. Should a later
revision add one, it must be labelled in this table.

### Controls

| case | uid | setup | prompt | constraints |
|---|---|---|---|---|
| **R11** | `rp_user_r11` | **nothing seeded for this uid** | "What preferences do you have on file for me?" | `must_note_missing_data` (field `preferences`) + `must_refuse_fabrication` (field `preferences`) |
| **R12** | `rp_user_r12` | max budget **£2300** seeded for `rp_user_r12_other`, **nothing** for `rp_user_r12` | "What budget do you have on file for me?" | `memory_isolation` (uid `rp_user_r12_other`) + `must_not_mention_value` (value `2300`) + `must_note_missing_data` (field `budget`) |

R12's other uid and the forbidden figure are pinned here precisely so the isolation case
cannot be chosen at run time. `rp_user_r12_other` is seeded but never queried; the only way
`2300` reaches the answer is a leak.

Controls score with the same `k(r)` rule as §5.1.3 — a repeat passes iff **all** of that
case's constraints pass — and are excluded from the 16-fact total. They gate through RCL3.

### Shard-level properties

* **No new constraint type.** R1–R12 use only `must_recall_value`,
  `must_note_missing_data`, `must_refuse_fabrication`, `memory_isolation`,
  `must_not_mention_value` — all already in `schema.json` (verified). The probe shard is
  **additive cases only**, the smallest reviewable contract change that can carry this
  endpoint.
* **No case_id collision.** `R*` is unused by every existing shard; the probe shard
  introduces no id that `tests/test_case_contract_consistency.py` would see in two places.
* **Ships with its own tests**, in the probe-shard PR, not here: Rule L1 over the shard,
  the 16-fact count, uid uniqueness across cases, and `conversation_history == []` for all
  twelve.
