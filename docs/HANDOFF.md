# HANDOFF — START HERE

Master index for the fc_loop work. **Every other document is a leaf; this is the only file
that spans all branches.** Last updated 2026-07-25.

> ## Status: fc_loop IS LIVE ON THE PUBLIC EDGE as of 2026-07-26. The p50 gate did NOT pass; the owner overrode it.
>
> **THE GATE WAS NOT MET. IT WAS OVERRIDDEN.** Read that as written — nothing below
> retroactively turns `8793c0b` green, and no future round may cite this cutover as evidence
> that the gate was satisfied. §3.10 records the decision, what was known at the time, and
> how to undo it.
>
> The 2026-07-19 → 07-23 latency/correctness phase is **closed** (two product experiments
> NO-GO, §4). What followed is a deliberate rebuild in order — **infrastructure → contract
> → product**. All of the infrastructure/contract/defect work has merged, and on 2026-07-25
> the first complete paid verification round was run against candidate `8793c0b`.
>
> **Right now:**
> * `telemetry/v2-layer-b` is the **mainline**, not `main`. Head is
>   **`8793c0b17963a6a2b375903a164d3d96395dc834`**. Offline suite **1804 passed, 3 skipped,
>   1 xfailed**.
> * **The verification round of record is `.runtime/round-8793c0b-internal-2026-07-25/`.**
>   Verdict **STAGE-PAUSE (exit 2)**: fc p50 **8466ms** > 6000ms and partial+soft **10.45%**
>   > 10%. Zero-tolerance clean. **No cutover** (§3.6). §3.8 has the full result.
> * **The Phase-2 fc-vs-legacy verdict, which had never been written, now exists** — §3.9.
>   Both relative gate metrics PASS and fc is *better* than legacy on each: fabricated
>   numbers 2.04% vs 3.06%, forbidden-tool 3.06% vs 4.08%, contradicted claims 0 vs 1,
>   pass rate 60.2% vs 34.7%.
> * **The canned-template fallback is gone.** All 7 soft wraps in the round carry
>   `wrapped_by='llm'`; canned share **0.00%** (was ~80% conversion once a wrap fired).
> * **PR #9** — `memory_context` pre-registration, DESIGN ONLY. Revision 3, CHANGES
>   REQUESTED twice, awaiting a short final design review. **Not approved or frozen.**
> * **PR #12** (lever ledger) and **PR #15** (output-length/latency pre-registration, every
>   threshold still `<TO BE FILLED>`) are OPEN. **#15 must not be filled in against the
>   8466ms now on record** — that is back-fitting a threshold to a known result (§3.5).
> * **No candidate exists for the §3.3 memory experiment. Do not create one.** `8793c0b` is
>   ordinary merged mainline, *not* a measurement candidate for §3.3 — do not conflate the
>   two: that candidate may carry the memory wiring and nothing else.
>
> **Do NOT cherry-pick product code from either NO-GO branch.** Revisiting memory wiring,
> the critic fallback or tool-surface hardening means a new hypothesis, a new candidate and
> its own gate — each separately. The experiment in §3 is exactly that, done properly.

**Why this file exists:** the per-topic docs live on *different branches*, so whichever
branch you check out shows only part of the picture. Read this first, then the leaf you
need.

---

## 1. Branch map

| branch | head | state | what lives here |
|---|---|---|---|
| `telemetry/v2-layer-b` | **`8793c0b`** | **the trunk — branch from this, not `main`** | infrastructure + contract + defect fixes, all merged |
| `main` | `f20ad11` | **stale, do not use** | 43 commits / 263 files behind mainline |
| `eval/measurement-infrastructure` | `0d710d3` | **MERGED** (PR #6 → `f053508`) | measurement machinery; branch kept so the SHA stays citable |
| `eval/evaluator-contract` | `b7a61d6` | **MERGED** (PR #7 → `32454d3`) | G2/G3/E11 amendments + claim taxonomy |
| `fix/gitleaks-example-secret` | `1f43e53` | **MERGED** (PR #8 → `5402336`) | the searxng example-secret placeholder |
| `fix/fc-wrap-and-critic-evidence` | `379e317` | **MERGED** (PR #11 → `103eef8`) | canned-fallback conversion + evidence-blind critic repair |
| `docs/handoff-refresh` | `4af58e1` | **MERGED** (PR #10 → `042c477`) | HANDOFF §3.5/§3.6/§3.7 |
| `fix/retired-model-default` | `7568513` | **MERGED** (PR #13 → `a5a5110`) | `app/config.py` retired-model default + one-env-one-default invariant test |
| `fix/observer-wired-at-startup` | `705a33e` | **MERGED** (PR #14 → `8793c0b`) | zero-LLM-call turns stay in the population |
| `design/memory-context-preregistration` | `e91293f` | **PR #9 OPEN, rev 3, under review** | DESIGN ONLY pre-registration. No candidate. |
| `docs/output-length-latency-prereg` | — | **PR #15 OPEN, DESIGN ONLY** | every threshold `<TO BE FILLED>`; see §3.5 before filling any of them |
| `fastpath/deterministic-phase1` | `7842f60` | **TERMINATED / NO-GO** | the deterministic fast path + its full record |
| `hardening/correctness-only` | `ae1c035` | **TERMINATED / NO-GO** | product candidate `d2004e0`; the correctness bundle |
| `measurement/capture-e7977e6` | `8c96c12` | retained, reproduction only | baseline capture tree (evidence probe on `e7977e6`) |

Verify: `git branch -v` · `gh pr list --state open`

**`af65e40` is poison.** It is a twin of `994be81` with the same commit message but an
untracked results package (`evaluation/results/schema_compaction_ab_2026-07-22/`, 3469
lines) swept in by `git add -A`. `994be81` is the clean replacement. Never merge `af65e40`;
earlier notes that named it as the shippable SHA were wrong.

---

## 2. Document index — what to read, and where it lives

| document | branch | read it for |
|---|---|---|
| **`docs/HANDOFF.md`** (this file) | mainline | the map. Start here. |
| **`docs/memory_context_preregistration.md`** | `design/memory-context-preregistration` | **the next experiment, in full.** Hypothesis, 120-line change budget, injection invariants I1–I8, endpoints RCL1–RCL4, gate N1–N6, §6.0–6.6 sequence, Appendix A recall probe shard. |
| `docs/evaluator_contract.md` | mainline | what the contract branch changed, the 588-grading offline delta, and the two false positives that measurement caught |
| `docs/fastpath_handoff.md` | `fastpath/deterministic-phase1` | **the fullest single record.** Final ledger, both NO-GO reasons, the §9 trap list (15 entries, all paid for), and the historical validation sequence kept for reproduction. |
| `docs/fc_fastpath_design.md` | `fastpath/deterministic-phase1` | the fast-path design v2.1 and its TERMINATED status block (two independent vetoes, with numbers). |
| `docs/fc_followup_filter.md` | `fastpath/…` + `hardening/…` | the route-conformance rules: H3/H9/H1/H13/H4 and the later H13b `web_search` suppression. |
| `docs/hardening_correctness_only.md` | `hardening/correctness-only` | what the correctness bundle carried and deliberately did not; the pre-registered Base98 gate; round 1 and round 2 results; the TERMINATED block. |
| `docs/recall_case_audit.md` | `hardening/correctness-only` | per-case ruling on when an EMPTY tool trace is contract-legal for a recall case, with the evidential criterion. |
| `docs/eval_infrastructure.md` | `eval/measurement-infrastructure` | what the shippable branch adds, and why items 5–6 exist (both are scars). |
| `docs/canary_runbook.md` | all branches | canary/rollout operations: image build out of band, stage table, gate metrics, rollback. **Read §1 "Image build" before building any candidate.** |
| `docs/output_length_latency_preregistration.md` | `docs/output-length-latency-prereg` (PR #15) | the surviving latency lever, as a design. **Every threshold is `<TO BE FILLED>` and §3.5 constrains how they may be filled.** |
| `.runtime/round-8793c0b-internal-2026-07-25/README.txt` | deploy tree, not committed | procedure and caveats for the round of record (§3.8/§3.9). Authoritative on how that round was actually run. |

Verify a doc's branch: `git ls-tree -r --name-only <branch> -- docs/`

---

## 3. Where the work is RIGHT NOW, and what happens next

### 3.1 What has landed, in order

| # | PR | what | result |
|---|---|---|---|
| 1 | **#6** | `eval/measurement-infrastructure` | merged as `f053508`. Three-layer identity, evidence persistence, single-evaluator re-score, shard preflight, out-dir reuse guard. |
| 2 | **#7** | `eval/evaluator-contract` | merged as `32454d3`. G2/G3/E11 case amendments + six claim-taxonomy rules. |
| 3 | **#8** | gitleaks fix | merged as `5402336`. Secret scan green — keep it that way. |
| 4 | **#11** | `fix/fc-wrap-and-critic-evidence` | merged as `103eef8`. Canned-fallback conversion + evidence-blind critic repair. Touches no gate threshold. **Measured effect: canned share of soft wraps 0.00% (§3.8).** |
| 5 | **#10** | `docs/handoff-refresh` | merged as `042c477`. §3.5/§3.6/§3.7. |
| 6 | **#13** | `fix/retired-model-default` | merged as `a5a5110`. `app/config.py` still defaulted to the provider-retired `deepseek-chat`; adds a source scan, a resolved-default test and a one-env-one-literal-default invariant. |
| 7 | **#14** | `fix/observer-wired-at-startup` | merged as **`8793c0b`**. A zero-LLM-call turn in a process that had built no model emitted a contract-invalid record and left the population — distorting p50 and every rate denominator. Verified fixed in production (§3.8). |
| — | **#9** | pre-registration | **OPEN, rev 3, under review. Design only.** |
| — | **#12** | lever ledger (docs) | **OPEN.** |
| — | **#15** | output-length/latency pre-registration | **OPEN, DESIGN ONLY**, all thresholds `<TO BE FILLED>`. See §3.5. |

The order is the point: measurement first so a re-score is trustworthy, contract second so
the bar is stable, product last. Do not reorder it.

### 3.2 The gating sequence — nothing may skip ahead

```
1. PR #9 rev-3 design review passes               <- CURRENT POSITION
   - review exact head e91293f, not the stale PR body
   - known clerical fix before sign-off: §6.6 still says §5.1.3 (E1–E5);
     revision 3 renamed/moved these to §5.1.4 (RCL1–RCL4)
   - this is design acceptance only; placeholders still authorise nothing
2. merge PR #8 (green)                            <- DONE, 5402336
3. probe-shard contract PR, branched from the current mainline commit
   - cases_recall_probe.jsonl, 12 cases, spec'd in Appendix A of the prereg
   - its own review; NOT generated until #9's design is approved
4. backfill the 13 <TO BE FILLED> identity fields in the prereg; FINAL FREEZE of #9
5. only then: build the candidate from BASELINE_PRODUCT_SHA
```

**Approval is of a filled-in document.** A pre-registration approved with placeholders
authorises nothing.

Until step 1 is complete, **do not open the probe-shard PR**. Until steps 2–4 are complete,
**do not create a product candidate or run a paid command**.

### 3.3 The experiment, in one paragraph

Single hypothesis: *correctly injecting long-term memory into the FC arm improves
cross-session recall and does not degrade other tasks.* On mainline `create_initial_state`
hard-codes `memory_context=""` (`src/uk_rent_agent/agent/state.py` → `create_initial_state`,
the `memory_context` initializer — line 130 at `8793c0b`; cite the symbol, not the line, since
PR #11's `wrapped_by` channel already shifted it once), so the retrieved
block never reaches the FC message array. Permitted change surface is three product files
plus tests, **≤120 lines**. The trap: the production entry point **already prefixes** the
retrieved block onto the query string, so an FC path reading both `user_query` and
`memory_context` shows the model the block **twice** — which improves recall while
invalidating the result. Invariant I4 asserts the value appears **exactly once**.

### 3.4 Three SHAs, deliberately distinct

```
BASELINE_PRODUCT_SHA  = the commit after PR #8 merges — the candidate's ONLY parent
PREREG_SHA            = the approved PR #9 head; must NOT be an ancestor of the candidate
CANDIDATE_SHA         = BASELINE_PRODUCT_SHA + memory wiring only
```

If the candidate branched from a mainline containing the pre-registration document, the
static-diff gate would see that doc and fail the three-product-files rule. Rule C1 asserts
this mechanically.

### 3.5 Latency position — STAGE-PAUSE stands, and the SLO is UNCHANGED

**Two independent rounds now agree. Neither revises the other; both stand as taken.**

```
2026-07-22, candidate 2d48d22, 100 records      fc p50 6878ms > 6000ms  -> STAGE-PAUSE (2)
2026-07-25, candidate 8793c0b,  67 turns        fc p50 8466ms > 6000ms  -> STAGE-PAUSE (2)
```

The two are **not comparable to each other**: they used different probe populations (the
07-22 round used an unrecorded 10-message set; the 07-25 round declares the 67 single-turn
cases of `evaluation/benchmark/cases.jsonl`, an adversarial retrieval-heavy corpus that is a
*harder* workload than real traffic). Neither number may be quoted as a trend against the
other. Fixing one population for all future rounds is a precondition for ever claiming a
trend. §3.8 has the second round in full.

**Consequence for PR #15.** 8466ms is now a known result. Filling #15's `<TO BE FILLED>`
thresholds against it would be choosing a pass mark after seeing the measurement it judges —
the exact move rejected below. #15 must either be frozen on grounds independent of this
round, or validated on a **fresh, independent** round. The 07-25 round cannot serve as #15's
validation round.

**`P50_LIMIT_MS` remains 6000. The SLO has NOT been revised, and this round is NOT
retroactively passed.** The 6 s bar is not a number derived from this measurement: it was
pre-registered in `14312f0` on **2026-07-20** — `scripts/canary_report.py:48` and
`docs/canary_runbook.md:174` — while the paired control ran on **2026-07-22**. Downgrading
it to a non-gating "reference" *after* seeing 6878 ms would be changing the decision rule
once the result was known, which is exactly what this project refuses to do. It was
proposed in review on 2026-07-25 and **rejected** on that ground.

**p95 does not substitute for p50.** They control different things — p95 bounds the tail,
p50 bounds the typical user's wait. Keeping only the 30 s p95 gate would permit most turns
to get steadily slower while the gate stayed green.

**The open measurement question, recorded but NOT acted on.** Recomputation from
`.runtime/paired/` (2026-07-25) shows the aggregate p50 comparison is confounded by
response-type mix: legacy returned `clarification` on **25/50** paired turns (p50 1,323 ms)
and `chat` on the other 25. On the turns legacy actually answered its p50 was **7,870 ms**
(20/25 over 6 s) against fc's 9,322 ms, and its p95 was 47,215 ms against fc's 18,703 ms.
So the paired control does not establish that 6 s is reachable *at fc's answer quality*.

That finding does **not** license relaxing this gate. The correct handling, and the only
one authorised:

1. keep this round's verdict as it stands — no retroactive green;
2. pre-register a **separate v2 gate** that defines the answer/clarification stratification
   and its quality conditions **in advance**;
3. freeze that definition, then validate it in a **fresh, independent round**.

A v2 gate, if it is ever written, is an **independent forward-only rule**. It must never be
recorded as a revision of this round's SLO, and it may not be applied to any measurement
taken before it was frozen.

### 3.6 Cutover rule — every gate is necessary; none substitutes for another

**A public cutover requires ALL gates to pass, p50 included. If a verification round reports
`STAGE-PAUSE (exit 2)` for any reason, there is no cutover.**

The quality criteria the owner set on 2026-07-25 — no principled errors, no pile of
hallucinations, not constantly falling back to fixed templates — are **additional necessary
conditions layered on top of the existing gates**. They do not replace, outweigh or excuse
any of them. "Quality is good enough, so ship despite p50" is the same relaxation as editing
`P50_LIMIT_MS`, arrived at by a different route, and it is refused on the same ground
(§3.5). This drift was proposed and corrected once already; do not re-derive it.

This prediction was **made in advance and then tested**. Before the 07-25 round this section
said PR #11 "does not target p50 and must not be expected to move it… the honest expectation
is p50 still ≈6.9 s, still STAGE-PAUSE, still no cutover." The round returned STAGE-PAUSE, as
predicted; it also confirmed the fix's intended effect (canned share of soft wraps 0.00%).
The prediction is left standing here on purpose — a pre-registration is only worth anything
if the failed and the fulfilled ones are both kept.

**The quality result does not change the answer.** The 07-25 round showed fc is *better than
production legacy* on every principled-error metric (§3.9). That is a genuine result and it
is still not a cutover: p50 is an absolute gate, not a comparison. "fc is better than what we
ship today, so ship it" is the same relaxation as editing `P50_LIMIT_MS`, reached by a third
route, and it is refused on the same ground.

Reaching a cutover legitimately requires one of exactly two things:

1. **product work that actually moves the median**, or
2. **a separately pre-registered, frozen, forward-only v2 gate** (§3.5) validated on a
   fresh independent round.

On (1), the lever ledger as of 2026-07-25 — prompt size, message array, call count and
schema compaction are all **refuted**; call count was closed by arithmetic (2nd-call marginal
cost −280 ms; 2-call turns only 50.0% under the bar; 3+ call turns 0%; a p50 under the bar
needs >50% of *all* turns under it). Two candidate levers survive, and §3.8 shows they are
**not yet separable**, so neither may be planned against yet.

Ordering, not negotiable: build out of band from the merged mainline and smoke -> full
verification on the new SHA -> cutover **only if every gate passes**. Note that steps 1–3 are
now done for `8793c0b` and step 4 failed, so the next candidate must repeat all of them.

### 3.7 Re-pinning for any new build

`/etc/rentcompass/deploy.env` currently authorises **`2d48d225bc9a99eb4c5e982a9e86105158503b4b`
and nothing else**, and that pin is verified open (HEAD == pin, tracked tree clean). It is
not a standing permission: producing a new merge SHA means the deploy tree AND
`DEPLOY_PINNED_SHA` must BOTH be moved to that approved full SHA before building. The gate
demands exact equality, so moving one without the other fails closed — which is the intended
behaviour, not a fault.

**The 07-25 verification round required no re-pin and did not perform one.** The fc image was
built out of band from an isolated worktree, so `deploy/update.sh` never ran and the pin gate
was never touched; the deploy tree is still `2d48d22` and the public container
(`b7529f45c3c8`) was never recreated. A re-pin to `8793c0b…` is required only if a public
cutover is ever authorised — which, per §3.6, it currently is not.

### 3.8 The 2026-07-25 verification round — STAGE-PAUSE, and one unattributed 2232ms

Evidence package: **`.runtime/round-8793c0b-internal-2026-07-25/`** (98 files, SHA256SUMS
verified). Its README is authoritative on procedure; this is the summary.

**Round A — canary telemetry over the live serving path.** Declared population: the 67
single-turn cases of `evaluation/benchmark/cases.jsonl`. The 31 cases carrying a
`conversation_history` are excluded because the HTTP path cannot faithfully replay their
fixed assistant turns. 67/67 HTTP 200, external anchor matched 67/67, one candidate SHA,
150 LLM calls / 1,280,356 input tokens (71.2% cache read) / 33,389 output.

| gate | value | |
|---|---|---|
| p50 | **8466 ms** > 6000 | **BREACH** — only 20/67 (29.9%) under the bar |
| partial+soft | **10.45%** > 10% | **BREACH** — 7 wraps, all 25–29 s |
| p95 | 28460 ms < 30000 | pass, 1540 ms of margin |
| zero-tolerance | all 0 | clean |
| 5xx | 0 | pass |

**PR #11's fix is confirmed in production: all 7 soft wraps carry `wrapped_by='llm'` and the
canned share is 0.00%**, against a ~80% conversion rate before. The single "security
non-clean" record is G1 `denied_recall` with `dispatch_started=false` — the runbook's
documented safe path, not a zero-tolerance event.

**PR #14's fix is confirmed in production**: the greeting-as-first-request turn now emits
`llm_usage_status=no_llm_calls` with `provider_schema_400_count=0` (was
`not_instrumented`/`null`), so `--expect-turns` matches where the same smoke on `042c477`
observed one turn fewer than were sent.

**The 2232ms nobody can attribute yet.** Round B's harness measured the same code far faster:

```
eval p50, all 98 cases        5504 ms
eval p50, the SAME 67 cases   6234 ms   (+730ms is pure population effect —
                                         the 31 history cases have p50 4179ms)
canary p50, the SAME 67 cases 8466 ms   (paired median difference +1233ms)
```

Same 67 cases, soft-wrap rate: eval **2/67 = 2.99%** vs canary **7/67 = 10.45%**. Three of
the five canary-only wraps are +18 s to +23 s against their eval run (D5, D11, E7) and all
three had `tool_budget_timeout=true`.

**Two confounds, not separable from this data:**

1. **cache warmth** — Round A ran first on cold listing/crime caches; Round B reused exactly
   what Round A had just populated.
2. **measurement span** — canary `turn_latency_ms` wraps the whole HTTP request (Flask,
   identity resolution, conversation store, memory retrieval, persistence, telemetry); the
   eval harness wraps only the agent invocation.

They imply completely different work — cutting output length versus profiling the serving
path — so **do not plan an optimisation against the 2466 ms deficit until they are
separated.** A controlled diagnostic (re-running the identical 67 cases with caches now warm)
would decide it in one round, and **must be archived as a diagnostic**: re-running a round and
keeping the friendlier number is the post-hoc selection this project refuses.

For reference if the deficit does turn out to be generation-bound, this round's own fit is
`latency ≈ 618 + 16.2·output_tok + 524·llm_calls + 797·tool_batches` (R²=0.69, so indicative
only; the earlier independent fit gave 14.6 ms/token). At a median 452 output tokens, closing
2466 ms through output length alone means cutting ~152 tokens, a **34%** reduction.

### 3.10 Cutover, 2026-07-26 — an owner override, recorded as one

`fc_loop` serves the public edge. `deploy/nginx/…ssl.conf` upstream is `127.0.0.1:5002`;
`https://rentcompass.co.uk:8443/health` returns `x-agent-arch: fc_loop` and
`x-agent-version: 8793c0b17963a6a2b375903a164d3d96395dc834`.

**The p50 gate was not met.** The round of record (§3.8) returned **STAGE-PAUSE (exit 2)** at
8,466 ms cold / 7,402 ms warm against a 6,000 ms bar, and the partial+soft rate was 10.45%
against a 10% ceiling. Neither number improved before the cutover. The owner, who owns the
product and its users, decided to ship anyway and to evaluate by hand. That is a legitimate
call and it is recorded here **as an override, not as a pass**:

* `P50_LIMIT_MS` is still 6000 and is NOT revised.
* §3.8's verdict stands unchanged. This section does not amend it.
* **No later document may cite this cutover as evidence that the gate was satisfied.** The
  gate was not satisfied; a person with the authority to accept the risk accepted it.

**What was known at the time.** fc beat the incumbent on every correctness metric measured
(§3.9): pass 60.2% vs 34.7%, fabricated numbers 2.04% vs 3.06%, forbidden-tool 3.06% vs
4.08%, contradicted claims 0 vs 1, and fc produced 84% more groundable claims. It is slower:
median 7.4 s versus legacy's 2.7 s, and legacy's speed comes substantially from executing no
tool at all on 39.8% of turns. The canned-template fallback is gone (0.00% of soft wraps).
Zero-tolerance metrics were clean.

**Post-cutover verification.** A real answer-producing query through the public edge — not
`/health`, which cannot see a broken pool (§3A) — returned HTTP 200 in 8.6 s with a sourced
answer, `llm_usage_status=complete`, 0 provider 400s, no soft wrap, no DSML leak. Notably it
disclosed missing crime data rather than inventing a score.

**Rollback is one command and takes seconds:**

```
bash deploy/switch_pool.sh --to legacy --allow-unidentified-target
```

`--allow-unidentified-target` is required because the legacy pool still answers
`x-agent-version: unknown` (§3A).

**Open risk, ordered.**

1. **The health monitor is still not running.** Units are installed but the timer is
   `disabled`, and `ExecStart` points at the deploy tree's copy, which is the pre-#18 version
   that would not have caught the 2026-07-24 outage. Until that is fixed, a new architecture
   is live with nothing watching it. This is the highest-priority open item.
2. **`APP_CANDIDATE_SHA` is still unset for the `app` service**, so the rollback target cannot
   state its commit. The earlier plan was to fix this "for free" right after cutover, while
   legacy is idle. **That plan is withdrawn:** rebuilding the only rollback target immediately
   after moving public traffic onto an unproven candidate is the worst possible timing, since
   it is exactly when you are most likely to need it. Do it once fc has run clean for a
   sustained period, and flip back to legacy first if it must be done sooner.
3. The variance study (PR #19) is unfrozen and unrun. Its result now matters more, not less:
   with fc live, every future "did this change help?" question is asked against production.

### 3.9 Phase-2 fc-vs-legacy — the verdict that had never been written

Both arms, 98 cases each, live, same tree, same day. This closes a gap that had been open
since the fc work began: no fc-vs-legacy verdict had ever been recorded.

**The two eval-only STAGE-PAUSE metrics are RELATIVE to legacy (+1pp), so an fc-only sweep
cannot decide them.** Both arms were run. fc PASSES both and is better on each:

| relative gate | fc | legacy | delta | |
|---|---|---|---|---|
| `no_evidence_numbers` | 2/98 = 2.04% | 3/98 = 3.06% | **−1.02pp** | **PASS** |
| `forbidden_tool` | 3/98 = 3.06% | 4/98 = 4.08% | **−1.02pp** | **PASS** |

| quality | fc | legacy |
|---|---|---|
| passed | **60.2%** | 34.7% |
| route_accuracy | **80.6%** | 58.2% |
| grounded | **79.6%** (n=280) | 74.3% (n=152) |
| money_grounded | 84.4% (n=160) | **88.9%** (n=99) |
| contradicted_claims | **0** | 1 |
| task_completion | 100% | 100% |
| latency p50 / p95 | 5481 / 24011 ms | **2672 / 10438 ms** |
| cost | $0.0480 | $0.0218 |

**Read the denominators before quoting the rates.** fc makes ~84% more groundable claims
(280 vs 152) and 62% more money claims (160 vs 99). Legacy's higher `money_grounded` rate and
its lower latency are both partly a consequence of **legacy answering less** — the same effect
recorded on 2026-07-22, where legacy met 6 s only by returning `clarification` on 25/50 paired
turns. A metric computed over a much smaller claim base is not evidence of a better answer.

**Severity is not the same as rate.** fc's two fabrications (C6, C11) are both invented
commute minutes — a figure a user acts on directly. 2.04% is a low rate on a high-stakes
field, not a benign one.

---

## 3A. Operational facts a new session will not otherwise know

* **Dev tree is `/home/shuhan/telemetry-v2-layer-b`.** Never develop in
  `/home/shuhan/uk_rent_recommendation` — that is the deploy tree, on detached HEAD
  `2d48d22`, and it is production. That SHA is the deploy pin (§3.7); it deliberately does
  **not** track mainline, and a docs-only merge must never move it.
* **`gh` is authenticated** as `shuhan-wang1` (scopes `repo`, `read:org`, `gist`,
  `admin:public_key`). PRs, checks and branch protection can all be driven from the CLI.
  The system binary is `/usr/bin/gh` 2.45.0 — old enough that `gh pr edit` hits a
  deprecated Projects-classic GraphQL field; use `gh api -X PATCH repos/.../pulls/N` for
  body edits.
* **PR #9's GitHub body is stale.** It still uses the revision-1 `BASELINE_SHA` wording and
  a three-step sequence. The governing design is the file at head `e91293f`:
  `docs/memory_context_preregistration.md`. Review that file/diff, not the PR body summary.
* **Branch protection is ON for `telemetry/v2-layer-b`**: both checks required, `strict`,
  `enforce_admins: true`, no force-push, no deletion. **`required_pull_request_reviews` is
  deliberately null** — this is a single-maintainer repo and GitHub forbids self-approval,
  so requiring one approval plus `enforce_admins` would lock every PR out permanently. Add
  it only when a second reviewer exists.
* **gitleaks history.** The secret scan was red on PRs #6 and #7 and both merged anyway.
  Cause was pre-existing, not either PR: a committed 64-hex `secret_key` in
  `deploy/searxng-settings.yml.example`. It never applied on the deploy path (compose makes
  `SEARXNG_SECRET` mandatory and overrides it) and does **not** match the live production
  secret, so no rotation is needed. PR #8 replaces it with a placeholder. A permanently red
  scan is how a real leak gets merged unnoticed — keep it green.
* **The repo is public.** Unauthenticated `api.github.com` reads work, which is useful for
  diagnosis but also means committed literals are exposed.
* **Offline suite baseline: 1804 passed, 3 skipped, 1 xfailed** on mainline `8793c0b`
  (1793 at `042c477`, 1785 post-#7, 1710 before the infrastructure work). **The host has no
  `pytest` and no virtualenv** — run it in the `uk-rent-agent:bench-git` image with the
  worktree bind-mounted:
  `docker run --rm -v <worktree>:/patched uk-rent-agent:bench-git bash -c 'pip install -q
  pytest pytest-asyncio; cd /patched && OPENAI_API_KEY=dummy DEEPSEEK_API_KEY=dummy
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest tests/ -q -p no:cacheprovider'`.

**Traps that cost time on 2026-07-25 — every one of these silently produces a plausible
wrong answer rather than an error:**

* **Never `mv` a telemetry log out from under a running pool.** The writer keeps the moved
  inode's fd open, so subsequent turns append to the *archived* file while `.runtime/logs/`
  stays empty. A single free greeting caught this (archive grew 2111 → 3063 bytes); a
  67-turn round would have written entirely into the evidence package. **Archive only after
  recreating the pool**, or `cp` and leave the original live.
* **`canary_report.py --since` does NOT filter records.** It only feeds STAGE-PROGRESS
  elapsed hours; only `--window HOURS` filters. The external-anchor block nevertheless prints
  `window = the selected --window / --since range`, which is a **tool defect** — it invites
  exactly the mistake of assuming `--since` bounded the population. First run of the 07-25
  round counted the warm-up turn and returned INSTRUMENTATION-HOLD because of it.
* **`canary_report.py --json` takes a PATH argument.** A bare `--json` aborts in argparse
  with **exit 2** — indistinguishable from STAGE-PAUSE if only the exit code is checked.
* **Telemetry field names are `telemetry_schema_version` and `strict`**, not
  `schema_version`/`strict_mode`. Probing the wrong names returns `None` and looks like a
  contract violation that is not there.
* **Cold start costs 4.9–8.2 s on a zero-LLM-call turn** (4912 ms and 8172 ms measured on two
  recreates; the same greeting warm costs 334–397 ms). Since PR #14 correctly keeps zero-call
  turns in the population, the first turn after any recreate now lands in the p50 denominator.
  **Warm the pool with a throwaway turn and open the window after it.**
* **The eval harness records `git_commit: null` and `git_dirty: null`** even when `PRODUCT_SHA`
  is passed in the environment. Its summaries **cannot self-identify the commit that produced
  them**; the binding is external. Worth fixing.
* **`scripts/pricing/deepseek_prices_v1.json` is `unverified: true`**, so `canary_cost.py`
  refuses to compute — but `run_benchmark.py` prints a `total_cost_usd` anyway. Do not quote
  the two as if they came from the same verified source.

---

## 3B. Tooling added since the first phase

`/home/shuhan/fp-results/scripts/contract_delta.py` — measures what an **evaluator-contract
change** does to verdicts, using retained evidence only. `score` grades persisted
`grader_input.jsonl` with one tree's evaluator; `compare` diffs two dumps and attributes
every verdict flip to a constraint. No model, no tools, no network, no API spend.

It **deliberately bypasses** `rescore.py`'s contract-identity refusal. That refusal exists
to stop evidence being scored against a contract it was not recorded under — but here that
mismatch *is* the measurement. **Do not "fix" this by loosening `rescore.py`**; that would
remove the guarantee for every other caller. Report from the contract work:
`/home/shuhan/fp-results/contract_delta_2026-07-23.json`.

---

## 4. The two NO-GO conclusions, in one place

### Fast path — two independent vetoes, either sufficient

1. **Measured quality regression.** Base98's first execution: pass **ON 5/20 vs OFF
   12/20**, consistent across all three repeats; C11/D1/D12 failed ON and passed OFF 3/3.
   Pre-existing — `452b569` reproduces 0/3 with identical failing constraints. Root cause:
   **the fast path answers a generic version of the turn and loses the specific question
   asked.**
2. **The quality closure breaks the latency gate.** Usage stays adequate (18/50, bar
   10/50) but predicted p50 is **6,022 ms** @70% savings and **5,836 ms even at 100%**,
   against a 5,800 ms bar.

**Standing conclusion — under the current FC architecture, evidence contract and quality
bar there is NO data-supported path to a 5.8 s p50.** Prompt size was already refuted;
deleting the final answer call cannot satisfy semantic closure and the latency gate at
once. Reopening latency work means contesting one of those premises explicitly.

### Correctness bundle — condition 3

Passed static audit, smoke, loopback memory smoke and a **42/42** guard; failed Base98:

| # | condition | result |
|---|---|---|
| 1 | semantic-pass ≥ baseline | PASS — 160 vs 159 (+1) |
| 2 | route-matched ≥ baseline | PASS — 246 vs 238 (+8) |
| 3 | no case at candidate 0/3 vs baseline 3/3 | **FAIL — A4, A14** |
| 4–6 | hard-gate / zero-tolerance / targeted H-cases | PASS |

* **A14 — CONFIRMED**: the critic fail-closed fallback answers verbatim in all three
  repeats and drops the required "no studios matched" negative fact. **That fallback must
  not be carried elsewhere in its present form.**
* **A4 — behaviour difference CONFIRMED, cause NOT proven.** A tool-surface link is a
  hypothesis only.

**The conclusion is not "roughly equal overall"** — it is that shipping these hardenings
**as one bundle** causes a deterministic, localised quality loss.

---

## 5. Contract debt — what is still open after PR #7

The G2/G3/E11 amendments and the claim-taxonomy changes that this section used to list as
open **landed in PR #7**. See `docs/evaluator_contract.md`. What remains open:

**Excluded from PR #7 because it needs product code from a NO-GO branch:**
`no_false_retrieval_provenance`. Its grader imports `claims_no_retrieval` from
`uk_rent_agent.agent.critic`, which exists **only** on `hardening/correctness-only`.
`evidence_usable` is on mainline; `claims_no_retrieval` is not. Its `schema.json` enum entry
and the H3 guard-case amendment are held back with it. Reviving it means porting the
predicate deliberately, as its own change.

**Excluded on purpose:** `extract_tool_trace` skipping `suppressed` artifacts — evaluator
support for follow-up-capability suppression, which is NO-GO and not being extended.

**Cross-shard debt, recorded not resolved.** `tests/test_case_contract_consistency.py`
carries a `KNOWN_DIVERGENCES` allowlist of three cases defined differently in different
shards. They are different constraint TYPES, so choosing a winner changes what "pass" means
and is its own contract decision:

| case | Base98 | stale shard |
|---|---|---|
| E8 | `must_flag_unrealistic_constraint` | `ext_CDE`: `must_refuse_fabrication` |
| F11 | `must_flag_stale_data` | `ext_FG`: `must_note_missing_data` (still marked NEEDS_CHECKER) |
| G16 | `must_supersede_value` | `ext_FG`: `must_recall_value` |

Shrinking that set is progress; **growing it means an amendment forgot a shard** — which is
exactly what happened to G2/G3/E11 before PR #7 caught it.

---

## 6. Evidence index

**Committed, in-repo** (reproducible packages with manifests + digests):

| package | branch |
|---|---|
| `evaluation/results/fastpath_guard_2026-07-22/` | `fastpath/…` — guard summaries + manifests + REPORT.md with Addenda 1–5 |
| `evaluation/results/fastpath_counterfactual_2026-07-22/` | `fastpath/…` — the 50-turn latency counterfactual (de-identified `per_case.csv`, `MANIFEST.sha256`, rerunnable `counterfactual.py`) |
| `evaluation/results/phase2_ab_2026-07-19/`, `live_routed_98/` | both — pre-existing A/B packages |

**Out of repo, `/home/shuhan/fp-results/`** (large raws; retained, never committed):

| path | what |
|---|---|
| `guard_<sha>/` | every guard run (`452b569`, `b094a04`, `9d8c37b`, `d2004e0`) |
| `base98_r{1,2,3}_{on,off}/` | fast-path Base98 A/B — the veto-1 evidence |
| `hb98_*` + `hb98_gate_result.txt` | correctness round 1 (NOT re-scorable — predates evidence persistence) |
| `idp98_*` + `idp98_rescore.json` | correctness round 2 — the deciding measurement, fully re-scorable |
| `pools-<sha>/` | archived canary logs of every retired pool |
| `diagnostics-b094a04-notrun/` | prepared-but-never-run diagnostics, with `STATUS.txt` saying why |

**Deploy tree, `/home/shuhan/uk_rent_recommendation/.runtime/`** (2026-07-25; all carry
`SHA256SUMS.txt`, none is eligible for an official denominator except where stated):

| path | what |
|---|---|
| `round-8793c0b-internal-2026-07-25/` | **THE ROUND OF RECORD** (§3.8/§3.9). Canary telemetry + both eval arms + bodies + manifests + both gate reports. A later run may diagnose this but may not replace its verdict. |
| `archive-smoke-8793c0b-2026-07-25/` | the post-fix smoke that cleared `8793c0b` for the round. Contains `canary-fc_loop.jsonl.leaked-fd`, the file the running container kept appending to after it was moved — retained deliberately as the evidence for the `mv` trap in §3A. |
| `diagnostics-042c477-provider400-2026-07-25/` | the failed smoke that exposed the retired-model outage: report.json, exit 3, both bodies, container log with the traceback |
| `archive-smoke-and-restore-2026-07-25/` | the `042c477` smoke and the public-pool restore turns |
| `logs-archive-pre-042c477/` | four earlier JSONLs including the v1 `canary-legacy.jsonl`, which must stay out of `--input` or directory aggregation fails closed on schema v1 |

---

## 7. Ops scripts — `/home/shuhan/fp-results/scripts/` (outside the repo, no SHA)

| script | purpose |
|---|---|
| `launch_fp.sh`, `smoke_fp.sh`, `base98_fp.sh`, `round_fp.sh`, `base98_analyze.py` | fast-path pools + sequence (`CAND=<sha>`) |
| `launch_hardening.sh`, `smoke_hardening.sh`, `memory_smoke_hardening.sh` | correctness single-pool + the loopback memory gate |
| `base98_paired_hardening.sh`, `base98_paired_identity.sh` | paired A/B runners (the latter with three-layer identity) |
| `hb98_gate.py`, `idp98_gate.py` | the pre-registered Base98 gates (round 1 / round 2) |
| `capture_allowlist_check.sh` | asserts a capture commit touches only pre-registered evaluation paths — also committed as `scripts/eval_capture_allowlist_check.sh` |

---

## 8. Rules that still stand

1. **Never develop in the deploy tree** `/home/shuhan/uk_rent_recommendation` (detached
   HEAD, production pin). Dev tree is `/home/shuhan/telemetry-v2-layer-b`.
2. **Never modify or restart** `uk-rent-app` (:5001) or `uk-rent-app-fc` (:5002).
3. **Any code change ⇒ new SHA ⇒ restart from smoke.** A candidate whose code changed
   mid-sequence is no longer a measurement candidate.
4. **Nothing ships on reasoning alone.** Only repeated, interleaved A/B is evidence; every
   failed round is retained, never overwritten.
5. **Build only from clean checkouts** (`git status --porcelain` empty). The dev tree was
   clean when this handoff was refreshed; verify again immediately before every build.
6. **Score both arms of an A/B with ONE evaluator.** Each arm ships its own grader, so
   comparing each arm's own `passed` compares two evaluators as much as two products.
7. **Hard cost cap on every paid command** (`--max-cost-usd 5` standing).
8. **Declare the population before the round, and reuse it.** A p50 is a statement about a
   population, not about a build. The 07-22 and 07-25 rounds are both valid and mutually
   incomparable because their probe sets differ (§3.5). The 07-25 population — the 67
   single-turn cases of `evaluation/benchmark/cases.jsonl` — is now the declared one; changing
   it forfeits every comparison against this round.
9. **A re-run is a diagnostic, never a re-roll.** If a round is repeated to explain a result,
   the first round stays the round of record and the repeat is archived separately as a
   diagnostic. Keeping the friendlier of two runs is post-hoc selection, the same defect as
   editing a threshold after seeing the measurement (§3.5).
10. **Read denominators before quoting rates.** An arm that answers less produces fewer
    claims and so scores better per claim while helping the user less — this is exactly how
    legacy outscores fc on `money_grounded` and on latency (§3.9).

Full binding-rule list and the 15-entry trap list: `docs/fastpath_handoff.md` §2 and §9 on
`fastpath/deterministic-phase1`.

---

## 9. Audit it yourself

Every claim above is checkable without re-running anything paid:

```bash
cd /home/shuhan/telemetry-v2-layer-b

# branch states
git branch -v

# the infra branch changes EXACTLY these 8 files and nothing else. Check the whole
# list, not just the absence of product paths: writing this audit is what caught an
# untracked local results package that `git add -A` had swept into the commit.
git diff --name-only e7977e6 eval/measurement-infrastructure
#   .gitignore  docs/HANDOFF.md  docs/eval_infrastructure.md  evaluation/rescore.py
#   evaluation/results_package.py  evaluation/run_benchmark.py
#   scripts/eval_capture_allowlist_check.sh  tests/test_eval_measurement_infra.py
git diff --name-only e7977e6 eval/measurement-infrastructure \
  | grep -vE '^(\.gitignore|docs/(HANDOFF|eval_infrastructure)\.md|evaluation/(rescore|results_package|run_benchmark)\.py|scripts/eval_capture_allowlist_check\.sh|tests/test_eval_measurement_infra\.py)$'
#   ^ must print NOTHING

# the baseline capture tree is evaluation-only
BASE=e7977e6 CAP=8c96c12 bash /home/shuhan/fp-results/scripts/capture_allowlist_check.sh

# the deciding measurement, re-scored by one evaluator (no network, no model)
python3 /home/shuhan/fp-results/scripts/idp98_gate.py

# offline suite on the shippable branch
docker run --rm -v /home/shuhan/telemetry-v2-layer-b:/patched uk-rent-agent:bench-git bash -c '
pip install -q pytest pytest-asyncio 2>/dev/null
cd /patched && OPENAI_API_KEY=dummy DEEPSEEK_API_KEY=dummy HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python -m pytest tests/ -q -p no:cacheprovider'
```

Expected: both greps print nothing · `ALLOWLIST-PASS` · `BASE98-GATE FAIL: condition 3 ['A4','A14']`
· `1785 passed, 3 skipped`.

Note `idp98_gate.py` is *expected to exit 1* — it reports the failure that closed the
branch. That is the record, not a broken tool.

---

*Total live API spend across the whole effort: **< $1**. Record closed 2026-07-23.*

*(The other branches' SHAs are pinned because they are terminal and will not move. This
branch's own head is deliberately NOT pinned here: a SHA written into a file that is part
of the commit can never match it.)*
