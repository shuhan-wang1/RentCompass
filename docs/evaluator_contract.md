# Evaluator contract (`eval/evaluator-contract`)

Branched from mainline `f053508` (the merge of `eval/measurement-infrastructure`). It
carries **only** evaluator-contract changes — the case amendments and claim-taxonomy rules
that were deliberately held out of the infrastructure PR because they alter what "pass"
MEANS. No product change: nothing under `app/` or `src/` differs from mainline, and the
`evaluation` package gains no import of either.

Sequenced deliberately: infrastructure first (so a re-score is trustworthy), contract
second (so the bar is stable), product experiments only after that.

## What it changes

### Case contract

| case | change |
|---|---|
| **G2, G3** | drop `must_call_tool: recall_memory`; add `allowed_tool_paths` admitting a `recall_memory` call **or an empty trace**; widen `expected_grounding_sources` |
| **E11** | add `must_complete_requested_dimensions` (commute, nearby, safety) + the matching failure condition |

G2/G3: the harness replays `conversation_history` into long-term memory and injects the
retrieved block as `memory_context`, so the target fact is present **before the turn
runs** and the model can answer without a tool call. Requiring the call bound the contract
to an implementation the pre-injection architecture superseded. `must_recall_value` still
carries the semantics — the fact must still be right.

E11: a truncated or soft-wrapped reply that openly stated the commute/pharmacy/crime
checks were not done used to PASS, because saying less means fewer numbers to fail a
grounding constraint on. Judged on **executed tools**, never on prose, so a model cannot
talk its way past it.

### Claim taxonomy

1. **Thresholds are recognised on either side of a number**, within the clause. A
   one-sided "before" window judged by phrasing distance alone: "under your 25-minute
   limit" was spared, "meets your 25-minute limit" was not. Windows are clause-bounded so
   a marker cannot reach across a comma and mask a real measurement.
2. **CJK minutes are extracted from the answer.** `_CJK_MINUTES_RE` built the evidence
   pool but never read the reply, so a zh answer produced zero `commute_minutes` claims
   and `commute_leq_minutes` passed **vacuously** — a fabricated zh journey time could not
   be caught at all.
3. **Hypothetical constraints are not measurements.** "Consider a slightly longer commute
   (e.g. 30 minutes)" proposes a bound. Excluded only when a proposal cue AND a
   constraint-adjustment cue AND no measurement cue all hold in one clause; `e.g.` alone
   is deliberately not a free pass.
4. **A difference or a derived aggregate is not a journey time.** 「每天多花约 40-50 分钟」
   and 「每天往返只需约 30-52 分钟」 are arithmetic over measured values, so no evidence
   pool can ever contain them. Comparatives (`faster`, `slower`) are judged on a tight
   window right after the figure, not clause-wide — a colon is not a clause break, so
   "Cycling is faster: 19 minutes" must stay a measurement.
5. **Ranges yield both endpoints.** The minute regexes anchor on the unit, which follows
   the second endpoint, so 「15-26 分钟」 produced only 26 and a fabricated lower bound was
   unreachable. Latin and CJK both.
6. **An explicitly-labelled non-match does not violate `commute_leq_minutes`.** Reporting
   the nearest option as "37 minutes … slightly over your 30-minute limit" is the correct
   behaviour when nothing matches; failing it rewards silence. Built as the escape hatch
   it is: **two** independent cues required in the same clause (an exceedance word AND the
   noun exceeded), and it excuses only the OVER check — an **ungrounded** figure stays a
   violation however it is worded, so no phrasing can launder a fabricated duration.

Rules 4–6 were not designed up front. They are what the offline re-score below forced.

### Cross-shard consistency

Amending `cases.jsonl` alone left `cases_base45` (G2, G3) and `cases_ext_CDE` (E11) on the
superseded contract, so the same `case_id` meant different things depending on which shard
ran. The amendments are propagated, and `tests/test_case_contract_consistency.py` fails if
a future amendment forgets a shard.

It also records three **pre-existing** divergences as known debt — these are different
constraint TYPES, so picking a winner is its own contract decision:

| case | Base98 | stale shard |
|---|---|---|
| E8 | `must_flag_unrealistic_constraint` | `ext_CDE`: `must_refuse_fabrication` |
| F11 | `must_flag_stale_data` | `ext_FG`: `must_note_missing_data` (still marked NEEDS_CHECKER) |
| G16 | `must_supersede_value` | `ext_FG`: `must_recall_value` |

## What the change actually does to verdicts

Measured offline against **retained evidence only** — the six `idp98` rounds (98 cases ×
3 repeats × 2 arms = 588 gradings). Nothing re-executed: no model, no tools, no network,
no API spend. Tool: `/home/shuhan/fp-results/scripts/contract_delta.py`; report:
`/home/shuhan/fp-results/contract_delta_2026-07-23.json`.

| run | product | old | new |
|---|---|---|---|
| idp98_r1_base | e7977e6 | 56 | 56 |
| idp98_r2_base | e7977e6 | 49 | 50 |
| idp98_r3_base | e7977e6 | 53 | 53 |
| idp98_r1_cand | d2004e0 | 54 | 55 |
| idp98_r2_cand | d2004e0 | 55 | 56 |
| idp98_r3_cand | d2004e0 | 47 | 48 |

**10 verdict flips out of 588 gradings (1.7%)**, every one attributable to a named
constraint:

* **7 FAIL→PASS, all G2/G3, all on `must_call_tool`** — precisely the amendment's intent:
  the model answered correctly from injected `memory_context` without calling
  `recall_memory`.
* **3 PASS→FAIL, all ungrounded colloquial durations** — C1 ×2 ("roughly a 10-15 minute
  walk" against a single 12-minute measurement) and F12 ("a short 5-10 minute DLR ride"
  with **no commute evidence at all**, on a case whose contract requires noting the
  missing data). This is the defect rules 2 and 5 exist to catch.

Both arms move the same way, so the change does not favour either side of the paired A/B.

### `must_complete_requested_dimensions` fires but changes nothing here

It is violated in **6/6** runs — the dimensions genuinely were not completed — yet E11
already failed on `must_mention_source` in all six, so no verdict moves. The constraint is
live and correct on this evidence; its **value is unproven** until a round gets
`must_mention_source` right while still declining dimensions.

### Two false positives this measurement caught

Rules 4 and 6 exist because the first re-score produced verdicts nobody could defend, and
a second because the first fix was too broad:

1. C12 flipped on 「每天多花约 40-50 分钟」 — a daily *difference* read as a third journey
   time. It had passed before only because CJK minutes were never read at all, so the CJK
   fix exposed a pre-existing defect rather than causing one.
2. Clause-wide `faster` then over-suppressed, so C7 ("10-18 minutes faster each way") and
   C12's round-trip total needed the tight post-window and the aggregate cues.

E2's old PASS was itself accidental: "within your budget" from the **previous bullet**
reached into the 24-char window before "37 minutes" and suppressed the claim — exactly
what the clause-bounded window in rule 1 fixes. Its new verdict comes from rule 6, not
from the accident.

## Deliberately excluded

* **`no_false_retrieval_provenance`** — its grader imports `claims_no_retrieval` from
  `uk_rent_agent.agent.critic`, which exists **only** on the terminated
  `hardening/correctness-only` branch. Porting it would drag product code out of a NO-GO
  branch. Its schema enum entry and the H3 guard-case amendment are held back with it.
* **`extract_tool_trace` skipping `suppressed` artifacts** — evaluator support for
  follow-up-capability suppression, which is NO-GO and not being extended.
* Every product change on the hardening branch: the critic grounding fallback, the
  follow-up capability filter, the pure-recall constraint, the `memory_context` wiring.

## Audit it yourself

```bash
cd /home/shuhan/telemetry-v2-layer-b

# no product code, and no new product import
git diff --name-only f053508 eval/evaluator-contract | grep -E '^(app/|src/)'   # prints nothing
git diff f053508 eval/evaluator-contract -- evaluation/ \
  | grep -E '^\+.*(import|from).*(uk_rent_agent|app\.)'                         # prints nothing

# every shard loads, schema-validates, and agrees with its siblings
python -m pytest tests/test_case_contract_consistency.py -q

# the offline suite
docker run --rm -v /home/shuhan/telemetry-v2-layer-b:/patched uk-rent-agent:bench-git bash -c '
cd /patched && OPENAI_API_KEY=dummy HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python -m pytest tests/ -q -p no:cacheprovider'
```

Expected: both greps print nothing · `1785 passed, 3 skipped` (mainline: 1710).

Re-run the contract delta (no API spend) with `contract_delta.py score` once per tree and
`compare` across the two dumps; `git worktree add` a detached `f053508` for the old side.
