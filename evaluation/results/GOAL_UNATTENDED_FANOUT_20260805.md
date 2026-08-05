# 启动上下文（由启动方写入，非 GOAL 的一部分）

- 仓库：`/home/shuhan/uk_rent_recommendation`
- 分支：`evaluation/holdout-v3`
- 启动时 HEAD：`778714a72322a13808986a40d6f6f6c46948e5fb`（== `origin/main`，工作树干净）
- 启动时间：2026-08-05 18:40 BST
- **报告文件名固定为** `evaluation/results/FANOUT_AB_REPORT_20260805.md`（用启动日期，跑到次日凌晨也不改名）
- 「到 07:00 停止新请求」指 **2026-08-06 07:00 BST**
- **本文件即你的任务书。上下文被压缩后，重新读这个文件继续执行，不要凭记忆推进。**
- 你不会得到任何人的回复。所有岔路按第 4 节「自主决策规则」自己决定并记录。

---

# GOAL

两个阶段：**先修两个已定位的确定性缺陷（有现成的红测试作为 spec），再设计并跑完一组新的配对 A/B 实验（实验 D：维度 fan-out / 批次打包）**，产出可归档的证据报告。

**Phase 2 不得被 Phase 1 阻塞。** 若 Phase 1 任一项卡住超过 90 分钟，记录原因、回滚该项、直接进入 Phase 2。

## 完成定义（同时满足才算完成）

1. `evaluation/results/FANOUT_AB_REPORT_20260805.md` 存在，包含 Phase 1 与 Phase 2 两节。
2. Phase 2 要么有完整结果（阴性对照 + 主/次指标 + bootstrap CI + 分母），要么有明确的「未完成 + 原因」。
3. 报告中不出现任何没有来源文件路径的数字。
4. 产出可直接粘贴的 CV 条目建议（含 `[SAFE]` / `[SAFE-WITH-SCOPE]` / `[AVOID]` 标签与 required caveat）。
5. `PROGRESS.log` 记录完整时间线。

---

## 0. 硬边界（越界即停）

- **允许写入**：`evaluation/**`、`tests/**`、`PROGRESS.log`，以及 `app/` 下**仅限第 3 节 Phase 1 点名的两个文件**。
- **禁止修改**：`deploy/**`、任何 `.tex`、`fact-ledger.md`、`evaluation/results/CV_METRICS.md`。CV 条目只写进报告里**供人工粘贴**，不要自己动 `CV_METRICS.md`。
- **禁止 push**。只在本地新分支 `experiment/fanout-ab-20260805` 上提交。不要动 `main`、不要开 PR。
- **禁止部署、禁止重启生产服务、禁止切换发布池**（`deploy/switch_pool.sh`、`deploy/release.sh`、`deploy/update.sh` 一律不要碰）。线上 `rentcompass.co.uk` 正在对外服务。
- **评测必须跑在非服务实例上**：独立进程跑 benchmark，不要把请求打到正在服务的池子。
- **绝不把 API key / token / 密码写进任何日志、报告或提交**。提到配置时只写变量名。
- **绝不用 `git stash`**：`refs/stash` 在本机是跨 worktree 的单一全局栈，历史上因此丢过工作。要暂存就用新分支或新提交。

## 1. 环境与凭证

- Python 用 `/tmp/rentcompass-venv/bin/python`（普通 venv，3.12.3，满足 pyproject `>=3.10,<3.13`）。pytest 在 `/tmp/rentcompass-venv/bin/pytest`。**本机没有 conda**，不要去找 `uk_rent_recommendation` 这个 conda env，它不存在。
- DeepSeek 凭证在 `app/.env` 的 `DEEPSEEK_API_KEY`（`DEEPSEEK_BASE_URL=https://api.deepseek.com`，`DEEPSEEK_MODEL=deepseek-v4-flash`）。读取逻辑见 `app/core/llm_config.py`。**直接 import 应用现成的 client 构造器**，不要新建配置层、不要硬编码 key。
- **凭证陷阱（v6 就栽在这里）**：`evaluation/results/holdout_v6_live/STATUS.md` 记载首次 v6 因「干净检出用了 runner 的离线占位 key」而 HTTP 401 中止。若你从别处的干净检出跑，`app/.env` 不在那里。**开批前必须先用一次廉价调用断言认证成功**，失败就停下并写进 `PROGRESS.log`，不要用占位 key 跑。启动方已于 18:35 BST 实测该 key 可用（HTTP 200）。
- **测试环境陷阱**：`.runtime/logs/canary-*.jsonl` 是 root 属主（容器写的），任何发 canary 遥测的进程会 `PermissionError` 然后静默不记录。**跑任何测试或评测前先导 `CANARY_LOG_PATH` 到可写临时路径**。不导的话有 29 个测试假红，会把你带偏。
- 评审/被测模型固定 `deepseek-v4-flash`。

## 2. 资源上限（自己监控，超了就停当前阶段进入下一个）

| 项 | 上限 |
|---|---|
| 总墙钟 | **2026-08-06 07:00 BST** 必须停止新请求，进入收尾写报告 |
| 单请求超时 | 120s |
| Phase 2 最大 live 请求数 | 400 |
| 总成本 | **USD 5**。预估超过就停下写报告，不要硬跑 |
| 外部 live 工具并发 | ≤2，请求间隔 ≥300ms |
| 连续失败 | 同一实验连续 10 次请求失败 → 记录并放弃，进入收尾 |

**收尾优先于跑满。** 宁可实验只跑一半，也必须留至少 30 分钟写完报告。

---

## 3. 执行顺序与检查点

### Phase 1：两个确定性修复（各自有现成红测试作为 spec）

两项都要求：**先跑红、再改、再跑绿**，并且不得让全量套件新增任何失败。

**修复 A — `is_loop_synthesis` 分支绕过结构化契约挂载**

- 症状：held-out v6 的 `HO6-198` / `HO6-208` / `HO6-238` 三例，`tool_data` 只有 `{recommendations, search_criteria, area_recommendations}` 三个 key；而 57 例通过的 `E_multi_constraint` 全是八个 key（含 `eligible_recommendations` / `candidate_states` / `commute_evidence` / `excluded_candidates` / `unverified_candidates`）。三个结构化契约指标因此全挂。
- 关键：**这不是模型绕过工具**。`HO6-198` 调了 `calculate_commute` 八次、`HO6-208` 七次，答案文本本身是对的（点名了正确的合规房源、给了通勤分钟数、列了排除项）。是 formatter 的一条分支漏掉了挂载。
- 定位：`app/core/langgraph_agent.py` 的 `is_loop_synthesis` 分支（post-search dimension fan-out）构造的正是那个三 key 形状，且它是 `if`，后面整条 `elif tool_name == 'search_properties'` 被跳过——既不调 `validate_search_payload`，也不挂任何结构化字段。**这是全仓唯一产生该三 key 形状的地方，自己去源码确认后再改。**
- 目标：让该分支走与 `search_properties` 分支同一套契约挂载（复用，不要复制粘贴出第二份）。
- 验证：用 `evaluation/results/holdout_v6_live/raw_runs.jsonl` 里那三例的记录做回归断言（八 key 形状 + `commute_evidence` 非空）。**不需要重跑 live**。

**修复 B — `standalone_rent_conversion` 拦截过宽（B12 回归）**

- 现有两个红测试就是 spec，直接以它们为准：
  - `tests/test_tool_policy_dispatch.py::test_b12_search_properties_never_dispatches`
  - `tests/test_deposit_boundary.py::test_b12_still_reaches_the_model`
- 成因：commit `523745d` 为修 ÷4.33 加的 `core.tool_policy.standalone_rent_conversion` 会匹配 B12（`"I'm looking at a £380/week studio. What'll it cost me all-in per month, including bills and council tax?"`），把整个回合确定性短路掉。但用户问的是**含账单与市政税的全包成本**，一句周租转月租答不了。
- 目标：收窄到「纯换算」语义——句中出现附加成本线索（bills / council tax / all-in / utilities / 含账单 / 全包 等）时不接管。**不要**为了过测试而删掉 ÷4.33 修复本身；`tests/test_agent_contract_guards.py::test_weekly_to_monthly_uses_52_over_12_and_penny_rounding` 必须继续绿。
- 顺带：v6 的 30 道计算题全是同一个模板（`"Using the specified conversion weekly × 52 ÷ 12, calculate the monthly GBP equivalent of £N per week."`，去掉金额后只剩 1 个 distinct 模板），正好是这个拦截器的正例，所以它结构上抓不到 B12 这种形状。**在报告里记下这条**，并给 `evaluation/benchmark/holdout_v6/` 提出补题建议（只写建议，不要改冻结数据集）。

**Phase 1 完成条件**：两项各自的测试转绿，且全量 `pytest tests/`（带 `CANARY_LOG_PATH`）相对基线**不新增失败**。基线：38 failed / 3347 passed，其中 29 例是 canary 日志属主问题、3 例是 POI 缓存脏、1 例 `.env.bak`、1 例 monitor 安装漂移、2 例测试间 env 泄漏（单跑会过）、2 例即上述 B12。修完 B12 后真实失败应降到 0。

---

### Phase 2：实验 D — 维度 fan-out / 批次打包

**Step 0（必须先做）：冒烟。** 用 3~5 个 case 跑通完整链路，确认能落盘、能计分、能读到 DeepSeek 配置、认证成功。冒烟不过就不要放开跑，直接写报告说明卡在哪。

#### 为什么是这个实验（这段 framing 就是设计本身）

前一轮实验 C 试图证明 fc_loop 的**批内并发**比串行派发快。那是**已确认的负结果，不要重跑**：

- `evaluation/results/parallel_tools_ab/analysis_C.json` 与 `negative_control_C.json`：检索阶段延迟 CI 在 warm 与 cold 两轮**都跨 0**。
- 阴性对照**未通过**：16/60（warm）、18/60（cold）组配对执行的工具数不一致——串行化改变了时序，时序改变了模型接下来的规划，工具集合就变了。
- 空结果的根因：244 个工具批次里只有 65 个（26.6%）装了 ≥2 个调用。批次大小为 1 时批内并发收益恒为 0。见 `evaluation/results/EVAL_REPORT_20260804.md` 第 3 节与 `evaluation/results/parallel_tools_ab/case_selection.json`。
- `app/core/agent_loop.py` 里 `_dimension_fanout_cap` 附近的注释自己写明了："Intra-batch dispatch was ALREADY fully concurrent ... what was missing is putting more than one read INTO a batch."

所以 fc_loop 真正独有、legacy 没有的机制是**批次打包**：`_dimension_fanout_calls`（plan-time）与 `_completion_sweep_into_batch`（answer-time）把「用户 cue 到了但模型这批没要」的**读取**维度塞进**已有批次**，省下的是一整个 LLM round-trip，不是几毫秒的 dispatch。

**注意 framing 里的一个坑**：legacy **不是**串行的。`CV_METRICS.md` 里 legacy 的 map-reduce 扇出消融是 `[SAFE]` 正结果（检索阶段均值 −57.1%、p95 −42.0%，阴性对照 0/48 通过）。所以不要在报告里写「fc 并行 vs legacy 串行」，会被一眼打穿。

**开关现成，不需要改产品代码**：`FC_DIMENSION_FANOUT_MAX`（由 `_dimension_fanout_cap` 读取，默认 3）。设 `0` 时 `_dimension_fanout_calls` 直接 `if cap <= 0: return []`。**自己去源码确认这一点再依赖它。**

#### 待检验的主张（双边，不许缩成单纯的加速主张）

- **收益（主）**：覆盖率 —— 用户消息 cue 到的维度里，实际拿到工具证据的比例。预期 ON > OFF。
- **代价（主）**：`llm_calls` 与 `tool_batches`。预期 ON ≤ OFF。整个论点就是「多出来的读取搭了已有批次的顺风车，而不是多花一个 round-trip」。
- **次要**：端到端延迟、soft-wrap 率、成本 USD、tokens。
- **阴性对照**：(a) ON 臂的批次数不得增加；(b) fan-out 绝不得碰写工具（`remember`）或终端工具（`ask_user`）——代码里有断言，去录到的轨迹里验证它确实成立。

`llm_calls` 与 `tool_batches` 是离散计数，方差远小于延迟且不受网络抖动影响，是最可能达到显著的指标。**报告里要写明这一点。**

#### 要建的东西

1. **先完整读 `evaluation/results/_harness/ab_runner.py`。** 它已经有：配对逐例执行两臂、`--arch fc_loop`、warm/cold 缓存协议、逐 run append JSONL（崩溃可续跑）、以 **case 为重采样单位**的 cluster bootstrap CI，以及一个现成的**按臂改 env** 的模式（`--serial-tools-arm`，做法是替换 `agent_loop._TOOL_OFFLOAD_EXECUTOR`）。**照着这个已有模式加你的开关，不要另起一套 harness。**
2. 加一个按臂生效的 fan-out 开关（例如 `--fanout-max-arm NAME` + `--fanout-max N`，或像 `serial_tools`/`parallel_tools` 那样用臂名 `fanout_on`/`fanout_off`）。必须是进程内、按臂设置 `FC_DIMENSION_FANOUT_MAX`，**不改 `app/` 下任何东西**。
3. 把新指标写进逐 run 记录，至少要有 `tool_batches`、`llm_calls`、已执行工具列表，以及足以算出维度覆盖率的信息。
4. **维度 cue 的定义必须复用产品自己的**：`app/core/dimensions.py` 的 `DIMENSION_CUES` 与 `agent_loop._unserved_cued_dimensions`。**不要另写一个长得像的**，否则这个指标就不是它声称的东西。
5. **选例规则跑前冻结**，写进 `case_selection.json` 风格的文件并把规则本身写在里面。建议规则：消息 cue 到 ≥2 个维度的 case，数据源 `evaluation/benchmark/cases.jsonl`（98 例）。参照 `evaluation/results/parallel_tools_ab/case_selection.json` 的先例。
6. **跑之前先预注册**：把假设、两臂、主/次指标、阴性对照、停止规则、bootstrap 种子写进 `PREREGISTRATION.md`，与结果放在一起。这个仓库就是这么做的（见 `evaluation/benchmark/holdout_v3/PREREGISTRATION.md` 与 `design/*-preregistration` 分支）。**没有预注册的结果不可用。**

#### 跑法

- 你已经在 tmux 里。长跑用 `nohup` 或另开 tmux window，**逐 case append 落盘**，不要把结果攒在内存里最后一次性写。
- 断点续跑：重启后跳过已完成的 case。
- 每完成一个 case 往 `PROGRESS.log` 追加一行：时间戳、阶段、case_id、耗时、成功/失败。

#### 分析与报告

- bootstrap CI 以 **case 为重采样单位**，固定种子，风格对齐 `analysis_C.json`。
- 产出 `evaluation/results/fanout_ab/` 下的 `analysis_D.json`、`table_D.md`、`negative_control_D.json`，以及报告里的对应章节。
- **阴性对照写在头条数字之前。** 阴性对照不过就说结果不可用并解释原因——这是合法结局，不要粉饰。
- CI 跨 0 就写「未观察到显著差异」，**永远不要**写「没有差异」，也不要给方向性结论。
- 每个比率都要写分母，附精确 CI。
- 按 `CV_METRICS.md` 的方式给标签（`[SAFE]` / `[SAFE-WITH-SCOPE]` / `[AVOID]`）与 required caveat，但**只写进报告**，不要动 `CV_METRICS.md`。

---

## 4. 自主决策规则（遇到岔路按这个走，别停）

1. **设计冻结**：跑之前把设计写进 `PREREGISTRATION.md` 与 `PROGRESS.log`。跑完结果不好看，**不许**回头改设计/改指标/改筛选条件重跑。要改就记为「第二次设计」，两次结果都保留、都写进报告。
2. **数据不足**：n 不够就跑能跑的，报告写明实际 n，**不要**为凑数放宽筛选条件。
3. **外部工具挂了**：记录失败率，用缓存/快照继续，在报告里标注该结果受外部可用性影响。
4. **前提被推翻是好结局**：如果发现这个实验的前提本身错了（比如那个开关并没真的关掉 fan-out，或者覆盖率指标根本无法从已记录的字段算出），**停下来把这个发现写清楚**。被证伪的前提是有价值的产出，编造出来的结果不是。
5. **一切结论对着一手来源核**——源码、原始 run 记录，**绝不对着摘要或更早的报告核**。这个项目反复因为信任二手产物而得出错误结论。引用**按符号名**，不要按行号（行号会漂）。
6. **不确定就保守**：宁可少下一个结论，不要多下一个没证据的。

## 5. 交付物清单

- [ ] `experiment/fanout-ab-20260805` 分支上的本地提交（不 push）
- [ ] Phase 1 两项修复 + 各自回归测试
- [ ] `evaluation/results/fanout_ab/PREREGISTRATION.md`
- [ ] `evaluation/results/fanout_ab/case_selection.json`
- [ ] `evaluation/results/fanout_ab/{analysis_D.json, table_D.md, negative_control_D.json}` 与逐 run JSONL
- [ ] `evaluation/results/FANOUT_AB_REPORT_20260805.md`
- [ ] `PROGRESS.log` 时间线
- [ ] 报告末尾：可粘贴的 CV 条目建议（含标签与 caveat）
- [ ] 报告末尾：**明确列出你没做完的、跳过的、以及你认为不可信的部分**
