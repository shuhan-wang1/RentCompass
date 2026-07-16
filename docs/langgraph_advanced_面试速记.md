# LangGraph 进阶四件套 · 面试速记

> 本分支 `feat/langgraph-advanced` 给 UK 租房 Agent 补齐了 4 个原本缺失的 LangGraph 高频考点。
> 全部**默认关闭、加不破坏主链路**（`test_default_topology_unchanged` 锁定这一点）。
> 想现场演示：`python examples/langgraph_advanced_demo.py`（无需 LLM / 网络 / API key）。
>
> 代码位置：核心 helper 在 `app/core/graph_advanced.py`；织入点在 `app/core/langgraph_agent.py`
> 的 `build_agent_graph`；开关在 `src/uk_rent_agent/config.py`（`ENABLE_HITL` / `ENABLE_STORE`）。

---

## 1. HITL（human-in-the-loop）· `interrupt()` / `Command(resume=...)`

**落点**：多路搜索会真去抓 Rightmove/Zoopla（花钱、花时间、不可逆）。所以在 `decide_tool` 判定
`multi_search` 后、真正扇出之前，插一个 `confirm_search` 节点，用 `interrupt()` 暂停等人确认。

**三件套（缺一不可）**：① 编译时给 checkpointer（暂停态要落盘）② 节点里 `interrupt(payload)`
③ 恢复时 `graph.invoke(Command(resume=决定), 同一个 thread config)`。

支持三种恢复：`resume=True` 直接跑 / `resume={"action":"edit","searches":[...]}` 改完再跑 /
`resume={"action":"cancel"}` 取消。**edit-before-execute 是加分点**——人可以在执行前改计划。

**re-execution gotcha（面试分水岭）**：恢复时 `confirm_search` 整个节点**从头重跑**，
`interrupt()` 这次不再暂停、直接返回 resume 值。所以 **interrupt() 之前的代码必须幂等、不能有副作用**
（否则确认一次副作用做两次）。我把 payload 组装放在 interrupt 之前（纯读），副作用（抓取）在之后。

**为什么需要 checkpointer**：暂停就是把整张图的状态存进 checkpointer，进程重启/换机器都能凭
`thread_id` 恢复。没 checkpointer 时我让它**静默降级**成不暂停（`enable_hitl and checkpointer is not None`）。

---

## 2. Store（跨线程长期记忆）· `BaseStore` vs Checkpointer

**一句话区别**：
- **Checkpointer** 按 `thread_id = f"{user_id}:{conversation_id}"` 存**一次对话的完整状态**（转录）。
- **Store** 按 `namespace = ("user_prefs", user_id)` 存**用户的持久画像**（预算、目标学校、必备条件），
  **跨对话共享**——会话 A 存的，会话 B 能读到。

**落点**：图入口 `hydrate_prefs` 节点在图内用 `get_store()` 把用户画像灌进本轮 criteria（**只填空，
this-turn 值永远优先**，不会把"今天预算 £900"倒回成存档的 £1200）；图出口 `persist_prefs` 把本轮结构化
criteria 写回 Store（**只覆盖有意义的值**，不用 None/[] 清空存档）。

**和项目已有记忆的分工（一定要主动讲，否则像重复造轮子）**：项目本来有 ChromaDB 的 `AgentMemory` 做
**语义记忆**（自由文本、按相关度检索）。Store 补的是**结构化 KV 画像**（budget=1000 这种精确字段）。
两者互补：Store 存"是什么"，Chroma 存"聊过什么"。

---

## 3. Time-travel · `update_state` / `get_state_history`

**落点**："把预算改成 £800、从搜索那步重跑"——不用重放整段对话。

**做法**（`fork_from_checkpoint` helper）：① `get_state_history(cfg)` 拿到历史 checkpoint；
② 找到**原始运行中 `plan` 执行前**那个 checkpoint（此时搜索结果还空）；③ `update_state(该checkpoint的config, 新criteria)`
写出一个新分支；④ `invoke(None, forked_cfg)` 从那点继续，重跑搜索。

**坑**：如果直接在**已结束**的 thread 上 `update_state` 再 `invoke(None)`，图已在 END、无事可做，patch 不生效。
必须回退到还有下游节点没跑的 checkpoint。另外 `search_results` 用了 `operator.add` reducer，从结束态 fork 会
**追加**到旧结果上——要干净的另一分支就得回退到搜索**之前**的 checkpoint。

**两种定位方式**：`checkpoint_id`（回到某个具体历史点）或 `as_node="hydrate_prefs"`（把写入伪装成某节点刚产出，
从它的下游继续）。

---

## 4. Durability modes · invoke 时的 `durability=`

注意是 **invoke 时**参数，不是 compile 选项（LangGraph 1.x）：`graph.ainvoke(state, cfg, durability=...)`。

| 值 | 何时落盘 | 取舍 |
|---|---|---|
| `"exit"` | 只在图结束/中断时落一次 | 最快；崩溃丢当前这轮（适合便宜、可重跑的轮次）|
| `"async"` | 每个 super-step 后台异步落盘（**默认**）| 平衡 |
| `"sync"` | 每步阻塞到 checkpoint 真正落盘 | 最安全；当"重放会重复扣费"且光靠幂等不够时用 |

**串起 super-step**：LangGraph 是 Pregel/BSP 执行，checkpoint 只在 **super-step 边界**存。durability 就是在
调"边界要不要等落盘完成"。这也是为什么 §1 的幂等这么重要——`async`/`exit` 下崩溃恢复会重跑节点。

---

## 一分钟串讲（白板能默写）

> "我这个租房 Agent 是手搓的 LangGraph：`StateGraph` + `Command` 路由 + `Send` 扇出做多路搜索的 map-reduce，
> SqliteSaver 按 `user_id:conversation_id` 做 checkpointer。在此之上我补了四个生产能力：抓取前用 `interrupt()`
> 做人工确认（HITL，支持改计划），注意恢复时节点重跑、interrupt 前要幂等；用 `BaseStore` 存跨对话的结构化用户
> 画像，和已有的 Chroma 语义记忆分工；用 `update_state` 做时间旅行、改预算重跑；用 invoke 的 `durability=` 在
> 崩溃安全和延迟间取舍。全部默认关、有拓扑回归测试保证不动主链路。"

**演示**：`python examples/langgraph_advanced_demo.py` · **测试**：`pytest tests/test_langgraph_advanced.py`

---

## 附：对抗性压测发现的 5 个 P1 及修复（面试加分素材）

首版四件套 demo 跑通、20 个测试全绿之后，我用 3 个并行的对抗性审计 agent 分别攻击
HITL 集成、Store 并发语义、拓扑回归/时间旅行，坐实并修复了 5 个 P1。教训一句话：
**"demo 跑通 + 测试全绿" ≠ "功能真的能用"——尤其当测试拓扑和生产拓扑分叉的时候。**

| # | 发现 | 根因 | 修复 |
|---|------|------|------|
| 1 | HITL 确认后**永远无法执行**搜索 | 端点只会 `ainvoke(全新state)`，从不发 `Command(resume)`；实测 1.2.8 对 pending interrupt 线程收到新输入=干净重启、静默丢弃原计划 | 端点检测 `aget_state(cfg).next` 含 `confirm_search` 时，把明确的是/否回复映射成 `Command(resume=...)`；含混回复→按新话题干净重启（有意弃单） |
| 2 | cancel 消息被**真实** generate_response 覆盖（LLM 拿着 observation=None 凭空作答） | 我的测试把 generate_response stub 成 no-op，**测试拓扑与生产分叉**把 bug 藏住了 | cancel 改路由到 format_output（透传已设置的 final_response）；测试 mini 图改成会覆盖 final_response 的真实行为 |
| 3 | `fork_from_checkpoint(checkpoint_id=)` 参数路径 100% `KeyError: 'checkpoint_ns'`（两种 saver 都炸、零覆盖） | 手工 config 只有 thread_id，checkpointer.put 强依赖 checkpoint_ns | `configurable.setdefault("checkpoint_ns", "")` + 补参数路径的回归测试 |
| 4 | Store 丢更新竞态（30/30 复现）：`turn_lock` 按 (user, conversation) 分锁，同一用户双开 tab 不互斥，load→merge→put 交错丢字段 | 无锁读-改-写 | 模块级 `_PREFS_LOCK` 串行化整个 RMW（仿 sqlite checkpointer 的 `_db_lock` 模式） |
| 5 | 清除的条件**永久复活**："hydrate 只填空"+"persist 不写空"= 单向棘轮，Store 永远学不到"用户已清除预算" | 两条各自合理的守卫规则组合成系统级 bug | persist 改为**权威写入**：hydrate 先跑保证了"此刻为空=本轮被清除或从未存在"，两种情况删除都正确；列表同理改替换（union 在 hydrate 端发生），顺带修掉"偏好只增不减" |

P2 一并修复：eval 包装器把 `GraphInterrupt`（正常暂停信号）记成 ERROR 级 node.error →
特判 re-raise；HITL edit payload 不校验会让非 dict 条目在 search_worker 炸出未捕获异常 →
`_parse_resume` 改 fail-closed（垃圾输入→cancel，绝不静默启动昂贵扇出）+ worker 入口
isinstance 防御；langgraph 由不 pin 改为 `>=1.2,<2`（运行时 goto 到未注册节点的失败模式
是**静默提前结束**而非报错，未 pin 版本风险不可控）。

**面试可讲的三个通用教训**：
1. 测试里的 stub 节点必须镜像生产节点的**破坏性行为**（会覆盖状态的就要覆盖），否则测试通过恰恰掩盖集成 bug。
2. 两条各自正确的防御规则（只填空 / 不写空）组合可能形成系统级 bug（数据复活）——审计要看**规则的组合**，不只看单条。
3. 并发正确性看**锁的 key 粒度**：turn_lock 按对话分锁挡不住同一用户跨对话的竞态，共享资源的 RMW 必须自己上锁。
