# Pre-registration — answer length as the fc_loop p50 lever

**STATUS: DESIGN ONLY.** No candidate exists. No paid run has happened. Every threshold below
is `<TO BE FILLED>` and must be set by the maintainer **before** any candidate is built. A
pre-registration approved with placeholders authorises nothing.

**This document does not change any gate.** `P50_LIMIT_MS` stays 6000 and fc stays at
STAGE-PAUSE until a round passes on its own merits. See `HANDOFF.md` §3.5/§3.6.

---

## 1. Why this hypothesis, and why only now

Four latency levers have been tried and quantitatively refuted. All four were **input-side**:

| lever | verdict | evidence |
|---|---|---|
| prompt / cached-prefix size | REFUTED | schema-compaction A/B: −78 ms / +186 ms over 200 turns, 2 rounds; the 533 tok/call saving is 100 % cached prefix |
| deleting the final answer call | REFUTED | fast-path counterfactual: 5,836 ms even at 100 % savings against a 5,800 ms bar, and a Base98 quality veto (ON 5/20 vs OFF 12/20) |
| never-trimmed message array | REFUTED (2026-07-25) | at the median (2-call) turn, uncached input is **192 tokens**, cache hit 98.8 % — there is no input work to remove |
| tool latency | REFUTED (2026-07-25) | fast vs slow halves of the 2-call bucket have **identical** tool-batch counts, zero `tool_budget_timeout`, zero `partial`; the slow half has *fewer* uncached input tokens |

The refutations were computed offline from the retained paired-control telemetry
(`.runtime/logs-archive-pre-042c477/canary-fc_loop.jsonl`, 100 records, candidate
`2d48d22`) — no model, no network, no API spend.

**Reproduce every table in this section** (offline; the script lives outside the repo with the
other ops scripts, so it carries no product SHA):

```bash
python3 /home/shuhan/fp-results/scripts/output_length_latency.py \
  --fc     /home/shuhan/uk_rent_recommendation/.runtime/logs-archive-pre-042c477/canary-fc_loop.jsonl \
  --legacy /home/shuhan/uk_rent_recommendation/.runtime-legctl/logs/canary-legacy-control.jsonl \
  --pairs  /home/shuhan/uk_rent_recommendation/.runtime/paired/pairs.tsv
```

If its output stops matching the numbers below, this document is stale — not the other way
round.

The same data identifies **one lever that is not refuted**, and it is the first one that is
output-side:

```
all 100 turns   latency = 2,285 ms + 14.6 ms per output token      R² = 0.83
                median 312 output tokens -> generation ≈ 4,535 ms of a 6,885 ms turn (66 %)

2-call turns    latency = 3,980 ms +  7.9 ms per output token      R² = 0.23
(where p50 lives) median 266 output tokens -> generation ≈ 2,103 ms of a 5,975 ms turn (35 %)
```

R² = 0.83 across the workload. Output length is the dominant term, which is exactly why every
input-side lever failed.

**Corroborating cross-arch signal.** On the paired workload, restricted to the classes where
BOTH arms actually answered, fc's verbosity relative to legacy tracks its latency penalty
monotonically:

| class | fc / legacy output tokens | fc slower by |
|---|---|---|
| m0 | 1.0× | +1.6 s |
| m4 | 1.5× | +0.4 s |
| m6 | 1.9× | +5.1 s |
| m1 | 4.2× | +11.7 s |

(The 23–46× ratios on m2/m3/m5/m7/m8/m9 are **not** verbosity findings — legacy returns a
clarification on those and never answers, so its 9–13 output tokens are a refusal, not a
shorter answer. Those classes must be excluded from any verbosity comparison.)

---

## 2. Hypothesis, stated so it can fail

> **H:** fc_loop's median turn latency is dominated by output-token generation, and a
> prompt-only reduction in answer length reduces p50 **without** degrading grounding,
> disclosure or route conformance.

The second clause is the whole risk and is **not** assumed. fc's answers are long **because
the prompt demands it**: cite a source inline for every figure, name every requested
dimension that was not checked, report a completed-empty search honestly with its criteria.
Those are the behaviours that make fc beat legacy (45–49/98 vs 32/98 pass; 77–79/98 vs
54–55/98 route). **A length reduction that removes them buys latency by giving back exactly
the advantage the arch exists for**, and must fail this experiment.

### Predicted effect size (stated in advance, so it can be wrong)

Using the whole-workload coefficient, median output 312 → `<TO BE FILLED>` tokens predicts
p50 6,878 → `<TO BE FILLED>` ms. Using the conservative within-bucket coefficient
(7.9 ms/token) the same reduction predicts `<TO BE FILLED>` ms. **Both predictions are
recorded before the run; a result outside them is a failed prediction even if the gate
passes.**

---

## 3. Permitted change surface

Prompt text only. **No control-flow change, no node change, no tool change, no gate change.**

| file | permitted change |
|---|---|
| `app/core/loop_prompts.py` | length/format directives in `behaviour_rules()` and the synthesis/wrap directives |
| `app/core/langgraph_agent.py` | `SYNTHESIS_PROMPT` length guidance only |
| `tests/` | tests for the above |

Budget: **≤ `<TO BE FILLED>` lines**, and a static diff gate asserting no other path changed.

### Invariants — asserted mechanically, not reviewed by eye

* **L1** No directive that requires a source citation for a stated figure may be weakened or
  removed.
* **L2** No directive that requires naming an unchecked requested dimension may be weakened
  or removed.
* **L3** No directive governing a completed-empty search (report it honestly, name the
  criteria) may be weakened or removed.
* **L4** The 13 standing behaviour rules keep their identities and relative order; only
  length/format guidance may change.
* **L5** The candidate's diff touches only the files above. No product control flow moves.

L1–L3 exist because they are the cheapest possible way to shorten an answer and the most
expensive possible thing to lose.

---

## 4. Gate — every condition necessary, none substitutes for another

Per `HANDOFF.md` §3.6: a cutover requires ALL gates. This experiment adds conditions; it
retires none.

| # | condition | threshold |
|---|---|---|
| G1 | fc p50 ≤ 6000 ms (the STANDING gate, unchanged) | 6000 ms — **not adjustable by this experiment** |
| G2 | fc p95 ≤ 30000 ms | 30000 ms |
| G3 | median output tokens reduced by ≥ | `<TO BE FILLED>` % |
| G4 | Base98 semantic pass ≥ baseline | `<TO BE FILLED>` |
| G5 | Base98 route-matched ≥ baseline | `<TO BE FILLED>` |
| G6 | no case at candidate 0/3 vs baseline 3/3 | 0 cases |
| G7 | grounding: `unsupported` claim rate not worse than baseline by more than | `<TO BE FILLED>` pp |
| G8 | source-coverage rate not worse than baseline by more than | `<TO BE FILLED>` pp |
| G9 | zero-tolerance violations | 0 |
| G10 | canned-fallback rate (`wrapped_by` = `fallback_*` over wrapped turns) not worse than baseline | `<TO BE FILLED>` |

G7/G8 are the anti-cheat conditions: they are how "shorter" is distinguished from "less
grounded". G10 exists because a shorter answer must not be achieved by wrapping more often —
that path is already instrumented (`wrapped_by`, PR #11) and must be watched here.

`contradicted_claims` is deliberately **NOT** a gate condition. It was adjudicated on
2026-07-25 as a measurement artifact: ±40 % run-to-run variance under literally fixed code
(same commit, 12 vs 17), and it fell to 0 when the tool caches were warmed with the grader
byte-identical. It is reported as a diagnostic only. Reviving it as a gate needs the six
repairs listed in that adjudication.

---

## 5. Execution sequence — nothing may skip ahead

```
1. this document approved WITH THRESHOLDS FILLED IN        <- authorises nothing until then
2. record PREREG_SHA; it must NOT be an ancestor of the candidate
3. build the candidate from the post-#13/#14 mainline SHA, prompt files only
4. static diff gate (L5) + offline suite green
5. smoke on the internal pool: >=1 turn that BUILDS a model, then a zero-call turn
   (see the note below), single candidate_sha, contract-valid records, --expect-turns match
6. paired A/B against the baseline, 3 repeats, interleaved, ONE evaluator, per-case retained
7. evaluate G1-G10. Any failure = NO-GO for this candidate, recorded, not renegotiated
```

**Step 5 depends on PR #14.** Before that fix, a zero-LLM-call turn in a fresh process emits
a contract-invalid record and is excluded from the population — so it leaves the denominator
of p50 and of every rate. Running this experiment on an unfixed build would measure a p50 for
the expensive turns only. **#13 and #14 must be merged first.**

Archive every pre-experiment smoke/restore log out of `.runtime/logs/` before step 6.
Anything left there is swept into `--input`; that is how a previous stage gate counted 116
turns from three different candidates.

---

## 6. What this experiment cannot do

It cannot make the standing p50 bar adjustable. If a filled-in, frozen version of this design
runs cleanly and G1 still fails, the honest conclusion is that **fc_loop cannot meet a 6 s p50
at its own answer quality**, and the decision moves to the only remaining route in
`HANDOFF.md` §3.6: a separately pre-registered, frozen, **forward-only** v2 gate — which must
never be recorded as a revision of any past round's SLO, and may not be applied to any
measurement taken before it was frozen.

That route is a maintainer decision. This document deliberately does not take it.
