# 实验总索引

_更新于 2026-09-01（第五节遥测口径 + PR #83 三轮评审后的复算数字）；上一次内容更新 2026-08-31。_

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
4. **线上跑的是 `uk-rent-agent:canary-fc-loop-4171d84`**（root `.env` 的 `FC_CANARY_IMAGE`，与 `/etc/rentcompass/deploy.env` 的 `DEPLOY_PINNED_SHA=4171d84778be…` 一致，2026-09-01 核对）。本文件此前写的 `0952c56` 是 2026-07 的旧 pin，已过时。**PR #83 的 `manager_v1`／specialist 工作全部未合并、未部署**——合并 ≠ 部署，部署前先 `docker inspect --format '{{.Config.Image}}'`。

## 五、遥测口径变更（引用跨期数字前必读）

### 2026-08-31 — canary telemetry schema **v2 → v3**：`llm_calls` / `tool_batches` 语义已改

`app/core/canary_telemetry.py::SCHEMA_VERSION` 由 `2` 升到 `3`。升版本的原因不是加了字段，
而是**两个既有字段被就地改了含义**——加字段不需要版本，改含义必须有版本，否则跨越变更点的
窗口会把两种不同的测量平均成一个谁也不描述的数。

| 字段 | v2 口径 | v3 口径 |
|---|---|---|
| `llm_calls` | fc 上 = `final_state["loop_turn"]`（agent 超步数），**legacy 上恒为 `null`** | 所有架构上 = observer 统计的**计费 provider 调用数**；`loop_turn` 仅作为 FC 的 fail-closed 下界。**并且从 v3 起包含 `llm_interface._call_deepseek` 发起的嵌套工具内部调用**（一次 `search_properties` 回合约多 1 次），这些调用一直在计费、一直没被计数 |
| `tool_batches` | legacy 上 `null`，fc 上 = 不同 artifact turn 数 | artifact turn 数 + legacy execution-plan wave；两条臂上都是真实数字。`search_direct` 同样报观测到的 `0` 而不是 `null` |

**因此：**

1. **v2 与 v3 的 `llm_calls`／`tool_batches` 不可比。**
   跨 2026-08-31 变更点的窗口不能对这两个字段做均值、差值或 A/B 比较。
   `scripts/canary_report.py::validate_records` 现在会对混版本窗口直接 HOLD
   （`window mixes telemetry_schema_version [2, 3]`），并在
   `aggregate_arch` 输出 `schema_versions` 供人工确认。
2. **v3 的 `llm_calls` 会比 v2 的同类数字大**，尤其在会调用 `search_properties` 的回合上。
   这是口径修正而不是回归，任何"每回合 LLM 调用数上升"的结论必须先分版本再比。
   历史基线（例如 #1 的 07-25 对照、#3 实验 D 的 `llm_calls −0.45`）都是 **v2 口径**。
3. **历史记录不会被追溯判违规。** `scripts/canary_report.py::validate_record` 按记录**自身**的
   `telemetry_schema_version` 选规则：v≤2 用当时生效的契约，v3 才要求
   `llm_calls`/`tool_batches` 非空并与 `llm_usage.calls` 对账。
   用旧/新校验器回放真实日志的结果见下表。`OLD` = `git show 4171d84:scripts/canary_report.py`
   （`SUPPORTED_SCHEMA_VERSIONS = (2,)`；**按 sha 引用，不要写 `HEAD`** —— 本分支合并后
   `HEAD` 就是新校验器，那条指令会自我反转），`NEW` = 当前树。复算方式：把两份
   `canary_report.py` 分别 `importlib` 加载，对每条解析出的记录调用各自的
   `validate_record`，比较两个违规集合（`newly_broken = NEW - OLD`）。这两个是活日志，
   条数会涨，复算前先记条数。

   **下表的"清理前"行是快照（2026-08-31T11:52 UTC），"清理后"行是 owner 清理后的当前实测
   （2026-09-01 复算，同一方法）：**

   | 日志 | 时点 | records | v3 | violating_OLD | violating_NEW | newly_broken |
   |---|---|---|---|---|---|---|
   | `canary-fc_loop.jsonl` | 清理前/后（未变） | 231 | 0 | 0 | 0 | 0 |
   | `canary-legacy.jsonl` | **清理前**（现存于 `.bak-20260831`） | 2973 | 69 | 726 | 682 | 0 |
   | `canary-legacy.jsonl` | **清理后**（当前活日志） | 2904 | 0 | 657 | 657 | 0 |

   清理前 `OLD > NEW` 的 44 条差额全部来自那 69 条泄漏的 v3 记录：旧校验器把它们一律判成
   unknown-schema 违规（69/69），新校验器按 v3 规则只判其中 25 条，69 − 25 = 44。
   （此前正文写"42 条差额"与自己的表不符，已改正。）

   **这些泄漏记录已于 2026-08-31 由 owner 从活日志中清除，当前 `canary-legacy.jsonl` 内
   v3 记录为 0。** 因此今天照上述方法重跑会得到 `OLD == NEW == 657`——**这不反驳本节**：
   两个校验器只在 v3 记录上可能分歧，没有 v3 记录时相等才是预期结果。要复现有分歧的那张表，
   必须对着 `.runtime/logs/canary-legacy.jsonl.bak-20260831` 跑（该备份仍持有那 69 条，
   且它不在门禁的输入后缀白名单内，见下）。

   修复前同一回放为 fc `0 → 81`、legacy `2840 → 684`（审计当时是 `590 → 2643`／2748 条），
   即消费端曾在给一份写入时并不存在的契约追溯定罪。

   **备份文件不要留在 `.runtime/logs/` 里。** `scripts/canary_report.py::resolve_inputs`
   现在把 `.jsonl`/`.log`/`.ndjson` 后缀白名单同时施加于目录遍历**和 glob 展开**
   （`LOG_SUFFIXES`），所以 `.bak-20260831` 不会被门禁读进去；但这是安全带不是归档策略，
   备份应当移出该目录。

**v3 新增（纯增量，旧消费者可忽略）：** `variant_id`、rollout 身份块
（`rollout_id`/`rollout_stage`/`configured_candidate_percent`/`traffic_source`/`assigned_pool`）、
`agent_role`/`task_id`/`parent_task_id`、`tool_latency`（每工具
`count`/`p50_ms`/`max_ms`/`timed_out`/`abandoned`，content-free、不参与门禁）、
`llm_observer_installed`（bool，LangChain **回调**观察器在该回合是否挂载；见下第 5 条）、
以及 `specialist` 块的 `partial` / `denied_calls` / `dropped_error_codes` 计数器。

> **更正（2026-09-01）：`root_agent_context` 不是记录字段。** 此前本节把它列为 v3 新增字段，
> 但 `canary_telemetry.build_canary_turn_record` 从不写出这个键——它只把该上下文**摊平**成
> `agent_role` / `task_id` / `parent_task_id` 三个顶层字段。`root_agent_context` 只存在于
> 进程内的 `turn_observations` 累加器里。错的是文档，不是生产端。

**命名说明（2026-08-31，v3 内部改名）：** v3 的 specialist 生命周期诊断块名为
`specialist`。早期 v3 草稿用的是一个宣称多智能体架构的旧名——specialist 不发起自己的
模型调用、共享 manager 的上下文，该名字名不副实，故在 v3 尚无生产记录时就地改名，不留
兼容负担。生产端只写 `specialist`；消费端（`canary_report.specialist_block`）仍把旧键
当别名读取（旧键名见 `canary_report.LEGACY_SPECIALIST_BLOCK_KEY`），使改名前遗留的零星
记录被解读而非判为违规，同时携带两个键属违规。报告字段相应为 `specialist_turns`。

4. **崩溃回合按可观测性豁免（2026-08-31 二轮修）。** `tool_batches` 派生自 `final_state`，
   而崩溃回合按定义没有 `final_state`，也没有任何带外累加器能补上它——于是 v3 一旦要求它非空，
   **每一条 crash/5xx 记录都必然违规**：真实 `canary-legacy.jsonl` 里 v3 崩溃记录 11/11 全违规，
   而 crash+server_error 占该日志历史的约 14%，等于给任何含崩溃回合的窗口一个永久
   INSTRUMENTATION-HOLD，且 HOLD 的理由与候选质量无关。现在：

   - `validate_record` 的 v3 必填只对 `turn_outcome ∉ {crash, server_error}` 生效；
   - 生产端新增 `tool_ledger_status`（`complete` / `unavailable`），**双向校验**：
     `unavailable` 只允许出现在崩溃类 outcome 上（否则健康回合可以自行豁免工具开销统计），
     且必须伴随 `tool_batches: null`（否则标记与数值自相矛盾）；
   - 该字段**缺省时整个 key 不写**，所以既有的 69 条 v3 记录不会被一条它们写入时不存在的
     规则追溯定罪，消费端退回按 outcome 判定；
   - `aggregate_arch` 增加 `tool_batches_observed_turns` 作为 `tool_batches_total` 的分母，
     崩溃回合不再以 0 混进"每回合工具开销"。

   崩溃回合**仍然 HOLD**，理由是它真正没观测到的东西（`security.*` 为 null、
   `llm_usage_status=not_instrumented`），不再是一个它永远不可能有的字段。

5. **"无法测量" ≠ "没装仪表"（2026-09-01 三轮修，owner 裁定）。** 生产端不再把
   `no_llm_calls` 原样透传到不可观测的回合上：崩溃 / 超时 / 取消 / 5xx 且无任何调用完成时，
   `canary_telemetry.unknown_turn_signals` 把状态改写为 `partial`——**一个被杀掉的回合可能
   已经计费，它没有资格声称零花费**。同时新增 `llm_observer_installed`（只反映 LangChain
   回调观察器；一次裸 SDK 调用不再把整个进程标记为"已挂载"）。消费端按四格判定：

   | outcome | 回调观察器 | `llm_usage_status` | `canary_report` | `canary_cost` |
   |---|---|---|---|---|
   | `crash`/`server_error` | true | `partial` | 不违规（不可测量） | 计费、未测量、unobservable |
   | `crash`/`server_error` | false / 缺失 | `not_instrumented` | **违规 → HOLD** | 计费、未测量 |
   | `ok` | true | `no_llm_calls` | 不违规（可证明的零） | 不计费、已测量的零 |
   | `ok` | false | `not_instrumented` | **违规 → HOLD** | 计费、未测量 |

   被豁免的那一格不是隐形的：`aggregate_arch` 输出 `unmeasured_spend_turns` 与
   `unobservable_unmeasured_turns`，报告打印 `unmeasured spend turns` 与
   `of which unobservable`；`canary_cost` 仍把该回合算作**计费且未测量**，永远不按零计价。
   **回放中性**：现存日志里没有任何记录带 `llm_observer_installed`，所以没有一条旧记录能
   拿到这个豁免（上表 `newly_broken = 0` 已含此项）。取消的回合现在也会落记录
   （`turn_outcome: crash`、`http_status: 499`），不再静默缩小分母。

### 2026-08-31 — 离线配对门全量 98 例：**HOLD，不可作为质量证据引用**

产物：会话级临时目录下的 `paired_report.json`（`--out <dir>/paired_report.json`）。
**这份产物没有进仓库，对其他人不存在**；要复现请自行重跑
`python -m evaluation.run_paired_manager_eval --out <new-empty-dir>` 并读它自己的报告。

结论是 **HOLD**，原因不是两臂打平，而是**这一轮根本没有测到差异**：
`final_answer` 在 **98/98 对上逐字节相同**，因此 5 项质量 check 全部判为 **VACUOUS**
（判据恒为真，不携带信息）。一个恒真的 check 通过率是 100%，但那个 100% 不是证据。

**引用规则：** 本轮**不能**用于支持任何"候选臂质量不劣于/优于对照臂"的说法，
也不能用来反驳。字节相同的两臂说明两侧跑的是同一条产出路径（或同一份缓存），
先修复配对装置本身，再重跑，才谈得上结论。

## 六、复现须知（踩过的坑）

- **Python**：`/tmp/rentcompass-venv/bin/python`（3.12.3）。**本机没有 conda**。
- **凭证**：`app/.env` 的 `DEEPSEEK_API_KEY`。从别处的干净检出跑时该文件不在，会静默降级成占位 key 然后 HTTP 401——v6 首次运行就是这么中止的。**开批前先用一次廉价调用断言认证**。
- **测试**：`.runtime/logs/canary-*.jsonl` 是 root 属主（容器写的）。历史上不导
  `CANARY_LOG_PATH` 会有约 29 个测试假红（旧基线 **29 failed / 3364 passed**，真实产品失败
  0 个）——**该条件现已由 `tests/conftest.py` 的
  `os.environ.setdefault("CANARY_LOG_PATH", "off")` 从源头消除**，不必再手动导。
  2026-08-31 在 `e5a5ca7` 上实测 **4438 passed / 3 skipped / 21 deselected，177s**；
  2026-09-01 三轮修复后实测 **4581 passed / 3 skipped / 21 deselected，218s**
  （3 个 skip 是 env 门控的 live 测试；21 个 deselect 是整个
  `tests/test_monitor_install_provenance.py`，本机 monitor 安装漂移）。`pyproject.toml` 的
  `testpaths = ["tests", "tests_refactor"]`，所以只跑 `pytest tests/` 会少约 49 条。
- **测试曾污染生产遥测（2026-08-31 已修）**：`tests/test_canary_rollout.py::_restore_canary_sink`
  teardown 里做的是 `delenv("CANARY_LOG_PATH")` 再 `_wire_canary_sink()`。变量**未设**时
  `_wire_canary_sink` 走的是「默认启用」分支，把 handler 指向真实的
  `.runtime/logs/canary-<arch>.jsonl`——于是同一 pytest 进程里此后每个会发 canary 记录的测试
  都在往生产遥测里追加。`canary-legacy.jsonl` 里因此混入了 **69 条 `telemetry_schema_version: 3`、
  `candidate_sha=7db03e7` 的测试记录**（ts 2026-08-31T04:2x–04:3x，`agent_arch` 甚至是 fc_loop）。
  已改为恢复 conftest 的会话默认值 `off`，并加了守卫测试
  `test_the_sink_never_defaults_onto_the_production_log_during_tests`。
  **这 69 条已于 2026-08-31 由 owner 从活日志中删除**（2973 → 2904 行，当前 v3 记录 0 条），
  所以**不需要**再按 `telemetry_schema_version <= 2` 过滤——已经没有可过滤的东西了。
  它们仍存在于 `.runtime/logs/canary-legacy.jsonl.bak-20260831`，那是复现第五节回放表的唯一途径。
- **生产日志里还有更早的泄漏测试记录（未清理，`origin/main` 上同样存在）**：
  `canary-legacy.jsonl` 里有 `candidate_sha` 为 `shaLEGACY`（93 条，其中 32 条无
  `telemetry_schema_version`）/ `shaSER`（28 条，全部 v2）的行，以及 825 条
  `agent_arch: fc_loop` 的记录混在 legacy 日志里。2026-09-01 实测：shaLEGACY 32 条违规、
  shaSER 28 条全部违规（`agent_arch=fc_loop but strict is not true`）。因此
  `deploy/run_canary_gate.sh --input .runtime/logs/` 今天就会判
  **CANARY-BLOCK（exit 3）**，用新旧校验器都一样。这不是本轮引入的，跑真门禁前先知道。
- **一手来源**：所有结论对着源码与原始 run 记录核，**不要对着摘要或更早的报告核**。引用按符号名，不要按行号。
- **禁 `git stash`**：`refs/stash` 在本机是跨 worktree 的单一全局栈，历史上因此丢过工作。
