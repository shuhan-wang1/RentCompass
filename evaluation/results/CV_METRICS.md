# CV_METRICS — RentCompass Evaluation

_Last updated 2026-08-06, HEAD `d74561b`. Every number is copied verbatim from a result file; nothing is estimated. Each rate carries its denominator._

> **架构标注是必需的。** 本文件的条目分别测在两条架构上：`fc_loop`（`app/core/agent_loop.py::build_fc_graph`，**线上服务的那条**）与 `legacy`（`app/core/langgraph_agent.py`）。引用任何一条前先看它的 **Metric definition** 写的是哪条——把 legacy 上测到的数字说成"当前架构"是本文件最容易犯的错。

## 可安全使用

### [SAFE-WITH-SCOPE] Model-routing A/B on the CURRENT fc_loop architecture (n=98 x 3 x 2 = 588 live runs)

> 取代了 2026-07-12 的旧路由条目（−52.7% / −38.4%）。旧条目已移入「不建议使用」，原因见那一节。

- **中文 CV 表述**: 在 98 例自建基准上做配对 A/B（每臂 3 次运行，共 588 次 live 请求，0 失败）：相对「所有节点使用更强模型 deepseek-v4-pro」的基线，当前分档路由把成本降低 **67.7%**（bootstrap 95% CI −70.4%…−65.0%），端到端延迟均值降低 **5.5 s**（CI −7.4…−3.8 s），证据支撑率未见下降（77.0% → 84.1%）。
- **English CV statement**: On a 98-case self-built benchmark, a paired A/B (3 runs per arm, 588 live requests, 0 failures) shows the tiered model routing cuts cost by **67.7%** (bootstrap 95% CI −70.4%…−65.0%) and mean end-to-end latency by **5.5 s** (CI −7.4…−3.8 s) against an all-strong-model (deepseek-v4-pro) baseline, with no drop in grounding fidelity (77.0% → 84.1%).
- **Raw data (num/den)**: cost USD 0.4084 → 0.1320 (−67.7%, CI −70.4%…−65.0%); e2e_mean_ms 12,046 → 6,528 (−5,518 ms, CI −7,428…−3,815); e2e_p50 8,645 → 5,682 (−2,963 ms, CI −3,780…−2,240); e2e_p95 37,628 → 16,024 (−21,604 ms, CI −37,653…−8,935); tokens_total 6,141,143 → 5,435,909 (−11.5%, CI −19.9%…−3.1%); llm_calls 620 → 564 (−9.0%, CI −16.0%…−2.0%); pro-model calls 618/620 → 0/564; grounded 843/1095 (77.0%) → 812/965 (84.1%), +7.16 pp (CI +1.72…+13.00); money_grounded 489/655 (74.7%) → 447/560 (79.8%), +5.16 pp (CI −1.06…+11.53, **crosses 0**); contradicted 4 → 1; constraint_pass 227/294 vs 228/294
- **Metric definition**: Paired per-case A/B on the fc_loop graph (`app/core/agent_loop.py::build_fc_graph`). Control arm forces every node to `deepseek-v4-pro` via an in-process `ModelRouter.route` override (message protocol identical to production); test arm is `evaluation/configs/routed_models.yaml` unpatched. "Strong-model call" = a `deepseek-v4-pro` call, NOT a model-name heuristic — `chat` and `reasoner` resolve to the same model on this commit. Cost from the fixed price table in the report §0.2. CIs are cluster bootstrap with the CASE as the resampling unit, 2000 resamples, seed `20260804`.
- **Result file**: `evaluation/results/fc_loop_routing_ab/analysis_A.json` (+ `table_A.md`, raw `shard{0,1}/runs.jsonl`); design, limits and conclusions in `evaluation/results/EVAL_REPORT_20260804.md` §1.
- **Safe to use**: YES, with the scope below
- **Required caveat**: Measured at commit `0952c56` (branch `telemetry-issue78`, 2026-08-04) — **not the current HEAD**; `app/core/agent_loop.py` and `langgraph_agent.py` have changed since on `evaluation/holdout-v3`. Offline self-built benchmark in a throwaway container, NOT a production SLA (the serving pools were untouched). Latency is WARM-cache relative (every run restores the same 19-row snapshot), so it is not a cold-start figure — cost and call-count conclusions are unaffected. Do NOT call the grounding delta an accuracy gain: the two arms hit different external-tool failure rates (11.8% vs 5.9%), so the +7.16 pp cannot be cleanly attributed to model tier; write "未见质量下降", not "质量提升". Money-grounding CI crosses 0 → 未观察到显著差异. Never mix or add the retrieval-stage number to the end-to-end number. `task_completed` is saturated (294/294 both arms) and carries no information — do not quote it.

### [SAFE] All-thinking baseline fails intermittently inside the deep tool loop (mechanism finding, n=50)

- **中文 CV 表述**: 把任一节点切到 thinking 档后，多约束类请求有 **7/11 (63.6%)** 的概率在轮内 HTTP 400（`reasoning_content` 未回传给 API），全样本 **7/50 (14.0%)**；失败全部集中在 `E_multi_constraint`，其余六类 0 失败。这是消息构造层的机制缺陷，不是答案质量结论。
- **English CV statement**: Switching any node into thinking mode makes multi-constraint requests fail inside the turn with HTTP 400 (`reasoning_content` not passed back) in **7/11 (63.6%)** of cases — **7/50 (14.0%)** across the whole probe — with every failure landing in `E_multi_constraint` and zero in the other six categories. This is a message-construction mechanism defect, not an answer-quality result.
- **Raw data (num/den)**: 7/50 overall (smoke 1/5 + probe#1 0/25 + probe#2 6/20); by category E_multi_constraint 7/11, A 0/4, B 0/6, C 0/10, D 0/7, F 0/7, G 0/5; the v4-pro control arm failed 0/294 by contrast
- **Metric definition**: Feasibility probes of the discarded first-design control arm (`baseline_all_strong.yaml`, which only enables thinking mode). Thinking was verified active (all 30 `llm_call` events in probe #1 tagged `responder#think`), so this is not an unapplied patch.
- **Result file**: `evaluation/results/fc_loop_routing_ab/thinking_probe{,2}/runs.jsonl`, `evaluation/results/_smoke/runs.jsonl`; analysis in `EVAL_REPORT_20260804.md` §1.5.
- **Safe to use**: YES, as a mechanism/engineering finding
- **Required caveat**: Production never triggers this today — the fc hot path is built with `low_latency=True` and is always non-thinking. Quote it as "a latent failure mode found by the A/B, which is why the all-thinking baseline could not be used for a paired 588-run sweep" (arm-specific dropout concentrated in the hardest category would have biased every surviving pair). Do not present it as a production incident or an availability rate.

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

---

> ⚠️ **以下三节是 2026-08-05 手工追加的，不是 `evaluation/report.py` 生成的。**
> 若将来重新生成本文件，请保留以下手工追加段落；其直接来源是 `evaluation/results/holdout_v2_review/analysis_holdout_v2.json`、冻结数据集与门禁产物，不在旧的 `ablation_*.json` 中。

### [SAFE-WITH-SCOPE] Held-out benchmark: compliant-option identification (n=33, live model + frozen fixtures)

- **中文 CV 表述**: 构建并冻结了一套 110 例合成 held-out 租房评测集及版本化约束 schema（199 项确定性测试、静态门禁全部通过，与此前用于调参的 98 例逐字零重合）。6 例冒烟暴露原主指标定义缺陷后，在其余 **33** 道未查看的硬约束检索题上冻结替代指标并确认：使用真实模型推理与冻结工具 fixture，RentCompass **33/33** 次至少点名了一处由结构化证据判定为满足全部声明条件的候选房源（**Clopper–Pearson 95% CI 89.4%–100%**）。
- **English CV statement**: Built and froze a 110-case synthetic held-out rental benchmark with a versioned constraint schema (199 deterministic tests and a passing static preflight, with zero verbatim overlap against the 98 development cases). After a six-case smoke exposed a defect in the original primary metric, a replacement metric was frozen and confirmed on the remaining **33** unseen hard-constraint retrieval cases: with live model inference and frozen tool fixtures, RentCompass named at least one fixture-grounded candidate satisfying every stated condition in **33/33** cases (**Clopper–Pearson 95% CI 89.4%–100%**).
- **Raw data (num/den)**: 33/33 (primary, excludes HO2-001 / HO2-023 whose answers were seen during the 6-case smoke before this metric was defined); 35/35 including them, exact 95% CI [90.0%, 100%]. Dataset sha256 `0294584c69bf60147e199b6d8430bfc4b041d0ea110bec6d11922feb7b88fe34`; 110 agent runs, 0 failures, 0 timeouts, USD 0.0985.
- **Metric definition**: For each `retrieval_hard` case whose FROZEN fixture contains at least one listing satisfying EVERY stated user condition, PASS iff the answer names such a listing by its unique street token or its exact monthly price. Deterministic string match, no text heuristic. Predicates and evidence fields are frozen in `evaluation/results/_harness/constraint_schema_v2.py`.
- **Result file**: `evaluation/results/holdout_v2_review/analysis_holdout_v2.json` → `deterministic.D6_correct_option_identified.primary`; dataset `evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl`; freeze record `evaluation/benchmark/holdout_v2/FREEZE.json`; gate `preflight_report.json` (gate_passed=true, exit 0).
- **Safe to use**: YES, only with the exact scope above
- **Required caveat**: This is a synthetic fixed benchmark with LIVE MODEL inference and FROZEN/fixture-replayed tools (`fixture_served=true`), not a live external-listing/tool test, production SLA or overall answer accuracy. D6 measures compliant-option RECALL: it does not require the answer to avoid also recommending a non-compliant option, to call every required tool, or to be wholly correct. The metric was defined after a 6-case smoke, so the primary denominator excludes the two hard-retrieval answers seen at that point. Quote the Clopper-Pearson interval, never the degenerate [100%, 100%] bootstrap interval. One run per case; no agent-side variance is measured.

### [SAFE-WITH-SCOPE] Held-out benchmark: MODEL blind review of evidence contradictions (n=110, live models + frozen evidence)

- **中文 CV 表述**: 在同一套冻结的 110 例合成 held-out 基准上，三轮独立**模型盲审**（两轮 deepseek-v4-flash + 一轮 deepseek-v4-pro，temperature 0，配置与轮次标签均对评审模型隐藏）分别判定 **107/110、108/109、105/110** 个回答与冻结证据**零处矛盾**，跨模型**观察一致率 0.945**。**该结果是模型盲审，不是人工评审，也不是答案正确率。**
- **English CV statement**: On the same frozen 110-case synthetic held-out benchmark, three independent **model blind-review** rounds (two deepseek-v4-flash, one deepseek-v4-pro, temperature 0, configuration and round labels withheld) found zero claims contradicting the frozen evidence in **107/110, 108/109 and 105/110** answers respectively, with a cross-model **observed agreement of 0.945**. This is a model blind review — not human evaluation and not answer accuracy.
- **Raw data (num/den)**: zero-contradiction 107/110 (R1), 108/109 (R2, one response failed to parse and is counted as unjudged), 105/110 (R3 pro); cross-model observed agreement 0.945; same-model two-round observed agreement 0.982. All 330 review requests returned, but one returned response was unusable because it failed parsing; 0 out-of-vocabulary answers and 0 `cannot_assess` (no evidence packet was truncated).
- **Metric definition**: Each judge sees the request, the full history, the case's own allowed evidence sources, the reference calculations, the memory read/write results, the executed tool list and the complete structured tool evidence, then answers `contradicted_claim_count` as an integer (or `cannot_assess`). Labels outside the frozen vocabulary are recorded verbatim as judge failures and never normalised.
- **Result file**: `evaluation/results/holdout_v2_review/{round1,round2,round3_pro}.jsonl` and `analysis_holdout_v2.json` → `model_blind_review`.
- **Safe to use**: YES, with the scope below
- **Required caveat**: Write "模型盲审 / model blind review". NEVER "human evaluation", "人工盲审", "answer accuracy" or "inter-rater reliability" — no person labelled anything. **Report the counts and the OBSERVED agreement; do NOT report Cohen's kappa for this item**: ~96% of judgments fall in one category, so the expected-agreement term approaches 1 and kappa degenerates (0.226 cross-model against an observed agreement of 0.945). The third round is another model from the SAME vendor, not a genuinely independent third party.


### [SAFE-WITH-SCOPE] Held-out v6 structured retrieval and side-effect contracts (n=180, live model + frozen fixtures)

- **中文 CV 表述**: 在一套全新冻结的 180 例合成 held-out 租房基准上（90 检索、30 计算、30 记忆、30 澄清；与历史开发集分离），使用真实模型推理和闭合 fixture 工具，结构化检索契约达到：eligible recall **57/60 (95.0%, Clopper–Pearson 95% CI 86.1%–99.0%)**，recommendation precision **87/90 (96.7%, CI 90.6%–99.3%)**；逐候选通勤证据契约 **28/30 (93.3%, CI 77.9%–99.2%)**，记忆写入契约 **30/30 (100%, CI 88.4%–100%)**。
- **English CV statement**: On a newly frozen 180-case synthetic held-out rental benchmark (90 retrieval, 30 calculation, 30 memory, 30 clarification), with live model inference and closed fixture tools, RentCompass achieved **57/60 eligible recall (95.0%, exact Clopper–Pearson 95% CI 86.1%–99.0%)** and **87/90 recommendation precision (96.7%, CI 90.6%–99.3%)**; required per-candidate commute evidence was present in **28/30 (93.3%, CI 77.9%–99.2%)** cases and required memory writes in **30/30 (100%, CI 88.4%–100%)**.
- **Raw data (num/den)**: eligible_recall 57/60; recommendation_precision 87/90; complete_constraint_satisfaction 57/60; commute_per_search_candidate 28/30; remember_write 30/30; 180/180 runner records; 0 runner errors; fixture violations 0; cost `$0.0313`.
- **Metric definition**: Deterministic structured contracts over unique frozen listing IDs and frozen tool evidence; exact two-sided Clopper–Pearson intervals. Missing structured output remains a declared failure. No prose-only inference is used for retrieval membership.
- **Result file**: `evaluation/results/holdout_v6_live/HOLDOUT_V6_REPORT.md`, `analysis_summary.json`, `per_case_metrics.jsonl`; benchmark freeze `evaluation/benchmark/holdout_v6/FREEZE.json`.
- **Safe to use**: YES, only with this exact scope.
- **Required caveat**: **这一轮跑的是 `legacy` 架构，不是线上的 `fc_loop`**（`manifest.json` 的 `"arch": "legacy"`、命令行 `--arch legacy`、`AGENT_ARCH=legacy`）。契约加固同时改了两条路径，但**只有 legacy 那一份有 held-out 证据**；引用时必须写明架构，不可含糊成"RentCompass 达到…"。此外：合成 fixture 回放基准，不代表线上房源鲜度、生产 SLA 或整体答案正确率。三例检索题暴露了真实的结构化输出/通勤证据缺陷并保留为失败（已于 `d7a7702` 修复，但**未在 fc_loop 上复测**）。原始 composite 与 task-completion 刻意不引用：冻结的 no-result marker 列表产生了 13 个语义假阴性，属待修诊断项。另注：180 例中的 30 道计算题**去掉金额后只有 1 个 distinct 模板**，且由确定性模块回答；本条引用的四个数字分母（60/90/30/30）均不含这 30 例，但不要把"180 例"当卖点。

### [SAFE-WITH-SCOPE] fc_loop vs legacy 架构对照（各 98 例 live，同树同日，逐例配对）

- **中文 CV 表述**: 把智能体从固定路由图（legacy）重构为模型驱动的工具调用循环（fc_loop）后，在同一套 98 例基准上同日逐例配对对比：任务通过率 **34.7% → 60.2%**（34/98 → 59/98），路由准确率 **58.2% → 80.6%**（57/98 → 79/98），与证据矛盾的声明 **1 → 0**。关键在分母：新架构产出 **280** 条可核验声明（旧架构 152 条，**+84%**），在多答 84% 的前提下接地率仍从 74.3% 升至 **79.6%** —— 指标提升不是靠少答换来的。
- **English CV statement**: Re-architecting the agent from a fixed routing graph (legacy) to a model-driven tool-calling loop (fc_loop), compared case-by-case on the same 98-case benchmark on the same day: task pass rate **34.7% → 60.2%** (34/98 → 59/98), routing accuracy **58.2% → 80.6%** (57/98 → 79/98), evidence-contradicting claims **1 → 0**. Read the denominators: the new architecture makes **280** verifiable claims against legacy's 152 (**+84%**), and its grounding rate still rises from 74.3% to **79.6%** — the gain is not bought by answering less.
- **Raw data (num/den)**: passed fc 59/98 (60.2%) vs legacy 34/98 (34.7%); route_accuracy 79/98 (80.6%) vs 57/98 (58.2%); grounded 223/280 (79.6%) vs 113/152 (74.3%); money_grounded 135/160 (84.4%) vs 88/99 (88.9%); contradicted_claims 0 vs 1; latency p50 5,481 vs 2,672 ms, p95 24,011 vs 10,438 ms; total cost USD 0.0480 vs 0.0218; `execute_tools` node invocations 133 vs 60
- **Metric definition**: 两臂各跑 `evaluation/benchmark/cases.jsonl` 全部 98 例，**每例 1 次**，同一棵树、同一天、同一评分器。`arch` 由 `manifest.json` 标识（`fc_loop` / `legacy`）。"可核验声明"= 评分器可判定接地与否的声明条数，即 grounded 的分母。
- **Result file**: `.runtime/round-8793c0b-internal-2026-07-25/eval/{sweep,sweep-legacy}/summary.json`（+ `per_case.csv`、`raw_runs.jsonl`）；叙述见 `docs/HANDOFF.md` §3.9
- **Safe to use**: YES, within the scope below
- **Required caveat**: **每例只跑了 1 次，没有置信区间** —— 这是本条相对其它条目最大的短板，25.5pp 的差距很大但未做显著性检验；要升级成带 CI 的结论需每臂 3 次重复 + cluster bootstrap（harness `evaluation/results/_harness/ab_runner.py` 现成）。测于 commit `8793c0b`（2026-07-25），**不是当前 HEAD**。**必须主动披露 fc 更慢更贵**（p50 5,481 vs 2,672 ms，成本 $0.0480 vs $0.0218）并解释为"多做了 84% 的工作"。legacy 更高的 `money_grounded`（88.9% n=99 vs 84.4% n=160）同样是小基数效应，被问到用分母回答，不要主动引用。`.runtime/` 不在 git 里，引用前先确认该目录仍在磁盘上。

### [SAFE-WITH-SCOPE] Batch packing: dimension coverage up while LLM round-trips go down (paired A/B, 192 live runs)

- **中文 CV 表述**: 在 8 例多维度请求上做配对 A/B（每臂 12 次重复，共 192 次 live 请求，0 失败）：把「用户提到但模型没请求的读取」打包进同一批次后，维度覆盖率从 **138/252 (54.8%)** 升到 **204/252 (81.0%)**（配对差 **+0.229**，bootstrap 95% CI +0.038…+0.441），同时 LLM 调用数**下降** 0.45 次/回合（CI −0.94…−0.09）、工具批次数下降 0.28（CI −0.58…−0.03）。三项阴性对照全部通过。
- **English CV statement**: A paired A/B on 8 multi-dimension requests (12 repeats per arm, 192 live runs, 0 failures) shows that packing a *cued but unrequested* read into an existing tool batch raises dimension coverage from **138/252 (54.8%)** to **204/252 (81.0%)** — paired difference **+0.229**, bootstrap 95% CI +0.038…+0.441 — while **reducing** LLM calls by 0.45 per turn (CI −0.94…−0.09) and tool batches by 0.28 (CI −0.58…−0.03). All three negative controls passed.
- **Raw data (num/den)**: coverage 138/252 → 204/252; runs at full coverage 34/96 → 72/96; llm_calls mean 2.99 → 2.54 (−0.45, CI −0.94…−0.09); tool_batches mean 1.56 → 1.28 (−0.28, CI −0.58…−0.03); tools executed 2.67 → 3.78 (+1.11, CI +0.15…+2.30); e2e mean −5,819 ms (CI −15,248…−564) but paired **median −40 ms**, 51/96 pairs faster, p50 8,093 → 9,159, p95 66,031 → 51,752; cost_usd +0.0000 (CI −0.0001…+0.0001, **crosses 0**); tokens_in −4,180 (CI −11,135…+1,028, **crosses 0**); soft-wrapped 2/96 → 1/96 (CI **crosses 0**); 192/192 runs ok; total cost USD 0.1086
- **Metric definition**: Paired per-case A/B on the fc_loop graph (`app/core/agent_loop.py::build_fc_graph`), arms differing **only** in `FC_DIMENSION_FANOUT_MAX` (3 vs 0), set in process per arm; no product code changed. "Cued dimension" and "unserved dimension" are the product's own `core.dimensions.cued_dimensions` and `agent_loop::_unserved_cued_dimensions`, fed the run's **executed** tools — not a look-alike matcher. Population frozen before the run: cases in `evaluation/benchmark/cases.jsonl` cueing ≥2 distinct dimensions (n=8). CIs are cluster bootstrap, CASE as resampling unit, 10,000 resamples, seed `20260805`.
- **Result file**: `evaluation/results/fanout_ab/analysis_D.json` (+ `table_D.md`, `negative_control_D.json`, raw `main/runs.jsonl`); design frozen in `evaluation/results/fanout_ab/PREREGISTRATION.md`; write-up in `evaluation/results/FANOUT_AB_REPORT_20260805.md` §Phase 2.
- **Safe to use**: YES, within the scope below
- **Required caveat**: **n = 8 cases** — the bootstrap resamples cases, so every CI is wide; 2 of the 8 (E3, E9) never triggered the mechanism and 2 more (E7, E10) sit at coverage 0.000 in both arms because their fanned-out reads were denied at the execute-time gate. `E_multi_constraint` only. Measured on branch `experiment/fanout-ab-20260805`: product code `d7a7702` (i.e. `778714a` plus this session's two Phase 1 fixes, neither of which touches the fc_loop path exercised here — Fix A is in the legacy formatter and Fix B only affects rent-conversion turns), harness `6bad6d9`. **Not** a deployed build; the serving pools were untouched. Warm-cache relative (`warm_v3.sqlite3` restored per run), so latency is not a cold-start figure. **Never quote this as a latency win**: the paired median is −40 ms and 51/96 pairs favoured ON — the significant mean is tail-only, so say "shorter tail (p95 66.0 s → 51.8 s)", never "faster". Cost, tokens and soft-wrap CIs cross 0 → 未观察到显著差异. **Do not describe this as "fc parallel vs legacy serial"** — legacy is not serial, and its own map-reduce fan-out is a separate `[SAFE]` positive result in this file. The two arms hit different external-tool failure rates (12.7% vs 7.2%, because the ON arm attempts more of the flakiest tool class), so treat the latency and token figures as availability-dependent; the coverage figure counts executed tools only and is unaffected.

### [SAFE] Mechanism finding: only the plan-time entry point was exercised (n=192 runs)

- **中文 CV 表述**: 全部 192 次运行中，72 次维度扩展**全部**发生在 plan 阶段（`agent_node`，模型已经在开批次，扩展不额外花一次 LLM 往返）；answer 阶段的 `_completion_sweep_into_batch` **一次都没触发**。因此该实验只验证了「搭顺风车」这一条机制。
- **English CV statement**: Across all 192 runs, every one of the 72 fan-out firings happened at **plan time** (`agent_node`, where the model is already opening a batch, so the expansion costs no extra LLM round-trip); the answer-time `_completion_sweep_into_batch` fired **zero** times. The result therefore validates the free-ride mechanism only.
- **Raw data (num/den)**: firings plan-time 72/72, answer-time 0/72; runs where the fan-out fired 72/96 (ON arm); additions that actually executed 143/179 (79.9%), the remaining 36 denied at the execute-time gate; added-tool histogram `search_nearby_pois` 72, `calculate_commute` 60, `check_safety` 47, `remember`/`ask_user` 0
- **Metric definition**: Each call into `agent_loop::_dimension_fanout_calls` was tagged with its call site by walking the stack in the harness; the product function itself was wrapped, not reimplemented, so what is recorded is literally what the fan-out returned.
- **Result file**: `evaluation/results/fanout_ab/negative_control_D.json`, raw `main/runs.jsonl`
- **Safe to use**: YES, as a mechanism/engineering finding
- **Required caveat**: State it as scope, not as a property of the product — the answer-time sweep exists and is reachable; this 8-case population simply never took that branch, so **no claim about it is supported either way**. The "no extra batches" control passing is therefore evidence about plan-time packing only.

## 不建议使用

### [AVOID] 「维度 fan-out 让回合更快」

- **Why**: 96 组配对的延迟中位数差是 **−40 ms**，且只有 51/96 组配对偏向 fan-out 臂；fan-out 臂的 **p50 反而更高**（9,159 vs 8,093 ms）。那个显著的均值（−5,819 ms）完全由尾部产生。
- **Say instead**: 「延迟尾部收窄（p95 66.0 s → 51.8 s）；中位数未观察到差异。」
- **Result file**: `evaluation/results/fanout_ab/analysis_D.json` → `paired_contrasts.wall_ms`，以及 `table_D.md` 里的分臂 `wall_ms` 分位数。

### [AVOID] 「fc_loop 的批内并发比串行派发快」— 两轮实验均为负结果

- **Why**: warm 与 cold 两轮配对 A/B（各 120 次 live 运行）检索阶段延迟的 bootstrap 95% CI **都跨 0**；且**阴性对照两轮都未通过**（16/60、18/60 组配对执行的工具数不一致），配对前提不成立。根因是 244 个工具批次里只有 65 个（26.6%）装了 ≥2 个调用——批次大小为 1 时批内并发收益恒为 0。
- **Say instead**: 把它作为**方法论叙事**引用：「预注册了并发派发假设，两轮配对 A/B 均未观察到显著差异且阴性对照未通过；进一步测出只有 26.6% 的批次装了 ≥2 个调用，据此判定瓶颈在规划层而非派发层，并改做批次打包（见上文 `[SAFE-WITH-SCOPE]` 批次打包条目）。」批内并发的**机制正确性**另有确定性证据（`tests/test_parallel_tool_batch.py`：N 个独立读取 ≈ S 而非 N×S，n 至 8；重新串行化会让 17 个测试挂掉 10 个），但那**只能写成机制正确性，不可写成端到端加速**。
- **Result file**: `evaluation/results/parallel_tools_ab/{analysis_C.json, negative_control_C.json, analysis_C_cold.json, negative_control_C_cold.json}`；选例口径 `case_selection.json`；叙述见 `EVAL_REPORT_20260804.md` §3

### [AVOID] 旧的模型路由数字 −52.7% / −38.4%（2026-07-12，legacy 架构）— 已被取代，禁止引用

- **中文 CV 表述**: ~~强模型调用 165/170→78/172（−52.7%）、总 token −6.2%、成本 −24.3%、端到端均值 −38.4%~~。**禁止引用**：该口径的「强模型」判定依赖 `evaluation/run_ablation.py::_is_strong` 的**模型名匹配**，而它依赖的 `deepseek-reasoner` 已退役；在当前模型线上 `chat` 与 `reasoner` 解析成同一个模型，该判定会把两臂都算成 100% 强模型调用，数字无法复现也无法解释。且该轮是 legacy 图、单次运行。
- **English CV statement**: ~~strong-model calls −52.7%, tokens −6.2%, cost −24.3%, mean e2e −38.4%~~. **Do not quote**: the "strong model" predicate is a model-NAME match in `run_ablation.py::_is_strong`, and the name it relied on (`deepseek-reasoner`) is retired — on the current model line `chat` and `reasoner` resolve to the same model, so the predicate scores both arms as 100% strong. Legacy graph, single run.
- **Raw data (num/den)**: strong_calls 165/170 → 78/172; tokens 197981 → 185612; cost $0.02105 → $0.01594; e2e_mean_ms 9338 → 5754; grounded 160/207 vs 160/207
- **Metric definition**: 见 `evaluation/results/ablation_model.json`；判定逻辑 `evaluation/run_ablation.py::_is_strong`（已失效）。
- **Result file**: `evaluation/results/ablation_model.json (+ ablation_model.csv)`
- **Safe to use**: NO
- **Required caveat**: **改引用上面 [SAFE-WITH-SCOPE] 的当前 fc_loop 条目**（588 run 配对 A/B，成本 −67.7%，端到端均值 −5.5 s）。若要提强模型调用量，当前架构的正确写法是「deepseek-v4-pro 调用 618/620 → 0/564」。另注：`run_ablation.py` 没有 `--arch` 参数，它永远只测 legacy 图——这也是 2026-08-04 那一轮必须绕开它的原因。

### [AVOID] Held-out metrics that did NOT clear the threshold — do not quote any of these

- **中文 CV 表述**: 同一批次里以下指标**未过门槛，禁止进入 CV**：预注册的硬约束满足率 0/119（案例级 0/35）——谓词在该系统「符合／已排除」的答案格式上退化，把透明的排除清单也判成违规，已如实上报但禁止引用；模型盲审的硬约束满足判定（三轮可判分母 36/34/46，比率 88.9%/97.1%/78.3%）——**分母本身在轮次间变动，口径不稳**；模型盲审的证据支撑判定（80.4%/83.5%/62.6%）——跨模型 κ 0.571，pro 的 `partial` 用量是 flash 的三倍，粒度未收敛；计算题命中参考值 20/20——**n=20 < 30**，且其中 9 道由确定性模块 `app/core/tenancy_reference.py` 而非模型回答；无结果题不补金额 4/8——n=8 且属诊断项。
- **English CV statement**: From the same batch, these did NOT clear the pre-registered threshold and must not be quoted: the pre-registered hard-constraint satisfaction rate 0/119 (case level 0/35) — the predicate degenerates on this system's "meets / excluded" answer format and scores a transparent rejection list as a violation; the model blind review's hard-constraint judgment (judgeable denominators 36/34/46 across rounds, rates 88.9%/97.1%/78.3%) — the DENOMINATOR itself moves between rounds; the model blind review's evidence-support judgment (80.4%/83.5%/62.6%) — cross-model kappa 0.571 and the pro round uses `partial` three times as often; reference-calculation match 20/20 — n=20 < 30, and 9 of those turns were answered by the deterministic module `app/core/tenancy_reference.py` rather than the model; no-result turns free of invented money 4/8 — n=8 and diagnostic only.
- **Raw data (num/den)**: D1 0/119; D2 0/35; hard_constraints_satisfied 32/36, 33/34, 36/46; claims_evidence_supported 74/92, 76/91, 57/91; D5 20/20 (9/9 deterministic module + 11/11 model); D4 4/8.
- **Metric definition**: See `evaluation/results/holdout_v2_review/analysis_holdout_v2.json` and `evaluation/results/_harness/analyze_holdout.py`.
- **Result file**: `evaluation/results/holdout_v2_review/analysis_holdout_v2.json`
- **Safe to use**: NO
- **Required caveat**: Also do not quote as CV numbers the agent-side defect counts (4/16 commute tasks answered without ever calling `calculate_commute`; 3/10 memory-write tasks that never called `remember`; 3 no-result turns that invented market rents) — they are a real DEFECT LIST worth citing in a write-up, but the denominators are far too small to be rates.

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
