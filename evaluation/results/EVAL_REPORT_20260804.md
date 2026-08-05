# RentCompass 评测报告 — 2026-08-04（无人值守批次）

> 任务书：`evaluation/results/GOAL_UNATTENDED_EVAL_20260804.md`
> 完整时间线：仓库根目录 `PROGRESS.log`
> 本报告中的每一个数字都给出来源文件路径；没有来源的数字不写。

---

## 0. 头部（运行环境与边界）

| 项 | 值 |
|---|---|
| `git rev-parse HEAD` | `0952c56e21b9b0dac3fb10fe99ee907c36b3a2d8` |
| 分支 | `telemetry-issue78` |
| `git status --short`（开始时） | `?? evaluation/results/GOAL_UNATTENDED_EVAL_20260804.md`（仅任务书未跟踪） |
| `git status --short`（结束时） | 只有新增，**0 处修改**：`?? evaluation/results/EVAL_REPORT_20260804.md`、`?? evaluation/results/GOAL_UNATTENDED_EVAL_20260804.md`、`?? evaluation/results/_harness/`、`?? evaluation/results/fc_loop_routing_ab/`、`?? evaluation/results/llm_blind_review/`、`?? evaluation/results/parallel_tools_ab/`（`PROGRESS.log` 与所有 `*.jsonl` 被 .gitignore 忽略，见 §6.3） |
| 开始时间 | 2026-08-04 17:21 BST（环境勘察）／17:40 BST（第一次计费请求） |
| 第二批次（held-out v2，§2B） | 2026-08-05 07:16 BST 冻结数据集 → 07:18 首次计费请求 → 08:20 BST 跑完；440 次请求、0 失败；HEAD 未变 |
| 结束时间 | 2026-08-04 19:19 BST（最后一次计费请求）／19:25 BST（报告落盘）。**远早于任务书的 2026-08-05 07:00 停止线** |
| 凭证泄漏检查 | 已用 `app/.env` 中 `DEEPSEEK_API_KEY` 的字面值全文检索 `PROGRESS.log` 与 `evaluation/results/**`，命中 0；另用 `sk-[A-Za-z0-9]{16,}` 泛化模式复查，命中 0 |
| 架构 | `AGENT_ARCH=fc_loop` → `core.langgraph_agent.build_agent_graph` → `core.agent_loop.build_fc_graph` |
| 模式 | **live**（真实 DeepSeek + 真实工具/网络），非 mock |
| 评测进程 | 由应用镜像 `uk-rent-agent:canary-fc-loop-0952c56`（= HEAD）起的**一次性容器**，仓库以 bind mount 挂载。生产容器 `uk-rent-app`(:5001) / `uk-rent-app-fc`(:5002) **未重启、未切池**，`deploy/switch_pool.sh` 未被调用 |
| 凭证 | 复用 `app/.env` 的 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`，经 `evaluation.run_benchmark._bootstrap_env` 载入；运行器在解析到占位 key 时直接 abort。报告与日志中不含任何 key 值 |

### 0.1 为什么没有用 `evaluation/run_ablation.py`

`run_ablation.py` 没有 `--arch` 参数（`evaluation/run_benchmark.py::build_arg_parser` 有，`run_ablation` 没有），因此它测的是 **legacy 图**，而任务书要求测当前的 `build_fc_graph`。本批次改用
`evaluation/results/_harness/ab_runner.py`——它 import 仓库自带的
`evaluation.run_benchmark.CaseRunner` 与 `evaluation.configs.loader`，不新建配置层、不复制 key、不修改 `app/**`。两处运行时改动都是**进程内 monkeypatch**：

1. `ModelRouter.create` + `collector.instrument_chat_model` 包装，把该次调用的 thinking 模式写进 `llm_call` 事件的 `purpose`（`<purpose>#think` / `#nothink`）；
2. 实验 C 的对照臂把 `core.agent_loop._TOOL_OFFLOAD_EXECUTOR` 换成 1 worker 的线程池。

### 0.2 固定价表（成本换算用，写死在报告里）

来源：`evaluation/model_pricing.yaml`（`price_source: https://api-docs.deepseek.com/quick_start/pricing`，`price_as_of: 2026-07-19`），单位 USD / 1,000,000 tokens：

| model | input（cache miss） | cached_input（cache hit） | output |
|---|---|---|---|
| `deepseek-v4-flash` | 0.14 | 0.0028 | 0.28 |
| `deepseek-v4-pro` | 0.435 | 0.003625 | 0.87 |

成本由 `evaluation/metrics/pricing.py::Pricing.cost` 按 token 数换算，cache-hit token 走 `cached_input` 价。

### 0.3 全批次请求与失败计数（明细见 §6.1）

| | 值 |
|---|---|
| 带模型的请求总数 | **1,037**（887 次 agent case-run + 150 次评审请求） |
| 失败 | **7**，全部是 all-thinking 探针臂的同一个 HTTP 400（见 §1.5）。三个正式实验 A / B / C **各自 0 失败、0 超时** |
| LLM 调用总数（agent 侧） | 1,914 |
| 计费成本（agent 侧，按 §0.2 价表） | **$0.7200** |
| 各实验用量 vs 任务书上限 | A 588/700，B 150/400，C 240/400（warm 120 + cold 120，见 §3.8） |

---

## 1. 实验 A：模型分档路由 A/B（当前 fc_loop 重跑）

### 1.1 设计（冻结于 2026-08-04 17:40，见 PROGRESS.log）

| 项 | 值 |
|---|---|
| 数据集 · split | `evaluation/benchmark/cases.jsonl`，**全部 98 例**（7 类：A_retrieval 14 / B_money 15 / C_commute 12 / D_crime_poi 13 / E_multi_constraint 11 / F_grounding 17 / G_memory 16） |
| 臂 A（对照 baseline） | **所有节点 `deepseek-v4-pro`**，thinking 关闭（进程内 `ModelRouter.route` 覆盖，保留每个 purpose 原本的 temperature / max_tokens） |
| 臂 B（实验） | `evaluation/configs/routed_models.yaml` = 当前生产路由，未打补丁 |
| 配对 | 是。同 case、同重建输入、同一份还原的 listing 缓存快照；每对内先跑哪一臂按 repeat 奇偶交替 |
| 运行次数 | 每臂 **3 次** → 98 × 3 × 2 = **588 run** |
| live / mock | **live** |
| 缓存协议 | WARM：`evaluation/benchmark/cache_snapshots/warm_v3.sqlite3`（sha256 与 sidecar 校验一致）**每个 run 前还原到独立命名空间** |
| 单请求上限 | 120 s（`asyncio.wait_for`）；请求间隔 ≥300 ms；外部 live 并发 = 2（两个 shard 进程） |
| 统计 | cluster bootstrap，**重采样单位 = case**（同 case 的 3 次重复相关），2000 次重采样，seed `20260804`，输出配对差值的 95% 百分位 CI |
| 结果文件 | `evaluation/results/fc_loop_routing_ab/shard{0,1}/runs.jsonl`（每 run 一行，含时间戳、缓存命中、工具失败）、`.../grader_input.jsonl`（冻结证据）、`.../analysis_A.json`、`.../table_A.md` |

### 1.2 关键定义修正：为什么"强模型调用数"不能按模型名数

`src/uk_rent_agent/llm/router.py::ModelRouter.__init__` 在本 commit 上解析出
`chat_model = deepseek-v4-flash`、`reasoner_model = deepseek-v4-flash`、`pro_model = deepseek-v4-pro`。
**chat 与 reasoner 是同一个模型**，两档之间只差 `extra_body {"thinking": enabled|disabled}`。
`evaluation/run_ablation.py::_is_strong` 是按模型名判定的，在本 commit 上会把两臂都算成 100% 强模型调用——该口径在这里没有意义。

因此本报告的"强模型调用"= **`deepseek-v4-pro` 调用数**，并单独报告 thinking 模式调用数。
实测两臂的 thinking 调用都是 **0**：生产 fc 循环的驱动模型由
`app/core/agent_loop.py:614` 以 `ModelRouter().create("responder", low_latency=True)` 构造，即**永远走非 thinking 档**。所以本次 A/B 是一次**纯模型强度对照**。

另有一类调用绕过 ModelRouter：`app/core/llm_interface.py` 直接用 OpenAI SDK 并把事件记为 `purpose="memory"`，不受任何一臂的路由改动影响（对照臂 2 次 / 实验臂 0 次，见指标表）。

### 1.3 第一次设计为何被替换（两次设计都保留）

第一次设计的对照臂是 `evaluation/configs/baseline_all_strong.yaml`（把每个 purpose 的
`reasoning` 强制为 True）。冒烟测试（5 例）发现它在本架构上**会在深工具循环里间歇性失败**：

```
E1#r1#baseline_all_strong -> HTTP 400
"The `reasoning_content` in the thinking mode must be passed back to the API."
```

原因：该配置实际只打开了 thinking 模式；DeepSeek 要求把上一条 assistant 消息的
`reasoning_content` 回传，而 `core.agent_loop` 的消息构造不携带它（生产从不需要——热路径是非 thinking）。修 `agent_loop` 属于任务书 §0 禁区。

于是对照臂改为「所有节点走更强的模型 `deepseek-v4-pro`、消息协议与生产一致」。
**第一次设计的结果不丢弃**，作为机制结论单列于 §1.5。

### 1.4 指标表

#### 实验 A 指标表

来源文件：`evaluation/results/fc_loop_routing_ab/analysis_A.json`（由 `evaluation/results/_harness/analyze.py` 生成，输入 runs.jsonl 见该文件 `source_files` 字段）

运行总数 588，成功 588，失败 0（失败分布 {'baseline_all_pro': 0, 'routed_models': 0}，原因 无）；配对上的 case 数 98 / 出现过的 case 数 98。

| 指标 | baseline_all_pro（所有节点 deepseek-v4-pro） (对照) | routed_models（当前生产路由） (实验) | 差值 test−base，bootstrap 95% CI | 判读 |
|---|---|---|---|---|
| LLM 调用总数 | 620 | 564 | -9.0% [-16.0%, -2.0%] | CI 不跨 0 |
| 其中 thinking 模式调用 | 0 | 0 | — | — |
| 其中 deepseek-v4-pro 调用 | 618 | 0 | — | — |
| 绕过 ModelRouter 的调用 (purpose=memory) | 2 | 0 | — | 不受本 A/B 的路由改动影响，见正文 |
| 输入 token | 6,023,163 | 5,321,430 | — | — |
| 输出 token | 117,980 | 114,479 | -3.0% [-13.5%, 7.9%] | 未观察到显著差异 (CI 跨 0) |
| 总 token | 6,141,143 | 5,435,909 | -11.5% [-19.9%, -3.1%] | CI 不跨 0 |
| 缓存命中 token (provider 侧) | 5,364,864 | 4,701,824 | — | — |
| 成本 USD（固定价表） | 0.4084 | 0.1320 | -67.7% [-70.4%, -65.0%] | CI 不跨 0 |
| 端到端 mean (ms) | 12,046 | 6,528 | -5,518 ms [-7,428 ms, -3,815 ms] | CI 不跨 0 |
| 端到端 p50 (ms) | 8,645 | 5,682 | -2,963 ms [-3,780 ms, -2,240 ms] | CI 不跨 0 |
| 端到端 p95 (ms) | 37,628 | 16,024 | -21,604 ms [-37,653 ms, -8,935 ms] | CI 不跨 0 |
| 检索阶段 mean (ms) | 2,148 | 891 | -1,257 ms [-2,380 ms, -369 ms] | CI 不跨 0 |
| 检索阶段 p50 (ms) | 3 | 3 | -0 ms [-1 ms, 1 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 p95 (ms) | 9,170 | 1,424 | -7,746 ms [-25,764 ms, -1,153 ms] | CI 不跨 0 |
| 检索阶段 mean（仅真正执行了工具批次的 run，n=223 / 219） | 2,832 | 1,196 | — | 见正文 |
| 检索阶段 p50（同上子集） | 7 | 7 | — | 见正文 |
| 检索阶段 p95（同上子集） | 14,057 | 3,193 | — | 见正文 |
| 证据支撑率 grounded/verifiable | 843/1095 (77.0%) | 812/965 (84.1%) | 7.16 pp [1.72 pp, 13.00 pp] | CI 不跨 0 |
| 金额支撑率 money_grounded/money_total | 489/655 (74.7%) | 447/560 (79.8%) | 5.16 pp [-1.06 pp, 11.53 pp] | 未观察到显著差异 (CI 跨 0) |
| 与证据矛盾的主张数 (contradicted) | 4 | 1 | — | — |
| 任务完成 task_completed | 294/294 | 294/294 | 0.00 pp [0.00 pp, 0.00 pp] | 未观察到显著差异 (CI 跨 0) |
| 约束全通过 passed | 227/294 | 228/294 | 0.34 pp [-6.46 pp, 7.14 pp] | 未观察到显著差异 (CI 跨 0) |
| 执行的工具调用数 / run（均值差） | 1.49 | 1.21 | -0.27 [-0.51, -0.07] | CI 不跨 0 |
| 监听缓存命中率 (listing cache) | 34/81 (42.0%) | 37/58 (63.8%) | — | — |
| 外部工具失败率 | 52/439 (11.8%) | 21/356 (5.9%) | — | — |

bootstrap：cluster bootstrap（重采样单位 = case），重采样次数 2000，seed 20260804，配对 case 数 98。

### 1.5 机制结论：all-thinking 基线在深工具循环上间歇性 400（14.0%，全部落在多约束类）

第一次设计的对照臂（`baseline_all_strong.yaml`，全节点 thinking）没有被丢弃，而是单独做了两次可行性探针，给这个失败一个真实分母。

| 批次 | 案例范围 | run 数 | 失败 | 来源文件 |
|---|---|---|---|---|
| 冒烟 | A1,B1,C1,E1,G1 | 5 | 1 | `evaluation/results/_smoke/runs.jsonl` |
| 探针 #1 | 98 例的 1/4 分片（25 例） | 25 | 0 | `evaluation/results/fc_loop_routing_ab/thinking_probe/runs.jsonl` |
| 探针 #2 | 实验 C 选中的 20 个「多工具批次」案例 | 20 | 6 | `evaluation/results/fc_loop_routing_ab/thinking_probe2/runs.jsonl` |
| **合计** | | **50** | **7（14.0%）** | |

7 次失败**全部**是同一个 HTTP 400（`The reasoning_content in the thinking mode must be passed back to the API`），且**全部集中在 `E_multi_constraint`**：

| 类别 | 失败/总数 |
|---|---|
| E_multi_constraint | **7 / 11 (63.6%)** |
| A_retrieval | 0 / 4 |
| B_money | 0 / 6 |
| C_commute | 0 / 10 |
| D_crime_poi | 0 / 7 |
| F_grounding | 0 / 7 |
| G_memory | 0 / 5 |

thinking 模式确实生效（探针 #1 的 30 次 `llm_call` 事件全部标记为 `responder#think`），所以这不是"补丁没打上"。

**这个结论要精确地写**：全 thinking 基线**不是完全不可运行**，它是**在深工具循环上间歇性失败**。之所以不能拿它做 588 次配对扫描，不是因为"跑不动"，而是因为这种掉线是**臂特异的、且集中在最难的一类案例上**——存活下来的配对会是一个有偏子集，配对设计就此失效。作为对照，第二次设计的 v4-pro 对照臂在 294 次运行里失败 0 次。

**这本身是一条产品结论**：`core.agent_loop` 的消息构造不携带 `reasoning_content`。今天生产从不触发（fc 热路径由 `agent_loop.py:614` 以 `low_latency=True` 构造，永远非 thinking），但只要有人给某个节点打开 thinking 档，多约束类请求就会有约 6 成概率在轮内 400。

### 1.6 限制

1. **缓存协议**：每个 run 从同一份 19 行的 warm 快照起步，所以端到端与检索阶段延迟是"快照相对"的，不代表冷启动真实耗时。两臂协议完全相同，对照本身不受影响。
2. **`task_completed` 指标饱和**：两臂各 294/294，该口径在本数据上不含信息量，不应作为质量证据。
3. **证据支撑率的混杂因素**：两臂的外部工具失败率不同（对照 52/439 = 11.8%，实验 21/356 = 5.9%），即两臂面对的证据集合并不完全相同；证据支撑率的差异不能干净地归因于模型档位。
4. **listing 缓存命中率**样本很小（对照 81 次、实验 58 次缓存查询），不适合单独引用。
5. 98 例基准全部参与过历史开发，本实验测的是**工程指标**（成本 / 延迟 / 调用量），这类指标不受"见过题"影响；但其中的 grounding 数字应按 §2 的口径解读。

### 1.7 结论

- **成本下降：支持。** 当前分档路由相对"所有节点用 v4-pro"降低成本 **67.7%**，bootstrap 95% CI `[-70.4%, -65.0%]`，不跨 0。
- **延迟下降：支持。** 端到端 mean 降低 **5,518 ms**（CI `[-7,428, -3,815]`），p50 降低 2,963 ms，p95 降低 21,604 ms，均不跨 0。
- **证据支撑率未下降：支持（方向为上升，但有混杂）。** 84.1% vs 77.0%，差 **+7.16 pp**，CI `[+1.72, +13.00]` 不跨 0；金额支撑率 +5.16 pp，CI `[-1.06, +11.53]` **跨 0 → 未观察到显著差异**。因限制 3，此处只宜写「未见质量下降」，不宜写「质量提升」。
- **旧的 −52.7% / −38.4%（2026-07-12，旧架构、单次运行）在当前架构上不可复现，也不应再被引用**：那两个数字的"强模型"口径依赖 `deepseek-reasoner` 这一已退役模型名。

---

## 2. 实验 B：可信推荐的独立模型盲审

> 🛑 **本节（第一次设计）状态：评测仪器已有部分可复用组件，但本次设计本身尚无可用于质量结论的 held-out 结果。**
>
> ⏩ **2026-08-05 更新：held-out 条件已在 §2B 被满足。** 第四次设计用 110 例全新编写、与调参 98 例逐字零重合的 held-out 集重跑了整条链路，门禁通过、预注册在先。**§2.1–§2.14 全部保留为历史记录，不被 §2B 覆盖、不与之合并。**
>
> **先读 §2.9。** 2026-08-05 的人工抽查认定：本节四个判定项里
> **只有 `contradicted_claim_count` 可用**；`hard_constraints_satisfied`、
> `claims_evidence_supported`、`directly_actionable` 三项受评测包构造与 rubric 缺陷影响，
> **不能作为质量结论对外引用**，需按 §2.11 重做。§2.3–§2.4 的原始判读分布与 κ 全部保留
> （它们是重做时的对照基线），但**读之前必须先看 §2.9 的失效说明**。

### 2.1 设计

| 项 | 值 |
|---|---|
| 被评材料 | 实验 A **routed（生产路由）臂**产出的答案，及其**同一次运行冻结的工具证据**（`evaluation/results/fc_loop_routing_ab/shard{0,1}/grader_input.jsonl` 的 `evidence` 字段） |
| 数据集 · split | `evaluation/benchmark/cases.jsonl` 的 **50/98** 分层抽样 |
| 抽样种子 | `20260804`（`evaluation/results/llm_blind_review/sample.json`） |
| 分层结果 | A_retrieval 7 / B_money 8 / C_commute 6 / D_crime_poi 7 / E_multi_constraint 5 / F_grounding 9 / G_memory 8 |
| 覆盖要求 | 通勤约束 6 例、多约束冲突 5 例、带多轮上下文（`conversation_history` 非空，含多轮重排）17 例——比例抽样已直接满足三个下限，**未做任何补抽**（`sample.json.floor_additions` 全为 0） |
| 盲化 | 评审 prompt 不含任何配置/系统/臂的标识；条目顺序按 seed 打乱，四个判定项的**提问顺序**也按 seed 逐条打乱；每轮带不同 nonce |
| 评审模型 | 第 1、2 轮 `deepseek-v4-flash`（thinking 关闭，temperature 0）；第 3 轮 `deepseek-v4-pro`（`ModelRouter.create("pro")`，thinking 开启，temperature 0） |
| 运行次数 | 每轮 50 条，三轮共 **150 次评审请求**，0 次失败 |
| live / mock | **live**（真实评审模型调用；被评答案与证据来自实验 A 的 live 运行） |
| 结果文件 | `evaluation/results/llm_blind_review/{sample,items,round1,round2,round3_pro,agreement}.json` |

三轮的种子（order_seed / criterion_seed / nonce）：R1 = 20260805 / 20260806 / `RC-B-R1`；
R2 = 20260881 / 20260903 / `RC-B-R2`；R3 = 20261359 / 20261581 / `RC-B-R3`。

### 2.2 「未参与调参」这一条件没有被满足——如实说明

任务书要求从**未参与调参**的请求中抽样。本仓库不存在这样的池子：98 例基准全部在历史开发过程中被用于诊断与迭代。可选的替代池（`.runtime/conversations.sqlite3` 里的真实用户会话）涉及真实用户数据，不适合进入可归档报告。

**决定（记录于 PROGRESS.log 17:40 设计冻结）**：照抽 98 例中的 50 例，并把这一点作为限制写明，而不是悄悄改写成"held-out 集"。抽中的 50 例里 **26 例来自 `cases_base45.jsonl`（最早的调参集）、24 例来自后加的 ext 分片**（`sample.json.base45_vs_ext53`）。
因此本节结论只能读作「**在一个模型见过的基准上，独立模型对答案与证据一致性的判读**」，不能读作泛化能力或held-out 准确率。

### 2.3 评审判读分布

来源：`evaluation/results/llm_blind_review/agreement.json` → `verdict_summary`、`round3_pro.json`。

| 判定项 | R1 flash | R2 flash | R3 pro |
|---|---|---|---|
| 硬约束满足 `hard_constraints_satisfied` | yes 10 / no 5 / unclear 35 | yes 12 / no 5 / unclear 33 | yes 48 / no 1 / unclear 1 |
| 数值·地点主张有证据支撑 `claims_evidence_supported` | yes 31 / partial 4 / no 15 | yes 30 / partial 5 / no 15 | yes 28 / partial 8 / no 14 |
| 与证据矛盾的主张数 `contradicted_claim_count` | 0×48 / 1×1 / 2×1 | 0×48 / 1×1 / 2×1 | 0×47 / 1×2 / 2×1 |
| 可直接执行 `directly_actionable` | yes 36 / no 14 | yes 39 / no 11 | yes 37 / no 13 |

### 2.4 一致性（**这是同一模型的自一致性，不是评审者间信度**）

来源：`evaluation/results/llm_blind_review/agreement.json`。

**(a) 同模型两轮自一致性（deepseek-v4-flash，两轮独立评审，n=50）**

| 判定项 | 观察一致率 | Cohen's κ |
|---|---|---|
| hard_constraints_satisfied | 0.880 | **0.750** |
| claims_evidence_supported | 0.820 | **0.660** |
| contradicted_claim_count | 1.000 | **1.000** |
| directly_actionable | 0.900 | **0.735** |

**(b) 跨模型一致率（R1 `deepseek-v4-flash` vs R3 `deepseek-v4-pro`，n=50）**

| 判定项 | 观察一致率 | Cohen's κ |
|---|---|---|
| hard_constraints_satisfied | 0.240 | **0.040** |
| claims_evidence_supported | 0.740 | **0.532** |
| contradicted_claim_count | 0.980 | **0.793** |
| directly_actionable | 0.740 | **0.341** |

### 2.5 最重要的结论：同模型 κ 严重高估了这个判读的可靠性

硬约束这一项，同模型两轮 κ=0.750（"substantial"），换一个模型后 κ=0.040——**接近随机**。
而且分歧是**系统性的、不是噪声**：flash 在 50 例里给出 35 次 `unclear`，pro 给出 48 次 `yes`。
两者看的是同一份请求、同一份证据、同一段答案。

**把交叉表拉出来之后，κ=0.040 的成因是可以定位的，必须一并写清楚**
（来源：`round1.json` × `round3_pro.json` 逐条对照）：

| flash ＼ pro | yes | no | unclear |
|---|---|---|---|
| **yes** | 10 | 0 | 0 |
| **no** | 4 | 1 | 0 |
| **unclear** | **34** | 0 | 1 |

分歧几乎全部落在一格：**flash 判 `unclear` 的 35 例里有 34 例被 pro 判成 `yes`**。
读这两个模型给出的一句依据会发现，它们对**事实**并不分歧，分歧在**标签映射**。例如 item_049：

> flash：`The user did not state any hard constraints in this turn, and the assistant correctly notes it has no saved budget or preferences.` → 判 `unclear`
> pro：`No hard constraints were stated by the user, so the answer satisfies them.` → 判 `yes`

两句话说的是同一件事——**用户这一轮没有提出硬约束**——但评审 prompt 没有规定「没有硬约束时该判什么」，
于是一个模型选了"无从判断"，另一个选了"空条件默认满足"。**这是评审 rubric 的缺陷，不是被评系统的缺陷，也不完全是模型判读能力的差异。**

因此本项的准确结论是三条，不是两条：

* κ=0.040 **夸大**了真实的判读分歧（主因是一个未定义的边界情形）；
* 但同模型 κ=0.750 **同样不能**用来声称这一项可靠——它只是稳定地复现了同一个未定义选择；
* 真正实质的分歧只有 flash 判 `no` 的那 5 例：pro 只在其中 1 例同意。n=5，**不足以下任何结论**。

无论哪种读法，`hard_constraints_satisfied` 这一项在本轮**都不可用于对外表述**；
修法是给 rubric 加一条「本轮无硬约束 → 判 `not_applicable`」并重跑，本批次未做（列入次日待办）。

含义有两条，都必须写进任何对外表述：

1. **同模型两轮 κ 不能当作"评审可靠"的证据**。它测的是同一个模型的稳定性（temperature 0 下本来就该很稳），不是判读的正确性。
2. **`contradicted_claim_count` 是三轮里唯一稳健的判定项**（同模型 κ=1.000，跨模型 κ=0.793，48/50 与 47/50 都判 0 矛盾）。它也正好是四项里定义最客观的一项。相反 `hard_constraints_satisfied` 与 `directly_actionable` 属于**judgement-heavy** 项，跨模型不稳，不应作为指标对外引用。

### 2.6 与旧口径 152/204 的关系

旧的 152/204 是启发式文本标记评分，不是答案正确率；本实验也**不是**答案正确率。
本实验产出的是「独立模型对**答案—证据一致性**的判读分布 + 其跨模型稳定性」，两者口径不同、不可换算、不可相互替代。

### 2.7 限制

1. **未参与调参这一条件未满足**（§2.2）。
2. **50 例中有 3 例的工具证据在送审前被截断到 12,000 字符**（`items.json.evidence_truncated`；证据中位数长度 616 字符，所以这是长尾）。截断的条目在 prompt 里带有明确的截断提示，但它们的 `claims_evidence_supported` 判读仍可能偏严。
3. **第三轮是同一家厂商的另一个模型**（DeepSeek v4-pro），不是真正独立的第三方；跨模型一致率因此可能仍被高估。
4. **所有模型判读一律标注「待人工校准」**。本批次不等待人工标注。
5. 大量 `unclear` 与许多"无房源"的真实答案有关：抽样期内 Camden 等区在给定预算带确实没有房源（PROGRESS.log 17:32 已用直连抓取核实抓取链路健康），答案因此诚实地说"没有匹配"，而"硬约束是否满足"在没有候选房源时本就难判。

### 2.8 人工校准集

`evaluation/results/llm_blind_review/human_calibration_sheet.md`：从 50 例中按 seed `20265046` 抽出的 **15 例**，每例含请求、（截断到 8,000 字符的）工具证据、系统答案，以及四个判定项的空表格。**本批次不等待人工标注**；人工标注完成后可与 `round1.json` / `round3_pro.json` 交叉核对，那才是这四个判定项第一次拿到真正的参照系。

### 2.9 人工抽查（2026-08-05）：本节的三项指标不能作为质量结论

报告写完后做了一次人工抽查，结论是 **实验 B 的"低分"不能直接归因于 agent**：
评测包构造与盲审 rubric 有明显缺陷，**至少需要按 §2.10 重做后**才能作为质量结论。
以下每一条都在数据里复核过（复核脚本的输出记录在 PROGRESS.log 2026-08-05 条目）。

> 计数口径说明：下表引用的是 **round3_pro** 的判读（`claims_evidence_supported` no=14、
> `directly_actionable` no=13）。round1 对应为 15 与 14。两者都在 `round1.json` /
> `round3_pro.json` 里，差异只是轮次不同，不是重新计过。

| # | 问题 | 证据（已复核） | 影响 |
|---|---|---|---|
| 1 | **硬约束标签未定义** | flash 判 35/50 `unclear`，pro 判 48/50 `yes`；34 个分歧只是"本轮无硬约束"该怎么标（§2.5 交叉表）。50 例中真正声明了硬约束的只有 **6 例** | κ=0.040 测的是 rubric 空白，不是 agent 质量 |
| 2 | **证据包不含用户上下文 / 计算依据** | **16/50 条目的工具证据为空**（`A13,A8,B1,B14,B2,B3,B7,B8,C3,C8,D3,F12,F2,G11,G2,G5`）。其中 **12 例的正确行为本就不产生工具证据**：9 例 `expected_tools=[]`（`B1,B2,B3,B7,B8,B14,F2,G11,G5`）+ 3 例澄清/记忆类（`A13,A8,G2`，后者 case notes 明写"empty trace is contract-legal HERE"）。B1/B2/B3/B7/B8/B14 是**纯数学题，答案正确却被判"无证据支撑"** | `claims_evidence_supported` 的 14 个 `no` 中，至少 9–10 个是评测包构造问题或混合问题 |
| 3 | **"可直接执行"对不少任务不适用** | 13 个 `no` 里包含 `A13`（澄清）、`G1`/`G11`（记忆）等——它们的正确行为本就是澄清、记忆或处理冲突，却因"没给房源"被扣分 | 把**正确地不推荐**当成低质量 |
| 4 | **证据截断** | `A4`、`B13`、`E5` 三例证据被截到 12,000 字符，却仍被要求判断"所有事实是否有依据" | 可能造成假阴性；至少应有 `cannot_assess` 标签 |
| 5 | **样本不独立** | 50 例全部来自参与过历史调参的 98 例基准，其中 26 例来自最早的 `base45`（§2.2 已披露） | 不能声称泛化表现或独立质量评估 |
| 6 | **任务类型混杂** | 抽样含 8 例纯计算、8 例记忆类、多例拒答/澄清，并非统一的"可信推荐" | 一个统一的 actionability 指标**没有语义一致性** |

**因此本节的可用性判定改为：**

| 判定项 | 判定 |
|---|---|
| `contradicted_claim_count` | **可用，但要用观察一致率报，不要用 κ** —— 第一次设计 48/50、48/50、47/50 判 0 矛盾；第三次设计 49/50、48/50、47/50，跨模型观察一致率 0.940。κ 在这种 ~96% 单类别的边际下会退化（§2.12.3），不可作为稳健性证据 |
| `hard_constraints_satisfied` | **不可用** —— rubric 空白（问题 1），且真正有硬约束的只有 6 例 |
| `claims_evidence_supported` | **不可用** —— 证据包构造缺陷（问题 2、4） |
| `directly_actionable` | **不可用** —— 指标与任务类型不匹配（问题 3、6） |

### 2.10 但确有 agent 侧的真实缺陷（人工抽查认定，与上面的评测缺陷分开记）

这些**不是**评测构造问题，是答案本身与证据冲突或凭空补足，应当进入产品修复清单：

| case | 问题 |
|---|---|
| **C8** | 工具明确返回"Manchester 不在 TfL 覆盖内、无票价"，回答仍编造 Metrolink 票价、站点与 15–20 分钟步行时间。case notes 里写明正确行为是如实说明 |
| **E4** | 用户要 3-bed house，工具证据显示实际检索成了 studio，回答却称自己搜了 3-bed |
| **E5** | 声称 24 分钟通勤、54/100 安全评分和 Sainsbury's，但证据是 22 分钟，且没有安全/POI 数据 |
| **B13** | 说 £2,000 是最高价，证据里存在 £2,200 与 £2,350 的一居室 |
| **A2 / A3 / A10 / F6** | 在无结果或证据不完整时，补入未被支撑的市场价格、区域/房型判断 |

**注意 C8 的双重身份**：它同时出现在"证据为空的 16 例"里（因为工具诚实地返回了空），也出现在这张真实缺陷表里——
**证据为空正是它应该说"我查不到"的理由，而它却编了数字。**
所以"证据为空"不等于"该条判读无效"，逐条区分是必须的，这也是问题 2 只说"至少 9–10 个"而不是"全部 14 个"的原因。

### 2.11 重做要求（未做，列入次日待办）

1. 把样本拆成 **检索推荐 / 计算 / 记忆 / 拒答·澄清** 四类，**分开计分，不混算**。
2. 盲审输入补齐：当前请求、**完整历史**、**记忆读写结果**、**允许的派生计算**、工具证据。
3. 增加 `not_applicable`（无硬约束 / 正确澄清 / 纯计算）与 `cannot_assess`（证据被截断）两个标签。
4. 对"建议步行时间"这类**派生量预先规定公式或明令禁止推导**（金额公式见 `evaluation/benchmark/README.md`）。
5. 使用**未参与开发的固定 held-out 集**，并做人工校准。
   ⚠️ **第 5 条在本仓库当前无法满足**（§2.2）：不存在未参与调参的请求池。这意味着即使 1–4 全部实现，
   重做后的结果**仍然不能**作为泛化能力或独立质量评估引用——需要先构造 held-out 集。

---

### 2.12 第三次设计：rubric / evidence-packet 口径验证（2026-08-05）

> **这一节是工程验证，不是质量重评，也不是泛化评估。**
> 重做要求的第 5 条（未参与开发的 held-out 集）**未满足**，因此：
> - 本节的任何质量比例**禁止进入 CV 与 `fact-ledger.md`**；
> - **不得**与 §2.3–§2.4（第一次设计）的分布、κ 或任何比例**合并**；
> - 只能称为「**已调参基准上的口径验证**」，不得称为独立质量评估或泛化结果。
>
> 目的只有一个：把修好的测量工具跑一遍，看新口径是否稳定、把 harness 固化下来，
> 等真正的 held-out 集建好后原样复用。

**设计冻结在发出任何请求之前**（PROGRESS.log 2026-08-05 04:30），四条修复：
任务分四类分开计分 / 证据包补齐（完整历史 + reconstructed_context + reference_calculations +
记忆读写结果 + 已执行工具列表）/ 新增 `not_applicable` 与 `cannot_assess` /
派生量规则前置（金额公式逐字给出，其余一律禁止推导）。

样本**未重抽**：与第一次设计同样的 50 例、同样的 seed 20260804。
任务类别（冻结规则，来自 `cases.jsonl`）：retrieval 32 / calculation 7 / memory 7 / clarify 4。
三轮共 150 次请求，0 失败，**0 条越出词表的回答**（说明 rubric 被严格遵守）。
结果目录：`evaluation/results/llm_blind_review_v3design_rubric_validation/`。

#### 2.12.1 口径修复的效果（这是**仪器**指标，不是产品指标）

跨模型一致性（flash vs pro，n=50），**只用于判断量表是否可用**：

| 判定项 | 旧口径 obs / κ | 新口径 obs / κ | 读法 |
|---|---|---|---|
| `hard_constraints_satisfied` | 0.240 / **0.040** | 0.920 / **0.735** | ✅ 修复有效，诊断被证实 |
| `directly_actionable` | 0.740 / 0.341 | 0.760 / **0.546** | ✅ 有改善 |
| `claims_evidence_supported` | 0.740 / 0.532 | 0.640 / **0.427** | ❌ **变差** |
| `contradicted_claim_count` | 0.980 / 0.793 | 0.940 / **0.239** | ⚠️ 见 §2.12.3，κ 在这里不可用 |

同模型两轮自一致性（新口径）：0.868 / 0.766 / 0.660 / 0.750（obs 0.960 / 0.880 / 0.980 / 0.860）。

#### 2.12.2 证据包修复确实消掉了假阴性——但也暴露了真实样本量

新口径下 `not_applicable` 吸收了大量本来就不该判的条目，于是**每一项真正可判的 n 显著缩小**：

| 判定项 | 真正可判的条目（round1） | 落在哪些任务类别 |
|---|---|---|
| `hard_constraints_satisfied` | **8 / 50** | retrieval 7、memory 1 |
| `claims_evidence_supported` | 39 / 50 | retrieval 28、calculation 7、memory 3、clarify 1 |
| `directly_actionable` | 33 / 50 | retrieval 29、memory 3、clarify 1 |
| `contradicted_claim_count` | 50 / 50 | 全部 |

**这本身就是最重要的产出**：`hard_constraints_satisfied` 在这个抽样上**只有 8 例可判**。
无论 rubric 修得多干净，这一项在 n=8 上都不可能支撑任何对外结论——
问题不在 rubric，在**抽样里根本没有足够多带硬约束的请求**。重做 held-out 集时必须按这一项的
可判性来配额，否则修好 rubric 也是白修。

#### 2.12.3 一条必须写下的更正：κ 不适用于 `contradicted_claim_count`

第一次设计里我把 κ=0.793 当作「这一项稳健」的证据，**那个推理是错的**，
本轮的数据把它暴露了出来：

```
跨模型： 观察一致 0.940，但期望一致已达 0.921
        κ = (0.940 − 0.921) / (1 − 0.921) = 0.239，分母只剩 0.079
        边际 A = {0: 49, 3: 1}   B = {0: 47, 1: 1, 2: 1, cannot_assess: 1}
```

当 ~96% 的判读都落在同一个类别时，期望一致率本身就接近 1，**κ 的分母趋于 0、数值极不稳定**
（Cohen's κ 的已知退化情形）。第一次设计的 κ=0.793 与本轮的 κ=0.239 之间的差距，
主要来自边际分布的微小变化，**不是这一项变得不可靠**。

因此这一项的正确报告方式是**观察一致率 + 原始计数**，不是 κ：

| | round1 | round2 | round3(pro) |
|---|---|---|---|
| 判为 0 处矛盾 | 49/50 | 48/50 | 47/50 |
| 跨模型观察一致率 | — | — | **0.940** |

§2.9 的可用性表与 §4 的 ledger 行 B-3 已按此更正。

#### 2.12.4 仍未解决的问题

1. **`claims_evidence_supported` 跨模型反而更差了（κ 0.532 → 0.427）**。分布显示原因：
   pro 大量使用 `partial`（14 次，flash 只有 4 次），两个模型在 yes / partial 的边界上不一致。
   新增的标签并没有定义「部分支撑」到什么程度算 partial。**这一项仍不可用。**
2. **非检索类的 `directly_actionable` 仍不稳**：分类别跨模型 κ 在 memory 上是 −0.167、
   clarify 上是 −0.333（n 分别为 7 和 4）。两个模型在「该用 `not_applicable` 还是 `yes`」上
   分歧。**这一项在非检索类上仍不可用**；retrieval 类 κ=0.579，勉强可读但不足以对外引用。
3. **第 5 条（held-out 集）未满足**，所以以上全部只是仪器读数。

#### 2.12.5 本轮结论

- **修好了的**：`hard_constraints_satisfied` 的口径（κ 0.040 → 0.735），以及证据包的假阴性
  （纯计算、澄清、记忆类不再因为"没有工具证据"被判无依据）。
- **修坏了 / 没修好的**：`claims_evidence_supported` 需要定义 partial 的判据；
  `directly_actionable` 需要为非检索类单独定义"完成了该完成的事"。
- **意外收获**：这个抽样对 `hard_constraints_satisfied` 只有 **8 例**可判——
  held-out 集必须按可判性配额来构造。
- **仪器已固化**：`evaluation/results/_harness/blind_review_v2.py` + 冻结的四条规则，
  held-out 集就绪后可原样复用，不需要再改。
- **本节不产生任何可引用的质量数字。**

---

### 2.13 held-out 集的配额规范（按「每项可判」配额，不按七大类别分层）

第三次设计给出的可判率矩阵（round1，新口径）——**这是配额的依据**：

| 任务类别 | n | `hard_constraints` | `claims_supported` | `directly_actionable` | `contradicted` |
|---|---|---|---|---|---|
| retrieval | 32 | **7/32 (22%)** | 28/32 (88%) | 29/32 (91%) | 32/32 |
| calculation | 7 | 0/7 | 7/7 | 0/7 | 7/7 |
| memory | 7 | 1/7 | 3/7 | 3/7 | 7/7 |
| clarify | 4 | 0/4 | 1/4 | 1/4 | 4/4 |
| **合计** | **50** | **8/50 (16%)** | 39/50 (78%) | 33/50 (66%) | 50/50 (100%) |

**照搬本抽样构成会付出的代价**，按上面的可判率外推到「每项 ≥30 例可判」：

| 判定项 | 可判率 | 需要出题 |
|---|---|---|
| `hard_constraints_satisfied` | 16.0% | **约 188 例** |
| `directly_actionable` | 66.0% | 约 45 例 |
| `claims_evidence_supported` | 78.0% | 约 38 例 |
| `contradicted_claim_count` | 100% | 约 30 例 |

即：**只要继续按七大类别分层，`hard_constraints_satisfied` 就需要近 190 道题才能凑够 n=30**——
这正是本轮 n=8 问题的复现机制。

**可判性在出题时就能预测。** 用 `cases.jsonl` 的静态属性（`expected_constraints` 是否含
预算上限 / 卧室数 / 区域 / 入住日期 / 通勤上限）去预测「`hard_constraints` 是否可判」，
与实际判读一致 **43/50 (86%)**（按 §2.13.2 冻结枚举重算；此前写的 44/50 是用错误集合算的）。
⚠️ **这 88% 只是规划验证，不是保证**——它是在旧样本上量出来的一致率。真正的保证来自 §2.13.1 的
preflight 门禁：出题后逐题静态校验 + 人工抽查，不合格的题**替换**，而不是等跑完用 `not_applicable` 消化。

#### 建议配额（目标：每项 ≥30 例可判；总量约 110 例）

| 分层 | 例数 | 该分层必须满足的属性 |
|---|---|---|
| retrieval **含显式硬约束** | **35** | 请求中明确写出至少一项用户住房条件，**且以 `hard_constraints.py::MACHINE_CHECKABLE` 里的类型表达**（当前为 `all_results_satisfy` / `room_type_match` / `commute_leq_minutes`）。地点、入住日期、物业特征需先补类型，见 §2.13.2 |
| retrieval 不含硬约束 | 20 | 覆盖「无硬约束」路径本身 |
| calculation | 20 | 纯计算，`expected_tools=[]`，带 `reference_calculations` |
| memory | 20 | 记忆读 / 记忆写各半 |
| clarify | 15 | 信息不足 / 超出覆盖范围各半 |
| **合计** | **110** | |

按本轮观测率外推的可判量：`hard_constraints` ≈ 31、`claims_supported` ≈ 82、
`directly_actionable` ≈ 63、`contradicted` = 110。**四项同时过 30。**

> ⚠️ 上表的可判率来自 50 例样本，本身有抽样误差，外推只作规划用；
> 真正的保证来自「出题时按属性配额」这一条，而不是外推数字。

#### 抽样前必须一并固定的三件事（否则本轮的问题会换个形式复现）

1. **配额**（上表），写进设计冻结；
2. **标签词表**：`yes / no / partial / not_applicable / cannot_assess`，
   并补上 §2.12.4 缺的两条定义——`claims_evidence_supported` 的 **partial 判据**、
   非检索类任务的**「完成了该完成的事」定义**；
3. **完成定义**：每一类任务「正确行为」是什么，在出题时就与 case 一起写死，
   而不是让评审模型自己推断。

#### 2.13.1 执行门槛：出题完成后、发起任何模型请求之前必须过的 preflight

第三次设计里那条 **44/50 (88%) 的静态-实判一致率只能作为规划验证，不能当保证**。
要让「每项 ≥30 可判」成为**设计保证**而不是对旧样本比例的外推，held-out 出题完成后必须先过门禁：

**静态一半（已实现，可执行）**：`evaluation/results/_harness/holdout_preflight.py`
退出码 0 = 通过、1 = 有题未通过。四项检查：

| 代号 | 检查 | 静态能查到什么 |
|---|---|---|
| **Q** | 配额 | 每个分层的题数是否达标；并额外算「替换掉未通过题之后是否仍达标」 |
| **H** | 硬约束**明确 / 可验证 / 互不矛盾** | H1 计入 hard 配额却无可核验硬约束；H2 约束缺参数（无法机器核验）；H3 约束数值未出现在请求/历史文本里（不算明确声明）；H4 同字段上下界冲突或精确值互斥。判定用的类型集合来自 `hard_constraints.py`（§2.13.2） |
| **C** | 已写明该任务的**正确完成态** | 是否存在非空的 `correct_completion` / `expected_behaviour` 字段 |
| **E** | 每条可评主张都有**依据来源** | E1 无用户上下文；E2 纯计算类缺 `reference_calculations`；E3 声明了 `expected_tools` 却无 `fixture`（工具证据未冻结，跨轮不可复现） |
| **Q3** | 词表审计 | 任何**未归类**的 constraint type 直接判失败——「没被排除」和「忘了考虑」在只有 include-list 时分不清 |

**人工一半（清单已可生成）**：同一脚本用 `--checklist` 输出抽查表，逐题人工确认
「硬约束明确可验证且不矛盾 / 已写明正确完成态 / 可评主张都有依据来源」。

**处置规则（写死）**：**未通过者替换，不允许在跑完之后用 `not_applicable` 消化。**
脚本的 `residual_after_replacement` 字段专门算这一点——替换掉不合格题后各分层是否仍够配额，
不够就得继续出题，而不是降低配额。

**在当前 98 例基准上的冒烟运行**（证明门禁会拦、不是空跑）：

```
gate_passed: false        退出码 1
分层：retrieval_hard 12 / retrieval_soft 51 / calculation 13 / memory 15 / clarify 7
配额缺口：retrieval_hard 12/35、calculation 13/20、memory 15/20、clarify 7/15
未通过：98/98    问题分布：C1 98（无 correct_completion 字段）、E3 20（无 fixture）、H3 1
词表审计：user_hard 3 类 / excluded_instrument 21 类 / UNCLASSIFIED 0
缺类型的语义槽位：area、move_in_date、property_feature
```

即：**现有 98 例基准一题都过不了这道门禁**——`correct_completion` 是 held-out 出题时才引入的
新字段，20 例声明了工具却没冻结证据。

> **这句话的边界要说准**：98/98 未通过只证明「**现有基准不满足新版 held-out 门禁的元数据与
> 证据冻结要求**」，它**本身不能证明「不能作为 held-out 集」**——后者的决定性理由始终是
> **这些题已经参与过调参**（§2.2）。门禁查的是元数据完备性，查不了污染；
> 一道题即使补齐了 `correct_completion` 和 fixture 从而通过门禁，只要它参与过开发，
> 就仍然不能进 held-out 集。

#### 2.13.2 一处必须更正的口径错误（2026-08-05，owner ruling 后重查）

我原先写「preflight 的硬约束集合比 §2.13 推导时用的更宽，所以 10 vs 6」。**这个诊断是错的**，
而且掩盖了一个更严重的问题：两处用的其实是**同一个集合**，而那个集合里
**8 个类型名有 6 个在 `cases.jsonl` 里根本不存在**（`all_results_within_budget`、`max_budget`、
`budget_ceiling`、`area_match`、`move_in_date`、`commute_within` —— 是我凭印象编的），
同时**漏掉了真实存在的 `commute_leq_minutes`**（7 次使用，是通勤上限这一语义槽位的唯一载体）。
「10 vs 6」的真实成因只是 **98 例总体 vs 50 例抽样**，与定义宽窄无关。

按 owner 冻结的语义标准（只计「用户提出的、可被答案满足/违反的住房条件」，
排除 `must_call_tool`／来源要求／`no_fabricated_number`／注入防护／记忆隔离等仪器条件）
重建枚举后，**三处已引用同一份冻结定义**：`evaluation/results/_harness/hard_constraints.py`
（配额推导、`holdout_preflight.py`、`blind_review_v2.py` 的 judge prompt 都从它取）。

重算结果（不是改数字，是按新枚举重跑）：

| 项 | 旧（错误集合） | 新（冻结枚举） |
|---|---|---|
| 可机器核验的用户硬约束类型 | 名义 8 个，实际只有 2 个能命中 | **3 个**：`all_results_satisfy`(field=monthly_rent)、`room_type_match`、`commute_leq_minutes` |
| 98 例中带用户硬约束 | 13 | **15** |
| 其中 task_class=retrieval（= `retrieval_hard` 分层） | 10 | **12** |
| 50 例抽样中带用户硬约束 | 6 | **7** |
| 静态属性与实判可判性的一致率 | 44/50 (88%) | **43/50 (86%)** |

**词表审计**（preflight 现在每次都输出）：24 个 constraint type 全部归类，
`UNCLASSIFIED` 为空 —— 3 个属于用户硬约束、21 个属于仪器条件。
`result_count` 归入仪器条件：它 6 次使用里有 5 次是 `== 0`，那是对世界/fixture 状态的断言，
不是答案能去满足或违反的用户住房条件。

**⚠️ 由此暴露的词表缺口（held-out 出题前必须补）**：owner 语义标准展开为 **7 个槽位**
（budget / bedroom_count / room_type / commute / area / move_in_date / property_feature），
当前词表覆盖 4 个。**地点/区域、入住日期、明确物业特征这三类硬约束在现有词表里
没有任何可机器核验的类型**，只能写在散文里——既进不了 hard 配额，也无法被确定性 grader 核验。
（`bedroom_count` 经核实**是**可核验的，但与 `room_type` 挤在同一个重载类型里，见 §2.13.3。）
held-out 出题时必须先新增 `area_match`、`move_in_date_satisfied`、`property_feature_present`，
并把 `bedroom_count_match` 从 `room_type_match` 拆出，否则 35 例 hard 配额只能靠
预算/房型/通勤填满，覆盖面是结构性残缺的。

#### 2.13.3 `room_type_match` 核实结论：覆盖卧室数，但是**重载**的

去查了 `evaluation/metrics/graders.py::_listing_room_type_ok` 与 `_room_type_in_text`。
结论：**`room_type_match` 确实核验卧室数**，不属于「缺类型」——

```python
m = re.match(r"(\d+)", value)          # "2-bed" -> n=2
if m and isinstance(beds, (int, float)):
    return int(beds) == n              # 对每一条 listing 精确比对 bedrooms
```

现有 9 次使用里：`studio` 4 次、`1-bed` 3 次、`2-bed` 1 次、`shared/room` 1 次
—— 即 **5 次承载卧室数、4 次承载房型标签，同一个类型名两种语义**。

由此产生三个必须在 schema 扩展时解决的问题：

| # | 问题 | 后果 |
|---|---|---|
| 1 | **重载**：一个类型名承载「房型标签」与「卧室数」两种语义，靠 `re.match(r"\d+")` 分流 | **按槽位配额无法直接统计**——必须解析 value 字符串才知道一道题覆盖的是哪个槽位 |
| 2 | **只支持精确相等**：`int(beds) == n`，没有 `op` | 「至少 2 间」「2–3 间」这类真实用户表达**无法编码** |
| 3 | 无 listing 时退化为**文本标记启发式**（`heuristic=True`），只检查答案里有没有出现 "2-bed" | 那是「有没有提到」，不是「有没有满足」；无结果分支的通过判据与有结果分支不同源 |

**处置**：held-out schema 扩展时把它拆成
`room_type_match`（仅标签：studio / shared / flat / house…）与
**`bedroom_count_match`（带 `op`，支持 `==` / `>=` / 区间）**。
在拆分落地之前，`hard_constraints.py::slot_of()` 先靠解析 value 把两个槽位分开，
让按槽位配额现在就能统计——这是权宜，不是终态。

#### 2.13.4 schema 版本化扩展规范（出 held-out 题之前先做）

**顺序是硬的：先版本化扩展约束 schema，再出题。** 每个新类型至少冻结五件事：

| 冻结项 | 含义 |
|---|---|
| 1. **用户文本规范化** | 用户怎么说 → 规范化成什么值（"Zone 2"、"N1"、"Camden" 各自归一到什么） |
| 2. **fixture 中可核验的证据字段** | 该约束要比对 listing 的哪个字段；字段不存在时该类型不得使用 |
| 3. **确定性通过/失败谓词** | 不含启发式、不含文本标记回退；与「有无结果」无关地同源 |
| 4. **无结果 / 未知 / 部分匹配时的预期完成态** | 三种情形各自的正确行为，写进 case 的 `correct_completion` |
| 5. **judge packet 里哪些事实可作为支撑证据** | 评审模型允许拿哪些字段当依据，避免从房源描述自由推断 |

需要新增的四个类型及其**特别注意事项**：

| 新类型 | 特别注意 |
|---|---|
| **`area_match`** | 必须定义**匹配边界**：行政区（Camden ⊂ London？）、邻近区域（"near King's Cross" 算不算 Islington）、邮编（N1 vs N1C 前缀匹配到第几位）。这三种边界不定义清楚，这个类型就是新的争议来源 |
| **`move_in_date_satisfied`** | 「可入住日期 ≤ 用户日期」还是「区间重叠」；listing 的 `Available From` 常常是 "Contact agent" —— 该值必须映射到 **unknown**，走「未知」的完成态，不得当作失败 |
| **`property_feature_present`** | 必须**限定允许的特征词表**（furnished / pet-friendly / garden / parking …）与**对应的证据字段**；**明令禁止**从房源自由文本描述里推断特征——否则又回到「听起来合理就放行」 |
| **`bedroom_count_match`** | 从 `room_type_match` 拆出，带 `op`（`==` / `>=` / 区间），谓词只比对 `listing.bedrooms` |

#### 2.13.5 按语义槽位的最低覆盖配额（已实现并生效）

只卡 `retrieval_hard` 总量 35 会出现「hard 总数够了，但地点/日期/特征完全没有分母」。
因此在总量之外**再冻结每个槽位的最低覆盖数**，**允许同一题覆盖多个槽位**
（所以各槽位下限之和可以大于 35）：

| 语义槽位 | 最低覆盖 | 现有 98 例基准的覆盖 |
|---|---|---|
| budget | 15 | 13 |
| bedroom_count | 12 | 4 |
| room_type | 8 | 5 |
| commute | 12 | 7 |
| **area** | 12 | **0** |
| **move_in_date** | 8 | **0** |
| **property_feature** | 8 | **0** |

已实现为 preflight 的 **Q4** 检查（`hard_constraints.py::SLOT_MIN_COVERAGE` +
`slot_coverage()`），任一槽位不达标即门禁失败。
现有基准 **7 个槽位全部不达标，其中 3 个是 0** —— 这正是「总量够、槽位空」问题的实测证据。

> ⚠️ 上表「现有 98 例基准的覆盖」列用的是**旧口径**（不区分满足性与行为）。
> 按 §2.13.6 的满足性口径重测后这些数字会大幅缩水，见 **§2.13.7**——那才是配额真正要卡的分母。

#### 2.13.6 满足性分母 vs 行为覆盖：两套数字，绝不混算（硬门禁）

owner ruling 2026-08-05：`room_type_match` 的无-listing 启发式分支**不得再计入可核验硬约束配额**。
推广成三条硬规则，已实现为门禁：

1. **计入槽位满足性分母的前提**：fixture 里有对应的**结构化字段**、有可比对的记录、
   并且存在**确定性谓词**。三者缺一，该约束就不进这个分母。
2. **`heuristic=True` / unknown / 无 listing 的文本标记分支**，最多计入
   「**正确处理无结果/未知**」的行为覆盖，**不得**计入对应槽位的满足性分母。
3. **无结果题仍然保留**，但其 `correct_completion` 必须明确写成
   「**诚实说明无匹配、不得声称约束已满足**」——不能把「没有候选」当成「候选满足了约束」。

实现（`hard_constraints.py` + `holdout_preflight.py`）：

| 检查 | 内容 |
|---|---|
| **Q4** | 只认**满足性**分母；报文里同时给出「另有 N 题只能走无结果/未知分支，不计入本分母」 |
| **Q5** | 行为覆盖（无结果/未知处理）**单独**卡下限、单独报，不与 Q4 合并 |
| **H5** | 一道占 hard 配额的题，若**没有任何**约束能走确定性满足性谓词 → 失败。它衡量的是「会不会复述用户条件」，不是「约束是否被履行」 |
| **N1** | 无结果/无 listing 的题，`correct_completion` 未写明「诚实说明无匹配」→ 失败（静态只查措辞标记，是否真写对由人工抽查确认） |

**证据字段的位置必须与语义一起冻结。** `SLOT_EVIDENCE_FIELD` 现在是 `(作用域, 字段)`：

| 槽位 | 作用域 | 字段 |
|---|---|---|
| budget | listing | `price_raw`（别名 `monthly_rent`） |
| bedroom_count | listing | `bedrooms` |
| room_type | listing | `property_type` |
| **commute** | **tool_result** | `duration_minutes` |
| area / move_in_date / property_feature | listing | schema 扩展后才存在 |

> 这一条是踩出来的：commute 的 `duration_minutes` 挂在 `calculate_commute` 的
> `data.duration_minutes`（**工具结果层**），不在 `recommendations` 里。
> 最初按 listing 层去找，得到一个**假的 0**。作用域不冻结，配额就会凭空少掉一整个槽位。

#### 2.13.7 用满足性口径重新测量现有基准：数字塌了

| 语义槽位 | **满足性覆盖** | 下限 | 仅行为覆盖 |
|---|---|---|---|
| budget | **1** | 15 | 12 |
| bedroom_count | **1** | 12 | 3 |
| room_type | **1** | 8 | 4 |
| commute | **2** | 12 | 5 |
| area | **0** | 12 | 0 |
| move_in_date | **0** | 8 | 0 |
| property_feature | **0** | 8 | 0 |

无结果/未知题 12 例（行为下限 12，**这一项达标**）；`H5` 在 12 道 `retrieval_hard` 里触发 **8** 次。

对比 §2.13.5 用旧口径（不区分满足性/行为）算出来的 13 / 4 / 5 / 7：
**换成满足性口径后塌到 1 / 1 / 1 / 2。**

**这就是这条 ruling 的价值**：旧口径下「budget 覆盖 13」看着只差一点点就达标，
实际上其中 **12 例根本无法确定性判定约束是否被满足**——它们只能证明模型有没有复述用户条件。
现有 98 例基准在满足性口径下，**七个槽位没有一个能支撑任何满足率结论**。

`move_in_date` 的 `Contact agent → unknown` 遵循同一原则：unknown 走行为覆盖，
不进满足性分母，也**不得**判为失败。

### 2.14 实验 B 的当前状态（唯一准确的一句话概括）

> **评测仪器已有部分可复用组件，但尚无可用于质量结论的 held-out 结果。**

具体拆开：

| | 状态 |
|---|---|
| 可复用的仪器 | `evaluation/results/_harness/blind_review_v2.py` + 四条冻结规则；`hard_constraints` 口径已验证（κ 0.040 → 0.735）；证据包已消掉纯计算/澄清/记忆类的假阴性 |
| 尚不可用的仪器部分 | `claims_evidence_supported` 缺 partial 判据（跨模型 κ 0.427）；非检索类的 `directly_actionable` 缺完成定义（κ 为负） |
| 唯一可报的读数 | `contradicted_claim_count`：三轮 49/50、48/50、47/50 判 0 处矛盾，跨模型观察一致率 0.940（**报计数与观察一致率，不报 κ**） |
| 质量结论 | **本设计无**。held-out 条件在本设计下未满足。**2026-08-05 的 §2B 满足了该条件并产出了结论**，但那些数字属于 §2B，不得回填到本表或与本节任何比例合并 |

---

## 2B. 实验 B — **第四次设计：真正的 held-out 集**（2026-08-05，新预注册批次）

> **这一节与 §2.1–§2.14 是并列的第四次设计，不覆盖、不合并、不替换前三次。**
> §2.12 明写的唯一剩余阻塞项——「重做要求第 5 条：未参与开发的固定 held-out 集」——在本节被满足：
> 本节用的 110 例是 2026-08-05 全新编写的，与调参用的 98 例**逐字零重合**。
>
> 这是一个**新的预注册批次**，不动用、不延续、也不冒充 2026-08-04 实验 B 的剩余额度
> （那一批在 300/400 结束并保持结束）。预注册全文见 `PROGRESS.log` 2026-08-05 07:20 条目，
> 写在**本批次第一次计费请求之前**。

### 2B.0 一句话结论

> **在 110 例预注册 held-out 基准上，确定性地测到：agent 在 33/33 道未被提前看过的硬约束检索题里，都点名了满足全部条件的那处房源；同时暴露了三类真实缺陷——4/16 道通勤题从未调用通勤工具却声称已按通勤上限筛选、3/10 道记忆写入题从未调用 `remember`、以及在无结果回合里凭空补入市场价。**
> 另一方面，预注册的 D1 口径**自己坏了**（0/119，全数落空），原因和处置写在 §2B.5 —— 那一条被**禁止**进入 CV。

### 2B.1 先做 schema v2（出题之前）

模块 `evaluation/results/_harness/constraint_schema_v2.py`，`SCHEMA_VERSION = "rentcompass/hard_constraints/v2"`。
测试 `test_constraint_schema_v2.py`：**199 项确定性检查，0 失败**，在编写第一道题之前跑通。

| v1 的问题（§2.13.2–§2.13.3） | v2 的处置 |
|---|---|
| `room_type_match` **重载**：靠 `re.match(r"\d+")` 分流「房型标签」与「卧室数」 | **拆成两个类型**：`room_type_match`（仅标签，受控词表 studio/flat/house/room_in_shared/maisonette/bungalow）与 `bedroom_count_match`（带 `op`：`==` / `>=` / `<=` / `between`） |
| 地点、入住日期、物业特征**没有类型**，只能写散文 | 新增 `area_match`、`move_in_date_satisfied`、`property_feature_present` |
| 预算与通勤 | 原样迁移：`all_results_satisfy`(field=monthly_rent)、`commute_leq_minutes` |
| 无 listing 时退化成**文本标记启发式** | v2 的谓词**没有任何文本回退**；字段不在就是 `unknown`，不是「猜一个」 |

**每个类型冻结五件事**（§2.13.4 的规范，逐条落地，见 `freeze_digest()`）：
用户文本规范化规则 / fixture 里的 `(作用域, 字段)` / 确定性通过-失败谓词 /
无结果·未知·部分匹配三种情形各自的正确完成态 / judge packet 允许引用的证据来源。

三条被点名的特别规则：

* **`area_match` 的匹配边界**：`postcode_district` 比较**整个** outward district，大小写折叠后**精确相等**——所以 **`N1` 不匹配 `N1C`**（N1C 是另一个 district，不是 N1 的子集），**前缀匹配被明确拒绝**；`postcode_sector` 是 district + inward 首位数字；`borough` 比对 listing 的 `area_normalized` 或 `borough`；`city` 只通过 listing 自己的 `city` 字段成立（"Camden 属于 London" 靠该 listing 写着 `city=London`，schema 里**没有**隐式的 borough→city 表）；`adjacent`（"near King's Cross"）**只**由该题冻结的 `accept` 列表满足，**邻近关系永不在评分时推断**。
* **`move_in_date_satisfied` 的 unknown**：`Contact agent` / `On application` / `TBC` / 字段缺失 → **unknown**，**既不算满足也不算失败**，只进行为覆盖，正确回答必须说「需要向中介确认」。
* **`property_feature_present` 的自由文本禁令**：谓词**只读**结构化的 `listing.features` 列表；从房源描述文字推断特征，对 grader 与 judge **一律禁止**。

**词表审计**：7 个 INCLUDED（用户硬约束）/ 26 个 EXCLUDED（仪器条件），
`UNCLASSIFIED` 为空；非空即门禁失败（Q3）。

**v2 最关键的一处修正**（在出题前做的）：**满足性谓词对着「答案」评，不再对着 fixture 评。**
v1 的 `_c_all_results_satisfy` 跑在 `_listings_from_evidence(ctx.evidence)` 上，也就是**工具返回了什么**——
在一份全合规的 fixture 上它无论助手写什么都真空通过。这正是 §2.13.7 里「budget 覆盖 13、可判 1」的成因。
v2 因此要求每一条计入分母的约束都带**违规陷阱**（冻结证据里既有满足的记录、也有违反的记录），
否则该题**根本进不了满足性分母**。

**两处 preflight 规则修正，都做在一道 held-out 题都还不存在的时候**：

1. **N1（诚实说明无匹配的措辞）收窄到检索类**。v1 对「fixture 里没有 listing」的题一律要求该措辞，会误伤纯计算 / 记忆 / 澄清类——它们的正确完成态本来就不是「说没找到房子」。
2. **H3 被 H6 取代**。v1 问「约束的数值有没有出现在请求文本里」，既太松（"2 bedrooms" 里的 2 能满足预算 2）又太紧（入住日期规范化成 ISO 串，永远不会逐字出现）。v2 要求每条硬约束携带**原文 `user_text` 片段**，门禁**重新跑一遍冻结的规范化函数**，复现不出存储值就判失败。

### 2B.2 held-out 集：110 例，全新编写

| 项 | 值 |
|---|---|
| 文件 | `evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl` |
| **sha256** | `0294584c69bf60147e199b6d8430bfc4b041d0ea110bec6d11922feb7b88fe34` |
| 冻结记录 | `evaluation/benchmark/holdout_v2/FREEZE.json`（含全部 sha256 与 git identity） |
| fixture | `evaluation/benchmark/fixtures/ho2_*.json`，共 **75** 份，逐份 sha256 记于 `MANIFEST.json` |
| 生成器 | `evaluation/results/_harness/build_holdout_v2.py`，**完全确定性、无任何 RNG** —— 所以「seed」对出题不适用，重跑得到逐字节相同的输出 |
| 用到 seed 的地方 | preflight 抽查采样 `20260805`；三轮盲审 seed 见 §2B.4；bootstrap `20260805` |
| git | HEAD `0952c56e21b9b0dac3fb10fe99ee907c36b3a2d8`，分支 `telemetry-issue78`，identity `shuhan-wang1 <130167280+shuhan-wang1@users.noreply.github.com>` |
| 冻结时间 | 2026-08-05 07:16 BST（**第一次计费请求之前**） |
| schema | `evaluation/benchmark/schema_v2.json`（v1 的 `schema.json` **未改动**，仍管辖 98 例） |

**分层配额（全部达标）**

| 分层 | 要求 | 实际 |
|---|---|---|
| retrieval_hard | 35 | **35** |
| retrieval_soft | 20 | **20** |
| calculation | 20 | **20** |
| memory | 20 | **20** |
| clarify | 15 | **15** |

**语义槽位的满足性覆盖（Q4，只收能被确定性谓词判定的题）**

| 槽位 | 下限 | 实际 |
|---|---|---|
| budget | 15 | **16** |
| bedroom_count | 12 | **13** |
| room_type | 8 | **22** |
| commute | 12 | **16** |
| area | 12 | **35** |
| move_in_date | 8 | **9** |
| property_feature | 8 | **8** |

行为覆盖（无结果 / 未知，Q5）：**13**（下限 12）——8 道无结果检索题 + 5 道可入住日期为 `Contact agent` 的未知分支题。

**与 98 例的非重复性**：地理完全不相交（本集用 Walthamstow / Peckham / Leyton / Tooting / Acton /
Crouch End / Bermondsey / New Cross / Wood Green / Catford / Streatham / Willesden Green /
Forest Gate / Colindale / Balham / Deptford / Harringay / Hendon；98 例用 UCL / Canary Wharf /
Camden / Islington / Shoreditch / Bloomsbury / Chessington / Clapham / Kensington / Whitechapel /
Hackney / Stratford / Manchester），通勤目的地不相交，人物、价格、地址、日期全新。
**逐字请求文本重合数 = 0**（对 `cases.jsonl` 实测）。每题另带自己的 `novelty_note`。

**冻结后未再改动任何一题、任何答案、任何规则；没有删除任何失败案例。**

### 2B.3 门禁与逐题审查

**静态门禁**：`holdout_preflight.py --schema v2`

```
gate_passed: true      退出码 0
n_failed: 0            quota_problems: []      residual_after_replacement: []
vocabulary_audit.UNCLASSIFIED: []    semantic_slots_without_a_type: []
```

Q（配额）/ H（明确·可验证·互不矛盾，含 H2b 参数词表、H5 确定性可判、H6 原文复现）/
C（正确完成态）/ E（可评主张有依据来源）/ Q3（词表）/ Q4（满足性配额）/ Q5（行为配额）**全部通过**。
报告 `evaluation/benchmark/holdout_v2/preflight_report.json`，清单 `preflight_checklist.md`。

**逐题审查**：`evaluation/benchmark/holdout_v2/AUTHOR_AUDIT.md`，**110 例全部逐条列出**，
**110 PASS / 0 REPLACE**。

> ⚠️ **这是 author audit（出题者自审），不是 human review。**
> 没有任何人参与标注。本文件、本节、以及 CV 里都不得把它写成人工评审、人工盲审或评审者间信度。

该自审**发现并在冻结前修掉了两处缺陷**（不是跑完之后的补救）：

1. **22 道 hard 题在请求里说了地点，却没有把地点声明成约束**。现已改为：只要请求里说出了地点，就声明 `area_match` 并配上违规陷阱（这也是 area 满足性覆盖从 13 变成 35 的原因）。
2. **计算题措辞与澄清题完成态是同一套模板**。已按题分化：每个公式族给 3–5 种真实说法，每道澄清题写明它自己缺的是什么。

### 2B.4 预注册（写在第一次计费请求之前）

`PROGRESS.log` 2026-08-05 07:20 条目，含：数据集 hash / n=110 / 分层与槽位覆盖；judge 标签词表；
`claims_evidence_supported` 的 yes-partial-no 定义；非检索任务「完成了该完成的事」的逐类判据；
`not_applicable` / `cannot_assess` / `unknown` 的适用规则；模型、轮次、seed、温度；全部主指标与统计方法；
CV 可引用门槛；最大请求数与成本。

| 项 | 值 |
|---|---|
| 被测系统 | 生产路由 `evaluation/configs/routed_models.yaml`（未打补丁），`AGENT_ARCH=fc_loop`，**live**，每例 1 次 |
| 盲审 R1 / R2 | `deepseek-v4-flash`，thinking 关，**temperature 0** |
| 盲审 R3 | `deepseek-v4-pro`，**temperature 0** |
| 三轮 seed（顺序 / 提问序 / nonce） | R1 `20260811` / `20260812` / `RC-HO2-R1`；R2 `20260887` / `20260909` / `RC-HO2-R2`；R3 `20261365` / `20261587` / `RC-HO2-R3` |
| 盲化 | prompt 不含配置、臂标识、预期结论，也不含其他轮的判读 |
| 统计 | cluster bootstrap，**重采样单位 = case**，2000 次，seed `20260805`，95% 百分位区间 |
| 请求预算 | agent 110 + flash 220 + pro 110 = **440**，硬上限 460，最大成本 USD 5，单请求 120 s，外部并发 ≤2，间隔 ≥300 ms，连续 10 次失败即停 |

**证据包**（`blind_review_holdout.py`）：当前请求 + **完整历史** + `reconstructed_context` +
**该题自己的 `allowed_evidence_sources`** + `reference_calculations` + **记忆读写结果** +
已执行工具列表 + **完整结构化工具证据**。
证据上限 24,000 字符，实测**最长 4,861 字符**——**本批次没有任何一条被截断**，
所以 §2.7 限制 2（截断后仍强制评分）在本节**不存在**。

### 2B.5 确定性指标

> 每一条都给原始分子/分母。**边界值上 bootstrap 会退化**（33/33 的每一次重采样仍是 33/33，
> 百分位区间塌成 `[100%, 100%]`），因此每个 case 级比率**同时**给 Clopper-Pearson 精确二项区间，
> **对外引用的是精确区间**。加精确区间是统计口径修正，不是改指标，且对所有确定性比率一视同仁。

| 代号 | 指标 | 结果 | 精确 95% CI | 可否进 CV |
|---|---|---|---|---|
| **D6** | **点名了满足全部条件的房源**（主分母 n=33） | **33/33 (100%)** | **[89.4%, 100%]** | ✅ |
| D6 | 同上，全部 35 例 | 35/35 (100%) | [90.0%, 100%] | 仅并列参考 |
| D5 | 计算题命中冻结参考值 | 20/20 (100%) | [83.2%, 100%] | ❌ n=20 < 30 |
| D4 | 无结果题未出现用户自述之外的金额 | 4/8 (50%) | [15.7%, 84.3%] | ❌ n=8，且是诊断项 |
| **D1** | 预注册的硬约束满足率（(题, 约束) 对） | **0/119 (0%)** | — | ❌ **口径自坏，见下** |
| D2 | 案例级全约束通过 | 0/35 (0%) | [0%, 10.0%] | ❌ 同 D1 |

#### D1 为什么是 0/119，以及为什么这不等于「agent 违反了每一条约束」

D1 的预注册判据是：**答案里只要出现了违反约束的冻结房源，就判 FAIL**。
实测 0/119——**每一对、每一个槽位、每一道题都失败**。这种整齐划一本身就是诊断：
它不是「agent 全错」，是**我的谓词在这套答案格式上退化了**。

该系统的答案稳定地写成「**符合你条件的：…**」后面跟「**已排除（不符合）：… 理由 …**」。
D1 把答案里**任何位置**出现的房源都算作 surfaced，于是那份**透明的排除清单**让每道题都失败。
手工核对了 HO2-001 / HO2-016 / HO2-023 / HO2-026 / HO2-034 五例来确认这是格式问题而非质量问题。例如 HO2-001：

> **Meets all your criteria:** 1. **11 Fernbrook Row** — £1,550/month … This is the only listing that fully satisfies every one of your requirements.
> **Excluded (did not meet every criterion):** 13 Halstow Row — £1,900/month → over budget；14 Wraysbury Row — 3-bed, not 2-bed；16 Ashlin Row, **Deptford** — over budget and not in Walthamstow

这正是本仓库 PR #7 判过的「**如实报告并明确标注越界选项是正确行为；判它失败是在奖励沉默**」，
也正是该题自己 `correct_completion` 里写的「不得把它们**当作匹配项**推荐」——
而谓词看不见「当作匹配项」这四个字。

**处置（重要）**：
* D1 **原样保留、原样上报 0/119**，不放宽、不删除、不重跑；
* 它的**正确读法**是「答案是否**完全没有**把不合规选项摆到用户面前」，是约束纪律的**下界**，
  **不是**「助手只推荐了合规选项」；
* 它在 **2026-08-05 07:26** 就被标记为 **NOT CV-ELIGIBLE**——那时这些数字还不存在
  （标记写在 6 例冒烟之后、104 例正式跑之前，见 PROGRESS.log）。

#### D6：冒烟后新增，且已披露

D6 =「答案是否**点名**（街名 token 或精确价格，与 D1 同一套确定性匹配）至少一处满足**全部**已声明条件的冻结房源」。
它是在 6 例冒烟之后定义的，因此**主分母剔除了当时已经看过答案的 2 道 hard 题**（HO2-001、HO2-023）→ **n = 33**。
全部 35 例的数字并列给出，并标明它含那两道。**只有 n=33 这一版可用于对照 CV 门槛。**

#### D5：谁答对了，要拆开说

计算题 20/20 全部命中冻结参考值。但**其中 9 道由 `app/core/tenancy_reference.py` 这个确定性非-LLM 模块回答**
（0 次 LLM 调用、约 20 ms），另外 11 道由模型回答。

| | 分子/分母 |
|---|---|
| 确定性模块（0 LLM 调用） | **9/9** |
| 模型回答 | **11/11** |

一个笼统的「计算 100%」会把这件事藏起来，所以这里拆开报。

### 2B.6 模型盲审（**不是人工评审，不是答案正确率，不是评审者间信度**）

三轮共 **330** 次评审请求，**0 次失败**，**0 条越出词表的回答**，**0 条 `cannot_assess`**。
R2 有 **1 条**回答 JSON 未能解析（HO2-006），按未判读计入，未做任何静默归一化。

#### 2B.6.1 每项真正可判的 n

| 判定项 | R1 可判 / N-A / cannot_assess / 越词表 | R2 | R3(pro) |
|---|---|---|---|
| `hard_constraints_satisfied` | **36** / 74 / 0 / 0 | 34 / 75 / 0 / 0 | **46** / 64 / 0 / 0 |
| `claims_evidence_supported` | **92** / 18 / 0 / 0 | 91 / 18 / 0 / 0 | 91 / 19 / 0 / 0 |
| `contradicted_claim_count` | **110** / 0 / 0 / 0 | 109 / 0 / 0 / 0 | 110 / 0 / 0 / 0 |
| `task_completed_correctly` | **110** / 0 / 0 / 0 | 109 / 0 / 0 / 0 | 110 / 0 / 0 / 0 |

**这是本节相对 §2.12 最大的改进**：§2.12.2 里 `hard_constraints_satisfied` 在 50 例抽样上**只有 8 例可判**；
在按可判性配额出题的 110 例上，它是 **36 / 34 / 46**——三轮都过了 n≥30。
`cannot_assess` 全部为 0，因为没有一条证据被截断。

#### 2B.6.2 三轮原始标签计数

| 判定项 | R1 flash | R2 flash | R3 pro |
|---|---|---|---|
| `hard_constraints_satisfied` | yes 32 / no 4 / N-A 74 | yes 33 / no 1 / N-A 75 / 未解析 1 | yes 36 / no 10 / N-A 64 |
| `claims_evidence_supported` | yes 74 / partial 11 / no 7 / N-A 18 | yes 76 / partial 8 / no 7 / N-A 18 / 未解析 1 | yes 57 / partial 32 / no 2 / N-A 19 |
| `contradicted_claim_count` | 0×107 / 1×3 | 0×108 / 1×1 / 未解析 1 | 0×105 / 1×4 / 2+×1 |
| `task_completed_correctly` | yes 104 / no 6 | yes 107 / no 2 / 未解析 1 | yes 100 / no 10 |

#### 2B.6.3 可判分母上的比率（分子/分母 + 精确 95% CI）

| 判定项 | R1 | R2 | R3(pro) |
|---|---|---|---|
| `hard_constraints_satisfied` = yes | 32/36 (88.9%) [73.9, 96.9] | 33/34 (97.1%) [84.7, 99.9] | 36/46 (78.3%) [63.6, 89.1] |
| `claims_evidence_supported` = yes | 74/92 (80.4%) [70.9, 88.0] | 76/91 (83.5%) [74.3, 90.5] | 57/91 (62.6%) [51.9, 72.6] |
| `contradicted_claim_count` = 0 | 107/110 (97.3%) [92.2, 99.4] | 108/109 (99.1%) [95.0, 100] | 105/110 (95.5%) [89.7, 98.5] |
| `task_completed_correctly` = yes | 104/110 (94.5%) [88.5, 98.0] | 107/109 (98.2%) [93.5, 99.8] | 100/110 (90.9%) [83.9, 95.6] |

#### 2B.6.4 一致性（同模型两轮 = 自一致性，**不是**评审者间信度）

| 判定项 | 同模型两轮 obs | 同模型 κ | 跨模型 obs | 跨模型 κ | κ 是否退化 |
|---|---|---|---|---|---|
| `hard_constraints_satisfied` | **0.945** | 0.877 | **0.873** | 0.750 | 否 |
| `claims_evidence_supported` | **0.954** | 0.907 | **0.745** | 0.571 | 否 |
| `contradicted_claim_count` | **0.982** | 0.493 | **0.945** | 0.226 | **是** |
| `task_completed_correctly` | **0.945** | 0.229 | **0.945** | 0.598 | **是** |

> **prevalence degeneration 必须写明**：`contradicted_claim_count` 与 `task_completed_correctly` 上，
> **单一类别占了 ≥90% 的判读**（分别是「0 处矛盾」与「yes」）。此时期望一致率本身逼近 1，
> κ 的分母趋于 0、数值极不稳定——`contradicted` 的观察一致率高达 0.982 / 0.945，κ 却只有 0.493 / 0.226。
> **这两项一律报观察一致率与原始计数，κ 只作辅助，绝不用 κ 单独证明可靠性。**
> 这与 §2.12.3 的更正是同一条规律，在一个全新的数据集上又复现了一次。

#### 2B.6.5 `contradicted_claim_count` 的原始 0 / 1 / 2+ 计数

| | R1 | R2 | R3(pro) |
|---|---|---|---|
| 判 0 处矛盾 | **107/110** | **108/109** | **105/110** |
| 判 1 处 | 3 | 1 | 4 |
| 判 2 处及以上 | 0 | 0 | 1 |
| 未解析 | 0 | 1 | 0 |

跨模型**观察一致率 0.945**。

#### 2B.6.6 分任务类别的跨模型观察一致率

| 判定项 | retrieval_hard (35) | retrieval_soft (20) | calculation (20) | memory (20) | clarify (15) |
|---|---|---|---|---|---|
| `hard_constraints_satisfied` | 0.886 | **0.600** | 1.000 | 0.950 | 0.933 |
| `claims_evidence_supported` | 0.829 | **0.500** | **0.650** | 0.850 | 0.867 |
| `contradicted_claim_count` | 0.857 | 1.000 | 0.950 | 1.000 | 1.000 |
| `task_completed_correctly` | 0.886 | 1.000 | 0.950 | 0.950 | 1.000 |

**`claims_evidence_supported` 仍然是最不稳的一项**，成因与 §2.12.4 诊断的完全一致且更明显：
**pro 大量使用 `partial`（32 次，flash 只有 11 次）**。新增的 partial 判据（「至少一条有依据且至少一条没有依据」）
把边界说清楚了，但两个模型对「一条主张算不算一条」的粒度仍不一致。**这一项在本节仍不作为质量结论引用。**

### 2B.7 agent 侧的真实缺陷（逐例，与评测缺陷分开记）

以下每一条都对着冻结证据核过，是**产品问题**，不是仪器问题。

| # | 缺陷 | 证据 | 涉及案例 |
|---|---|---|---|
| **1** | **声称按通勤上限筛选，却从未调用通勤工具** | `tools_called` 里没有 `calculate_commute`，`fixture_served=false`、`fixture_unserved_tools=["calculate_commute"]`；HO2-028 写「I've applied your 25-minute commute cap to Farringdon in the search」，HO2-024 更进一步断言某房源「is well over 25 minutes from London Bridge」——**没有任何工具数据支撑这句话** | **4 / 16 道通勤题**：HO2-024、HO2-028、HO2-030、HO2-034 |
| **2** | **记忆写入指令没有被持久化** | 用户明说「Note that…」「For future reference…」「Record that…」，agent 调的是 `recall_memory` 或什么都没调，`remember` fixture 未被消费。HO2-089 反过来说「I don't have any saved details about your housing search yet」并改问用户 | **3 / 10 道记忆写入题**：HO2-089、HO2-092、HO2-093 |
| **3** | **无结果回合凭空补入市场价** | 工具返回 0 结果，回答如实说了没有匹配（对），随后自行断言市场行情：HO2-052「5-bed houses in Forest Gate generally start around **£2,500–£3,000+**」、HO2-053「Studio or one-bed options there are usually **£1,200+**」、HO2-054「a 1-bedroom flat in Bermondsey usually starts around **£1,400–£1,600** … a 2-bed maisonette would realistically be in the **£1,800–£2,200** range」。这些数字**不在任何证据里** | HO2-052、HO2-053、HO2-054（HO2-049 的「stretch to £800–£900」属预注册就说明过的假阳性形状：那是建议不是断言） |
| **4** | **把不满足条件的房源列在「符合全部条件」标题下** | HO2-034 把 47 Thackray Terrace（Deptford）列在「Options that meet ALL your conditions」里，同一行又写它其实不在 Forest Gate；HO2-022 用户要 **house**，回答把 34 Ockendale Mews（**Studio**）列进「Results found in Acton」而未标注不符；HO2-026 的「Verified options that meet ALL your conditions」标题下第 2–4 项自己写着 Excluded | HO2-022（三轮全判 no）、HO2-026、HO2-034 |
| **5** | **计算题给出被明令禁止的口径** | HO2-063 同时给出 £572.75（月租 ÷ 4.33）与 £572.31（年租 ÷ 52），把 ÷4.33 说成「another convention」。`evaluation/benchmark/README.md` 明写 **Do not approximate with `* 4` or `* 4.33`**。最终推荐值对，但一条答案里出现两个互相冲突的周租数字 | HO2-063（R1/R3 判 1 处矛盾，R2 判未完成） |
| **6** | **把内部对账过程写进了给用户的答案** | HO2-033 开头是「The commute for Pentworth Terrace was already computed in the earlier batch (returned as 34 minutes, attributed to "Denbury Terrace" in the route summary…)」；HO2-023 开头写「Let me clarify the commute results. The tool returned results in a slightly mixed order…」 | HO2-023、HO2-033 |

> 缺陷 6 有一半是**本评测的性质**造成的，必须说清楚：`run_benchmark.load_fixture_queue` 的重放队列**按工具名排队、按调用顺序出队**，所以 agent 第 n 次 `calculate_commute` 拿到的是第 n 条冻结记录，未必对应它当时问的那处房源。记录里带 `from_address` 与 `origin_uid`，所以**可以**正确对上（HO2-023 就对上了），确定性评分也始终按 `origin_uid` 归属、不受调用顺序影响。但它确实给 agent 增加了一道对账负担。**缺陷 6 因此只作为观察记录，不计入产品缺陷率。**

### 2B.8 运行、失败、成本、延迟

| 项 | 值 |
|---|---|
| agent 回合 | **110 / 110**，**失败 0，超时 0** |
| 盲审请求 | **330**（flash 110×2 + pro 110），**失败 0**，越词表 0，未解析 1（R2 的 HO2-006） |
| **本批次请求总数** | **440**（预注册 440，硬上限 460）——冒烟的 6 次 agent + 18 次盲审写进最终文件，被续跑复用，**没有重复计费** |
| agent 侧 LLM 调用 | 208（其中 **9 道计算题为 0 次调用**，由确定性模块回答） |
| token | 输入 2,079,544 / 输出 40,834 |
| **agent 侧成本** | **USD 0.0985**（§0.2 价表） |
| 外部工具 | 125 次调用，**7 次失败（5.6%）** |
| agent 端到端延迟 | mean **5,037 ms** / p50 **4,575 ms** / p95 **11,826 ms** |
| 估算总成本（含盲审） | **远低于预注册的 USD 5 上限** |
| 结果文件 | `evaluation/results/holdout_v2/{runs,grader_input,events_shard0}.jsonl`、`evaluation/results/holdout_v2_review/{items,design,round1,round2,round3_pro}.{json,jsonl}`、`analysis_holdout_v2.json` |

**断点续跑**：agent 侧按 `ab_run_key` 去重，盲审侧按 `item_id` 逐条 append 并去重；
每完成一例立即落盘，没有任何结果只存在内存里。

### 2B.9 限制

1. **D1 的口径缺陷**（§2B.5）：预注册的主确定性指标在这套答案格式上退化。已如实上报 0/119 并禁止进 CV，但这意味着「助手是否只把合规选项当作匹配推荐」这一问题，本批次**只有模型盲审的读数**，没有确定性读数。
2. **D6 是冒烟后新增的**，主分母因此缩到 n=33。它满足 n≥30，但这是一条**在看过 2 份答案之后**才写下的规则，已逐字披露。
3. **fixture 重放按工具名排队**，深工具循环里 agent 拿到的记录可能与它当时问的房源错位（§2B.7 缺陷 6 的注）。评分不受影响，但答案质量受了一点无关的干扰。
4. **第三轮仍是同厂商的另一个模型**（DeepSeek v4-pro），不是真正独立的第三方；跨模型一致率因此可能仍被高估。
5. **仍然没有任何人工标注**。本节全部判读要么是确定性程序，要么是模型。`AUTHOR_AUDIT.md` 是出题者自审。
6. **每例只跑 1 次**，没有重复运行，所以本节不报 agent 侧的方差。
7. **`claims_evidence_supported` 仍不可用于质量结论**（跨模型 obs 0.745 / κ 0.571，pro 的 partial 用量是 flash 的三倍）。
8. **8 例的 fixture 未被送达**（4 例 `calculate_commute`、3 例 `remember`、1 例 `search_properties`）。这**本身就是 §2B.7 的缺陷 1 与 2**，不是评测故障；但它意味着这 8 例的部分约束 agent 根本没拿到证据。

### 2B.10 本节可引用 / 不可引用一览

| 指标 | n（真正可判） | 结果 | 判定 |
|---|---|---|---|
| **D6 点名满足全部条件的房源** | **33** | 33/33，精确 CI [89.4%, 100%] | ✅ **可进 CV**（确定性） |
| **模型盲审：与证据矛盾的主张数** | **110** | 三轮 107/110、108/109、105/110 判 0 矛盾；跨模型观察一致率 **0.945** | ✅ **可进 CV**（必须写「模型盲审」，报计数与观察一致率，**不报 κ**） |
| 模型盲审：`task_completed_correctly` | 110 | 104/110、107/109、100/110 | ⚠️ 可在报告里写；κ 退化，只报观察一致率 0.945 |
| 模型盲审：`hard_constraints_satisfied` | 36 / 34 / 46 | 88.9% / 97.1% / 78.3% | ❌ **不进 CV** —— **分母本身在轮次间变动**（34→46），口径不稳 |
| 模型盲审：`claims_evidence_supported` | 92 / 91 / 91 | 80.4% / 83.5% / 62.6% | ❌ **不进 CV** —— 跨模型 κ 0.571，partial 粒度未收敛 |
| D5 计算题命中参考值 | 20 | 20/20 | ❌ **不进 CV** —— n=20 < 30 |
| D4 无结果题不补金额 | 8 | 4/8 | ❌ **不进 CV** —— n=8，且是诊断项 |
| D1 / D2 预注册硬约束满足率 | 119 / 35 | 0/119、0/35 | ❌ **不进 CV** —— 口径自坏（§2B.5），2026-08-05 07:26 即已标记 |
| 通勤题未调用通勤工具 | 16 | 4/16 | ❌ 不进 CV（缺陷清单，n 太小） |
| 记忆写入未持久化 | 10 | 3/10 | ❌ 不进 CV（缺陷清单，n 太小） |


---

## 3. 实验 C：并行工具编排 A/B（重跑 + 补选例口径）

### 3.1 先定规则再筛（这是本节最重要的产出）

选例规则在**任何 C 运行开始之前**冻结（PROGRESS.log 2026-08-04 17:40），原文：

> 一个 case 合格，当且仅当在实验 A 的 **routed（生产路由）** 运行中，至少有一个已执行的工具批次包含 **≥2 个工具调用**（即 `RunResult.tool_trace` 存在长度 ≥2 的批次）。**互不依赖**由构造保证：模型把这些调用放进了**同一个批次**，说明它不需要其中任何一个的结果去构造另一个。主阈值：3 次重复中 **≥2 次**满足。

规则跑在 A 的 294 次 routed 运行上（`evaluation/results/parallel_tools_ab/case_selection.json`）：

| 口径 | 合格 case 数 |
|---|---|
| 3 次重复中 ≥1 次出现 ≥2 调用批次 | **22** |
| 3 次重复中 ≥2 次（**主口径**） | **20** |
| 3 次重复全部 3 次 | **18** |

全部 294 次 routed 运行的**批次大小分布**（同一来源文件）：

| 批次内工具调用数 | 批次数 |
|---|---|
| 1 | 179 |
| 2 | 32 |
| 3 | 21 |
| 4 | 12 |

即 244 个批次里只有 **65 个（26.6%）** 装了 ≥2 个调用。

**结论（直接回答任务书的问题）**：合格总体就是 **20 例左右**，不是"远多于 16"。
**首轮的 n=16 已经接近全集**，不是从大池子里挑出来的 16 例。这条本身就是本次评测的一个结果，应当明确写出来，而不是继续留白。

选中的 20 例：`B13, C2, C6, C7, C9, C10, C12, D2, D7, D9, E1, E3, E5, E6, E7, E9, E10, E11, F9, F13`
（类别分布：E_multi_constraint 8 / C_commute 6 / D_crime_poi 3 / F_grounding 2 / B_money 1）

静态交叉核对：按 case 声明的 `expected_tools ≥ 2` 只能筛出 8 例（`E1,E2,E3,E5,E6,E9,E11,G16`），与实测的 20 例只有部分重合——**说明静态声明筛不出真实的并行机会**，只有跑出来的批次结构能筛。这也是首轮口径说不清楚的根因。

### 3.2 设计

| 项 | 值 |
|---|---|
| 数据集 · split | 上述 20 例（来自 `evaluation/benchmark/cases.jsonl` 的 98 例） |
| 对照臂 serial_tools | `core.agent_loop._TOOL_OFFLOAD_EXECUTOR` 换成 **1 worker** 线程池（进程内 monkeypatch）——批次组成、工具、参数、证据、预算全不变，**只有调度不同** |
| 实验臂 parallel_tools | 生产线程池（`FC_TOOL_OFFLOAD_WORKERS` 默认 32） |
| 为什么不用 `serial_retrieval.yaml` | 该配置设的是 LangGraph `max_concurrency=1`，只节流 **legacy 图**的 map-reduce Send 扇出；fc 循环的批内并发来自线程池，与之无关。且 `_tool_offload_executor()` 对 env 做了 `max(4, workers)` 下限，**env 本身无法串行化** |
| 配对 | 是（同 case、同重复号、同还原快照，臂顺序按 repeat 奇偶交替） |
| 运行次数 | 每臂 3 次 → 20 × 2 × 3 = **120 run**（上限 400） |
| live / mock | **live** |
| 统计 | 与实验 A 相同：cluster bootstrap，重采样单位 = case，2000 次，seed `20260804` |
| 结果文件 | `evaluation/results/parallel_tools_ab/shard{0,1}/runs.jsonl`、`analysis_C.json`、`negative_control_C.json`、`table_C.md`、`case_selection.json` |

### 3.3 主结果：未观察到显著差异

#### 实验 C 指标表

来源文件：`evaluation/results/parallel_tools_ab/analysis_C.json`（由 `evaluation/results/_harness/analyze.py` 生成，输入 runs.jsonl 见该文件 `source_files` 字段）

运行总数 120，成功 120，失败 0（失败分布 {'serial_tools': 0, 'parallel_tools': 0}，原因 无）；配对上的 case 数 20 / 出现过的 case 数 20。

| 指标 | serial_tools（工具批次串行派发） (对照) | parallel_tools（生产并发派发） (实验) | 差值 test−base，bootstrap 95% CI | 判读 |
|---|---|---|---|---|
| LLM 调用总数 | 150 | 149 | -0.7% [-6.4%, 5.2%] | 未观察到显著差异 (CI 跨 0) |
| 其中 thinking 模式调用 | 0 | 0 | — | — |
| 其中 deepseek-v4-pro 调用 | 0 | 0 | — | — |
| 绕过 ModelRouter 的调用 (purpose=memory) | 0 | 0 | — | 不受本 A/B 的路由改动影响，见正文 |
| 输入 token | 1,522,825 | 1,568,722 | — | — |
| 输出 token | 40,537 | 40,934 | 1.0% [-5.5%, 7.1%] | 未观察到显著差异 (CI 跨 0) |
| 总 token | 1,563,362 | 1,609,656 | 3.0% [-3.7%, 9.9%] | 未观察到显著差异 (CI 跨 0) |
| 缓存命中 token (provider 侧) | 1,445,632 | 1,485,312 | — | — |
| 成本 USD（固定价表） | 0.0262 | 0.0273 | 4.2% [-8.5%, 17.3%] | 未观察到显著差异 (CI 跨 0) |
| 端到端 mean (ms) | 19,980 | 19,331 | -649 ms [-5,002 ms, 4,093 ms] | 未观察到显著差异 (CI 跨 0) |
| 端到端 p50 (ms) | 6,802 | 6,784 | -18 ms [-1,802 ms, 1,639 ms] | 未观察到显著差异 (CI 跨 0) |
| 端到端 p95 (ms) | 84,791 | 78,540 | -6,252 ms [-48,226 ms, 20,831 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 mean (ms) | 11,555 | 10,807 | -748 ms [-4,828 ms, 4,053 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 p50 (ms) | 7 | 13 | 5 ms [-706 ms, 142 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 p95 (ms) | 73,356 | 55,905 | -17,451 ms [-48,346 ms, 21,836 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 mean（仅真正执行了工具批次的 run，n=60 / 60） | 11,555 | 10,807 | — | 见正文 |
| 检索阶段 p50（同上子集） | 7 | 13 | — | 见正文 |
| 检索阶段 p95（同上子集） | 73,356 | 55,905 | — | 见正文 |
| 证据支撑率 grounded/verifiable | 281/294 (95.6%) | 312/330 (94.5%) | -1.03 pp [-3.74 pp, 1.88 pp] | 未观察到显著差异 (CI 跨 0) |
| 金额支撑率 money_grounded/money_total | 70/77 (90.9%) | 98/103 (95.1%) | 4.24 pp [-2.28 pp, 15.08 pp] | 未观察到显著差异 (CI 跨 0) |
| 与证据矛盾的主张数 (contradicted) | 0 | 0 | — | — |
| 任务完成 task_completed | 60/60 | 60/60 | 0.00 pp [0.00 pp, 0.00 pp] | 未观察到显著差异 (CI 跨 0) |
| 约束全通过 passed | 42/60 | 38/60 | -6.67 pp [-18.33 pp, 5.00 pp] | 未观察到显著差异 (CI 跨 0) |
| 执行的工具调用数 / run（均值差） | 2.80 | 3.17 | 0.33 [0.13, 0.60] | CI 不跨 0 |
| 监听缓存命中率 (listing cache) | 9/28 (32.1%) | 12/29 (41.4%) | — | — |
| 外部工具失败率 | 5/168 (3.0%) | 6/190 (3.2%) | — | — |

bootstrap：cluster bootstrap（重采样单位 = case），重采样次数 2000，seed 20260804，配对 case 数 20。

**检索阶段与端到端分开报，不合成，不相加。** 两者的 CI 全部跨 0。

### 3.4 阴性对照——**没有通过**，而这是本节比延迟数字更重要的发现

来源：`evaluation/results/parallel_tools_ab/negative_control_C.json`。

| 项 | 值 |
|---|---|
| 比较的配对数 | **60** |
| 已执行工具调用数**不一致**的配对 | **16 / 60** |
| 完成态（`task_completed`）不一致的配对 | **0 / 60** |
| 不一致的方向 | 并行臂执行更多：**12**；串行臂执行更多：4 |

配合两臂的预算/超时计数（同一文件）：

| 项 | serial_tools | parallel_tools |
|---|---|---|
| budget timeout 事件 | **35** | 25 |
| 被预算杀掉的工具 | **35** | 25 |
| 触发 soft wrap 的 run | 5 | 3 |
| 工具调用事件总数 | 168 | **190** |
| 工具调用失败 | 5 | 6 |
| LLM 重试次数 | 0 | 0 |

**读法**：把批内派发串行化，会把批次推过它的时间窗，工具被放弃，于是**两臂根本没有在做同一件事**（并行臂多执行了工具，串行臂多吃了 40% 的预算超时）。
所以「同输入同证据」这个前提在 **live fc 循环**里并不成立——它在首轮的 legacy 规划器上成立，是因为那里的扇出是提前规划好的固定集合。

这条直接影响首轮那句「48 组配对无工具数/完成态差异」的可迁移性：**在当前架构下不成立**（16/60 有差异）。首轮的结论没有被推翻（架构、串行化手段、案例集都不同，两者不可直接比较），但它**不能被当作当前架构的性质引用**。

### 3.5 次要视图（事后子集，仅作提示，不作为结论）

同样的 bootstrap 设定，只换样本子集：

| 子集 | n（配对/case） | 检索阶段 mean serial → parallel | 差值 95% CI | 判读 |
|---|---|---|---|---|
| 工具调用数**一致**的配对（"同输入同证据"真正成立的那些） | 44 / 18 | 3,696 → 2,376 ms | **−1,222 ms [−3,278, −22]** | CI 上界只有 −22 ms，**贴着 0**，只能说"有提示性" |
| 至少一臂出现过 ≥2 调用批次的配对 | 58 / 20 | 11,885 → 11,176 ms | −697 ms [−4,753, +4,063] | 跨 0，未观察到显著差异 |

**这两行是事后（post-hoc）子集分析**，不是预先注册的主指标；第一行还存在选择偏差（"工具数一致"本身与批次是否被预算杀掉相关）。**不可以**拿 −1,222 ms 去写 CV。

### 3.6 限制

1. **n 小**：20 个合格 case、60 组配对。这是规则筛出来的全集，不是抽样——但它就是很小，任何延迟差异都需要很大的效应量才能在 CI 上显现。
2. **阴性对照未通过**（§3.4）：主结果的配对前提被削弱。
3. **warm 缓存协议**让相当一部分工具批次是毫秒级返回（两臂检索阶段 p50 都只有 7–13 ms），真正的网络工作集中在尾部（p95 达 55–73 s）。因此本实验实际上只在少数几次真正的重工具批次上有辨别力。
4. 串行臂是**进程内 monkeypatch 线程池**，不是产品里可配的开关（`FC_TOOL_OFFLOAD_WORKERS` 有 `max(4, n)` 下限），所以这是一个"实验室对照"，不等于生产上关掉并发会发生什么。

### 3.7 结论（第一次设计 / warm 缓存）

- **「并行工具编排降低检索阶段延迟」：证据不足。** 在 20 个合格案例、120 次 live 运行上，检索阶段 mean/p50/p95 与端到端 mean/p50/p95 的 bootstrap 95% CI **全部跨 0** → **未观察到显著差异**。
- **「串并两臂做同样的工作」：不支持。** 60 组配对里 16 组的已执行工具数不同，且方向不对称（并行臂更多，12/16），串行臂多出 40% 的预算超时。
- **「合格案例远多于 16」：不支持。** 全集是 20 例（≥2/3 口径），首轮的 16 例已接近全集——这是本节最可复用的结论。
- 旧的「检索阶段 −57.1% / p95 −42.0%、0/48 无差异」**不能作为当前架构的性质引用**；它来自不同架构、不同串行化机制、不同案例集。

> ⚠️ 本小节的结论受一个已知的测量下限影响（warm 协议下检索阶段 p50 只有 7–13 ms）。
> **§3.8 用冷缓存把这一点单独复核了一遍，两次设计的结果都保留。合并结论见 §3.8 末尾。**

### 3.8 第二次设计：冷缓存复核（两次设计的结果都在这里）

**为什么做，以及为什么这不是"结果不好看就重跑"。**
§3.3 的 warm 结果是一个全线跨 0 的空结果，而它最可能的原因是**测不出来**而不是**没有效应**：
warm 协议下两臂的检索阶段 p50 只有 7–13 ms，绝大多数工具批次在并发能起作用之前就从缓存返回了。
冷缓存让每一次 `search_properties` 都真的去抓，这才是串行/并行可能有差别的条件。
**§3.3 的预注册结果原样保留、不降级、不删除**——无论冷缓存跑出什么。理由与决定都在
PROGRESS.log 2026-08-04 19:27 的条目里，写在结果产生之前。

除缓存协议（warm → cold，每个 run 一份全新的空 listing 缓存）之外，**其余一切相同**：
同样 20 例、同样两臂、同样 3 次重复、同样的 bootstrap（seed `20260804`，2000 次）。
120 run，0 失败。C 的请求用量累计 240/400。
结果文件：`evaluation/results/parallel_tools_ab/cold_shard{0,1}/runs.jsonl`、
`analysis_C_cold.json`、`negative_control_C_cold.json`、`table_C_cold.md`。

**测量灵敏度确实上去了**：检索阶段 p50 从 warm 的 7/13 ms 变成 607/568 ms，mean 从 11.6/10.8 s 变成 19.8/16.4 s。

#### 实验 C 指标表（第二次设计：冷缓存）

来源文件：`evaluation/results/parallel_tools_ab/analysis_C_cold.json`（由 `evaluation/results/_harness/analyze.py` 生成，输入 runs.jsonl 见该文件 `source_files` 字段）

运行总数 120，成功 120，失败 0（失败分布 {'serial_tools': 0, 'parallel_tools': 0}，原因 无）；配对上的 case 数 20 / 出现过的 case 数 20。

| 指标 | serial_tools（串行派发） (对照) | parallel_tools（并发派发） (实验) | 差值 test−base，bootstrap 95% CI | 判读 |
|---|---|---|---|---|
| LLM 调用总数 | 151 | 155 | 2.6% [-5.6%, 12.1%] | 未观察到显著差异 (CI 跨 0) |
| 其中 thinking 模式调用 | 0 | 0 | — | — |
| 其中 deepseek-v4-pro 调用 | 0 | 0 | — | — |
| 绕过 ModelRouter 的调用 (purpose=memory) | 0 | 0 | — | 不受本 A/B 的路由改动影响，见正文 |
| 输入 token | 1,586,217 | 1,629,175 | — | — |
| 输出 token | 38,461 | 40,130 | 4.3% [-7.1%, 18.2%] | 未观察到显著差异 (CI 跨 0) |
| 总 token | 1,624,678 | 1,669,305 | 2.7% [-7.5%, 14.1%] | 未观察到显著差异 (CI 跨 0) |
| 缓存命中 token (provider 侧) | 1,502,976 | 1,547,904 | — | — |
| 成本 USD（固定价表） | 0.0266 | 0.0269 | 1.2% [-13.3%, 16.5%] | 未观察到显著差异 (CI 跨 0) |
| 端到端 mean (ms) | 28,695 | 24,870 | -3,824 ms [-8,167 ms, 29 ms] | 未观察到显著差异 (CI 跨 0) |
| 端到端 p50 (ms) | 7,782 | 7,482 | -300 ms [-6,185 ms, 703 ms] | 未观察到显著差异 (CI 跨 0) |
| 端到端 p95 (ms) | 89,042 | 73,447 | -15,595 ms [-25,309 ms, -2,033 ms] | CI 不跨 0 |
| 检索阶段 mean (ms) | 19,813 | 16,366 | -3,448 ms [-7,868 ms, 333 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 p50 (ms) | 607 | 568 | -39 ms [-3,320 ms, 500 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 p95 (ms) | 76,552 | 56,547 | -20,005 ms [-27,589 ms, 2,449 ms] | 未观察到显著差异 (CI 跨 0) |
| 检索阶段 mean（仅真正执行了工具批次的 run，n=60 / 60） | 19,813 | 16,366 | — | 见正文 |
| 检索阶段 p50（同上子集） | 607 | 568 | — | 见正文 |
| 检索阶段 p95（同上子集） | 76,552 | 56,547 | — | 见正文 |
| 证据支撑率 grounded/verifiable | 270/289 (93.4%) | 281/293 (95.9%) | 2.48 pp [-2.32 pp, 8.24 pp] | 未观察到显著差异 (CI 跨 0) |
| 金额支撑率 money_grounded/money_total | 73/87 (83.9%) | 83/86 (96.5%) | 12.60 pp [0.00 pp, 19.88 pp] | 未观察到显著差异 (CI 跨 0) |
| 与证据矛盾的主张数 (contradicted) | 0 | 0 | — | — |
| 任务完成 task_completed | 60/60 | 60/60 | 0.00 pp [0.00 pp, 0.00 pp] | 未观察到显著差异 (CI 跨 0) |
| 约束全通过 passed | 44/60 | 42/60 | -3.33 pp [-10.00 pp, 5.00 pp] | 未观察到显著差异 (CI 跨 0) |
| 执行的工具调用数 / run（均值差） | 2.97 | 3.28 | 0.23 [-0.08, 0.62] | 未观察到显著差异 (CI 跨 0) |
| 监听缓存命中率 (listing cache) | 2/30 (6.7%) | 0/26 (0.0%) | — | — |
| 外部工具失败率 | 3/178 (1.7%) | 10/197 (5.1%) | — | — |

bootstrap：cluster bootstrap（重采样单位 = case），重采样次数 2000，seed 20260804，配对 case 数 20。

**冷缓存下的读数**

| 指标 | serial → parallel | 95% CI | 判读 |
|---|---|---|---|
| 检索阶段 mean | 19,813 → 16,366 ms | −3,448 ms [−7,868, **+333**] | **跨 0**（贴着 0） |
| 检索阶段 p50 | 607 → 568 ms | −39 ms [−3,320, +500] | 跨 0 |
| 检索阶段 p95 | 76,552 → 56,547 ms | −20,005 ms [−27,589, **+2,449**] | **跨 0**（贴着 0） |
| 端到端 mean | 28,695 → 24,870 ms | −3,824 ms [−8,167, **+29**] | **跨 0**（上界只有 +29 ms） |
| 端到端 **p95** | 89,042 → 73,447 ms | **−15,595 ms [−25,309, −2,033]** | **不跨 0** |

**结论（冷缓存）**：
1. **预注册的主指标（检索阶段 mean / p50 / p95）仍然全部跨 0 → 仍然是"未观察到显著差异"。**
   点估计比 warm 大得多且方向一致（并行更快），但 n=20 例撑不起这个方差。
2. **唯一 CI 不跨 0 的是端到端 p95（−15.6 s）。** 这条可以说，但必须写成"端到端 p95"，
   不能说成"检索阶段"，也不能和检索阶段的数字相加。
   ⚠️ **并且要带上这个附注**：n=60 次运行时，p95 落在分布中相邻两点跳跃很大的位置，
   点估计对"百分位怎么算"敏感。本报告统一用线性插值（`analyze.py::percentile`，
   `pos = q·(n−1)`）；换成最近秩（nearest-rank）法，并行臂的端到端 p95 会从 73,447 ms
   变成 84,052 ms，−15.6 s 会缩到约 −5.0 s。bootstrap CI 在每一次重采样里用的都是**同一个**
   约定、且两臂一致，所以区间本身是自洽的；但这个点估计**不适合当作一个精确数字去引用**。
3. **阴性对照更糟了**：60 组配对中 **18 组**工具数不一致（warm 是 16 组），方向依旧不对称
   （并行更多 12 组 / 串行更多 6 组）。两臂的预算超时差距从 35 vs 25 拉大到 **60 vs 39**，
   soft wrap 从 5 vs 3 拉大到 **10 vs 3**。
   这正好解释了为什么点估计变大而 CI 不收敛：冷缓存下串行臂被预算杀得更狠，两臂做的事差得更多。
4. 事后子集（工具数一致的 42 组配对 / 17 例）：检索阶段 8,248 → 6,179 ms，
   CI [−7,683, −74]，同样贴着 0。**依旧是 post-hoc，依旧不可用于 CV。**

**两次设计放在一起该怎么说**：在当前 fc 架构、20 个合格案例上，
**没有证据支持"并行工具编排显著降低检索阶段延迟"**；有一条较弱的证据支持
"并行降低端到端 p95 尾延迟"（仅冷缓存条件下，CI 不跨 0）。
更稳的发现是**方法层面的**：在 live fc 循环里把派发串行化，本身就会改变被执行的工具集合
（16/60 和 18/60），所以这个 A/B 的配对前提在这个架构上并不成立——
要继续做，得先换一个不改变工具集合的串行化手段。

---

## 4. fact-ledger 补丁段落（可直接粘贴）

> 下面每一行六项齐全：`baseline` / `数据集·split` / `n` / `seed·运行次数` / `live 还是 mock` / `来源文件路径`。
> 状态标记按任务书的四档给出。**本批次不修改 `fact-ledger.md` 本身**，请人工粘贴。

| # | 主张（可写进 ledger 的那一句） | baseline | 数据集 · split | n | seed · 运行次数 | live/mock | 来源文件路径 | 建议状态 |
|---|---|---|---|---|---|---|---|---|
| A-1 | 当前 fc_loop 上，分档路由相对「所有节点 deepseek-v4-pro」降低成本 67.7%（bootstrap 95% CI −70.4%…−65.0%） | 所有节点 deepseek-v4-pro（thinking 关） | `evaluation/benchmark/cases.jsonl`，全部 98 例 | 98 例配对 × 3 重复 × 2 臂 = 588 run（0 失败） | bootstrap seed 20260804，2000 次重采样；每臂 3 次运行 | **live** | `evaluation/results/fc_loop_routing_ab/analysis_A.json` | `[SAFE-WITH-SCOPE]` |
| A-2 | 同一对照下端到端延迟均值降低 5,518 ms（CI −7,428…−3,815），p50 −2,963 ms，p95 −21,604 ms | 同上 | 同上 | 同上 | 同上 | **live** | `evaluation/results/fc_loop_routing_ab/analysis_A.json` | `[SAFE-WITH-SCOPE]` |
| A-3 | 同一对照下证据支撑率未下降：77.0%（843/1095）→ 84.1%（812/965），差 +7.16 pp（CI +1.72…+13.00） | 同上 | 同上 | 同上 | 同上 | **live** | `evaluation/results/fc_loop_routing_ab/analysis_A.json` | `[CAUTION]`（两臂工具失败率不同，见 §1.6 限制 3） |
| A-4 | 金额类支撑率 74.7%（489/655）→ 79.8%（447/560），CI +（−1.06…+11.53）pp **跨 0 → 未观察到显著差异** | 同上 | 同上 | 同上 | 同上 | **live** | `evaluation/results/fc_loop_routing_ab/analysis_A.json` | `[SAFE]`（作为"未观察到差异"引用） |
| A-5 | 把任一节点切到 thinking 档，多约束类请求有 7/11 (63.6%) 概率在轮内 HTTP 400（`reasoning_content` 未回传）；全样本 7/50 = 14.0% | 生产（非 thinking） | `cases.jsonl` 子集（冒烟 5 + 1/4 分片 25 + 多工具 20） | 50 run | 单次运行 / 每例 1 次 | **live** | `evaluation/results/fc_loop_routing_ab/thinking_probe{,2}/runs.jsonl`、`evaluation/results/_smoke/runs.jsonl` | `[SAFE]`（机制结论，非质量结论） |
| A-6 | ~~强模型调用 165/170→78/172（−52.7%）、端到端 −38.4%~~ | — | 2026-07-12 旧架构 | 98 × 1 | 单次运行 | live | `evaluation/results/ablation_model.json` | `[BANNED-FOR-CV]` — 口径依赖已退役的 `deepseek-reasoner` 模型名；当前架构不可复现，见 §1.2 |
| B-1 | ~~同模型两轮自一致性 κ：硬约束 0.750、可执行性 0.735、证据支撑 0.660~~（矛盾主张数 1.000 见 B-3） | 无（一致性指标，不是对照实验） | `cases.jsonl` 分层抽 50 | 50 条 × 2 轮 = 100 次评审 | 抽样 seed 20260804；顺序/提问序 seed 见 `round*.json` | **live** | `evaluation/results/llm_blind_review/agreement.json` | `[BANNED-FOR-CV]` — 2026-08-05 人工抽查（§2.9）认定这三项受 rubric 空白与证据包构造缺陷影响；κ 数值本身正确，但它度量的不是 agent 质量 |
| B-2 | **跨模型**一致率（v4-flash vs v4-pro，n=50）：矛盾主张数 κ=0.793、证据支撑 κ=0.532、可执行性 κ=0.341、硬约束 κ=0.040 | 无 | 同上 | 50 条 × 2 模型 | 同上 | **live** | `evaluation/results/llm_blind_review/agreement.json` | `[SAFE-WITH-SCOPE]` — 同厂商两模型，不是真正的第三方 |
| B-3 | 四个判定项中**只有**「与证据矛盾的主张数」可用：第一次设计三轮 48/50、48/50、47/50 判 0 矛盾，第三次设计 49/50、48/50、47/50，跨模型观察一致率 0.940。**报观察一致率与原始计数，不要报 κ** —— 该项 ~96% 判读落在同一类别，κ 会退化（§2.12.3） | 无 | 同上 | 50 | 同上 | **live** | `evaluation/results/llm_blind_review/{round1,round3_pro}.json`、`evaluation/results/llm_blind_review_v3design_rubric_validation/agreement_v3.json` | `[SAFE-WITH-SCOPE]` — 唯一幸存的一项；必须同时说明另外三项已作废（§2.9） |
| B-7 | **第三次设计（口径验证）**：修 rubric 后 `hard_constraints_satisfied` 的跨模型一致性由 κ=0.040 升至 **0.735**，证实旧值测的是 rubric 空白；同时暴露该项在本抽样上**只有 8/50 例真正可判** | 无（仪器指标，不是产品指标） | 同一 50 例，未重抽 | 50 例 × 3 轮 = 150 次评审，0 失败 | 设计冻结于发请求前；seed 20260805 | **live** | `evaluation/results/llm_blind_review_v3design_rubric_validation/agreement_v3.json` | `[SAFE-WITH-SCOPE]` —— **只能作为量表可用性证据**；held-out 条件未满足，**本行不得引申出任何质量比例** |
| B-8 | **第四次设计（真正的 held-out）**：在 110 例预注册 held-out 基准上，确定性判定 agent 在 **33/33** 道未被提前看过的硬约束检索题里都点名了满足全部已声明条件的那处房源（精确二项 95% CI 89.4%–100%） | 无（这是绝对指标，不是对照实验） | `evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl`，sha256 `0294584c…`，retrieval_hard 分层 35 例中的 33 例（剔除冒烟时看过答案的 HO2-001/HO2-023） | 33 | 生成器无 RNG；bootstrap seed 20260805（在边界退化，故引用 Clopper-Pearson 精确区间）；每例 1 次运行 | **live** | `evaluation/results/holdout_v2_review/analysis_holdout_v2.json` → `deterministic.D6_correct_option_identified.primary` | `[SAFE]` — 唯一同时满足「门禁通过 + 冻结在先 + n≥30 + 确定性」的指标 |
| B-9 | **模型盲审**（三轮、temperature 0、盲化）在同一 110 例上判定「与证据矛盾的主张数 = 0」的比例：R1 **107/110**、R2 **108/109**、R3(pro) **105/110**；跨模型**观察一致率 0.945** | 无 | 同 B-8，全部 110 例 | 110 | 三轮 seed 20260811/20260887/20261365，temperature 0 | **live** | `evaluation/results/holdout_v2_review/{round1,round2,round3_pro}.jsonl`、`analysis_holdout_v2.json` | `[SAFE-WITH-SCOPE]` — 必须写「模型盲审」；**报计数与观察一致率，不报 κ**（该项 ~96% 落在单一类别，κ 退化至 0.226–0.493） |
| B-10 | **agent 侧真实缺陷（held-out 上复现）**：4/16 道通勤题从未调用 `calculate_commute` 却声称已按通勤上限筛选（HO2-024 更断言某房源「well over 25 minutes」）；3/10 道记忆写入题从未调用 `remember`；3 道无结果题补入证据里没有的市场价（HO2-052/053/054） | 无（缺陷清单） | 同 B-8 | 逐例认定 | 每例 1 次运行 | **live** | `evaluation/results/holdout_v2/{runs,grader_input}.jsonl` + 报告 §2B.7 | `[SAFE-WITH-SCOPE]` — 作为**缺陷清单**可用；**不可**换算成任何比率进 CV（n 太小） |
| B-11 | ~~held-out 预注册硬约束满足率 0/119（案例级 0/35）~~ | 无 | 同 B-8 | 119 / 35 | 同上 | live | `analysis_holdout_v2.json` → `deterministic.D1_hard_constraint_satisfaction` | `[BANNED-FOR-CV]` — 谓词在该系统的「符合／已排除」答案格式上退化，把透明的排除清单也判成违规；0/119 的整齐划一即是证据。2026-08-05 07:26 已标记（在数字产生之前），如实上报但禁止引用 |
| B-12 | **schema v2 + 门禁**：七个语义槽位各有独立可机器核验类型（`room_type_match` 拆出 `bedroom_count_match`，新增 `area_match` / `move_in_date_satisfied` / `property_feature_present`），199 项确定性测试 0 失败；110 例 held-out 通过 preflight（gate_passed=true，退出码 0，UNCLASSIFIED 为空），逐题 author audit 110 PASS / 0 REPLACE | 无（仪器能力，不是质量结论） | 同 B-8 | 110 题 / 199 测试 | 无 RNG | 不适用 | `evaluation/results/_harness/{constraint_schema_v2,test_constraint_schema_v2,holdout_preflight}.py`、`evaluation/benchmark/holdout_v2/{preflight_report.json,AUTHOR_AUDIT.md,FREEZE.json}` | `[SAFE]` — 但 author audit **不得**写成 human review |
| B-4 | ~~152/204 grounding 视为答案正确率~~ | — | — | — | — | — | `evaluation/results/live_routed_98/summary.json` | `[BANNED-FOR-CV]` — 那是启发式文本标记评分，既不是正确率，也与本次盲审不可换算 |
| B-5 | ~~证据支撑率 31/50、可执行性 36/50~~ | — | 同上 | 50 | 同上 | live | `evaluation/results/llm_blind_review/round1.json` | `[BANNED-FOR-CV]` — 16/50 条目工具证据为空，其中 12 例的正确行为本就不产生证据（纯计算/澄清/记忆）；13 个"不可执行"里含正确的澄清与记忆任务。见 §2.9 |
| B-6 | 人工抽查在同一批答案中认定的 **agent 侧真实缺陷**：C8 编造 Metrolink 票价/站点/步行时间（工具已明确返回无数据）、E4 声称搜了 3-bed 而证据是 studio、E5 编造安全评分与 POI、B13 价格上限与证据冲突、A2/A3/A10/F6 无结果时补入未支撑的市场价 | 无（缺陷清单，非对照实验） | 上述 50 例抽样 | 逐条人工认定 | 单次人工抽查 | **live** | `evaluation/results/llm_blind_review/{items,round1,round3_pro}.json` + `fc_loop_routing_ab/shard*/grader_input.jsonl` | `[SAFE-WITH-SCOPE]` — 作为**缺陷清单**可用；**不可**换算成任何比率（分母未经系统化认定） |
| C-1 | 98 例中满足「单轮内 ≥2 个互不依赖工具调用」的案例共 **20 个**（3 次重复中 ≥2 次口径；≥1 次 22 个、3 次全中 18 个）；全部 244 个工具批次里只有 65 个（26.6%）装了 ≥2 个调用 | 无（这是总体计数，不是对照） | `evaluation/benchmark/cases.jsonl` 全部 98 例的 routed 运行 | 294 次 routed 运行 / 244 个批次 | 规则冻结于运行前；每例 3 次运行 | **live** | `evaluation/results/parallel_tools_ab/case_selection.json` | `[SAFE]` |
| C-2 | 在这 20 例上，串行 vs 并行工具派发的检索阶段延迟 **未观察到显著差异**（mean −748 ms，95% CI −4,828…+4,053；p50、p95、端到端同样跨 0） | 串行派发（工具线程池 1 worker） | 上述 20 例 | 20 例 × 3 重复 × 2 臂 = 120 run（0 失败） | bootstrap seed 20260804，2000 次；每臂 3 次运行 | **live** | `evaluation/results/parallel_tools_ab/analysis_C.json` | `[SAFE]`（作为负结果引用） |
| C-3 | 该实验的**阴性对照未通过**：60 组配对中 16 组的已执行工具调用数不一致（并行臂更多 12 组），串行臂多出 40% 的预算超时（35 vs 25） | 同上 | 同上 | 60 组配对 | 同上 | **live** | `evaluation/results/parallel_tools_ab/negative_control_C.json` | `[SAFE]`（限制性结论，必须与 C-2 一起引用） |
| C-4 | ~~并行检索让检索阶段均值 −57.1%、p95 −42.0%，48 组配对 0 差异~~ | — | 2026-07-12 首轮，legacy 图，16 例 × 3 | 48 | 单轮 | live | `evaluation/results/ablation_retrieval.json` | `[BANNED-FOR-CV]` 用于描述**当前架构**；作为历史 legacy 结果可标 `[SAFE-WITH-SCOPE]`，但必须写明 legacy 图 + 不同串行化机制 |
| C-5 | **冷缓存**复核（第二次设计，同 20 例）：检索阶段 mean 19,813→16,366 ms，CI −7,868…+333 **仍跨 0**；唯一 CI 不跨 0 的是**端到端 p95** 89,042→73,447 ms（−15,595 ms，CI −25,309…−2,033） | 串行派发（工具线程池 1 worker） | 上述 20 例，冷缓存协议 | 20 例 × 3 重复 × 2 臂 = 120 run（0 失败） | bootstrap seed 20260804，2000 次；每臂 3 次运行 | **live** | `evaluation/results/parallel_tools_ab/analysis_C_cold.json` | `[SAFE-WITH-SCOPE]` — 只能写"端到端 p95"，不可写成检索阶段，不可与检索阶段数字相加 |
| C-6 | 冷缓存下阴性对照更差：18/60 组配对工具数不一致；两臂预算超时 60 vs 39、soft wrap 10 vs 3 | 同 C-5 | 同 C-5 | 60 组配对 | 同 C-5 | **live** | `evaluation/results/parallel_tools_ab/negative_control_C_cold.json` | `[SAFE]`（必须与 C-5 一起引用） |

---

## 5. CV 措辞建议

任务书的禁写/可写对照表在下面逐条落实。**每一条都给出"可以这样写"与"不可以这样写"。**

### 5.1 实验 A（模型分档路由）

**可以这样写（中文）**
> 在 98 例自建基准上做配对 A/B（每臂 3 次运行，共 588 次 live 请求，0 失败）：相对"所有节点使用更强模型 deepseek-v4-pro"的基线，当前分档路由把成本降低 67.7%（bootstrap 95% CI −70.4%…−65.0%），端到端延迟均值降低 5.5 s（CI −7.4…−3.8 s），证据支撑率未见下降（77.0% → 84.1%）。

**可以这样写（English）**
> On a 98-case self-built benchmark, a paired A/B (3 runs per arm, 588 live requests, 0 failures) shows the tiered model routing cuts cost by 67.7% (bootstrap 95% CI −70.4%…−65.0%) and mean end-to-end latency by 5.5 s (CI −7.4…−3.8 s) against an all-strong-model (deepseek-v4-pro) baseline, with no drop in grounding fidelity (77.0% → 84.1%).

**不可以这样写**

| 不可以 | 为什么 | 只能写 |
|---|---|---|
| "显著提升答案准确率 7 个百分点" | 三重问题：`准确率`不是本指标；`显著`需要 CI 支撑；且两臂工具失败率不同（11.8% vs 5.9%），差异不能干净归因 | "证据支撑率 grounding fidelity 从 77.0% 变为 84.1%（CI +1.72…+13.00 pp），但两臂面对的工具证据并不完全相同" |
| "金额准确率提升 5 个百分点" | 金额支撑率的 CI 跨 0 | "金额类证据支撑率未观察到显著差异（+5.16 pp，95% CI −1.06…+11.53 pp）" |
| "端到端提速 46%" 或 "检索提速 57% + 端到端提速 38%" | 阶段不可混用、不可相加 | 分开写："端到端延迟均值 12.0 s → 6.5 s"；"检索阶段均值 2.15 s → 0.89 s"，并注明这是两个不同阶段的数字 |
| "线上 SLA / 生产指标" | 本批次全部跑在离线基准的一次性容器上 | "自建基准上的 live 离线实验结果（生产池未参与）" |
| "强模型调用减少 52.7%" | 该口径来自 2026-07-12 旧架构，且依赖已退役的 `deepseek-reasoner` 名字 | 当前架构写："强模型（deepseek-v4-pro）调用 618/620 → 0/564"，或者干脆不提这一项 |
| "任务完成率 100%" | 该指标在本数据上两臂都是 294/294，已饱和、无信息量 | 不引用；改引用"约束全通过 227/294 vs 228/294" |

### 5.2 实验 B（独立模型盲审）

> ⚠️ 2026-08-05 人工抽查后，本节**只剩一条可以写**（其余三项已作废，见 §2.9）。
> 第三次设计（§2.12）是**口径验证**，其产出只能写成「修好量表后，硬约束判定的跨模型一致性从 κ=0.040 升到 0.735」这类**仪器**表述，
> **不得**引申出任何质量比例，也不得与第一次设计的数字合并。

**可以这样写（中文）**
> 用一个独立模型（deepseek-v4-flash）对 50 例做**盲审**（不告知配置、顺序随机打乱），四个判定项中「与证据矛盾的主张数」在三轮里稳定：三轮分别有 48/50、48/50、47/50 例被判为 0 处与证据矛盾，同模型两轮自一致性 κ=1.000，与另一模型（deepseek-v4-pro）的跨模型一致性 κ=0.793。**其余三个判定项经人工抽查认定受评测设计缺陷影响，未作为质量结论。**

**可以这样写（English）**
> An independent model (deepseek-v4-flash) blind-reviewed 50 cases (configuration withheld, presentation order shuffled). Two independent rounds of the SAME model give a self-consistency kappa of 1.000 / 0.750 / 0.735 / 0.660 across the four judgments. A third round with a different model (deepseek-v4-pro) gives cross-model agreement of kappa 0.793 on "claims contradicting the evidence" but only 0.040 on "hard constraints satisfied" — i.e. the same-model kappa overstates how reliable that judgment is.

**不可以这样写**

| 不可以 | 为什么 | 只能写 |
|---|---|---|
| "人工评审 / 双人盲审 / human evaluation" | 没有任何人参与标注 | "独立模型盲审 / 第三方模型评审" |
| "评审者间信度 κ=0.75" | 那是同一个模型跑两遍 | "同模型两轮自一致性 κ=0.75"，或引用跨模型一致率 |
| "模型评审确认答案正确率 62%" | 既不是正确率，四项里三项跨模型还不稳 | 只引用跨模型稳健的那一项："50 例中 48 例（round1）/47 例（round3）被判定为 0 处与证据矛盾" |
| "在 held-out 集上验证" | 98 例基准全部参与过历史开发 | "在一个模型已见过的自建基准上做的盲审；未参与调参的请求池在本仓库不存在（见 §2.2）" |
| "盲审证据支撑率 62%（31/50）" | 16/50 条目工具证据为空，其中 12 例的正确行为本就不产生证据（纯计算/澄清/记忆），分母无意义 | 不写。重做（§2.11）之前这一项没有可引用形式 |
| "72% 的回答可直接执行（36/50）" | 抽样里混了纯计算、记忆、拒答/澄清任务，"可执行"对它们不适用；13 个 no 里含正确的澄清与记忆 | 不写。重做时按任务类型分开计分 |
| "硬约束满足率 …" | rubric 未定义"本轮无硬约束"，且真正有硬约束的只有 6/50 | 不写。重做后最好也只能是 "n=6，证据不足" |
| 引用 κ 时不带方向 | κ=0.040 与 κ=0.750 是同一批数据的两个读数 | 引用 κ 必须同时给出同模型与跨模型两个数字 |

### 5.2B 实验 B 第四次设计（**真正的 held-out**，2026-08-05）

> 这是本报告里**唯一**可以写成「held-out」的实验 B 结果。§5.2 的三条限制只约束前三次设计。
> 措辞门槛（预注册于 PROGRESS.log 07:20）：门禁通过 + 真正可判 n≥30 + 数据规则跑前冻结 +
> 没有用结果调题或调 rubric + 带原始分子分母与区间 + 写明是自建 held-out 基准上的 live 评测 +
> 模型判读一律写「模型盲审」。

**可以这样写（中文）**

> 自建并预注册了一套 110 例 held-out 租房任务基准（与此前用于调参的 98 例逐字零重合，出题前先完成约束 schema v2 并通过 199 项确定性测试，出题后过静态门禁：配额、槽位覆盖、约束可验证性、正确完成态、证据来源全部通过）。在该基准的 live 评测中，RentCompass 在 33/33 道未被提前查看的硬约束检索任务上确定性地点名了满足全部已声明条件的房源（精确二项 95% CI 89.4%–100%）；评测覆盖预算、卧室数、房型、地点、入住日期、通勤与物业特征七个语义槽位。

**可以这样写（English）**

> Built and pre-registered a 110-case held-out rental-task benchmark with zero verbatim overlap against the 98 cases used during development. A hard-constraint schema v2 (199 deterministic tests, 0 failures) was frozen before authoring, and the set passed a static gate on quota, per-slot coverage, constraint verifiability, stated completion condition and evidence provenance. In a live evaluation on that benchmark, RentCompass deterministically named a listing satisfying every stated condition in 33/33 unseen hard-constraint retrieval tasks (exact binomial 95% CI 89.4%–100%), across budget, bedroom count, property type, area, move-in date, commute and property-feature constraints.

**模型盲审那一条只能这样写（中文）**

> 在同一套 110 例预注册 held-out 基准上，三轮独立**模型盲审**（两轮 deepseek-v4-flash + 一轮 deepseek-v4-pro，temperature 0，配置与轮次标签均对评审模型隐藏）分别判定 107/110、108/109、105/110 个回答**与冻结证据零处矛盾**，跨模型**观察一致率 0.945**。**该结果是模型盲审，不是人工评审，也不是答案正确率。**

**模型盲审那一条只能这样写（English）**

> On the same 110-case pre-registered held-out benchmark, three independent **model blind-review** rounds (two deepseek-v4-flash, one deepseek-v4-pro, temperature 0, configuration and round labels withheld) found zero claims contradicting the frozen evidence in 107/110, 108/109 and 105/110 answers respectively, with a cross-model **observed agreement of 0.945**. This is a model blind review — not human evaluation and not answer accuracy.

**不可以这样写**

| 不可以 | 为什么 | 只能写 |
|---|---|---|
| 「人工盲审 / human evaluation / 双人评审」 | 全程没有任何人参与标注；`AUTHOR_AUDIT.md` 是**出题者自审** | 「模型盲审」「出题者自审（author audit）」 |
| 「评审者间信度 κ=0.75」 | 那是两个模型，且其中两项的 κ 已退化 | 「同模型两轮自一致性 obs 0.945」「跨模型观察一致率 0.873」，并注明 κ 仅作辅助 |
| 「答案正确率 100%」 | 33/33 测的是「**是否点名了满足全部条件的那处房源**」，不是答案整体正确 | 逐字用 §5.2B 给出的句子 |
| 「硬约束满足率 88.9%」 | 三轮可判分母在 34→46 之间变动，口径不稳 | 不写。见 §2B.10 |
| 「证据支撑率 80.4%」 | 跨模型 κ 0.571，pro 的 `partial` 用量是 flash 的三倍 | 不写 |
| 「计算题 100% 正确（20/20）」 | n=20 < 30，且其中 9 道由确定性模块而非模型回答 | 不写进 CV；报告里必须拆成 9/9 模块 + 11/11 模型 |
| 「硬约束满足率 0%」 | D1 的谓词在这套答案格式上退化（§2B.5） | 不写。如实保留在报告里，并写明它测的是「有没有把不合规选项摆出来」的**下界** |
| 只报百分比不报分母 | 门槛第 5 条 | 一律写成 `分子/分母 (百分比) [区间]` |
| 把 bootstrap `[100%, 100%]` 当作区间引用 | 边界上每次重采样都相同，区间退化 | 引用 Clopper-Pearson 精确区间 `[89.4%, 100%]` |
| 把 §2B 的数字与 §2.3–§2.12 的分布或 κ 合并 | 不同数据集、不同 rubric、不同批次 | 分开写，并注明 §2B 是唯一的 held-out 批次 |

### 5.3 实验 C（并行工具编排）

本节是**负结果**，能写进 CV 的其实是「口径」那一条，而不是延迟数字。

**可以这样写（中文）**
> 为「并行工具编排」补齐了此前缺失的选例口径：在 98 例基准的 294 次运行中，按「单轮内存在 ≥2 个互不依赖工具调用」判定，合格案例共 20 个（244 个工具批次里只有 26.6% 装了 ≥2 个调用）。在这 20 例上做了两轮配对 A/B（warm 与 cold 缓存协议各 120 次 live 运行，每臂 3 次）：检索阶段延迟的 bootstrap 95% CI **两轮都跨 0，未观察到显著差异**；冷缓存条件下端到端 p95 降低 15.6 s（CI −25.3…−2.0 s，不跨 0）。同时阴性对照未通过——16/60（warm）与 18/60（cold）组配对的已执行工具数不一致，说明在这个架构里"串行化"本身会改变被执行的工具集合。

**可以这样写（English）**
> Wrote down the selection rule the earlier round never stated: across 294 runs of the 98-case benchmark, 20 cases contain a single-turn batch of >=2 mutually independent tool calls (only 26.6% of the 244 tool batches held >=2 calls). Two paired A/B rounds on those 20 cases (warm and cold cache protocol, 120 live runs each, 3 per arm) show **no significant difference** in retrieval-stage latency (bootstrap 95% CIs cross zero in both), with a −15.6 s reduction in END-TO-END p95 under the cold protocol (CI −25.3…−2.0 s). The negative control does not hold in either round: 16/60 and 18/60 pairs executed a different number of tools.

**不可以这样写**

| 不可以 | 为什么 | 只能写 |
|---|---|---|
| "并行工具编排把检索阶段延迟降低 57.1%" | 那是 2026-07-12 legacy 图 + LangGraph `max_concurrency=1` 的结果；当前架构的两轮重跑都未观察到显著差异 | "legacy 架构上曾测得检索阶段 −57.1%（16 例 × 3）；当前 fc 架构上的两轮重跑（20 例 × 3，warm 与 cold）均未观察到显著差异" |
| "冷缓存下并行把检索阶段降低 3.4 秒" | 检索阶段 mean 的 CI 是 [−7,868, **+333**]，跨 0 | "冷缓存下检索阶段 mean 点估计 −3,448 ms，但 95% CI 跨 0，未观察到显著差异" |
| "并行降低 p95 尾延迟 20 秒" | −20,005 ms 是**检索阶段** p95，其 CI 跨 0；不跨 0 的是**端到端** p95 的 −15,595 ms | "冷缓存条件下端到端 p95 降低 15.6 s（95% CI −25.3…−2.0 s）；检索阶段 p95 的 CI 跨 0" |
| 把 "端到端 p95 −15.6 s" 当精确数字引用 | n=60 时该点估计对百分位算法敏感（换最近秩法会变成约 −5.0 s，见 §3.8 附注） | "冷缓存条件下端到端尾延迟（p95）下降，95% CI 不跨 0；点估计随百分位口径在 −5…−16 s 之间，故只声明方向与显著性，不声明精确幅度" |
| "并行化让检索快 1.2 秒" | −1,222 ms 来自**事后**的子集（工具数一致的 44 组配对），CI 上界只有 −22 ms，且该子集存在选择偏差 | 不写；若一定要提，必须标注为 post-hoc 子集 + CI 贴 0 |
| "并行化让系统快 X%" | 阶段不可混用 | "**检索阶段**延迟 …（端到端另行给出，两者不相加）" |
| "n=16 是抽样" | 事实相反 | "全部 98 例里满足『单轮内 ≥2 个可并行工具调用』的案例共 20 个（≥2/3 重复口径）；首轮的 16 例已接近全集" |
| "并行不影响结果（0/48 无差异）" | 当前架构下是 16/60 有差异 | "在 legacy 架构上曾观察到 0/48 差异；当前 fc 架构上 16/60 配对的已执行工具数不一致，前提不成立" |

---

## 6. 全批次汇总、未完成清单

### 6.1 请求与失败计数

| 批次 | 案例回合数（agent run） | 失败 | LLM 调用 | 成本 USD | 来源文件 |
|---|---|---|---|---|---|
| 冒烟（第一次设计，all-thinking + routed） | 10 | 1 | 23 | 0.0133 | `evaluation/results/_smoke/runs.jsonl` |
| v4-pro 对照臂验证探针 | 4 | 0 | 10 | 0.0133 | `evaluation/results/_smoke_pro/runs.jsonl` |
| 实验 A shard0 | 294 | 0 | 581 | 0.2506 | `evaluation/results/fc_loop_routing_ab/shard0/runs.jsonl` |
| 实验 A shard1 | 294 | 0 | 603 | 0.2897 | `evaluation/results/fc_loop_routing_ab/shard1/runs.jsonl` |
| all-thinking 探针 #1 | 25 | 0 | 51 | 0.0269 | `evaluation/results/fc_loop_routing_ab/thinking_probe/runs.jsonl` |
| all-thinking 探针 #2 | 20 | 6 | 41 | 0.0190 | `evaluation/results/fc_loop_routing_ab/thinking_probe2/runs.jsonl` |
| 实验 C shard0 | 60 | 0 | 157 | 0.0301 | `evaluation/results/parallel_tools_ab/shard0/runs.jsonl` |
| 实验 C shard1 | 60 | 0 | 142 | 0.0234 | `evaluation/results/parallel_tools_ab/shard1/runs.jsonl` |
| 实验 C 冷缓存 shard0（第二次设计） | 60 | 0 | 157 | 0.0294 | `evaluation/results/parallel_tools_ab/cold_shard0/runs.jsonl` |
| 实验 C 冷缓存 shard1（第二次设计） | 60 | 0 | 149 | 0.0242 | `evaluation/results/parallel_tools_ab/cold_shard1/runs.jsonl` |
| **agent run 小计** | **887** | **7** | **1,914** | **0.7200** | |
| 实验 B 评审请求（3 轮 × 50） | 150 | 0 | 150 | 未单独计价 | `evaluation/results/llm_blind_review/round{1,2,3_pro}.json` |
| 实验 B 第三次设计评审请求（3 轮 × 50） | 150 | 0 | 150 | 未单独计价 | `evaluation/results/llm_blind_review_v3design_rubric_validation/round*.json` |
| **held-out v2 agent 回合（2026-08-05 新批次）** | **110** | **0** | 208 | **0.0985** | `evaluation/results/holdout_v2/runs.jsonl` |
| **held-out v2 盲审请求（3 轮 × 110）** | **330** | **0** | 330 | 未单独计价 | `evaluation/results/holdout_v2_review/round{1,2,3_pro}.jsonl` |
| **全批次合计** | **1,037 次带模型的请求** | **7** | | | |

**全部 7 次失败都是同一件事**：all-thinking 探针臂的 HTTP 400（`reasoning_content` 未回传），集中在 `E_multi_constraint`。三个正式实验（A / B / C）**各自 0 失败、0 超时**。

各实验的请求数都在任务书上限内：A 用了 588 / 700，B 用了 150 / 400，C 用了 240 / 400（warm 120 + cold 120）。
另有 59 次（冒烟 + 两次探针 + pro 验证）计入 A 的机制探针预算。

### 6.2 未完成 / 受限清单

| # | 项 | 状态 | 原因 |
|---|---|---|---|
| 0 | **实验 B 的三项质量指标整体作废** | **不可用，需重做** | 2026-08-05 人工抽查（§2.9）：评测包 16/50 证据为空（12 例的正确行为本就不产生证据）、rubric 缺 `not_applicable`/`cannot_assess`、抽样混了四类任务。`contradicted_claim_count` 一项幸存。重做要求见 §2.11 |
| 1 | 实验 B 的「未参与调参的请求池」 | **未满足** | 本仓库不存在这样的池子；98 例基准全部参与过历史开发。唯一的替代池（`.runtime/conversations.sqlite3` 的真实用户会话）涉及真实用户数据，不适合进入可归档报告。已在 §2.2 明写，抽样的 base45/ext53 构成也已披露 |
| 2 | 实验 B 的人工校准 | **未做（按任务书要求不等待）** | `human_calibration_sheet.md` 已生成 15 例待标注；所有模型判读一律标注「待人工校准」 |
| 3 | 第一次设计（all-thinking 基线）的完整 98 例 A/B | **放弃** | 该臂在 `E_multi_constraint` 上有 63.6% 的 HTTP 400，掉线是臂特异且集中在最难类别，配对设计会被系统性偏差破坏。改跑 50 次探针给出分母，见 §1.5 |
| 4 | 实验 C 的检索阶段延迟结论 | **证据不足（负结果，两轮一致）** | warm 与 cold 两次设计下检索阶段 CI 都跨 0。n 只有 20 例 / 每轮 60 组配对——这是选例规则筛出的**全集**，不是抽样不足，所以不能靠"多抽一点"解决。冷缓存下唯一 CI 不跨 0 的是端到端 p95（§3.8） |
| 5 | 实验 C 的阴性对照 | **未通过（两轮都未通过）** | warm 16/60、cold 18/60 组配对的已执行工具数不一致，方向不对称。见 §3.4 与 §3.8 |
| 6 | 冷启动（cold cache）下的延迟 | **实验 C 已补测（§3.8）；实验 A 未测** | 实验 C 用冷缓存完整复核了一轮（120 run）。实验 A 仍只有 warm 协议的数字——A 的 588 次运行改冷缓存需要数小时的真实抓取，本批次未做 |
| 6b | 实验 B 重做要求第 1–4 条 | **已完成（第三次设计，§2.12）** | 口径验证已跑，150 次请求 0 失败；`hard_constraints` κ 0.040→0.735，但 `claims_evidence_supported` 跨模型反而降到 0.427、非检索类的 `directly_actionable` κ 为负。仪器已固化在 `_harness/blind_review_v2.py` |
| 6c | 实验 B 重做要求第 5 条（held-out 集） | ✅ **已满足（2026-08-05，§2B）** — 下面这一整段是 2026-08-04 当时的状态，保留为历史记录 | 本仓库不存在未参与开发的请求池；须先构造。**在此之前 §2.12 的任何比例都不得引用**。构造规范见 **§2.13**（按每项可判性配额，建议 110 例；照搬现有构成需约 188 例才能让 `hard_constraints_satisfied` 达到 n=30） |
| 7 | 真正第三方（非 DeepSeek）的评审模型 | **未测** | 本仓库只配置了 DeepSeek 凭证；跨模型一致率用的是同厂商的 v4-pro |
| 8 | `evaluation/report.py` 的 REPORT.md / CV_METRICS.md 再生成 | **未做** | 那两个文件由旧的 `ablation_*.json`（legacy 架构）驱动，重新生成会把本批次的结论与旧架构数字混在一起。本报告独立成篇；`CV_METRICS.md` 的更新建议见 §4 与 §7 |

### 6.3 收尾时的仓库状态

- `git rev-parse HEAD` = `0952c56e21b9b0dac3fb10fe99ee907c36b3a2d8`（全程未变）
- 本批次**新增**：`evaluation/results/**` 下的结果与工具（`_harness/`、`fc_loop_routing_ab/`、`llm_blind_review/`、`parallel_tools_ab/`、`_smoke*/`、本报告）与仓库根的 `PROGRESS.log`
- **未修改**任何 `app/**`、`deploy/**`、`.tex`、`fact-ledger.md`、`evaluation/` 下的既有代码或配置
- 生产容器 `uk-rent-app`(:5001)、`uk-rent-app-fc`(:5002)、`uk-rent-searxng`、`uk-rent-valkey` 全程未重启、未切池

**⚠️ 归档前必须注意：原始证据被 .gitignore 排除。**
`.gitignore:135` 是 `evaluation/results/**/*.jsonl`，`.gitignore:51` 是 `*.log`。
因此下列文件**在磁盘上存在、但 `git add` 不会带上**：

- 每次运行的原始记录：`*/runs.jsonl`（887 条）
- 冻结的工具证据：`fc_loop_routing_ab/shard{0,1}/grader_input.jsonl`
- 采集事件：`*/events_shard*.jsonl`
- 时间线：`PROGRESS.log`

派生产物（`analysis_A.json`、`analysis_C.json`、`analysis_C_cold.json`、`negative_control_C.json`、
`negative_control_C_cold.json`、`case_selection.json`、`table_*.md`、`llm_blind_review/*.json`、
本报告）不受影响，可以正常提交。
要归档原始证据，需要 `git add -f <path>`，或由人决定是否调整 `.gitignore`——
**本批次不修改 `.gitignore`**（它不在任务书允许写入的路径内）。

---

## 7. 次日待办（需要人来做的事）

1. ✅ **已完成（2026-08-05，见 §2B）**：held-out 集已构造、冻结、过门禁并跑完，实验 B 的最后一个阻塞项解除。以下为当时写的规格，保留备查。 **构造 held-out 集**（实验 B 重做的唯一剩余阻塞项）。第 1–4 条已在第三次设计中完成并验证（§2.12），仪器固化在 `_harness/blind_review_v2.py`，held-out 集就绪后原样复用即可。
   **按 §2.13 的配额规范出题：按「每个判定项可判」配额，不再只按七大类别分层。** 照搬现有构成会让 `hard_constraints_satisfied` 需要约 188 道题才够 n=30；按建议配额（110 例）四项可同时过 30。可判性在出题阶段即可预测（静态属性与实判一致 44/50）。
   抽样前须一并冻结三件事：配额、标签词表（含 §2.12.4 缺的 `partial` 判据与非检索类的完成定义）、每类任务的「正确行为」定义。
   **出题完成后、发起任何模型请求之前，先跑 §2.13.1 的 preflight 门禁**
   （`python evaluation/results/_harness/holdout_preflight.py --cases <holdout.jsonl> --out <report.json> --checklist <checklist.md>`，退出码 1 = 未通过），
   再按生成的清单做人工抽查。**未通过的题替换掉，不允许在跑完之后用 `not_applicable` 消化。**
   门禁已在现有 98 例基准上冒烟验证会拦（98/98 未通过：无 `correct_completion` 字段 98 例、无 fixture 20 例）。
2. **修 §2.10 列出的 agent 侧真实缺陷**：C8（工具明确无数据仍编造 Metrolink 票价/站点/步行时间）、E4（证据是 studio 却称搜了 3-bed）、E5（编造安全评分与 POI）、B13（价格上限与证据冲突）、A2/A3/A10/F6（无结果时补入未支撑的市场价）。这些与评测缺陷无关，是产品问题。
3. **标注人工校准集**：`evaluation/results/llm_blind_review/human_calibration_sheet.md`（15 例 × 4 项）。注意重做 rubric 后这份表需要同步更新标签词表。
4. **把 §4 的 ledger 行粘进 `fact-ledger.md`**，并把标 `[BANNED-FOR-CV]` 的两条（旧 −52.7% / −38.4%，以及把 152/204 当正确率的写法）在 ledger 里显式作废。
5. **修 `evaluation/run_ablation.py`**：加 `--arch`，否则它永远只测 legacy 图；同时 `_is_strong` 的按名判定在 v4 模型线上已经失效（chat 与 reasoner 同名）。这是本批次必须绕开它的原因。
6. **产品侧决定**：`core.agent_loop` 的消息构造不携带 `reasoning_content`。今天生产不触发（热路径恒为非 thinking），但只要有人给任一节点打开 thinking 档，多约束类请求就有约 6 成概率轮内 400。要么支持回传，要么在路由层显式禁止 fc 路径进入 thinking 档。
7. **实验 A 的冷启动复核**：实验 C 的冷缓存复核已在本批次完成（§3.8），实验 A 仍只有 warm 协议下的延迟数字。A 的成本/调用量结论不受缓存协议影响，但延迟数字是"快照相对"的。
8. **决定原始证据要不要进版本库**（§6.3）：`runs.jsonl` / `grader_input.jsonl` / `PROGRESS.log` 目前被 `.gitignore` 排除。要么 `git add -f`，要么给 `evaluation/results/` 下的归档目录开白名单——本批次没有动 `.gitignore`。
10. **修 §2B.7 列出的 held-out 侧真实缺陷**：(a) 4/16 道通勤题声称按通勤上限筛选却从未调用 `calculate_commute`（HO2-024 更凭空断言「well over 25 minutes」）；(b) 3/10 道记忆写入题从未调用 `remember`，用户的保存指令没有被持久化；(c) 无结果回合补入证据里没有的市场价（HO2-052/053/054）；(d) HO2-063 把 README 明令禁止的 ÷4.33 口径当作可选方案给出。
11. **给「答案是否只把合规选项当作匹配推荐」补一个确定性口径**。§2B.5 的 D1 在「符合／已排除」这种答案格式上退化到 0/119，本批次因此只有模型盲审能回答这个问题。可行方向：让产品输出一段**结构化的推荐清单**（而不是只有自然语言），评测就能对着那段结构评分，不必去解析标题。
12. **修 fixture 重放的归属**：`run_benchmark.load_fixture_queue` 按工具名排队、按调用顺序出队，深工具循环里 agent 拿到的记录会与它当时问的房源错位（§2B.7 缺陷 6 的注）。确定性评分按 `origin_uid` 归属，不受影响，但 agent 被迫在答案里做对账。
9. 若要继续追实验 C，**先解决阴性对照**（§3.4）：在 fc 循环里，把派发串行化本身会改变被执行的工具集合，需要一个不改变工具集合的串行化手段（例如在固定证据回放下比较），否则"同输入同证据"的前提无法成立。
