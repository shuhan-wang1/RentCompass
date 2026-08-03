# Pre-registration — round-to-round variance of the gate statistic

**Status: DRAFT, NOT FROZEN.** Nothing may be run against this document until the owner
freezes it. Once frozen, no field below may be edited; a changed design is a new document
with a new SHA, and any round already run under the old one keeps its own verdict.

**Version:** 1 · **Authored:** 2026-07-26 · **Base:** mainline `d7f6f505`

---

## 0. What is being estimated, and what is NOT

This is the step most likely to be got wrong, so it is first.

The observation that motivated this study is **per-case**: on 2026-07-25 case `E6` moved from
15,382 ms to 29,374 ms between two runs of identical code (LLM calls 2 → 5), and 22 of 64
paired cases got slower. But **the gate does not read per-case latency. It reads a
round-level p50**, and a median is robust to exactly the kind of tail movement `E6`
exhibits. Concluding "single-round comparisons are untrustworthy" from per-case dispersion
would be building a possibly-false conclusion on a correct observation.

> **ESTIMAND — the one number this study exists to produce**
>
> **σ(p50)** — the standard deviation of the **round-level p50 of `turn_latency_ms`**, over
> repeated executions of the *entire* round against a *fixed* commit.

Per-case standard deviation is recorded as a **diagnostic** that may explain the mechanism.
It is explicitly **not** the estimand and no decision in §5 may be taken from it.

**Nothing in this document is a claim about whether fc should ship.** It measures the
precision of the instrument, not the thing being measured. The `8793c0b` STAGE-PAUSE verdict
of 2026-07-25 stands regardless of the outcome and is not re-opened by it.

---

## 1. Population and instrument — reproduce the round of record, do not improve it

| | fixed value |
|---|---|
| **population** | the **67 single-turn cases** of `evaluation/benchmark/cases.jsonl` **at commit `8793c0b`**, in the order recorded in `.runtime/round-8793c0b-internal-2026-07-25/manifest.json` |
| **instrument** | **canary telemetry** `turn_latency_ms`, read from `.runtime/logs/canary-fc_loop.jsonl` |
| **arm** | fc_loop only, `127.0.0.1:5002`, image `uk-rent-agent:canary-fc-loop-8793c0b` |
| **commit** | `8793c0b17963a6a2b375903a164d3d96395dc834`, unchanged across every round |

This is the population declared in `HANDOFF §3.8` and the instrument that produced the
8,466 ms STAGE-PAUSE. **It must not be substituted.**

**The selection rule is mechanical, and that is an assertion, not a description.** Selecting
every case with `conversation_history == []` from `cases.jsonl` at `8793c0b` yields a set
**exactly equal** to the 67 `case_id`s in the round-of-record manifest — zero extra, zero
missing (verified 2026-07-26). Any round whose selection does not reproduce that equality is
excluded. Hashing the corpus alone would not have pinned this: the 98 → 67 rule is a separate
degree of freedom, so a different filter could yield the same corpus hash over a different
population. Hence `SELECTION_SHA256` below, alongside `CORPUS_SHA256`.

**The corpus hash is commit-qualified for a reason that has already bitten this population.**
`cases.jsonl` differs between `2d48d22` and `8793c0b`: PR #7's contract amendments changed
**E11, G2 and G3**. G2 and G3 are multi-turn and fall outside this population, but **E11 is
single-turn and is one of these 67**. Taking the hash "of `cases.jsonl`" without naming the
commit would therefore silently pin a different input.

In particular, the 98-case **eval-harness** figures (`sweep/per_case.csv`) may **not** be used
for this study, even though that instrument is cheaper and its data is already on disk. Its
paired median differs from the canary path by **+599 ms** on the identical 67 cases (measured
2026-07-25), so a σ computed there would be a *third* number that cannot be cited against
either of the first two. The question is whether **the STAGE-PAUSE conclusion is
reproducible**, and that can only be answered on the apparatus that produced it.

---

## 2. The only permitted variable is "run it again"

Every round records a manifest pinning: candidate SHA, arm, corpus file SHA-256, instrument,
image tag, container ID and start time, and cache state. A round whose manifest differs from
the others in any field except round index is **excluded and reported as excluded** — never
silently dropped. This project has already lost work to a contaminated twin branch
(`af65e40`), and a silently divergent round is the same failure wearing different clothes.

**Cache state is handled explicitly and is never mixed.** On 2026-07-25 the eval sweep reused
caches the canary round had just populated, producing a 2,232 ms gap that could not be
separated from measurement span. Therefore:

- **Round 0 is run and DISCARDED.** Its only job is to warm the listing/crime caches.
- **Rounds 1–5 are the estimation set.**
- The result is reported as **warm-cache round-to-round variance**, which is the state the
  gate actually operates in, and is labelled as such everywhere it is cited.

Cold-cache variance is **not** estimated here. Mixing the two would produce a number that
describes neither.

Additionally, and for the same reason the 2026-07-25 diagnostic did it: each round uses a
**fresh identity namespace** (`var<r>8793c0b-<corpus_user>`), so accumulated memory rows from
an earlier round cannot become a second uncontrolled variable.

---

## 3. Sample size and the stopping rule

The relative standard error of an SD estimate is approximately `1/sqrt(2(n-1))`:

| n | rel. SE of σ̂ |
|---|---|
| 3 | 50% |
| 5 | 35% |
| 10 | 24% |

The question is whether σ(p50) is of order **200 ms or 2,000 ms** — an order of magnitude.
n = 5 resolves that comfortably.

**n = 5** estimation rounds (plus the discarded round 0).

Cost is not a constraint and must not be used to justify n = 3. At the rates derived from the
2026-07-25 round (uncached input $0.139/M, cache read $0.004/M, output $0.259/M), a measured
**warm** 67-case round costs **$0.0225** (94.1% of input served from cache) and the discarded
cold round 0 costs **$0.064** (71.2%). Six rounds is therefore ≈ **$0.18**.

> **STOPPING RULE, frozen in advance.** If the n = 5 point estimate of σ(p50) falls within
> **±1 SE** of the **126 ms** decision threshold (§5) — 126 × 35.4% = 44.6, i.e. roughly
> **[81, 171] ms** — the study **does not conclude**. It extends to **n = 10** and decides
> there. Any other outcome decides at n = 5. Extending for any other reason, or stopping early
> because the answer already looks clear, invalidates the study.

---

## 4. A falsifiable mechanical prediction, registered before the data exists

From the 2026-07-22 paired data: 2-call turns were **29/58 = 50.0%** under the 6,000 ms bar,
and 3+-call turns were **0/9**. A round's p50 is therefore expected to be governed almost
entirely by *what fraction of its turns took 3 or more LLM calls*.

> **PREDICTION P1 — CORROBORATIVE ONLY, CANNOT FALSIFY.** Across the five rounds, round-level
> p50 is **monotonically non-decreasing** in the round's share of turns with `llm_calls >= 3`.
> Reported as Spearman ρ over n = 5 with the raw pairs printed.

**P1 carries no falsification power and this document forbids claiming any.** At n = 5 the
exact permutation distribution gives `P(|ρ| = 1) = 2/120 = 0.0167`; that is the *only*
attainable significant result, so any imperfect monotonicity is non-significant **by
construction**. A non-monotone outcome is therefore recorded verbatim as
**`P1: UNINFORMATIVE AT n=5`** and may **not** be written up as the mechanism being refuted.
Treating a low-power null as a refutation is the same error class this document exists to
prevent, and an earlier draft of this very section committed it.

> **PREDICTION P1b — this is where the falsification power lives.** The mechanistic claim is
> *"call count drives latency."* Its natural unit is the **case**, not the round. For every
> case whose `llm_calls` differs across the five rounds, pair that case's high-call rounds
> against its own low-call rounds and test the latency difference with a **Wilcoxon signed-rank
> test**, n = the number of cases that varied.
>
> **P1b is falsifiable and is the registered test of the mechanism.** If it fails, the
> "non-deterministic loop iteration count" explanation for `E6` is wrong and must be written up
> as such, not quietly dropped.

Pairing **within** a case is what makes P1b sound: across cases, call count and latency are
confounded — hard cases both take more calls and are intrinsically slower — so a cross-case
correlation would be uninformative about causation. Within a case that confounder is held
fixed by construction, and the power comes from up to **67 × 5** observations rather than 5.
`E6` is itself an instance of exactly this pairing: 2 → 5 calls, 15,382 → 29,374 ms.

If P1 holds it is reported as **supporting** evidence only; the conditionable-covariate
conclusion — that future rounds may stratify on call-count share and design that variance out
— requires **P1b**, not P1.

Every round must record, per case: `llm_calls`, `tool_batches`, `turn_latency_ms`,
`soft_wrapped`, `tool_budget_timeout`, `partial`; and per round: the distribution of
`llm_calls`, the count of `tool_budget_timeout`, and the count of `partial`.

---

## 5. Decision criteria, frozen before the first round

The gap that has to be closed, **stated on this study's own instrument and population**, is
**7,402 → 6,000 ms = 1,402 ms** — the warm canary p50 measured over these 67 cases on
2026-07-25. The 7,082 ms figure from the tool-stratified table is **not** used here: it comes
from the eval harness over 98 cases, and importing it would be precisely the cross-instrument
citation §6 forbids.

**Minimum interesting effect δ = 500 ms. Significance α = 0.05, two-sided. Power 1−β = 0.80.**
These three are the design inputs and must be restated in any document that reuses this
threshold — the threshold is meaningless without them.

The comparison a single-round A/B actually performs is a *difference of two round p50s*, so
its noise is `σ_D = σ√2`. Detecting δ at that α and power requires

```
δ  ≥  (z₀.₀₂₅ + z₀.₂₀) · σ · √2  =  2.8016 · σ · 1.41421  =  3.9621 · σ
σ  ≤  500 / 3.9621  =  126 ms
```

| σ̂(p50) | ruling |
|---|---|
| **< 126 ms** | A single round resolves a 500 ms effect at α=0.05, power 0.80. **Current practice stands.** |
| **≥ 126 ms** | **Single-round A/B is retired as a decision tool.** Every subsequent verdict requires `k = ceil(2·(2.8016·σ̂/500)²)` rounds, and `k` is reported alongside the verdict. |

The formula is self-consistent with the threshold: σ̂=126 → k=1, σ̂=250 → k=4, σ̂=400 → k=11.

> **Why not 250 ms.** An earlier draft of this section set the threshold at 250 ms on the
> reasoning that it is "roughly half the minimum interesting effect." That is a number picked
> to look reasonable, not a number derived, and this document exists to stop exactly that
> slide. It is recorded rather than deleted because the failure is instructive: at σ = 250 the
> difference-of-two-rounds SD is 353.6 ms, so δ/σ_D = 1.414 and the achieved power is
> **Φ(1.414 − 1.960) = 29%**. The rule "σ < 250 ms → current practice stands" would have
> *certified* a decision procedure that misses seven out of ten genuine 500 ms improvements —
> worse than having no criterion, because it carries the appearance of having been argued.

**This is also the number PR #15 is waiting on.** Its thresholds are blank precisely because
nobody knows the precision of the instrument they would be applied to. Nothing in this
document authorises filling them in: PR #15 remains bound by `HANDOFF §3.5` — it may not be
back-fitted to the 8,466 ms already on record, and it requires its own fresh, independent
round.

---

## 6. Non-citation declaration

The 2026-07-22 schema-compaction A/B recorded a benchmark run-to-run churn of roughly
**22/98 cases** flipping. **That is a count of pass/fail verdict flips. It is not a latency
standard deviation, and the two may never be cited interchangeably.** They have different
units, different estimands and different populations.

This clause exists because this project has already been burned once by exactly that move:
a 7,870 ms figure measured on the canary HTTP path with a different corpus was carried into
an argument about a different instrument, and had to be publicly withdrawn on 2026-07-26.
Any report produced under this pre-registration must reproduce this paragraph verbatim.

Likewise: σ(p50) established here describes **the canary instrument on the 67-case
single-turn population under warm caches**, and applies to nothing else without a new study.

---

## 7. Deliverable

A results package under `.runtime/variance-8793c0b-<date>/` containing: per-round manifests,
per-round telemetry copied out **by request id** (never by moving the live log — see the
`mv`/fd trap in `HANDOFF §3A`), the per-round p50 table, σ̂(p50) with its SE, the per-case SD
diagnostic, the P1 result (with raw pairs, and marked `UNINFORMATIVE AT n=5` if non-monotone),
the **P1b** Wilcoxon signed-rank result with its n, the §5 ruling with the `k` implied by σ̂,
and the annotated tag `prereg/round-variance-v1` identifying the frozen document.

`SHA256SUMS.txt` over every file.

---

## Freeze block

```
FROZEN_BY        : shuhan-wang1                       # the owner, and only the owner
REVIEWED_BY      : Claude Opus 5 (design review, 2026-07-26)
CANDIDATE_SHA    : 8793c0b17963a6a2b375903a164d3d96395dc834
CORPUS_SHA256    : 7f1ead524c421e33f4098afff036f019a92537d5f1f76deba59580aa34dc6907
                   # git show 8793c0b:evaluation/benchmark/cases.jsonl | sha256sum
SELECTION_SHA256 : c6438bc8a8b713fc808537941cd355756132bf34050d83a85b67426437530522
                   # the 67 case_ids in manifest.json order, LF-separated, trailing LF
```

**There is deliberately no `PREREG_SHA` and no `FROZEN_AT` field.** Both are self-referential:
a commit cannot contain its own hash, and it cannot contain its own committer timestamp
either — writing either one in changes the commit it claims to describe. `FROZEN_AT` was
originally specified as "the freeze commit's committer timestamp, not hand-written", which is
the same structural problem and gets the same treatment: **derive both from the commit, store
neither in the file.**

The frozen identity is an annotated tag created *after* the freeze commit:

```
git tag -a prereg/round-variance-v1 -m "frozen pre-registration, round-variance v1"
```

Its target commit **is** the PREREG_SHA, and its date is FROZEN_AT:

```
git rev-list -n1 prereg/round-variance-v1              # -> PREREG_SHA
git log -1 --format=%cI prereg/round-variance-v1^{}    # -> FROZEN_AT
```

§7's requirement that the results package record "this document's frozen SHA" is satisfied by
citing that tag. Neither value can drift from the commit it describes, because neither is
written down twice.

**`FROZEN_BY` must be the owner.** This document was drafted and reviewed by an assistant;
having a non-owner sign the owner's field would be exactly the provenance error the rest of
this document exists to prevent, which is why review is recorded on its own line.

A pre-registration with placeholders authorises nothing.
