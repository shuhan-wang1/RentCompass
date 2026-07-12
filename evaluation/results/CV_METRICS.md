# CV_METRICS — RentCompass Evaluation

_Generated 2026-07-12T06:27:47Z, HEAD `070675d`. Every number is copied verbatim from a result file; nothing is estimated. Each rate carries its denominator._

## 可安全使用

### [SAFE] Model-routing A/B engineering deltas (n=98, live)

- **中文 CV 表述**: 在 98 例真实基准上，按节点路由模型（强模型仅用于必要节点）相较全强模型基线：强模型调用 165/170→78/172（-52.7%），总 token 197981→185612（-6.2%），输出 token -28.6%，成本 -24.3%，端到端均值 -38.4%，grounding 基本持平（160/207 vs 160/207）。
- **English CV statement**: On the 98-case live benchmark, per-node model routing vs an all-strong baseline cut strong-model calls 165/170->78/172 (-52.7%), total tokens (-6.2%), output tokens (-28.6%), cost (-24.3%), and mean e2e latency (-38.4%), while grounding held (160/207 vs 160/207).
- **Raw data (num/den)**: strong_calls 165/170 -> 78/172; tokens 197981 -> 185612; output_tokens 56014 -> 40013; cost $0.02105 -> $0.01594; e2e_mean_ms 9338 -> 5754; grounded 160/207 vs 160/207
- **Metric definition**: Aggregated per-config token/call/cost/latency counters over 98 cases x 1 repeat; cost = published DeepSeek rate x measured tokens.
- **Result file**: `evaluation/results/ablation_model.json (+ ablation_model.csv)`
- **Safe to use**: YES
- **Required caveat**: Cost/token saving is TOKEN-VOLUME driven, NOT a cheaper per-token rate (chat & reasoner share one rate). Single live run; offline benchmark, not real users; live tool variance applies.

### [SAFE] Grounding fidelity on the live 98-case benchmark

- **中文 CV 表述**: 在 98 例真实基准（真实 DeepSeek + 真实/缓存工具 + 实时 web_search）上，可核验声明的 grounded 率 152/204 (74.5%)，金额类 grounded 率 121/152 (79.6%)，与证据矛盾的声明数 1。
- **English CV statement**: On the 98-case live benchmark, verifiable-claim grounded rate 152/204 (74.5%), money-claim grounded rate 121/152 (79.6%), with 1 claims contradicting the tool evidence.
- **Raw data (num/den)**: grounded 152/204, money_grounded 121/152, contradicted=1, source_coverage 120/204
- **Metric definition**: grounded_rate = grounded verifiable claims / total verifiable claims; money_grounded_rate = grounded money claims / total money claims; contradicted = claims that contradict tool evidence. Heuristic grader.
- **Result file**: `evaluation/results/live_routed_98/summary.json`
- **Safe to use**: YES
- **Required caveat**: Heuristic (text-marker) grading; single live run. SearXNG is operational, so web cases ARE included and grounded (cite Zoopla/Rightmove/SpareRoom), but live web/scrape results are NON-DETERMINISTIC across runs, so exact figures vary. Report as grounding FIDELITY, not overall answer quality.

### [SAFE] Retrieval parallelization: retrieval-stage latency reduction + race parity

- **中文 CV 表述**: 检索并发消融（16 例 x 3 重复 = 48 次/配置）：并行 map-reduce 将检索阶段延迟均值 -57.1%、p95 -42.0%；串并配对中仅 0/48 出现工具调用数差异（完成态不匹配 0，无结果在完成层丢失）。
- **English CV statement**: Retrieval-concurrency ablation (16 cases x 3 repeats = 48 runs/config): parallel fan-out cut retrieval-STAGE latency mean -57.1% and p95 -42.0%; only 0/48 paired runs showed a tool-count difference (completion_mismatch=0; no result dropped at completion level).
- **Raw data (num/den)**: retrieval_stage_mean_ms 1359 -> 582 (-57.1%); p95 5790 -> 3360 (-42.0%); race_anomalies 0/48 (tool_count=0, completion=0)
- **Metric definition**: retrieval-stage latency = time inside the retrieval/tool fan-out node; race anomaly = serial-vs-parallel pair differing in tool-call count or completion status.
- **Result file**: `evaluation/results/ablation_retrieval.json (+ ablation_retrieval.csv)`
- **Safe to use**: YES
- **Required caveat**: e2e latency is ~unchanged (synthesis-dominated) — quote the retrieval-STAGE reduction, not an end-to-end speedup. No race anomaly was observed (no dropped result).

### [SAFE] Fault-tolerance mechanics (15 real scenarios)

- **中文 CV 表述**: 在 15 个真实故障注入场景（真实工具/图/幂等/护栏代码，仅模型被 mock）中，故障正确暴露 15/15 (100.0%)，幂等写入 3/3 (100.0%)（重复持久化写入 0 次），降级回退 2/2 (100.0%)，故障后仍完成任务 13/15 (86.7%)。
- **English CV statement**: Across 15 real fault-injection scenarios (real tool/graph/idempotency/guardrail code; only the LLM mocked): faults surfaced honestly 15/15 (100.0%), idempotency 3/3 (100.0%) with 0 duplicate durable writes, fallback 2/2 (100.0%), post-fault completion 13/15 (86.7%).
- **Raw data (num/den)**: faults_surfaced 15/15, idempotency 3/3, fallback 2/2, dup_writes=0, retry_recovery 4/8, post_fault_completion 13/15
- **Metric definition**: See REPORT.md §9. Only the model is mocked; tool/idempotency/guardrail code is production code, so these are genuine resilience mechanics.
- **Result file**: `evaluation/results/fault_summary.json (+ fault_injection.csv)`
- **Safe to use**: YES
- **Required caveat**: Single run; model mocked (does NOT measure answer quality). retry_recovery 4/8 is expected — the 4 non-recoveries are non-recoverable-by-design faults. Report as 'resilience mechanics', not end-to-end accuracy.

### [SAFE] Long-term memory: REAL store isolation / forget / restart checks

- **中文 CV 表述**: 记忆存储的确定性检查（真实 ChromaDB，无模型参与）：用户隔离 5/5 (100.0%)，遗忘/删除 3/3 (100.0%)，进程重启恢复 1/1 (100.0%)，写入成功 4/4 (100.0%)，身份门控 7/7 (100.0%)。
- **English CV statement**: Deterministic memory-store checks (real ChromaDB, no model in the loop): user isolation 5/5 (100.0%), forget/delete 3/3 (100.0%), process-restart recovery 1/1 (100.0%), write success 4/4 (100.0%), identity gate 7/7 (100.0%).
- **Raw data (num/den)**: user_isolation 5/5, forget_delete 3/3, restart_recovery 1/1, write_success 4/4, identity_gate 7/7
- **Metric definition**: Store-level deterministic checks: per-user retrieval filtering, GDPR forget/delete, on-disk persistence across a fresh instance, and the pure identity-gate function. No LLM involved.
- **Result file**: `evaluation/results/memory_eval.json`
- **Safe to use**: YES
- **Required caveat**: These store checks are REAL, but small n (mostly 1/1-5/5). Keep them SEPARATE from the extraction/consolidation numbers, which are stubbed (see 不建议使用). Also covered by tests/test_agent_memory_isolation.py.

### [SAFE] Evaluation framework scope

- **中文 CV 表述**: 自建离线评测框架：98 例基准（7 类、23 种可机检约束类型、确定性打分器 + 可选 LLM 裁判），外加指标采集/定价/故障注入（15 场景）/消融/记忆评测子系统。
- **English CV statement**: Built a self-contained offline eval framework: a 98-case benchmark (7 categories, 23 machine-checkable constraint types, a deterministic grader + optional LLM judge), plus metrics-collection, pricing, fault-injection (15 scenarios), ablation, and memory-eval subsystems.
- **Raw data (num/den)**: 98 cases; 7 categories; 23 constraint types; 15 fault scenarios; retrieval A/B 16x3
- **Metric definition**: Counts derived from benchmark/cases.jsonl, metrics.graders.CONSTRAINT_CHECKERS, and the fault_injection scenario module.
- **Result file**: `evaluation/benchmark/cases.jsonl, evaluation/metrics/graders.py, evaluation/fault_injection/`
- **Safe to use**: YES
- **Required caveat**: Describe as engineering SCOPE (framework built), not as a quality score.

## 不建议使用

### [AVOID] Raw end-to-end pass_rate as a quality headline

- **中文 CV 表述**: 端到端整体通过率 34/98 (34.7%)。不建议作为质量头条：该数值被真实的智能体行为发现（D 类地区治安过度澄清、G 类记忆未落盘/召回缺失、E 类多约束链路与来源引用不足）、启发式约束打分器、以及实时工具波动（web/scrape 结果跨运行不确定）共同拉低。
- **English CV statement**: Overall end-to-end pass_rate 34/98 (34.7%). Do NOT use as a quality headline: it is dragged down by REAL agent findings (D area-crime over-clarification, G memory-not-persisted / recall gaps, E multi-constraint chaining + source-citation gaps), heuristic constraint checkers, and live tool variance (web/scrape results non-deterministic across runs). It is a useful diagnostic, not a headline accuracy number.
- **Raw data (num/den)**: passed 34/98 (34.7%); constraints 215/321
- **Metric definition**: A case passes only if it completes AND calls no forbidden tool AND passes EVERY heuristic constraint AND has 0 contradictions.
- **Result file**: `evaluation/results/live_routed_98/summary.json`
- **Safe to use**: NO
- **Required caveat**: Report the component metrics (grounding, routing deltas) and the per-category findings instead. Quoting 34/98 alone misrepresents the system.

### [AVOID] Memory extraction_precision / update / consolidation numbers

- **中文 CV 表述**: 记忆抽取精度 3/3 (100.0%)、更新/替换/矛盾处理均为 1/1。不建议使用：这些数值下的 LLM 事实抽取/重要性/整合调用是被打桩（stub）的，只反映存储机制，不代表真实抽取质量。
- **English CV statement**: Memory extraction_precision 3/3 (100.0%) and the update/stale/contradiction checks (all 1/1). Do NOT use: the LLM extraction / importance / consolidation calls are STUBBED, so these reflect store plumbing, not real extraction quality.
- **Raw data (num/den)**: extraction_precision 3/3, update_correctness 1/1, stale_replacement 1/1, contradiction_handling 1/1
- **Metric definition**: Checks that route through the LLM extraction/consolidation path, which the eval replaces with canned outputs (see memory_eval.py docstring).
- **Result file**: `evaluation/results/memory_eval.json`
- **Safe to use**: NO
- **Required caveat**: Mechanics of a stub, not extraction quality. Real extraction quality needs a live model run and human/judge review.

### [AVOID] Any n<15 single-run rate as a standalone statistic

- **中文 CV 表述**: 任何 n<15 的单次运行比率（如各记忆子检查 1/1、单个 race 异常 1/36、部分工具的少量调用延迟）不建议单独引用：置信区间过宽。
- **English CV statement**: Any n<15 single-run rate quoted standalone (e.g. individual memory sub-checks at 1/1, the single retrieval race anomaly at 1/36, low-call-count per-tool latencies) — do NOT quote alone: confidence intervals are too wide.
- **Raw data (num/den)**: e.g. restart_recovery 1/1, retrieval_relevance 0/1, race_anomalies 1/36
- **Metric definition**: Rates whose denominator is a handful of runs.
- **Result file**: `evaluation/results/memory_eval.json, evaluation/results/ablation_retrieval.json`
- **Safe to use**: NO
- **Required caveat**: Aggregate or repeat before quoting; otherwise present only as directional.

### [AVOID] LLM-judge agreement with the deterministic grader

- **中文 CV 表述**: LLM 裁判与确定性打分器的一致率：本轮未运行（裁判为可选 `--judge`，未启用），无结果文件可引用，切勿编造。
- **English CV statement**: LLM-judge vs deterministic-grader agreement: NOT run this round (the judge is optional via `--judge` and was not enabled), so there is no result file to cite. Do not fabricate a number.
- **Raw data (num/den)**: not produced (no judge column in this run's outputs)
- **Metric definition**: Agreement between the optional LLM judge and the deterministic grader.
- **Result file**: `not produced (run: run_benchmark --live --judge to generate)`
- **Safe to use**: NO
- **Required caveat**: Only quote once actually measured.

