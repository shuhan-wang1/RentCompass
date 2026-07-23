# HANDOFF — START HERE

Master index for the fc_loop latency + correctness work (2026-07-19 → 2026-07-23).
**Every other document is a leaf; this is the only file that spans all branches.**

> ## Status: CLOSED. Two product experiments ruled NO-GO; one infrastructure branch ships.
>
> * No outstanding release or measurement action.
> * No experiment pools running. Production `uk-rent-app` / `uk-rent-app-fc` never touched.
> * **Do NOT cherry-pick product code from either NO-GO branch.** Revisiting memory
>   wiring, the critic fallback, or tool-surface hardening means a new hypothesis, a new
>   candidate and its own gate — each separately.

**Why this file exists:** the per-topic docs each live on a *different branch*, so
whichever branch you check out shows only part of the picture. Read this first, then the
leaf you need.

---

## 1. Branch map

| branch | head | state | what lives here |
|---|---|---|---|
| `telemetry/v2-layer-b` | `e7977e6` | **accepted mainline** | the base both experiments branched from |
| `eval/measurement-infrastructure` | *(this branch — `git rev-parse --short HEAD`)* | **SHIPPABLE — the only one** | measurement machinery, no product change |
| `fastpath/deterministic-phase1` | `7842f60` | **TERMINATED / NO-GO** | the deterministic fast path + its full record |
| `hardening/correctness-only` | `ae1c035` | **TERMINATED / NO-GO** | product candidate `d2004e0`; the correctness bundle |
| `measurement/capture-e7977e6` | `8c96c12` | retained, reproduction only | baseline capture tree (evidence probe on `e7977e6`) |

Verify: `git branch -v`

---

## 2. Document index — what to read, and where it lives

| document | branch | read it for |
|---|---|---|
| **`docs/HANDOFF.md`** (this file) | `eval/measurement-infrastructure` | the map. Start here. |
| `docs/fastpath_handoff.md` | `fastpath/deterministic-phase1` | **the fullest single record.** Final ledger, both NO-GO reasons, the §9 trap list (15 entries, all paid for), and the historical validation sequence kept for reproduction. |
| `docs/fc_fastpath_design.md` | `fastpath/deterministic-phase1` | the fast-path design v2.1 and its TERMINATED status block (two independent vetoes, with numbers). |
| `docs/fc_followup_filter.md` | `fastpath/…` + `hardening/…` | the route-conformance rules: H3/H9/H1/H13/H4 and the later H13b `web_search` suppression. |
| `docs/hardening_correctness_only.md` | `hardening/correctness-only` | what the correctness bundle carried and deliberately did not; the pre-registered Base98 gate; round 1 and round 2 results; the TERMINATED block. |
| `docs/recall_case_audit.md` | `hardening/correctness-only` | per-case ruling on when an EMPTY tool trace is contract-legal for a recall case, with the evidential criterion. |
| `docs/eval_infrastructure.md` | `eval/measurement-infrastructure` | what the shippable branch adds, and why items 5–6 exist (both are scars). |
| `docs/canary_runbook.md` | all branches | pre-existing canary/rollout operations. Unrelated to these experiments. |

Verify a doc's branch: `git ls-tree -r --name-only <branch> -- docs/`

---

## 3. The two NO-GO conclusions, in one place

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

## 4. Still OPEN — not closed, not landed

The **G2 / G3 / E11 case-contract amendments** and the **grader taxonomy changes**
(two-sided English thresholds, CJK minute blind spot, hypothetical-constraint classifier,
`must_complete_requested_dimensions`) are **evaluator-contract** changes: they alter what
"pass" means. Deliberately excluded from the infrastructure branch; they live on
`hardening/correctness-only` awaiting their own review.

---

## 5. Evidence index

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

## 6. Ops scripts — `/home/shuhan/fp-results/scripts/` (outside the repo, no SHA)

| script | purpose |
|---|---|
| `launch_fp.sh`, `smoke_fp.sh`, `base98_fp.sh`, `round_fp.sh`, `base98_analyze.py` | fast-path pools + sequence (`CAND=<sha>`) |
| `launch_hardening.sh`, `smoke_hardening.sh`, `memory_smoke_hardening.sh` | correctness single-pool + the loopback memory gate |
| `base98_paired_hardening.sh`, `base98_paired_identity.sh` | paired A/B runners (the latter with three-layer identity) |
| `hb98_gate.py`, `idp98_gate.py` | the pre-registered Base98 gates (round 1 / round 2) |
| `capture_allowlist_check.sh` | asserts a capture commit touches only pre-registered evaluation paths — also committed as `scripts/eval_capture_allowlist_check.sh` |

---

## 7. Rules that still stand

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

## 8. Audit it yourself

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
