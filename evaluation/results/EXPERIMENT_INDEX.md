# 实验总索引

_更新于 2026-08-06，HEAD `d74561b`。_

这份索引回答三个问题：**每个实验测了什么、跑在哪条架构上、结论能不能引用。**
可引用的措辞与 caveat 一律以 `CV_METRICS.md` 为准，本文件只做导航与状态。

## 怎么读这张表

- **架构**：`fc_loop` = `app/core/agent_loop.py::build_fc_graph`（**线上服务的那条**）；`legacy` = `app/core/langgraph_agent.py`。
  把 legacy 上测到的数字说成"当前架构"是这个项目最容易犯的错，每次引用前先看这一列。
- **阴性对照**：有没有验证过"两臂除了被操纵的变量之外确实一致"。**没有阴性对照的 A/B，配对前提是假设不是事实。**
- **CI**：单次运行没有置信区间。差距再大也只是观察，不是显著性结论。

---

## 一、可引用（详见 `CV_METRICS.md`）

| # | 实验 | 日期 | 架构 | n | 阴性对照 | 结论 | 产物 |
|---|---|---|---|---|---|---|---|
| 1 | **fc_loop vs legacy 架构对照** | 07-25 | 两条 | 98×1/臂 | — | **fc 胜**：通过率 34.7%→60.2%，路由 58.2%→80.6%，矛盾声明 1→0，可核验声明 152→280 | `.runtime/round-8793c0b-internal-2026-07-25/eval/{sweep,sweep-legacy}/` |
| 2 | **实验 A：分档模型路由 A/B** | 08-04 | fc_loop | 588 | — | **正**：成本 −67.7% [−70.4,−65.0]，e2e 均值 −5.5s [−7.4,−3.8] | `fc_loop_routing_ab/analysis_A.json` |
| 3 | **实验 D：维度 fan-out / 批次打包** | 08-05 | fc_loop | 192 | **3/3 通过** | **正**：覆盖率 54.8%→81.0% [+0.038,+0.441]，`llm_calls` −0.45，`tool_batches` −0.28 | `fanout_ab/{analysis_D,negative_control_D}.json` |
| 4 | 检索并发消融（legacy map-reduce） | 07-12 | legacy | 16×3/配置 | **通过 0/48** | **正**：检索阶段均值 −57.1%、p95 −42.0% | `ablation_retrieval.json` |
| 5 | held-out v6 结构化契约 | 08-05 | **legacy** | 180 | — | 正（限 legacy）：eligible recall 57/60，precision 87/90，通勤证据 28/30，记忆写入 30/30 | `holdout_v6_live/` |
| 6 | held-out v3 合规房源识别 | — | — | 33 | — | 正：33/33，CI 89.4–100% | `holdout_v3*/` |
| 7 | 模型盲审证据矛盾 | — | — | 110×3 轮 | — | 正：107/110、108/109、105/110 零矛盾，跨模型一致率 0.945 | `llm_blind_review/` |
| 8 | 全 thinking 档基线故障 | 08-04 | fc_loop | 50 | — | 机制发现：`E_multi_constraint` 7/11 轮内 HTTP 400 | `fc_loop_routing_ab/` |
| 9 | 故障注入 | — | — | 15 场景 | — | 正：暴露 15/15，幂等 3/3，降级 2/2 | `fault_summary.json` |
| 10 | 长期记忆存储确定性检查 | — | — | — | — | 正：隔离 5/5，遗忘 3/3，重启恢复 1/1 | `memory_eval.json` |

## 二、负结果与已终止（同样是资产，按方法论叙事引用）

| # | 实验 | 日期 | 架构 | 阴性对照 | 结论 |
|---|---|---|---|---|---|
| 11 | **实验 C：批内并发 vs 串行派发** | 08-04 | fc_loop | **未通过**（warm 16/60、cold 18/60） | **负**：两轮 CI 都跨 0。根因：244 个批次里只有 26.6% 装 ≥2 个调用，批内并发无发挥空间。**已被实验 D 取代** |
| 12 | fast path | — | fc_loop | — | **NO-GO，已终止**。不要从该分支 cherry-pick 产品代码 |
| 13 | correctness bundle | — | fc_loop | — | **NO-GO，已终止**。同上 |
| 14 | 旧模型路由数字（−52.7% / −38.4%） | 07-12 | legacy | — | **禁止引用**：强模型判定依赖已退役的 `deepseek-reasoner` 名字匹配，不可复现。用 #2 取代 |

## 三、缺陷清单（不是指标，不可换算成比率）

| 来源 | 架构 | 内容 | 状态 |
|---|---|---|---|
| held-out v2 §2B.7 | **fc_loop** | 6 条真实缺陷：①声称按通勤筛选却没调工具 4/16 ②记忆写入未持久化 3/10 ③无结果回合凭空补市场价 3 例 ④不合规房源列在"符合全部条件"下 3 例 ⑤给出被禁的 ÷4.33 口径 ⑥内部对账过程写进用户答案 | ①②③④ 已由契约加固修复（`962b2b5` / `1c4e42b` / `523745d`）；⑤ 已修；**修复后未在 fc_loop 上复测** |
| held-out v6 | legacy | 3 例结构化契约失败（HO6-198/208/238）：`is_loop_synthesis` 分支绕过契约挂载 | **已修** `d7a7702`，回归测试 `tests/test_holdout_v6_dimension_contract_mount.py` |
| held-out v6 | — | 13 例 task_completion 假阴性：冻结 marker 列表过窄 | 评测器缺陷，marker 已修（16 tests），**未重跑** |
| 实验 D 副产物 | fc_loop | fan-out 提出的 179 次添加有 36 次（20%）在 E7/E10 被 closed-fixture 拒绝 | **非生产 policy 缺陷**。原始拒绝均为 `fixture denied unbound tool`：评测 harness 保留完整 `list_specs()`，但为维持冻结证据边界会在 execute-time 拒绝 fixture 未绑定的 commute/POI 工具；生产 `_read_tool_denial` 仅管辖 `search_properties` / `web_search`，没有拒绝这些 fan-out 调用。后续指标应拆分 `policy_denied` 与 `fixture_unbound`，避免再次误归因。 |

## 四、已知的空缺

1. **fc_loop 上没有修复后的 held-out 复测。** 契约加固与 Phase 1 修复都只在 legacy（v6）或单测层面验证过。这是当前最大的证据空缺。
2. **#1 的 fc-vs-legacy 对照每例只跑 1 次，没有 CI。** 升级路径明确：每臂 3 次重复 + cluster bootstrap，harness `_harness/ab_runner.py` 现成。
3. **实验 D 只测到 plan-time 一半机制**，answer-time 的 `_completion_sweep_into_batch` 在 192 次运行中触发 0 次。
4. **线上跑的是 `uk-rent-agent:canary-fc-loop-0952c56`**，早于全部契约加固与 Phase 1 修复。合并 ≠ 部署。

## 五、复现须知（踩过的坑）

- **Python**：`/tmp/rentcompass-venv/bin/python`（3.12.3）。**本机没有 conda**。
- **凭证**：`app/.env` 的 `DEEPSEEK_API_KEY`。从别处的干净检出跑时该文件不在，会静默降级成占位 key 然后 HTTP 401——v6 首次运行就是这么中止的。**开批前先用一次廉价调用断言认证**。
- **测试**：`.runtime/logs/canary-*.jsonl` 是 root 属主（容器写的），不导 `CANARY_LOG_PATH` 会有约 29 个测试假红。当前基线 **29 failed / 3364 passed**，其中真实产品失败 **0** 个：22 个 canary/dsml（全量下的顺序污染，单独跑全绿）、3 个 POI 缓存脏、2 个测试间 env 泄漏（单独跑过）、1 个 `.env.bak` 在树里、1 个 monitor 安装漂移。
- **一手来源**：所有结论对着源码与原始 run 记录核，**不要对着摘要或更早的报告核**。引用按符号名，不要按行号。
- **禁 `git stash`**：`refs/stash` 在本机是跨 worktree 的单一全局栈，历史上因此丢过工作。
