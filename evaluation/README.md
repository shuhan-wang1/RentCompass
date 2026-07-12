# RentCompass Offline Evaluation Framework

A self-contained, **offline-first** evaluation harness for the RentCompass rental
agent. It measures routing, tool use, grounding, cost, latency, resilience, and
long-term memory — with a hard cost cap and honest denominators everywhere.

> **Golden rule:** every reported number comes from a result file, with its
> denominator (e.g. `64/76 (84.2%)`). Where a metric was genuinely not produced in a
> given run (e.g. the optional LLM judge was not enabled, or SearXNG was unreachable),
> the report says so with the reason — **never a fabricated or estimated value.**
>
> The live suite HAS been run in the `uk_rent` conda env (valid DeepSeek key, chromadb
> present). The current `results/REPORT.md` + `results/CV_METRICS.md` are driven purely
> from the real files listed below.

---

## Directory map

```
evaluation/
├── AUDIT.md                  architecture + file:line seams (Phase-1)
├── README.md                 <- this file
├── model_pricing.yaml        DeepSeek per-token rates (chat & reasoner: SAME rate)
├── benchmark/                45 cases, 10 smoke, fixtures, schema, constraint vocab
│   ├── cases.jsonl           the benchmark (7 categories, 20 constraint types)
│   ├── fixtures/             recorded tool outputs for deterministic replay
│   ├── schema.json           case schema
│   └── README.md             constraint vocabulary + UK money formulas
├── configs/                  eval configs (DATA; loader turns them into patches)
│   ├── baseline_all_strong.yaml   ModelRouter.route -> reasoner for EVERY purpose
│   ├── routed_models.yaml         default per-purpose routing
│   ├── serial_retrieval.yaml      max_concurrency = 1
│   ├── parallel_retrieval.yaml    unbounded fan-out
│   └── loader.py                  load_config / apply_config
├── metrics/
│   ├── collector.py          event capture (llm_call/tool_call/node_span/critic/turn)
│   ├── pricing.py            cost from token counts (None when unconfirmed)
│   ├── fake_llm.py           deterministic unbilled model seam
│   └── graders.py            deterministic grader + optional LLM judge
├── run_benchmark.py          the benchmark runner (CaseRunner + writers)
├── run_ablation.py           Phase-4 model A/B + Phase-5 retrieval A/B
├── fault_injection/          resilience harness (15 injected-fault scenarios)
│   ├── injectors.py          fault injectors + ScenarioResult
│   ├── scenarios.py          the 15 scenarios
│   └── run.py                entry point
├── memory_eval.py            Phase-7 long-term-memory eval (needs chromadb)
├── report.py                 REPORT.md + CV_METRICS.md generator
└── results/                  all outputs land here
```

---

## Entry commands

All commands are **offline/unbilled by default**. Run from the repo root.

| Command | What it does | Key outputs |
|---|---|---|
| `python -m evaluation.run_benchmark --smoke --offline` | 10 smoke cases, mechanics-only | `results/<out>/summary.json`, `per_case.csv`, `tool_metrics.csv`, `model_usage.csv`, `raw_runs.jsonl` |
| `python -m evaluation.run_ablation --study both --offline --smoke` | Model + retrieval A/B | `results/ablation_model.{json,csv}`, `results/ablation_retrieval.{json,csv}` |
| `python -m evaluation.fault_injection.run` | 15 fault scenarios (GENUINE) | `results/fault_injection.csv`, `results/fault_summary.json` |
| `python -m evaluation.memory_eval` | Memory eval (needs chromadb) | `results/memory_eval.json` |
| `python -m evaluation.report --results evaluation/results --out evaluation/results` | Aggregate everything | `results/REPORT.md`, `results/CV_METRICS.md` |

Common flags (benchmark + ablation): `--smoke`, `--limit N`, `--category A_retrieval`,
`--repeat K`, `--offline` / `--live`, `--max-cost-usd F`, `--resume`, `--out DIR`,
`--timestamp TS`.

---

## Offline vs Live

| | **Offline (default)** | **Live (`--live`)** |
|---|---|---|
| Model | deterministic `fake-chat` (unbilled) | real DeepSeek via `ModelRouter` |
| Tools | fixtures replayed / stubbed | fixtured cases replay; others run in-process |
| Cost | $0 | metered against the cap |
| Validates | routing / tool selection / latency / memory-isolation / **resilience mechanics** | grounding & answer **quality**, real token/cost/latency deltas |
| Grounding numbers | mechanics-only (canned text) — **NOT quality** | real |

Offline runs prove the orchestration end-to-end and produce genuine **mechanics**
numbers (fault-tolerance, race-safety, scheduling). Anything that depends on real
model text/tokens (grounding quality, Phase-4 cost/token deltas, Phase-5 latency
deltas, LLM-judge) needs a live run.

---

## Cost cap

`--max-cost-usd` is a **hard cap**. The benchmark refuses to *start* a case whose
estimated cost would exceed the cap and stops with a recorded `stopped_reason`.
`run_ablation` shares **one** budget across the entire ablation (all configs) and
checkpoints after every case, so `--resume` continues where a cap/interruption
stopped it. Default cap: `$15` (≈¥110; see `model_pricing.yaml`). Offline cost is
always $0, so the cap never triggers offline.

---

## Environment (what actually ran)

Everything below was produced in the **`uk_rent` conda env** (Python 3.10, chromadb
1.1.0, openai 2.41.0, langgraph 1.2.4). Run Python via
`conda run --no-capture-output -n uk_rent`, and export
`PYTHONIOENCODING=utf-8 PYTHONUTF8=1` first (the default Windows console is gbk and
will otherwise mangle the Chinese in the reports).

| Item | State in this env | Effect |
|---|---|---|
| **DeepSeek key** | valid | `--live` runs work; live cost is metered (whole live suite cost < $0.02) |
| **chromadb** | installed (1.1.0) | memory store eval RAN — see `memory_eval.json` (`status: ok`) |
| **SearXNG** | **not reachable** | live `web_search` returns empty → web-dependent B/F cases can't ground web claims (a real, disclosed limitation, `REPORT.md` §6 + §12) |

`memory_eval` note: the LLM extraction / importance / consolidation calls are STUBBED,
so `extraction_precision` and the update/stale/contradiction checks are store-plumbing
mechanics, **not** real extraction quality. The isolation / forget / restart / write /
retrieval checks are real deterministic store behaviour.

---

## What is CV-usable vs NOT

- **Well-supported (`results/CV_METRICS.md` → 可安全使用):** model-routing A/B engineering
  deltas at n=45 (strong-call −55.8%, cost −32.3%, e2e −38.6%, grounding maintained
  63↔64/78); live grounding fidelity (grounded 64/76, money 45/53, contradicted 0);
  retrieval-stage parallelization latency (−67.5% mean / −62.1% p95, 1/36 benign race
  anomaly); fault-tolerance mechanics (surfaced 15/15, idempotency 3/3, 0 dup writes);
  real memory store isolation/forget/restart checks; framework scope.
- **Not a headline (`→ 不建议使用`):** raw end-to-end pass_rate 20/45 (dragged by real
  agent findings + heuristic checkers + SearXNG-down); stubbed memory-extraction numbers;
  any n<15 single-run rate; LLM-judge agreement (not run this round).

See `results/CV_METRICS.md` for per-claim 中文/English wording, raw num/den, definition,
result-file path, safe flag, and the required caveat.

---

## Reproducing the current results (conda `uk_rent`)

Run **from the repo root** with a valid `DEEPSEEK_API_KEY` in `app/.env`. Each command
below is the actual invocation that produced the corresponding result file (commit
`070675d`). Timestamps use a UTC ISO string; cost is metered against a shared hard cap
(live cost was trivial — well under $0.02 total).

```bash
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CR="conda run --no-capture-output -n uk_rent python"

# 1. Main benchmark — LIVE, routed_models, all 45 cases  -> results/live_routed_45/
$CR -m evaluation.run_benchmark --live --config routed_models --max-cost-usd 5 \
    --out evaluation/results/live_routed_45 --timestamp "$TS"

# 2. Model-routing A/B (baseline_all_strong vs routed_models) — LIVE, all 45 cases
#    -> results/ablation_model.{json,csv}
$CR -m evaluation.run_ablation --study model --live --limit 45 --max-cost-usd 5 \
    --out evaluation/results --timestamp "$TS"

# 3. Retrieval A/B (serial vs parallel) — LIVE, first 12 cases x 3 repeats
#    -> results/ablation_retrieval.{json,csv}
$CR -m evaluation.run_ablation --study retrieval --live --limit 12 --repeat 3 \
    --max-cost-usd 5 --out evaluation/results --timestamp "$TS"

# 4. Fault injection — real tool/graph/idempotency/guardrail code, mocked model
#    -> results/fault_summary.json + fault_injection.csv
$CR -m evaluation.fault_injection.run --out evaluation/results

# 5. Memory eval — needs chromadb (present in uk_rent)  -> results/memory_eval.json
$CR -m evaluation.memory_eval --out evaluation/results

# 6. Regenerate REPORT.md + CV_METRICS.md purely from the files above
$CR -m evaluation.report --results evaluation/results \
    --out evaluation/results --timestamp "$TS"
```

Notes:
- `run_ablation` defaults to the smoke subset unless a selector is given, so the
  `--limit 45` / `--limit 12` flags above are what pin the case counts.
- The optional LLM judge is off by default; add `--judge` to step 1 (LIVE only) to also
  emit judge-vs-grader agreement. It was **not** run for the current results.
- To (optionally) unblock `web_search` quality, start a SearXNG instance before step 1;
  otherwise `web_search` returns empty and the web-dependent cases stay depressed.
- On Windows PowerShell, set `$TS = (Get-Date -AsUTC -Format s) + 'Z'` and call
  `conda run --no-capture-output -n uk_rent python ...` directly.
