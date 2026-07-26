# Design proposal — a layered (orchestrator / executor) agent architecture for RentCompass

**Status: PROPOSAL. Documentation only. Nothing here is approved, frozen, or authorised to run.**
No product code, test, gate threshold or configuration is changed by this document.

**Version:** 1 · **Authored:** 2026-07-26 · **Base:** mainline `telemetry/v2-layer-b` @ `d285bac`

**Question it answers** (owner, 2026-07-26):

> 复杂问题是否能拆成 multi-agent 框架。用小模型执行工具调用，聪明模型用于全局调度。
> 你可以参考一些论文或者 claude code 的整体设置框架，看看高级的 multi agent 系统是如何处理复杂问题，问题分层优化响应时间的。

**Short answer, stated before the argument so it cannot be buried:**

> A planner/executor split is the right *shape* for this system, but on the measured data it is
> **not** a latency fix. The cheap, obvious parts of it buy **≈308 ms of the 1,402 ms gap and move
> zero turns under the bar**. The parts that could close the gap all depend on one number nobody
> has measured — how much faster a smaller model emits a tool call than `deepseek-v4-flash` does —
> and **there is currently no smaller model configured in this stack to measure**. The naive
> version of what was asked for (a supervisor that dispatches to sub-agents that themselves loop)
> is **actively worse**: it raises LLM calls per turn, and 0 of 14 turns with ≥3 LLM calls were
> under the bar on the warm round.
>
> The honest recommendation is to build the split for **correctness, cost and tail control**, keep
> a latency claim out of it, and treat answer length (PR #15) as the only lever the data currently
> supports for the median.

---

## 0. Four premises in the brief that the repo contradicts

These are checked, not assumed. Each changes the design, so they come first.

### 0.1 `perf/parallel-tool-batch` is empty, and tool dispatch is *already* parallel

```
$ git rev-parse perf/parallel-tool-batch telemetry/v2-layer-b
d285bac3c245a51911abd3983208e1e5e1c8d3fa
d285bac3c245a51911abd3983208e1e5e1c8d3fa
$ git log --oneline telemetry/v2-layer-b..perf/parallel-tool-batch | wc -l
0
```

The branch has **zero commits** and is identical to mainline. Within a batch, `execute_tools_node`
(`app/core/agent_loop.py`) already issues every read concurrently — `asyncio.ensure_future` per
call, then a single `asyncio.wait(..., timeout=batch_window)`, each call offloaded to a
32-worker `ThreadPoolExecutor`. The colleague's work in progress is a set of **probes**
(`tests/test_probe_parallel.py`, untracked, header `TEMPORARY PROBE … Deleted before commit`)
measuring whether the *existing* concurrency delivers — the suspected defect is thread-pool
starvation by abandoned, uncancellable offload threads.

So "stage 0 = add parallel tool execution" is not available. Stage 0 is a **diagnosis**, and this
proposal builds on that reading of it.

### 0.2 There is almost nothing to parallelise anyway

From the 98-case eval sweep of the round of record (`eval/sweep/raw_runs.jsonl`, `tool_trace`):

| tools in one batch | batches | share |
|---|---|---|
| 1 | 113 | **87.6%** |
| 2 | 10 | 7.8% |
| 3 | 4 | 3.1% |
| 4 | 2 | 1.6% |

**87.6% of tool batches contain exactly one tool.** Perfect within-batch parallelism has nothing
to do on seven batches in eight. §4 quantifies what remains.

### 0.3 There is no smaller model in this stack

`src/uk_rent_agent/llm/router.py` exposes three aliases. Two of them are the same model:

```
chat_model     = DEEPSEEK_CHAT_MODEL     or DEEPSEEK_MODEL or "deepseek-v4-flash"
reasoner_model = DEEPSEEK_REASONER_MODEL or "deepseek-v4-flash"
pro_model      = DEEPSEEK_PRO_MODEL      or "deepseek-v4-pro"     # zero callers
```

After the 2026-07-24 DeepSeek retirement, `chat` and `reasoner` both resolve to
`deepseek-v4-flash`; the only surviving distinction is `extra_body={"thinking": …}`. The fc pool
uses exactly one route — `ModelRouter().create("responder", low_latency=True)` — which is
`deepseek-v4-flash`, thinking **disabled**, `temperature=0.1`, `max_tokens=4000`.

The tier that exists and is unused is `deepseek-v4-pro`, which is **bigger**, not smaller. The
owner's ask — *小模型执行工具调用* — has no instrument today. Every number in this document that
depends on a small model's speed is therefore `<TO BE FILLED>`, and §6 says what to measure.

### 0.4 The pattern already exists in production, un-instrumented

`search_properties` makes **two nested DeepSeek calls of its own**
(`clarify_and_extract_criteria` and `generate_recommendations`, via
`app/core/llm_interface.py::_call_deepseek`). These use the raw `openai.OpenAI` SDK, bypass
`ModelRouter`, and therefore **bypass `install_observer`** — so they are invisible to the canary
`llm_calls` counter.

In the round-of-record eval sweep they are visible as `purpose="memory"` LLM calls:

```
llm_call purposes:  responder 246   memory 48
memory-call latency: n=48   p50 934 ms   mean 987 ms   sum 47,359 ms
search_properties tool calls: 46, across 40 of 98 cases
```

So a turn that searches carries roughly **one extra ~934 ms model call that the gate cannot
see**. Two consequences:

1. "The median turn makes 2 LLM calls" is true of the *instrument*, not of the system.
2. **Any new executor layer would be equally invisible unless the observer is fixed first.** That
   makes observer coverage a hard prerequisite, not a nice-to-have (§8, Stage 1).

---

## 1. The measured starting point

All figures are from artefacts on disk, not re-run. Provenance:

| source | what it is |
|---|---|
| `/home/shuhan/uk_rent_recommendation/.runtime/round-8793c0b-internal-2026-07-25/` | **round of record**, verdict STAGE-PAUSE, cold caches, 67 turns, canary p50 **8,466 ms** |
| `/home/shuhan/uk_rent_recommendation/.runtime/diagnostic-8793c0b-warmcache-2026-07-25/` | **warm-cache diagnostic**, 64 turns, canary p50 **7,401.9 ms**. A diagnostic, not a verdict (`HANDOFF §3.8`); it does not revise STAGE-PAUSE |
| `…/round-…/eval/sweep/` | eval harness, fc arm, 98 cases, in-process |

The warm diagnostic is the right baseline for *design* work — the gate operates warm — and it is
the population the variance study also chose (PR #19 §2). Everything below uses it.

```
warm canary p50           7,401.9 ms      bar 6,000 ms      gap 1,402 ms
turns under the bar       26 / 64  (40.6%)
cache read share          94.09% of input tokens
median output tokens      416.5
```

### 1.1 Latency is concentrated in one stratum, and that stratum is the median

| `llm_calls` | n | p50 | under 6,000 ms |
|---|---|---|---|
| 1 | 15 | 3,649 | **15/15 (100%)** |
| 2 | **35** | **7,604** | 11/35 (31%) |
| 3 | 6 | 13,230 | 0/6 |
| 4 | 4 | 12,732 | 0/4 |
| 5 | 2 | 22,971 | 0/2 |
| 6 | 1 | 17,984 | 0/1 |
| 7 | 1 | 28,616 | 0/1 |

**0 of 14 turns with ≥3 LLM calls made the bar**, reproducing the 2026-07-22 finding (0/9) on a
different round and a different population. This is the single most stable fact in the dataset and
it is the reason §10 rejects the supervisor-of-sub-agents design outright.

The brief quotes 2-call turns at 50% under the bar; that is the 07-22 paired figure. On the warm
07-25 round the same stratum is **31%**. Both instruments agree on the shape; do not cross-cite the
rates.

### 1.2 `llm_calls` and `tool_batches` are not separately identifiable

```
corr(llm_calls, tool_batches) = 0.975
llm_calls == tool_batches + 1  in 59 / 64 turns
```

The loop makes this true by construction: one agent call per super-step, one batch per super-step
that requests tools. Refitting the round-of-record regression on the warm data therefore blows up:

```
warm 4-term refit : lat ≈ 1663 + 13.16·out_tok − 731·llm_calls + 2520·tool_batches   R² = 0.642
```

A **negative** coefficient on `llm_calls`. The cited fit
`618 + 16.2·out_tok + 524·llm_calls + 797·tool_batches` (R² 0.69) is not wrong, but its two
count coefficients cannot be read as separate causal prices. Collapse them:

```
warm, 2 terms : lat ≈ −189 + 12.45·out_tok + 1657·loop_iters        R² = 0.634
warm, 1 term  : lat ≈ 1005 + 17.66·out_tok                          R² = 0.606
```

**Output tokens alone explain 60.6% of the variance; adding loop iterations adds 2.8 points.**
Two usable prices, both indicative:

* **one loop iteration ≈ 1,657 ms**, output tokens held fixed
* **one output token ≈ 12.5–17.7 ms**, depending on whether iterations are controlled

### 1.3 A reconciled budget for the median turn

The median turn is 2 LLM calls / 1 tool batch, warm canary latency **7,604 ms**. The canary record
has no per-node timing, so the budget is assembled from the eval harness `node_spans` plus one
measured serving-path delta, and the tool term is taken as the residual.

| component | ms | how obtained |
|---|---|---|
| serving path (Flask, identity, conversation store, memory retrieval, persistence, telemetry) | ~600 | **measured**: +599 ms paired canary-vs-eval median, identical 67 cases (PR #19 §1) |
| LLM call #1 — emits the tool call | ~1,900 | **measured**: `node_spans` `agent` calls followed by `execute_tools`, p50 1,928 ms (n=133); 1,987 ms on non-fixtured cases (n=40) |
| tool batch — 1 batch, real network | ~2,000 | **residual.** Corroborated independently: `execute_tools` is 26.2% of wall time across the 37 **non-fixtured** eval cases, and 0.262 × 7,604 = 1,992 ms |
| LLM call #2 — writes the answer | ~3,000 | **measured**: `node_spans` terminal `agent` calls, p50 3,139 ms (n=98); 2,890 ms non-fixtured |
| critic + graph plumbing | ~100 | **measured**: critic median 1 ms in this stratum (it runs on 39/39 but rarely repairs); `guard`/`extract_preferences`/`format_output_fc` total 80 ms over all 98 cases |
| **total** | **~7,600** | vs measured 7,604 |

Two warnings about this table. The tool term is a **residual and therefore absorbs every error in
the other four rows** — it is not a measurement. And the eval harness replays recorded evidence for
**61 of 98 cases** (`notes: "LIVE mode: fixtured cases replay recorded evidence"`), which is why
`execute_tools` has a p50 of 10 ms across all cases and 26.2% of wall only on the non-fixtured
subset. **Per-tool timing in canary telemetry is the missing instrument** (§8, Stage 1).

Read the table plainly: **roughly 4,900 ms of a 7,600 ms median turn is a model generating
tokens.** Tools are about 2,000 ms and are already dispatched concurrently. That is the shape
every proposal below has to survive.

---

## 2. The proposed decomposition

### 2.1 Design principle: layer *without adding a model call to the median turn*

Every architecture below is judged against one constraint derived from §1.1:

> **The median turn may not gain an LLM call.** A turn at 2 calls that becomes 3 moves from a
> stratum where 31% clear the bar to one where 0/14 did.

This rules out the textbook supervisor topology. What survives is a **flattening**, not a
deepening: the same two model calls the median turn already makes, given distinct roles and
distinct context, with the loop between them replaced by a planned dispatch.

### 2.2 The three roles

**Orchestrator (one call, strong model).** Input: user turn, conversation context, accumulated
criteria, memory block, and a *capability catalogue* — not the 14 full JSON schemas. Output: a
structured plan, no prose. The plan names the tool invocations (a small DAG: independent calls in
one stage, dependent calls in a second), an **answer contract** (what the answer must state, which
sources it must cite, and a length budget in tokens), and an explicit `no_tools` verdict when the
turn needs none.

The orchestrator decides: *is this a search, a comparison, a factual lookup, or a clarification?
which tools, with which arguments? does anything depend on anything else? how long may the answer
be? what must it cite?*

**Executor (zero or one call, small model — model `<TO BE FILLED>`).** Runs the plan. For an
invocation whose arguments are fully determined by the plan, **no model is involved at all** —
it is a deterministic dispatch through the existing `execute_tools_node` machinery. A model call
happens only for *argument derivation*: when stage-2 arguments must be read out of a stage-1
result (e.g. a postcode from a listing feeding `calculate_commute`). The executor never writes
user-visible prose and never chooses which tools exist — it fills in arguments and reports.

**Synthesiser (one call, strong model).** Input: the answer contract, the compacted tool results,
and nothing else — not the tool schemas, not the plan's reasoning. Output: the answer. This is the
existing final `agent` call with a narrower prompt and a length budget.

The critic stays exactly where it is. It is measurably cheap (median 1 ms in the median stratum)
and it is load-bearing for `grounded` and `contradicted_claims`.

### 2.3 Which side of the line each tool falls on

The boundary is not "cheap vs expensive". It is **"can this be decided from the user's turn alone,
or does it need a judgement about the answer?"**

| tool | side | why |
|---|---|---|
| `search_properties` | **orchestrator decides, executor dispatches — with a caveat** | Deciding *to* search and with what budget/area/bedrooms is the central routing decision — `expected_route` is `search_properties` on 27 of 98 eval cases and `multi_search` on 10 more, the largest routing class by a wide margin. But this tool contains its own two-call model pipeline (§0.4). It is **already a mis-drawn layer boundary**: criteria extraction belongs in the orchestrator's plan, not buried in the tool. Fixing that is the single largest structural cleanup available, and it is a *correctness* change with an unknown latency sign. |
| `get_property_details` | **executor, deterministic** | Local pandas over the listing cache. p50 20 ms, max 134 ms, no network. Its arguments are a property id that the plan or a prior result already contains. Nothing to think about. |
| `check_safety` | **executor, deterministic** | `data.police.uk` by lat/lon. Arguments are an area the orchestrator already named. Pure retrieval; the *interpretation* of a crime score belongs to the synthesiser, and the answer contract should carry the citation requirement (`must_mention_source: data.police.uk`). |
| `calculate_commute` | **executor, one derivation call may be needed** | TfL Journey Planner. Origin often has to be read out of a `search_properties` result — a genuine stage-2 dependency and the clearest case for a small-model argument-derivation call. Note this is exactly where fc's two recorded fabrications live (C6, C11 — invented commute minutes), so the layer that touches it is a **high-severity** layer. |
| `calculate_commute_cost`, `check_transport_cost`, `get_transport_info` | **executor, deterministic** | Fare tables and TfL lookups. `check_transport_cost` is a static CSV (~1 ms). Arguments are origin/destination/mode, all in the plan. |
| `search_nearby_pois` | **executor, deterministic, hard-capped** | OSM Overpass. p90 9,870 ms, **max 55,981 ms** (n=216) — the worst tail in the system, already carrying an internal 20 s budget and a 25 s tool timeout. This tool should never be on the critical path of a median turn; it is a tail problem, not a median problem. |
| `web_search` | **executor, deterministic** | SearXNG. Remarkably stable on cache misses: p50 2,046 / p90 2,072 / max 2,236 ms (n=621) — a 190 ms spread across 621 calls. Its predictability makes it the natural first candidate for speculative pre-dispatch (§8, Stage 5). |
| `get_weather` | **executor, deterministic** | 1,746 ms, and it is a `forbidden_tools` entry on several eval cases. The orchestrator's job here is to *not* plan it; the executor should not be able to invent it. |
| `recall_memory` | **orchestrator only** | Reading memory shapes the plan itself. It must run before planning, not inside it. It is already effectively free (p50 0 ms, max 235 ms). |
| `remember` | **orchestrator only — never delegated** | The **only** `side_effect="write"` tool in the registry. It drives the taint gate, the write audit, and the `denied_recall` / `user_authorized` records that the zero-tolerance gate reads. A write authorised by a small model on a narrowed context is a security regression, full stop. It stays on the strong model, in the fully-contexted path, behind the existing guard. |
| `ask_user` | **orchestrator only** | `terminal=True`. Choosing to ask instead of answer is the highest-leverage decision in the system — it is how legacy achieved its 2,672 ms p50, by declining to answer on 25/50 paired turns. An executor must never be able to reach for it. |
| `compare_or_rank_areas` | **orchestrator decides, executor dispatches** | Highest p50 of any tool (2,075 ms) and semantically a mini-plan of its own. Treat as one planned invocation for now; decomposing it is out of scope. |

**The rule that falls out:** the executor gets **read-only, side-effect-free, argument-determined**
tools. Every tool that is terminal (`ask_user`), that writes (`remember`), or that changes what the
plan *is* (`recall_memory`) stays above the line. That is 11 of 14 tools below the line and 3
above — and the 3 above are the entire security surface.

### 2.4 What this does *not* change

The topology is still a single turn producing a single answer. There is no fan-out into parallel
sub-agents each running their own loop. §10 explains why that was rejected.

---

## 3. Where the latency actually goes — the arithmetic

### 3.1 What "closing the gap" mechanically requires

n = 64, so the median is the mean of the 32nd and 33rd smallest: (7,199 + 7,604) / 2 = 7,401.5 ms.
A **uniform** per-turn saving of **1,402 ms** is exactly sufficient. Savings are never uniform, so
the binding constraint is the seven turns straddling the bar:

| rank | latency (ms) | must lose |
|---|---|---|
| 27 | 6,388 | 388 |
| 28 | 6,388 | 388 |
| 29 | 6,389 | 389 |
| 30 | 6,655 | 655 |
| 31 | 6,990 | 990 |
| 32 | 7,199 | 1,199 |
| **33** | **7,604** | **1,604** |

26 turns are already under. Seven more must cross. All seven are in the 2-call / 1-batch stratum —
the stratum with **exactly one** tool batch, where batch-level parallelism has nothing to merge.

### 3.2 Simulation of each design variant against the warm round

Applied per-turn to all 64 warm turns, using the §1.2 prices (1,657 ms per removed loop iteration;
17.66 ms per output token; 1,928 ms measured planning-call p50).

| variant | new p50 | under 6,000 | vs gap |
|---|---|---|---|
| **V0** baseline (warm round of record) | **7,402** | 26/64 (40.6%) | +1,402 |
| **V1** collapse every ≥2-batch turn to 1 batch @1,657 ms/iter | 7,094 | **26/64 (40.6%)** | +1,094 |
| **V1b** same, priced at the measured 1,928 ms planning call | 7,066 | 27/64 (42.2%) | +1,066 |
| **V2** V1 **but** a mandatory plan hop costs zero-tool turns +1,928 ms | 7,306 | **21/64 (32.8%)** | +1,306 |
| **V3** V1 + **all tool time = 0** (Amdahl bound, physically impossible) | 5,098 | 37/64 (57.8%) | −902 |
| **V4a** V1 + answers 20% shorter | 5,498 | 34/64 (53.1%) | −502 |
| **V4b** V1 + answers 30% shorter | 4,599 | 41/64 (64.1%) | −1,401 |
| **V5a** V1 + planning call 25% faster on a small model | 6,478 | 30/64 (46.9%) | +478 |
| **V5b** V1 + planning call **40%** faster | 5,923 | 33/64 (51.6%) | −77 |
| **V5c** V1 + planning call 50% faster | 5,781 | 33/64 (51.6%) | −219 |
| **V5d** V1 + planning call 75% faster | 5,153 | 38/64 (59.4%) | −847 |

**Read V1 first, because it is the one being built.** Merging multi-batch turns into single wider
batches — the entire payoff of the parallel-tool-batch work applied at the plan level — moves the
p50 by **308 ms and moves zero turns across the bar.** The turns it helps (14 of 64, all with ≥2
batches) sit at p50 13,230 / 12,732 / 22,971 / 17,984 / 28,616 ms. Saving each of them 1,657 ms
per collapsed iteration leaves every one of them still far over 6,000 ms. It is a **p95 lever**,
not a p50 lever, and it should be measured and defended as one.

**V2 is the warning.** A planner/executor split implemented the obvious way — always plan, then
always act — makes 12 currently-fast zero-tool turns (p50 3,674 ms, 12/12 under the bar) pay for a
planning hop they do not need. The p50 gets *better* than V0 by 96 ms while the count of turns
under the bar **drops from 26 to 21**. This is precisely the trap the project's own rule about p50
vs p95 warns about, running in the other direction: a median can improve while the product gets
worse for more users. **The orchestrator must be able to emit an answer directly, in the same
call, with no second hop.**

**V3 is the ceiling.** Even if every tool returned instantaneously — no network, no Overpass, no
TfL, no scraper — the p50 lands at 5,098 ms and only 57.8% of turns clear the bar. The entire
tool-side optimisation space, taken to its physical limit, is worth about 1,900 ms. Every real
mechanism recovers a fraction of that.

**V5 is where the ask lands, and it is conditional.** Moving only the tool-emitting LLM call to a
smaller model needs that call to be **≥40% faster** to close the gap, on top of V1. At the measured
1,928 ms planning-call p50 that means the small model must emit the same tool call in **≤1,157 ms**.
Whether any available model does that at acceptable route accuracy is `<TO BE FILLED>` — see §6.

### 3.3 The honest summary sentence

> **A planner/executor split with a strong orchestrator, priced only on what is measured today,
> buys ≈308 ms of the 1,402 ms gap and zero turns under the bar. To reach 6,000 ms it must be
> combined with either a ≥40% faster tool-emitting call (model unavailable, unmeasured) or a ~30%
> shorter answer (which is PR #15's lever, not this architecture's).**

### 3.4 One number that should temper every claim above

Between the cold round and the warm diagnostic — **identical code, identical population** — the
round p50 moved 8,493 → 7,402 ms on the paired set, a swing of **1,091 ms**. The *paired per-case*
median moved only **−350 ms**, and it is still −350 ms when restricted to the 50 cases whose LLM
call count did not change. The remaining ~740 ms of round-p50 movement is composition: **14 of 64
cases changed their LLM call count between the two runs** (deltas from −3 to +3).

So a round-level p50 shift of ~740 ms was produced by **no code change at all**. Almost every
effect in §3.2 is smaller than that. This is exactly what PR #19 exists to quantify, σ(p50) is
unknown, and until it is known **no stage in §8 may be judged on a single round.**

---

## 4. Context isolation, and why its latency value is approximately zero here

Claude Code's subagents get a fresh, isolated context window and return only a summary; the
documented worked example is a subagent that read 6,100 tokens and returned 420 (§7, A3). Anthropic
frames sub-agents primarily as a **context-management** technique for exactly this reason (§7, A6).
The same isolation is available here and is worth having. It is **not** worth having for latency,
and the brief was right to ask for that to be checked.

### 4.1 What isolation would remove

Fixed per-call prompt overhead today. These are **character counts from source divided by 4**, not
tokenised counts; the directive contains CJK, for which ÷4 under-counts. Exact figures are
`<TO BE FILLED>` and should come from a tokeniser, not this estimate — the §4.2 conclusion does not
depend on the precision, because it lands near zero for any value in this range.

| block | chars | ~tokens |
|---|---|---|
| `CAPABILITIES_NOTE` | 687 | ~170 |
| `SECURITY_DIRECTIVE` | 1,868 | ~470 |
| language directive + 13 behaviour rules | ~4,950 | ~1,240 |
| system directive, total | ~7,500 | **~1,900** |
| **14 tool schemas, all sent on every call** | 17,561 | **~4,390** |
| fixed total re-sent every loop iteration | ~25,100 | **~6,300** |

A synthesiser that receives no tool schemas drops ~4,390 tokens. An executor holding only the 4–5
tools its plan names drops ~2,500–3,000.

### 4.2 Why that is worth almost nothing in milliseconds

**94.09% of input tokens are served from the provider cache when warm.** The median turn sends
17,196 input tokens of which **16,384 are cache reads and only 698 are uncached**. The ~6,300-token
fixed prefix is precisely the part that caches best, because it is byte-identical across turns.

Two independent estimates of what an uncached input token costs:

* **Regression (weak).** Adding uncached input tokens to the warm 2-term fit gives
  `lat ≈ 1765 + 8.86·out_tok + 734·llm_calls + 1.61·uncached_in_tok`, R² 0.653 versus 0.634
  without it — **1.9 points of R² for a whole extra regressor.** The coefficient is barely
  identified. *(The same fit on **total** input tokens returns R² 0.848 with a **negative**
  coefficient on input and +15,794 ms per LLM call — a textbook collinearity blow-up, since input
  tokens ≈ calls × prefix. It is reported here only so nobody quotes it.)*
* **Natural experiment (better).** Cold → warm moved the median turn from **7,002 to 698 uncached
  input tokens** — 6,304 tokens removed — for a paired median gain of **350 ms**. That is
  **0.056 ms per uncached input token**, 29× less than the regression coefficient.

Take the empirical figure. Removing 3,000 tokens of tool schema from a sub-agent's prompt is worth
**3,000 × 0.056 ≈ 170 ms** *if those tokens were uncached* — and they are not, they are the most
reliably cached tokens in the system. The realistic latency value of context isolation at the
median is **0–170 ms, and closer to 0.**

### 4.3 The risk is larger than the reward, and it points the other way

DeepSeek's context cache matches on an **exact token prefix**. Splitting one prompt into three
role-specific prompts creates three prefixes — fine in steady state, each individually cacheable.
But the failure mode is cheap to hit and expensive to pay for: **if turn-specific content (the
plan, the user's query, the tool results) is placed before the stable block, nothing caches at
all.** Running the ledger backwards, the cold round's 71.18% cache-read rate cost 350 ms at the
paired median against the warm 94.09%; a genuinely 0% prefix would put the median turn's uncached
input at 17,196 tokens instead of 698.

**Design rule, non-negotiable:** every role's prompt must be laid out `[stable role system prompt]
→ [stable tool schemas for that role] → [turn-specific content]`, in that order, and the
cache-read share must be a monitored quantity per role.

### 4.4 What isolation *is* worth

Cost, and the two quality metrics the gate reads. fc costs $0.0480 per case against legacy's
$0.0218; a synthesiser call that drops 4,390 schema tokens and an executor that drops ~3,000 both
reduce billable input on the uncached fraction. More importantly, a synthesiser that **cannot see
the tool schemas cannot hallucinate a tool**, and one that receives an explicit answer contract has
a narrower surface for the `no_evidence_numbers` failure. Claim isolation on **`forbidden_tool`
(fc 3.06%), `no_evidence_numbers` (fc 2.04%) and cost** — not on latency.

---

## 5. Where the token budget actually is

The median turn emits **416.5 output tokens** warm (452 on the cold round of record). Output tokens
alone explain 60.6% of latency variance at 17.66 ms each. Stratified:

| `llm_calls` | n | median output tokens | p50 latency |
|---|---|---|---|
| 1 | 15 | 232 | 3,649 |
| 2 | 35 | 473 | 7,604 |
| 3 | 6 | 605 | 13,230 |
| 5 | 2 | 903 | 22,971 |
| 7 | 1 | 1,999 | 28,616 |

And by category:

| category | n | p50 | under bar | soft-wrapped | median calls | median out-tok |
|---|---|---|---|---|---|---|
| B_money | 13 | 4,456 | 10/13 | 0/13 | 1 | 265 |
| G_memory | 2 | 4,823 | 2/2 | 0/2 | 2 | 208 |
| D_crime_poi | 11 | 5,896 | 6/11 | 1/11 | 2 | 381 |
| C_commute | 5 | 8,305 | 1/5 | 0/5 | 2 | 518 |
| A_retrieval | 14 | 9,375 | 3/14 | 0/14 | 2 | 525 |
| F_grounding | 9 | 9,428 | 2/9 | 1/9 | 2 | 417 |
| **E_multi_constraint** | **9** | **10,033** | **1/9** | **4/9 (44%)** | **3** | **706** |

`E_multi_constraint` is the slowest category on every axis at once: most LLM calls, most output
tokens, most soft wraps. Its four worst cases are 28.6–29.4 s with 3–7 LLM calls and 890–1,999
output tokens. **This is the category a multi-agent design is supposed to be for, and it is the
category where adding calls is most lethal** — E6 went 5 calls / 29,374 ms, E11 went 7 calls /
1,999 output tokens / 28,616 ms. The lever for E is fewer iterations and shorter answers, in that
order; it is not more agents.

The orchestrator's **answer contract** — a per-category output-token budget carried into the
synthesiser prompt — is the mechanism by which this architecture could touch output length at all.
It is the same lever PR #15 pre-registers, reached through a different door, and it must not be
double-counted: **if PR #15 ships an answer-length limit, this proposal's V4 column belongs to
PR #15, not to the architecture.**

---

## 6. Model tiering

### 6.1 What could move down a tier

| call | today | candidate tier | risk |
|---|---|---|---|
| tool-emitting call (~1,928 ms p50, n=133) | `deepseek-v4-flash` | small model | **route accuracy.** fc's 80.6% is the metric most directly at stake; it is also fc's largest win over legacy (58.2%) and the thing most likely to be given back |
| executor argument derivation (new, would not exist today) | — | small model | **`no_evidence_numbers`.** Both of fc's two fabrications (C6, C11) are invented commute minutes — the exact field an argument-deriving executor handles |
| the two nested calls inside `search_properties` (~934 ms p50, n=48) | `deepseek-v4-flash`, un-routed | small model, **and routed** | lowest risk, highest housekeeping value: these bypass `ModelRouter` entirely and should be routed before anything else is tiered |
| synthesiser (~3,139 ms p50) | `deepseek-v4-flash` | **do not move down** | it is 40% of the median turn, but it is also where `grounded` (79.6%, n=280) and `money_grounded` (84.4%, n=160) are won or lost |
| orchestrator | `deepseek-v4-flash` | consider moving **up** to the unused `deepseek-v4-pro` route | this is the "聪明模型用于全局调度" half of the ask, and it is available today at zero new integration cost. It will be **slower**; it is a quality bet, not a latency one |
| `remember` / write path | `deepseek-v4-flash` | **never** | the only write tool; the entire zero-tolerance surface |

### 6.2 The blocker

There is no smaller model in the router (§0.3). Before any tiering claim can be made, someone must
establish, for a specific candidate:

* `SMALL_MODEL_ID` = `<TO BE FILLED>` — the model, its provider, and whether it supports this
  stack's function-calling contract (including `DEEPSEEK_STRICT` / `app/core/strict_schema.py`)
* `SMALL_MODEL_PLANNING_P50_MS` = `<TO BE FILLED>` — measured on the same 133-call population
* `SMALL_MODEL_ROUTE_ACCURACY` = `<TO BE FILLED>` — 98-case eval, fc arm
* `SMALL_MODEL_FORBIDDEN_TOOL_RATE` = `<TO BE FILLED>` — must not exceed legacy + 1 pp, the
  existing relative gate

§3.2 gives the pass mark that the first of these must clear: **≤1,157 ms**, i.e. 40% faster than
the current 1,928 ms, and even that only reaches 5,923 ms with 33/64 turns under the bar — a
77 ms margin, comfortably inside the round-to-round noise of §3.4. **A single round could not
distinguish that from failure.**

### 6.3 How a regression would be detected

The 98-case harness (`evaluation/run_benchmark.py`) already emits everything needed, and the two
gate metrics are **relative to legacy at +1 pp**, so both arms must be run — an fc-only sweep
cannot decide them (this is the §3.9 lesson).

Guardrail metrics, in severity order, with current fc values as the reference point:

| metric | fc today | direction |
|---|---|---|
| `route_accuracy` | 80.6% | the primary tiering risk |
| `no_evidence_numbers` | 2/98 = 2.04% | **severity ≫ rate** — both instances are invented commute minutes |
| `forbidden_tool` | 3/98 = 3.06% | relative gate, +1 pp vs legacy |
| `passed` | 60.2% | composite |
| `grounded` | 79.6% (n=280) | **read the denominator** — a model that answers less scores better |
| `contradicted_claims` | 0 | any non-zero is disqualifying |

**Regression thresholds are deliberately left blank.** All of these are now known values; choosing
a tolerance against them after the fact is the move `HANDOFF §3.5` rejected for PR #15 and §5 of
PR #19 documents as having once certified a 29%-power procedure. Each stage's tolerance must be
pre-registered before the round that judges it:

```
STAGE_n_ROUTE_ACCURACY_FLOOR      = <TO BE FILLED>
STAGE_n_NO_EVIDENCE_NUMBERS_CEIL  = <TO BE FILLED>
STAGE_n_P50_DELTA_MIN_MS          = <TO BE FILLED>   # requires σ(p50) from PR #19
STAGE_n_ROUNDS_REQUIRED_k         = <TO BE FILLED>   # k = ceil(2·(2.8016·σ̂/δ)²), PR #19 §5
```

The `grounded` denominator deserves its own guard. A tiered system that answers *less* will score
better on every rate metric while being worse — legacy's 88.9% `money_grounded` on n=99 against
fc's 84.4% on n=160 is that effect already on record. Any tiering round must report **claim counts
alongside claim rates**, and a drop in the denominator is a regression even when the rate rises.

---

## 7. Prior art

Everything below was fetched and read on 2026-07-26. Sources that could not be opened are omitted.

### A. Anthropic / Claude Code

**A1. "How we built our multi-agent research system"** — Hadfield, Zhang, Lien, Scholz, Fox, Ford,
Anthropic Engineering, 2025-06-13. <https://www.anthropic.com/engineering/multi-agent-research-system>

Orchestrator-worker: a lead agent decomposes the query and spawns subagents that operate in
parallel, each with an isolated context window. Reports that *"a multi-agent system with Claude
Opus 4 as the lead agent and Claude Sonnet 4 subagents outperformed single-agent Claude Opus 4 by
90.2% on our internal research eval"*, and — the part that matters more here — that *"agents
typically use about 4× more tokens than chat interactions"* while *"multi-agent systems use about
15× more tokens than chats."* On BrowseComp, three factors explained 95% of variance and *"token
usage by itself explains 80% of the variance."* Published scaling heuristics: simple fact-finding →
1 agent / 3–10 tool calls; comparisons → 2–4 subagents; complex research → 10+.

Two traps, flagged because they are easy to mis-cite. The 90.2% is on an **internal** eval, not
BrowseComp. And the post's *"cut research time by up to 90%"* refers to parallelisation changes
**within** the multi-agent system, **not** multi-agent versus single-agent — it is not a citation
for "multi-agent is faster."

**A2. Claude Code — "Create custom subagents".** <https://code.claude.com/docs/en/sub-agents>

*"Each subagent starts with a fresh, isolated context window. It doesn't see your conversation
history, the skills you've already invoked, or the files Claude has already read."* Tools are an
explicit allowlist (`tools`) plus denylist (`disallowedTools`). **Model pinning is first-class**:
the `model` frontmatter field takes `sonnet`/`opus`/`haiku`/`fable`, a full model id, or `inherit`
(the default). Nesting is off by default — *"a subagent you ask to delegate does the work itself
and returns one summary."*

**A3. Claude Code — "Explore the context window".** <https://code.claude.com/docs/en/context-window>

The best *quantified* statement of the isolation benefit found anywhere, and a worked example
rather than a claim: *"Only the subagent's final text response comes back to your context, plus a
small metadata trailer with token counts and duration. The subagent read 6,100 tokens of files. You
got a 420-token result. That's the context savings."* The same page itemises the honest cost side —
~900 tokens of subagent system prompt, 1,800 for its own CLAUDE.md copy, ~970 for MCP tools and
skills, 120 for the task prompt. That per-subagent fixed cost is directly analogous to §4.1 here.

**A4. Model tiering inside Claude Code** (same page as A2). The stated benefits include *"Control
costs by routing tasks to faster, cheaper models like Haiku"*; the built-in `claude-code-guide`
agent is pinned to Haiku. The most useful datapoint is a changelog note: *"As of v2.1.198, Explore
inherits the main conversation's model instead of always running on Haiku"* — i.e. the built-in
exploration subagent **historically ran unconditionally on the small model**, and the docs still
recommend defining a custom `Explore` with `model: haiku` to keep it there. This is the closest
published analogue to what the owner is asking for.

**A5. "Building effective agents"** — Anthropic Engineering, 2024-12-19.
<https://www.anthropic.com/engineering/building-effective-agents>

The canonical pattern vocabulary: **prompt chaining, routing, parallelization** (sectioning /
voting), **orchestrator-workers**, **evaluator-optimizer**, and autonomous **agents**.
Orchestrator-workers as defined: *"A central LLM dynamically breaks down tasks, delegates them to
worker LLMs, and synthesizes their results."* Two lines this proposal takes as binding: *"Agentic
systems often trade latency and cost for better task performance"* and *"add multi-step agentic
systems only when simpler solutions fall short."*

**A6. "Effective context engineering for AI agents"** — Rajasekaran, Dixon, Ryan, Hadfield,
Anthropic Engineering, 2025-09-29.
<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>

Frames subagents as a **context-management** technique rather than a parallelism one: *"Each
subagent might explore extensively, using tens of thousands of tokens or more, but returns only a
condensed, distilled summary of its work (often 1,000-2,000 tokens)."* This is the framing §4
adopts — isolation is a correctness and cost mechanism here, not a latency mechanism.

### B. Planner/executor splits

**B1. ReAct: Synergizing Reasoning and Acting in Language Models** — Yao et al., arXiv
**2210.03629**, ICLR 2023. <https://arxiv.org/abs/2210.03629> — The interleaved reason-then-act
loop that fc_loop implements. Reports absolute success-rate gains of +34% on ALFWorld and +10% on
WebShop. Notably its abstract claims **no** latency or token benefit; it is the baseline the two
papers below attack on cost.

**B2. ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models** — Xu
et al., arXiv **2305.18323**. <https://arxiv.org/abs/2305.18323> — Diagnoses interleaved loops as
causing *"huge computation complexity from redundant prompts and repeated execution"*, and
generates the whole plan once, up front. Reports **5× token efficiency and 4% accuracy improvement
on HotpotQA**, and offloads reasoning from 175B GPT-3.5 into a **7B LLaMA** — direct prior art for
a small model in the executor slot.

**B3. An LLM Compiler for Parallel Function Calling (LLMCompiler)** — Kim et al., arXiv
**2312.04511**, ICML 2024. <https://arxiv.org/abs/2312.04511> — Three components: a Function
Calling Planner, a Task Fetching Unit, and an Executor. This is the cleanest published statement of
the split §2.2 proposes. Reports, versus ReAct: **latency speedup up to 3.7×, cost savings up to
6.7×, accuracy up to ~9% better.**

The 3.7× is the number most likely to be quoted at this proposal, so it needs its caveat attached
here: LLMCompiler's speedup comes from **parallelising function calls that ReAct issues serially**.
§0.2 measured that 87.6% of this system's tool batches contain exactly one tool, and §3.2 measured
that collapsing every multi-batch turn is worth 308 ms and zero turns. The mechanism is real; the
workload it needs is not present here.

**B4. Plan-and-Solve Prompting** — Wang et al., arXiv **2305.04091**, ACL 2023.
<https://arxiv.org/abs/2305.04091> — Plan-then-execute as a *prompting* strategy for zero-shot
reasoning, with **no tools** and **no latency, token or cost numbers** in the abstract. Cited for
lineage only.

**B5. HuggingGPT** — Shen et al., arXiv **2303.17580**. <https://arxiv.org/abs/2303.17580> — The
archetype of a strong planner dispatching to many weaker specialised executors, in four stages:
task planning → model selection → task execution → response summarisation. Abstract reports
qualitative results only; no efficiency delta to cite.

### C. Routing and cascades

**C1. FrugalGPT** — Chen, Zaharia, Zou, arXiv **2305.05176**. <https://arxiv.org/abs/2305.05176> —
Names prompt adaptation, LLM approximation and **LLM cascade**; reports matching GPT-4 performance
at **up to 98% cost reduction**, or +4% accuracy at equal cost. Also documents that API pricing
across providers *"can differ by two orders of magnitude"* — the economic premise for tiering. Note
it is a **cost** result, not a latency one.

**C2. RouteLLM** — Ong et al., arXiv **2406.18665**. <https://arxiv.org/abs/2406.18665> — Routers
trained on preference data that select between a stronger and a weaker model at inference. Reports
**>2× cost reduction in certain cases without compromising response quality**, and — most useful
for a system that has already had a model retired underneath it — that the routers **transfer**:
performance holds when the strong and weak models are swapped at test time.

### D. Speculative and parallel tool execution

**D1. Parallelizing Tool Execution and LLM Generation for Low-Latency Agent Serving (PASTE)** — Sui
et al., arXiv **2603.18897**. <https://arxiv.org/abs/2603.18897> — Predicts likely future tool
invocations from recurring agent patterns and **executes them speculatively while the LLM is still
generating**, isolating speculative results until confirmed. Reports **43.5% reduction in average
task completion time and 1.8× lower observed tool latency.** *(Cite the current title; v1 was
circulated as "Act While Thinking" and search snippets carry stale figures.)*

**D2. Speculative Interaction Agents** — Hooper et al., arXiv **2605.13360**.
<https://arxiv.org/abs/2605.13360> — Asynchronous I/O plus speculative tool calling; **1.3–1.7×
speedups on cloud models out of the box with minor accuracy loss**, 1.6–2.2× with 3B local models.
Frames the problem in the same terms as this repo: agentic tool-calling *"can add several seconds
or more of latency"* against a ~1 s interactive budget.

**D3. SPAgent** — Huang et al., arXiv **2511.20048**. <https://arxiv.org/abs/2511.20048> — The most
directly relevant *justification* for a tiered planner, because of why it works: *"early agent
steps often involve simple evidence-gathering, where correct actions can often be predicted without
full reasoning."* Reports **up to 1.65× end-to-end speedup while maintaining or improving
accuracy** — and states the honest limit of naive predict-verify, that *"it retains the full
original workload and adds extra inference overhead."*

### E. Can a small model make the tool call?

**E1. The Berkeley Function Calling Leaderboard (BFCL)** — Patil, Mao, Yan, Ji, Suresh, Stoica,
Gonzalez, ICML 2025, PMLR 267:48371–48392. <https://proceedings.mlr.press/v267/patil25a.html> —
The load-bearing sentence for §2.3's boundary: *"while state-of-the-art LLMs excel at single-turn
calls, memory, dynamic decision-making, and long-horizon reasoning remain open challenges."*
Evaluates serial and parallel calls via AST matching, plus abstention and stateful multi-step
settings.

**E2. BFCL v1 blog.**
<https://gorilla.cs.berkeley.edu/blogs/8_berkeley_function_calling_leaderboard.html> — *"in terms
of simple function calling (without complex planning and chained function calling), finetuning an
open-source can be as effective as propriety models"*, while proprietary models remain better at
*multiple* and *parallel* calls. 2,000 question-function-answer pairs; categories simple / multiple
/ parallel / parallel-multiple plus relevance detection.

**E3. BFCL V3 (multi-turn).**
<https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html> — Adds 200 base and 800 augmented
multi-turn entries (missing parameters, missing functions, long context, composite), scored on
backend **state** after execution. Methodology only — **no per-model scores on the page**, so no
specific small-model number may be quoted from it.

This is the capability boundary the §2.3 split is drawn along: the executor only ever issues
single-turn, argument-determined calls, which is the regime where small models are reported
competitive. Long-horizon decisions, memory and abstention stay with the orchestrator, which is the
regime BFCL reports as still hard.

### F. The published downside

**F1. A1 again.** Anthropic's own post carries the counter-evidence: *"in practice, these
architectures burn through tokens fast"*; *"multi-agent systems require tasks where the value of
the task is high enough to pay for the increased performance."* On the architecture's own limits:
lead agents *"execute subagents synchronously, waiting for each set of subagents to complete before
proceeding… creates bottlenecks in the information flow"*, and *"the entire system can be blocked
while waiting for a single subagent."* On scope: *"some domains that require all agents to share
the same context or involve many dependencies between agents are not a good fit."*

**F2. Why Do Multi-Agent LLM Systems Fail?** — Cemri et al., arXiv **2503.13657**.
<https://arxiv.org/abs/2503.13657> — Opens: *"Despite enthusiasm for Multi-Agent LLM Systems (MAS),
their performance gains on popular benchmarks are often minimal."* Builds MAST, a taxonomy of **14
failure modes in 3 categories** (system design, inter-agent misalignment, task verification), from
150 expert-annotated traces (κ = 0.88) scaled to 1,600+ traces across 7 frameworks. Concludes the
failures need structural fixes, not prompt tweaks.

**F3. "Don't Build Multi-Agents"** — Walden Yan, Cognition, 2025-06-12.
<https://cognition.com/blog/dont-build-multi-agents> — An engineering-blog counterpoint published
one day before A1 and arguing the opposite. Two principles: *"Share context, and share full agent
traces, not just individual messages"* and *"Actions carry implicit decisions, and conflicting
decisions carry bad results."* The argument is that parallel subagents act on conflicting implicit
assumptions with no visibility into each other.

### G. What the literature actually says about *this* system

The evidence is genuinely split, and the split is informative rather than confusing. A1 reports
+90.2% on research-style tasks; F2 reports MAS gains on public benchmarks are *"often minimal"*; F3
argues against it outright for coding. A1 supplies the reconciliation: parallel fan-out pays where
subtasks are **independent and read-only**, and Anthropic explicitly excludes coding because *"most
coding tasks involve fewer truly parallelizable tasks than research."*

RentCompass looks more like the coding case than the research case. Its median turn issues **one**
tool call, 87.6% of its batches are width-1, and its slowest category (`E_multi_constraint`) is
slow because of **dependent** constraints — find a flat, *then* commute from it, *then* check crime
*there* — which is a dependency chain, not a fan-out. The literature's parallelism results
(B3, D1, D2) therefore do not transfer at their published magnitudes, and this proposal does not
claim them.

What **does** transfer is B2's decoupling (plan once, do not re-derive), E1/E2's capability
boundary (single-turn calls are the safe delegation unit), and A3/A6's context isolation as a
correctness-and-cost mechanism.

---

## 8. Staged migration

Each stage is independently shippable, independently measurable, and reversible. Ordering is by
**what unblocks measurement**, not by what is most exciting.

### Stage 0 — finish the parallel-batch diagnosis (already in flight, not this proposal's work)

**Owner: the colleague on `perf/parallel-tool-batch`.** Determine whether existing within-batch
concurrency actually delivers, and whether abandoned offload threads starve the 32-worker pool.
This proposal builds on the answer and does not duplicate it.

**Expected p50 effect: none.** §0.2 and §3.2 (V1) show the addressable population is 12.4% of
batches and 14 of 64 turns, all far above the bar. Defend this as a **p95 / soft-wrap** stage.
Current p95 is 28,460 ms against a 30,000 ms gate with only **1,540 ms of margin** — protecting that
margin is worth doing on its own terms.

### Stage 1 — **the smallest first step: make the layer boundary observable** (docs + telemetry only)

**This is the recommended first stage and it ships no architecture at all.**

Three defects block every later stage from being measurable:

1. **The observer misses nested LLM calls.** `search_properties`'s two `_call_deepseek` calls
   bypass `ModelRouter` and therefore `install_observer` (§0.4). Route them. Until this is fixed,
   an executor layer's cost is invisible to the gate — the same class of blindness as the
   2026-07-24 DeepSeek outage that `/health` could not see.
2. **Canary telemetry has no per-tool timing.** `tool_batches` is a count and `tool_budget_timeout`
   is a boolean; per-tool `elapsed_ms` exists on `tool_artifacts` and in the eval event stream but
   is not in production telemetry. Without it, §1.3's ~2,000 ms tool term stays a residual and
   nobody can attribute a change to the tool side or the model side.
3. **`llm_usage.models` should be reported per role.** Tiering cannot be evaluated if all calls
   report under one model key with no role label.

**Falsification.** This stage is falsified if, after it lands, the §1.3 budget still cannot be
reproduced from canary telemetry alone on a fresh round — specifically if per-turn
`sum(tool_elapsed_ms) + sum(llm_latency_ms) + serving_overhead` does not reconstruct
`turn_latency_ms` to within `<TO BE FILLED>` ms at the median. It is also falsified if observed
`llm_calls` on `search_properties` turns does not rise by the expected ~1 per invocation; if it
does not rise, the nested calls were not what §0.4 says they are.

**Cost:** telemetry only, no product-path behaviour change. **This is the only stage this proposal
recommends starting.**

### Stage 2 — the answer contract, without the split

Add an explicit, per-category output-token budget to the existing final call. No new agent, no new
call, no new model. This is the smallest change that touches the one lever the data supports.

**Expected effect:** V4a/V4b in §3.2 — a 20% answer cut projects to p50 5,498 ms, a 30% cut to
4,599 ms, at 17.66 ms/token.

**Conflict declaration, deliberate:** this **is** PR #15's lever. If PR #15 ships, Stage 2 is
PR #15 and this proposal claims nothing for it. Stage 2 exists here only so that the architecture's
projected numbers are not silently double-counting PR #15's.

**Falsification.** Falsified if median output tokens fall by ≥20% and the p50 does not fall by at
least `<TO BE FILLED>` ms — which is a real possibility, since the 17.66 ms/token coefficient comes
from a single-term fit with R² 0.606 and cross-turn variation confounds length with difficulty.
Also falsified, and more importantly, if `grounded` claim **counts** (not rates) fall: a shorter
answer that makes fewer claims scores better on every rate while being worse (§6.3).

### Stage 3 — split the roles, keep the call count

Implement §2.2's three roles as three prompts against the **same** model. The orchestrator emits a
plan **or an answer** in one call (never a mandatory second hop — see V2). The executor dispatches
deterministically with no model call where arguments are determined. The synthesiser gets the
answer contract and the compacted results but **not** the tool schemas.

**Expected effect: ≈ +0 to −308 ms.** This stage is for `forbidden_tool`, `no_evidence_numbers` and
cost, per §4.4. It should be **pre-declared as latency-neutral** so that a null result is not
written up as a failure.

**Falsification.** Falsified as a *correctness* stage if `forbidden_tool` and `no_evidence_numbers`
do not improve, or if `route_accuracy` falls below `<TO BE FILLED>`. Falsified as a *safe* stage —
and immediately reverted — if any zero-tolerance counter moves off zero, or if any `remember` /
`ask_user` invocation is ever attributed to a non-orchestrator role. Falsified as *harmless* if the
count of turns under 6,000 ms falls at all, which is the V2 failure mode and the one to watch.

### Stage 4 — tier the executor (blocked on Stage 1 and on a model)

Only after Stage 1 makes the cost visible and a `SMALL_MODEL_ID` exists. Move the tool-emitting
call and the argument-derivation call down a tier; consider moving the orchestrator **up** to the
unused `deepseek-v4-pro` route in the same round, since that is the other half of the owner's ask
and it is available today.

**Expected effect:** §3.2 V5 — needs the planning call ≥40% faster (≤1,157 ms) to reach 6,000 ms,
and lands at 5,923 ms with 33/64 under the bar even then. **A 77 ms margin, which is smaller than
the ~740 ms of round-to-round composition drift measured in §3.4.**

**Falsification.** Pre-register `STAGE_4_ROUTE_ACCURACY_FLOOR`, `STAGE_4_NO_EVIDENCE_NUMBERS_CEIL`
and `STAGE_4_P50_DELTA_MIN_MS`, all `<TO BE FILLED>`, **before** the round. Falsified if the
measured planning-call p50 on the small model exceeds `SMALL_MODEL_PLANNING_P50_MS`, or if any
guardrail in §6.3 breaches, or if the `grounded` denominator falls. Given the 77 ms projected
margin, this stage **requires k rounds** where `k = ceil(2·(2.8016·σ̂/δ)²)` from PR #19 §5 — and
σ̂ is unknown, so **k is unknown, so this stage cannot currently be judged at all.**

### Stage 5 — speculative pre-dispatch (research, not scheduled)

D1/D2's mechanism: start the most likely tool call while the orchestrator is still generating.
`web_search` is the natural first candidate — p50 2,046 / p90 2,072 / max 2,236 ms across 621
recorded calls, a 190 ms spread and the most predictable tool in the registry. A speculative
dispatch that lands could remove ~2,000 ms of serial wait from the median turn's budget; one that
misses costs a wasted call, and against a warm cache costs nothing at all (this round's sweep
measured `web_search` at a 79 ms p50 on cache hits).

**Not scheduled.** It requires the Stage 1 instrumentation, an accurate prior over which tool a
turn will call, and a mis-speculation accounting the gate can read. Listed so it is not lost.

### Ordering rationale

Stages 1 → 2 → 3 → 4 is deliberately the reverse of the intuitive order. The architecture (3) comes
after the instrument (1) because §0.4 shows this system can already hide a ~934 ms model call from
its own gate; adding a layer before fixing that would make the layer unjudgeable. Tiering (4) comes
last because it depends on both.

---

## 9. Falsification criteria, consolidated

Restating the project's standing rule, because it governs every row: **thresholds are pre-registered
before the measurement that judges them**, and single-round A/B is unreliable until PR #19 fixes
σ(p50). Every `<TO BE FILLED>` below is blank on purpose. PR #19 §5 records what happens when a
number is chosen to look reasonable — a 250 ms threshold picked as "roughly half the effect size"
that would have certified a procedure with **29% power**.

| stage | primary falsifier | guardrail falsifier | rounds needed |
|---|---|---|---|
| 0 parallel-batch diagnosis | p95 does not improve and thread-pool starvation is not demonstrated | any p50 regression | ≥1; p95 is noisier than p50, `<TO BE FILLED>` |
| 1 observability | telemetry cannot reconstruct `turn_latency_ms` to within `<TO BE FILLED>` ms at the median; observed `llm_calls` on search turns does not rise | any behaviour change on the product path at all | 1 (deterministic, no model) |
| 2 answer contract | ≥20% output-token cut yields < `<TO BE FILLED>` ms p50 improvement | `grounded` / `money_grounded` **claim counts** fall | `k` from PR #19 |
| 3 role split | `forbidden_tool` and `no_evidence_numbers` do not improve; `route_accuracy` < `<TO BE FILLED>` | **turns under 6,000 ms falls** (the V2 mode); any zero-tolerance counter ≠ 0; `remember`/`ask_user` reached by a non-orchestrator role | `k` from PR #19 |
| 4 tiering | small-model planning p50 > `<TO BE FILLED>` ms | any §6.3 guardrail; `grounded` denominator falls | `k` — **currently unknown, so unjudgeable** |
| 5 speculation | mis-speculation rate > `<TO BE FILLED>`; no median improvement | wasted-call cost exceeds `<TO BE FILLED>` | not scheduled |

**A pre-registration with placeholders authorises nothing.** None of these stages may run against
this document as written.

---

## 10. What I would not do, and why

**1. A supervisor that spawns sub-agents which run their own loops.** This is the textbook
multi-agent topology and it is what the ask most literally describes. It is rejected on this
system's own data. Such a turn costs at minimum: 1 orchestration call + 2 sub-agent loop calls +
1 synthesis call = **4 LLM calls**. On the warm round, **0 of 4 turns at 4 calls were under the
bar** (p50 12,732 ms); **0 of 14 turns at ≥3 calls** were under it. A1's own architecture section
names the same failure — *"the entire system can be blocked while waiting for a single subagent."*
Every additional layer of agency is another serial model call on the critical path.

**2. Parallel sub-agents fanning out over the tools.** The fan-out is not there. 87.6% of batches
are width-1; the median turn makes **one** tool call. And `E_multi_constraint`, the category this
would exist to serve, is a **dependency chain** — flat, then commute from the flat, then crime at
that flat — not independent work. A1 excludes coding from multi-agent for exactly this reason;
RentCompass's hard category resembles coding more than research.

**3. Re-proposing prompt-size, message-array, call-count or schema-compaction levers.** All four are
on the refuted list (`HANDOFF §3.6`) and §4.2 re-derives why for the first two: at 94.09% cache
read, the empirical price of an uncached input token is **0.056 ms**, so the entire ~6,300-token
fixed prefix is worth a few hundred milliseconds at most, and it is the best-cached part of the
prompt. Context isolation is proposed here **explicitly not** as a latency lever.

**4. Delegating any write, any terminal decision, or any memory decision.** `remember` is the only
`side_effect="write"` tool and carries the whole taint/audit surface. `ask_user` is terminal and is
how a system decides not to answer — legacy's flattering 2,672 ms p50 came substantially from
returning `clarification` on 25/50 paired turns, and an executor with `ask_user` would be a machine
for reproducing that. Both stay on the orchestrator.

**5. Judging any stage on one round.** §3.4: identical code, identical population, round p50 moved
~740 ms on composition alone (14 of 64 cases changed call count between runs). Most effects in
§3.2 are smaller than that.

**6. Filling in any threshold in this document.** Every latency and quality figure it would be
measured against is already known. Choosing tolerances now is choosing a pass mark after seeing the
measurement — the move rejected for PR #15 in `HANDOFF §3.5`.

**7. Claiming this architecture reaches 6,000 ms.** It does not, on anything measured. §3.3 states
the number: **≈308 ms of a 1,402 ms gap, and zero turns moved.** The routes that do close it are a
faster model (unavailable, unmeasured) and a shorter answer (PR #15's, not this document's).

---

## 11. Risks, ordered

1. **More agents means more LLM calls, and 3+ call turns never make the bar.** 0/14 warm, 0/9 on
   the 07-22 paired round. Two independent rounds, one direction. Any design that adds a call to
   the median turn is disqualified before it is measured. This is the risk that shapes the entire
   proposal, and V2 in §3.2 shows it materialising from a completely reasonable-looking design
   choice — 26 turns under the bar becomes 21.

2. **Stage 4's projected margin (77 ms) is an order of magnitude smaller than the measured
   round-to-round drift (~740 ms).** Even a *successful* tiering round would be indistinguishable
   from noise on a single round, and `k` is unknown until PR #19 runs.

3. **Argument derivation by a small model touches the highest-severity field in the system.** fc's
   two fabrications (C6, C11) are invented commute minutes — a number a user acts on. 2.04% is a
   low rate on a high-stakes field, not a benign one. The executor's most useful job is also its
   most dangerous.

4. **A prompt split can destroy the 94.09% cache-read rate.** Get the prefix ordering wrong and the
   median turn's uncached input goes from 698 to 17,196 tokens. The saving is ≤170 ms; the loss is
   much larger.

5. **The instrument cannot currently see the layer being proposed.** §0.4 — a ~934 ms model call
   already runs inside `search_properties` unobserved. Stage 1 exists because of this and must
   precede everything.

6. **The `grounded`-denominator trap, reached by a new route.** A tiered or contract-constrained
   system that answers *less* improves every rate metric while being worse. fc already makes 84%
   more groundable claims than legacy and scores *lower* on `money_grounded` for it. Any round must
   report counts beside rates.

---

## 12. Provenance

| claim | artefact |
|---|---|
| warm p50 7,401.9 ms; 26/64 under bar; 94.09% cache read; call/batch strata; per-category table | `.runtime/diagnostic-8793c0b-warmcache-2026-07-25/{manifest.json, canary-fc_loop.diagnostic.jsonl}` |
| cold p50 8,466 ms; STAGE-PAUSE verdict; 452 median output tokens; the cited regression | `.runtime/round-8793c0b-internal-2026-07-25/{README.txt, report-67.json, canary-fc_loop.copy.jsonl}`, `docs/HANDOFF.md` §3.8 |
| node-span decomposition; planning 1,928 ms / final 3,139 ms; tools 26.2% non-fixtured; 87.6% width-1 batches; nested `memory` calls at 934 ms | `.runtime/round-8793c0b-internal-2026-07-25/eval/sweep/{summary.json, raw_runs.jsonl, events.jsonl}` |
| fc-vs-legacy quality (pass 60.2%, route 80.6%, grounded 79.6%, `no_evidence_numbers` 2.04%, `forbidden_tool` 3.06%) | `docs/HANDOFF.md` §3.9; `eval/sweep/` and `eval/sweep-legacy/` |
| +599 ms serving-path paired median; σ(p50) unknown; the `k` formula; the 250 ms / 29%-power precedent | PR #19 `docs/round_variance_preregistration.md` (**DRAFT, unmerged, NOT FROZEN**) |
| 7,402 ms warm figure as an owner-override record | PR #20 `docs/cutover-2026-07-26` (**open, unmerged**) |
| per-tool latency distributions (`web_search` n=621, `search_nearby_pois` n=216, etc.) | `evaluation/results/**/events.jsonl*`, 38 files, `tool_call.execution_time_ms`. Pooled across cold and warm runs; the round-of-record sweep alone is too fixtured to show them |
| tool inventory, budgets, registry, router aliases, prompt sizes | `app/core/{agent_loop.py, tool_system.py, loop_prompts.py, context_assembler.py}`, `app/core/tools/*`, `src/uk_rent_agent/llm/router.py` |
| `perf/parallel-tool-batch` == mainline, 0 commits | `git rev-parse`, verified 2026-07-26 |

Derived quantities (the warm refits, the V0–V5 simulation, the 0.056 ms/uncached-token figure, the
§1.3 budget) were computed from those artefacts for this document. They are **indicative**: the
warm refits sit at R² 0.606–0.653 on n = 64, the simulation applies population-level coefficients
per turn, and the tool term in §1.3 is a residual. None of them is a gate measurement and none may
be cited as one.
