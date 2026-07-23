# Measurement infrastructure (`eval/measurement-infrastructure`)

Branched from the accepted mainline **`e7977e6`**. It carries **only** the machinery that
makes a paired A/B trustworthy — **no product change, no case-contract change, no
critic/filter behaviour change.** Verified by a purity audit: nothing under `app/`,
`src/`, `evaluation/benchmark/`, or `graders.py` differs from mainline.

It exists because two product branches were terminated (`fastpath/deterministic-phase1`,
`hardening/correctness-only`) while the *measurement* built alongside them repeatedly
caught defects the gates alone would have missed. That machinery is worth keeping; the
product change-sets are not.

## What it adds

1. **Three-layer identity** — `product_sha` / `capture_sha` / `evaluator_sha` in every
   manifest, plus `grader_sha256` and `case_contract_sha256`. A measurement probe added
   on top of an old product tree reports the OLD tree as `product_sha` and itself as
   `capture_sha`, so it can never masquerade as the product under test. `evaluator_sha`
   stays null until a re-score stamps it: a tree's own verdicts are not the gate.
   `PRODUCT_SHA` pins the product explicitly.
2. **Evidence persistence** — `<out>/grader_input.jsonl`, one record per run: raw evidence
   digest, normalized evidence, and the exact grader input. Without it a round is
   retainable as evidence but never re-scorable.
3. **Single-evaluator re-scoring** — `python -m evaluation.rescore --runs DIR... --cases
   CONTRACT`. Re-scores every arm with ONE pinned evaluator. In a paired A/B each arm runs
   different product code and therefore ships its own grader, so comparing the `passed`
   each arm computed for itself compares two evaluators as much as two products.
4. **Identity refusal + integrity** — a run dir missing `product_sha`, `capture_sha` or
   `case_contract_sha256`, or whose contract digest differs from the evaluator's, is
   REFUSED rather than scored with a defaulted identity. Evidence digests are verified,
   and duplicate `run_id`s are de-duplicated **and reported**.
5. **Shard preflight** — every benchmark shard must load and schema-validate before a
   measurement starts, not just the one being run.
6. **Output-dir reuse refused by default** — `--allow-reuse-out` is the explicit opt-out.

## Why 5 and 6 are in here at all

Both are scars.

**5**: `must_complete_requested_dimensions` was added to `cases.jsonl` but never to
`schema.json`. It survived **two green guard runs**, because the guard uses a different
shard — while the Base98 contract was unloadable and both arms aborted before running a
single case. *A green guard does not prove the other shards even load.*

**6**: `grader_input.jsonl` is appended to, so a reused output dir produced **196 records
for a 98-case round**. Silently scoring 196 records for 98 cases is how a duplicate
becomes a number nobody questions.

## Deliberately NOT included

* Any product behaviour (`app/`, `src/`) — including the critic grounding fallback, the
  follow-up capability filter, the pure-recall constraint and the `memory_context` wiring.
* **The G2 / G3 / E11 case-contract amendments.** Those are *evaluator contract*, not
  infrastructure, and they change what "pass" means. They need their own review and must
  not ride in on an infrastructure commit.
* The grader scoring changes (two-sided English thresholds, the CJK minute blind spot, the
  hypothetical-constraint taxonomy, `must_complete_requested_dimensions`). Same reason:
  they alter verdicts, so they are reviewable evaluator-contract changes.

## Companion tool

`scripts/eval_capture_allowlist_check.sh` — asserts that the diff between a baseline
product SHA and a capture commit lies entirely inside a pre-registered evaluation-only
allowlist, and fails loudly if anything under `app/` or `src/` was touched. It earned its
place immediately: it caught a `git add -A` sweeping an untracked local results package
into a capture commit.

Offline suite on this branch: **1710 passed, 3 skipped, 0 failed** (13 of them the new
infrastructure tests).
