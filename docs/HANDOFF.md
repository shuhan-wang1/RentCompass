# HANDOFF — START HERE

Master index for the fc_loop work. **Every other document is a leaf; this is the only file
that spans all branches.** Last updated 2026-07-23.

> ## Status: ACTIVE — measurement and contract have landed; one product experiment is being pre-registered.
>
> The 2026-07-19 → 07-23 latency/correctness phase is **closed** (two product experiments
> NO-GO, §4). What followed is a deliberate rebuild in order — **infrastructure → contract
> → product**. The first two have merged. The product experiment exists **only as a design
> under review**: no candidate has been built, no paid run has happened.
>
> **Right now:**
> * `telemetry/v2-layer-b` is the **mainline**, not `main` (43 commits / 263 files behind,
>   not a usable base for anything).
> * **PR #8** — gitleaks fix. OPEN, both checks green, `mergeStateStatus: CLEAN`. Merge next.
> * **PR #9** — `memory_context` pre-registration, DESIGN ONLY. Revision 3 (`e91293f`),
>   CHANGES REQUESTED twice, awaiting a short final review.
> * **No product candidate exists. Do not create one.** §3 has the gating sequence.
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
| `telemetry/v2-layer-b` | **mainline** | **the trunk — branch from this, not `main`** | infrastructure + contract, both merged |
| `main` | `f20ad11` | **stale, do not use** | 43 commits / 263 files behind mainline |
| `eval/measurement-infrastructure` | `0d710d3` | **MERGED** (PR #6 → `f053508`) | measurement machinery; branch kept so the SHA stays citable |
| `eval/evaluator-contract` | `b7a61d6` | **MERGED** (PR #7 → `32454d3`) | G2/G3/E11 amendments + claim taxonomy |
| `fix/gitleaks-example-secret` | `1f43e53` | **PR #8 OPEN, both checks green** | the searxng example-secret placeholder |
| `design/memory-context-preregistration` | `e91293f` | **PR #9 OPEN, rev 3, under review** | DESIGN ONLY pre-registration. No candidate. |
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
| `docs/canary_runbook.md` | all branches | pre-existing canary/rollout operations. Unrelated to these experiments. |

Verify a doc's branch: `git ls-tree -r --name-only <branch> -- docs/`

---

## 3. Where the work is RIGHT NOW, and what happens next

### 3.1 What has landed, in order

| # | PR | what | result |
|---|---|---|---|
| 1 | **#6** | `eval/measurement-infrastructure` | merged as `f053508`. Three-layer identity, evidence persistence, single-evaluator re-score, shard preflight, out-dir reuse guard. |
| 2 | **#7** | `eval/evaluator-contract` | merged as `32454d3`. G2/G3/E11 case amendments + six claim-taxonomy rules. |
| 3 | **#8** | gitleaks fix | **OPEN, green, ready.** |
| 4 | **#9** | pre-registration | **OPEN, rev 3, under review. Design only.** |

The order is the point: measurement first so a re-score is trustworthy, contract second so
the bar is stable, product last. Do not reorder it.

### 3.2 The gating sequence — nothing may skip ahead

```
1. PR #9 final review passes                      <- CURRENT POSITION
2. merge PR #8 (green)
3. probe-shard contract PR, branched from the post-#8 mainline commit
   - cases_recall_probe.jsonl, 12 cases, spec'd in Appendix A of the prereg
   - its own review; NOT generated until #9's design is approved
4. backfill the 13 <TO BE FILLED> identity fields in the prereg; FINAL FREEZE of #9
5. only then: build the candidate from BASELINE_PRODUCT_SHA
```

**Approval is of a filled-in document.** A pre-registration approved with placeholders
authorises nothing.

### 3.3 The experiment, in one paragraph

Single hypothesis: *correctly injecting long-term memory into the FC arm improves
cross-session recall and does not degrade other tasks.* On mainline `create_initial_state`
hard-codes `memory_context=""` (`src/uk_rent_agent/agent/state.py:123`), so the retrieved
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

---

## 3A. Operational facts a new session will not otherwise know

* **Dev tree is `/home/shuhan/telemetry-v2-layer-b`.** Never develop in
  `/home/shuhan/uk_rent_recommendation` — that is the deploy tree, on detached HEAD
  `20627c5`, and it is production.
* **`gh` is authenticated** as `shuhan-wang1` (scopes `repo`, `read:org`, `gist`,
  `admin:public_key`). PRs, checks and branch protection can all be driven from the CLI.
  The system binary is `/usr/bin/gh` 2.45.0 — old enough that `gh pr edit` hits a
  deprecated Projects-classic GraphQL field; use `gh api -X PATCH repos/.../pulls/N` for
  body edits.
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
* **Offline suite baseline: 1785 passed, 3 skipped** on mainline post-#7 (was 1710 before
  the infrastructure and contract work). Run it in the `uk-rent-agent:bench-git` image.

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
5. **Build only from clean checkouts** (`git status --porcelain` empty) — the dev tree has
   an untracked results dir.
6. **Score both arms of an A/B with ONE evaluator.** Each arm ships its own grader, so
   comparing each arm's own `passed` compares two evaluators as much as two products.
7. **Hard cost cap on every paid command** (`--max-cost-usd 5` standing).

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
· `1710 passed, 3 skipped`.

Note `idp98_gate.py` is *expected to exit 1* — it reports the failure that closed the
branch. That is the record, not a broken tool.

---

*Total live API spend across the whole effort: **< $1**. Record closed 2026-07-23.*

*(The other branches' SHAs are pinned because they are terminal and will not move. This
branch's own head is deliberately NOT pinned here: a SHA written into a file that is part
of the commit can never match it.)*
