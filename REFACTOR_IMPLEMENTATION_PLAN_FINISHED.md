# UK Rent Recommendation Agent — 重构实施手册（FINISHED）

> 版本：2026-07-07　|　目标分支基线：`feat/send-command-multiuser-fixes`　|　主分支：`main`
> 适用仓库：`C:\Users\shuhan\Desktop\uk_rent_recommendation`　|　活跃应用目录：`local_data_demo/`
> 运行环境：Python 3.10，Windows 11（命令给出 PowerShell 版本）

本文件是一份**可直接执行的重构操作手册**。所有对现状代码的论断都带有经过本人核对的 `文件:行号` 证据。凡是与审计摘要（六个 agent 的调查结论）冲突之处，一律以**实际代码为准**，并在正文相应位置以「⚠️ 审计校正」标注（完整清单见 §7）。

---

## 目录

- §1 执行摘要 + 阅读指南
- §2 现状架构速览（已核对，含 file:line）
- §3 目标架构（模块树 + 依赖规则 + 迁移映射）
- §4 分阶段实施
  - Phase 0 — 止血（安全 + 仓库瘦身）
  - Phase 1 — 安全网（特征化测试 + 打包）
  - Phase 2 — 删除优先（死代码清理）
  - Phase 3 — 统一数据层
  - Phase 4 — Agent 编排层
  - Phase 5 — 服务层
- §5 附录 A：完整删除清单
- §6 附录 B：魔法常数迁移表
- §7 附录 C：审计摘要校正清单（代码核对结果）
- §8 附录 D：`requirements.txt` 目标内容
- §9 附录 E：`pyproject.toml` 骨架
- §10 附录 F：特征化测试用例清单（测试名 + 断言内容）

---

## §1 执行摘要 + 阅读指南

### 1.1 这是什么

一个 UK 学生租房推荐 Agent：Flask 后端（`local_data_demo/app.py`）+ 单页 HTML 前端（`unified-ui.html`）+ 基于 LangGraph 的 StateGraph agent（`core/langgraph_agent.py`）+ 11 个工具（`core/tools/`）+ ChromaDB 长期记忆（`rag/agent_memory.py`）+ FAISS 房源向量检索（`rag/`）+ 真实爬虫链路（`core/scraping/`，OpenRent 可用）。LLM 默认走 DeepSeek 云 API（`LLM_PROVIDER=deepseek`）。

### 1.2 为什么要重构（问题分级）

| 级别 | 问题 | 证据锚点 |
|------|------|----------|
| P0 安全 | git 历史中含真实 Google API key；`.git` 达 562 MB；51 个 `.pyc` 被跟踪 | §2.1 |
| P0 正确性 | 工具双重包装 bug：`web_search` / `search_nearby_pois` 返回 `ToolResult` 被 `Tool.execute` 再包一层，导致内层 `success=False` 被吞、POI 卡片渲染路径死掉、经 MCP 传出 repr 字符串 | §2.4 |
| P1 数据 | 数据「精神分裂」：搜索工具用假 CSV、详情工具用爬取缓存、app 启动构建的 FAISS 从不被查询；进程内最多重复构建 3 次 FAISS | §2.3 |
| P1 多用户 | 收藏/历史是进程级全局共享；记忆工具的 schema 丢失 `user_id`，所有用户共用 `default` 记忆桶 | §2.5 |
| P1 路由 | 分类提示词只覆盖 8 个「伪工具」，5 个已注册工具永远不可达；决策逻辑散落 4 个函数 | §2.6 |
| P2 死代码 | 仓库约 28k LOC，活跃约 6k，约 3× 膨胀（`tests/`、`scrapped_data_demo/` 顶层、`map_visualization/`、`tool_system.py` 半死、`llm_interface.py` ~690 行 CLI 遗留、`conversation_memory.py`/`area_knowledge.py` 写死/输出丢弃） | §2.7 |
| P2 配置 | 3 个配置孤岛、core↔rag 半破环、魔法常数散落、chroma 路径 CWD 相对导致重复目录 | §2.8 |
| P2 服务 | `debug=True` 跑在 `0.0.0.0:5001`（Werkzeug 开发服务器 + 调试器 RCE）；无鉴权；CORS 全开；缓存不持久化；print 日志 | §2.9 |

### 1.3 阅读指南

- 想立刻降低风险 → 直接做 **Phase 0**（可在任何其它工作之前独立完成）。
- 想动重构但不想引入回归 → **必须先做 Phase 1**（特征化测试是后续所有阶段的安全网）。
- Phase 2→5 有严格顺序依赖：删除（2）→ 统一数据（3）→ Agent 编排（4）→ 服务层（5）。每个阶段结束都要求「特征化测试仍全绿」。
- 每个 Phase 小节结构固定：**目标 / 前置条件 / 详细步骤 / 涉及文件清单 / 具体命令 / 测试与验收标准 / 风险与回滚**。
- 附录集中放「可复制粘贴」的清单：删除清单、常数迁移表、requirements、pyproject、测试清单。

### 1.4 一次性全局约束

1. 除非该 Phase 明确要求，**不要**同时改行为与改结构；先搬家（保持行为）再改逻辑。
2. 任何删除操作前，先确认它不在活跃调用图内（本手册已逐项核对，直接照做即可）。
3. 新分支策略：每个 Phase 一个分支，例如 `refactor/phase-0-stop-bleeding`，做完开 PR、跑测试、合并再进下一个。
4. 秘钥一旦进过 git，**视为已泄露**，必须轮换——清历史只是防止再次泄露，不能挽回已泄露的 key。

---

## §2 现状架构速览（已核对）

> 下列每条都标注了本人核对过的 `文件:行号`。行号基于当前工作树（`feat/send-command-multiuser-fixes`）。

### 2.1 安全与仓库体积

- `.git` 体积：**562 MB**（`du -sh .git`）。
- 被跟踪的 `.pyc`：**51 个**（`git ls-files '*.pyc' | wc -l`），如 `__pycache__/cache_service.cpython-310.pyc`、`__pycache__/rightmove_scraper.cpython-3{9,10,11,12}.pyc` 等。
- git 历史中的 `.env` blob（`git rev-list --all --objects | grep '\.env$'`）：
  - `c6b3b011cd5040470e2aced584af260b0785375b`  → `fine_tuning/.env`（含 `GEMINI_API_KEY="AIzaS…"` 真实 key）
  - `62b38ae688ec26b18e26fc8caaac8627934e31a0` → `local_data_demo/.env`（含 `GEMINI_API_KEY="AIzaS…"`、`GOOGLE_MAPS_API_KEY="AIzaS…"`、`OPENROUTESERVICE_API_KEY="eyJvc…"` 真实值）
  - `c094e4568…`（`fine_tuning/.env`）与 `b9a34ecf9…`（`scrapped_data_demo/.env`）为占位值 `"your_…"`。
- **⚠️ 审计校正 1**：审计称「HEAD clean」。实际 `git ls-files` 显示 HEAD **仍在跟踪** `fine_tuning/.env`、`scrapped_data_demo/.env`、`tests/.env`——只是这些 HEAD 版本疑似为占位值。因此「HEAD 无真实 key」大体成立，但「HEAD 无被跟踪 .env」**不成立**，Phase 0 需一并 `git rm --cached` 这些 `.env`。
- **⚠️ 审计校正 2**：审计称 chroma sqlite「committed ~12×」。实际 `git rev-list --all --objects | grep 'chroma.*\.sqlite3' | wc -l` = **36** 个 sqlite blob（分布在 `chroma_db/`、`chroma_db_area/`、`local_data_demo/chroma_db/`、`local_data_demo/chroma_db_area/`）。是低估，不是高估。
- 历史最大 blob（`git rev-list --all --objects | git cat-file --batch-check` 排序）：`fine_tuning/student_model_lora/tokenizer.json`（10.9 MB）、`vocab.json`（2.6 MB）、`dataset_raw.json`（2.6 MB）、`train.jsonl`（1.8 MB）、`merges.txt`（1.6 MB）、多份 `chroma.sqlite3`（各 1.4 MB）与 `data_level0.bin`（1.6 MB）。
- 工作树未跟踪垃圾（`du -sh`）：根目录 `student_model_lora/`=**298 MB（0 个文件被跟踪，纯未跟踪副本）**、`maps/`=520 K、`UK_Rent_Agent_Technical_Report.pdf`=1.5 M、`quick_test_results.csv`=1 K、`diagnose_geocoding.py`=12 K。
- `fine_tuning/`=304 M，其中 `fine_tuning/student_model_lora/adapter_model.safetensors`、`tokenizer.json`、`vocab.json`、`merges.txt` 等**被跟踪且在历史里**（尽管 `.gitignore` 有 `*.safetensors`，那些文件是在忽略规则之前提交的，忽略规则不追溯）。

### 2.2 请求主链路（活跃路径）

```
浏览器 (unified-ui.html)
  → POST /api/alex            app.py:230  api_alex()
  → handle_with_react_agent   app.py:274
      → resolve_identity()    app.py:108   (body user_id > X-User-Id > session cookie > "default")
      → 注入长期记忆          app.py:414-423 (rag.agent_memory.get_agent_memory().retrieve(user_id=...))
      → create_initial_state  langgraph_agent.py:1055
      → agent_graph.ainvoke   app.py:438
          extract_preferences → decide_tool → (execute_tool | multi_search 扇出 | generate_response) → format_output
      → 后台写记忆            app.py:462-471 (remember_turn_async)
  → jsonify(response_type ∈ {search, clarification, chat, error})  app.py:512-537
```

- 工具执行提供方：优先 MCP stdio 子进程（`app.py:159-176`，`USE_MCP_TOOLS` 默认 `"1"`），失败回落进程内 registry。
- LangGraph graph 懒编译：`app.py:304-307`，`build_agent_graph(agent_tool_provider)`（`langgraph_agent.py:1017`）。

### 2.3 数据层「精神分裂」（P1）

存在三份互不一致的数据视图：

1. **app 启动视图**：`app.py:192` `all_properties = load_properties()`（受 `PROPERTY_SOURCE` 控制，默认 `auto`：有爬取缓存则用之，否则假 CSV，见 `data_loader.py:34-66`）→ `app.py:205` `rag_coordinator.property_store.build_index(all_properties)` 构建 FAISS。**但这个 `rag_coordinator`（`app.py:181`）在整个请求链路里从不被查询**——agent 用的是 `agent_tool_provider`，与该实例无关。
2. **搜索工具视图**：`search_properties` 工具自建单例 `_get_rag_coordinator()`（`search_properties.py:124-140`），内部 `load_mock_properties_from_csv()`（`:133`，**恒为假 CSV**）并 `build_index`（`:138`）；主流程再次 `load_mock_properties_from_csv()`（`:489`）。即搜索**永远只用假 CSV**，无视 `PROPERTY_SOURCE`。
3. **详情工具视图**：`get_property_details._active_data_path()`（`get_property_details.py:24-31`）调用 `provider.get_active_property_csv()`（`provider.py:30-34`，**有爬取缓存则用爬取缓存**，否则假 CSV）。

后果：搜索出来的房源（假 CSV）和「查看详情」的房源（爬取缓存）可能来自不同数据源；FAISS 在一个进程内可能被构建**最多 3 次**（app 启动 1 次 + 搜索工具单例 1 次 + MCP 子进程内搜索工具单例 1 次）。

数据加载入口（需在 Phase 3 合并）共 5 个：
- `data_loader.load_properties()`（`data_loader.py:34`）
- `data_loader.load_mock_properties_from_csv()`（`data_loader.py:9`）
- `data_loader.get_live_properties()`（`data_loader.py:90`，仅 `interactive_main.py` 用）
- `scraping.provider.get_active_property_csv()`（`provider.py:30`）
- `get_property_details._active_data_path()`（`get_property_details.py:24`）

rich schema 单一定义：`core/scraping/config.py:40` `RICH_COLUMNS`（14 列）；`enhanced_search` 返回三元组 `scored_results, past_context, area_info`（`rag_coordinator.py:57`），其中后两者在 `search_properties.py:507` 解包后**从不再被使用**。

### 2.4 工具返回契约的双重包装 bug（P0，已确认）

- 契约类型：`ToolResult`（dataclass，`tool_system.py:21-39`）；`Tool.execute`（`tool_system.py:94`）无条件把 `func` 的返回值包成 `ToolResult(success=True, data=result)`（`tool_system.py:122-127`）。
- 绝大多数工具的 `func` 直接返回**普通 dict**（正确）：`search_properties`（返回 dict）、`check_safety`（`check_safety.py:73/86` 返回含 `safety_score` 的 dict）、`calculate_commute_cost`（`calculate_commute_cost.py:371/415` 返回含 `success/commute/transport_cost` 的 dict）、`get_weather`（`get_weather.py:53` 返回含 `success` 的 dict）、`calculate_commute`、`check_transport_cost`、`get_property_details`（`get_property_details.py:181/195/215` 返回 dict）、`recall_memory`/`remember`（`memory_tools.py:18/30` 返回 dict）。
- **两个工具错误地返回 `ToolResult`**（触发双重包装）：
  - `web_search_func`（`core/tools/web_search.py:23`，返回 `ToolResult` 于 `:93 / :107 / :121`）
  - `search_nearby_pois_impl`（`core/tools/search_nearby_pois.py:413`，返回 `ToolResult` 于 `:442 / :486 / :511 / :524`）
- 后果链：
  - `execute_tool_node`（`langgraph_agent.py:748-757`）取 `raw_data = result.data`，此时 `result.data` 是内层 `ToolResult` 对象，`isinstance(result.data, (dict, list))`（`:752`）为 False → `observation = str(result.data)`（`:755`，即 repr 字符串）。内层 `success=False` 被外层 `success=True` 吞掉。
  - `format_output_node`（`langgraph_agent.py:828`）判定 `tool_name == 'search_nearby_pois' and isinstance(raw_data, dict) and raw_data.get('pois')`——因 `raw_data` 是 `ToolResult` 而非 dict，**`_format_pois`（`langgraph_agent.py:876`）永不触发**，POI 卡片渲染路径死亡。
  - `_route_after_execution`（`langgraph_agent.py:936-941`）同样因 `isinstance(raw, dict)` 失败而回落 `generate_response`。
  - 经 MCP：`mcp_server.py:78-87` 把 `result.data` 用 `json.dumps(..., default=str)` 序列化，内层 `ToolResult` 变成 repr 字符串传出。
- **⚠️ 审计校正 3**：审计把 web_search bug 定位在 `core/web_search.py:107-115`。**文件错了**。已注册的 `web_search` 工具来自 `core/tools/web_search.py`（`__init__.py:9`），bug 在该文件的 `web_search_func`（返回 `ToolResult` 于 `:93/:107/:121`）。`core/web_search.py` 是另一个文件（468 行），只提供 `get_search_snippets`（返回字符串，被 `core/tools/web_search.py:8` 导入）。行号 107 碰巧一致，文件名错误。
- **附注**：`get_property_details.py` 虽 `import ToolResult`（`:17`），但其 `func` 实际返回普通 dict，**不**触发双重包装。

### 2.5 多用户半成品（P1）

- `app.py:73-76` 的 per-user 字典（`_user_states`/`_user_histories`/`_user_last_results`）确实按 `user_id` 隔离了 L2 会话状态，但**无上限、无淘汰**（内存泄漏隐患）。
- `core/user_session.py:6-11` 的 `_session_data`（含 `search_history`/`favorites`/`pending_criteria`/`clarification_state`）是**进程级模块全局**，被所有用户共享。收藏/历史端点（`app.py:555-595`）直接调用 `add_to_favorites`/`get_favorites`/`_session_data['favorites']`/`_session_data['search_history']`，**完全无视身份**。
- 记忆工具泄漏：`recall_memory_impl`/`remember_impl`（`memory_tools.py:15/26`）的**实现签名**含 `session_id`/`user_id`（默认 `"default"`），但 `Tool.parameters` **schema 只暴露** `query`/`n`（`memory_tools.py:42-49`）与 `content`（`:61-67`），省略了 `user_id`。而 `execute_tool_node` 只从 LLM 决策的 params 构造调用（`langgraph_agent.py:697`），因此这两个工具一旦被 LLM 调用，`user_id` 永远回落 `"default"` → 所有用户共用同一记忆桶。（注意：web 主链路的记忆是隔离的，因为 `app.py:417/466` 显式传了 `user_id`；泄漏仅发生在「LLM 主动调用 recall/remember 工具」这条路径——而这条路径当前又因路由不可达（§2.6）而永不触发，属双重死角。）
- `AgentState`（`langgraph_agent.py:58-76`）与 `create_initial_state`（`:1055-1080`）**均无 `user_id` 字段**。

### 2.6 路由不匹配（P1）

- `CLASSIFICATION_PROMPT`（`langgraph_agent.py:278-293`）只列 8 个：`reasoning_property`、`search_properties`、`calculate_commute_cost`、`web_search`、`search_nearby_pois`、`check_safety`、`get_weather`、`multi_search`。其中 `reasoning_property`、`multi_search`、`direct_answer` 是「伪工具」（非注册工具，特殊处理）。
- 注册的 11 个工具（`tool_system.py:577-587` / `tools/__init__.py`）：`search_properties`、`calculate_commute`、`calculate_commute_cost`、`check_safety`、`get_weather`、`web_search`、`search_nearby_pois`、`get_property_details`、`check_transport_cost`、`recall_memory`、`remember`。
- 因此**永不可达的 5 个已注册工具**：`calculate_commute`（只路由到 `calculate_commute_cost`）、`check_transport_cost`、`get_property_details`、`recall_memory`、`remember`。✅ 与审计一致。
- 决策逻辑散落 4 处：`_compute_decision`（`langgraph_agent.py:389-425`）、`_majority_vote`（`:444-490`）、`_heuristic_fallback`（`:581-595`）、`_build_tool_params`（`:598-645`）。✅ 行号与审计完全一致。

### 2.7 死代码（约 3× 膨胀）

- `tests/`（仓库根）：陈旧的 app 快照，**零真实测试**（`grep 'def test_|import pytest|import unittest|assert '` = 0 命中），全仓库**无** `pytest.ini/setup.cfg/pyproject.toml/conftest.py/tox.ini`。含 `tests/core/tool_system.bak`、`.pyc`、`.env`、`apartment-finder-ui.html`（调用死端点 `/api/chat`:785、`/api/search`:857）。
- `scrapped_data_demo/`：顶层文件全部死代码（活跃应用对其无导入，仅 `core/scraping/config.py:8/34/137` 与 `__init__.py:4` 引用）。**唯一存活子集**是 `scrapped_data_demo/scrapper/`，通过 `config.py:136-148 load_legacy()`（`sys.path.insert` + `importlib.import_module`）被动态导入。实际被加载的模块只有 `rightmove_scraper.py`（`core/scraping/rightmove.py:161`）与 `scrape_zoopla_listings.py`（`core/scraping/zoopla.py:54`）。
- `map_visualization/`（仓库根）：死代码（`grep map_visualization local_data_demo/` = 0 命中），且自带陈旧 `chroma_db/`、`chroma_db_area/`。
- `core/tool_system.py`（789 行）：`class FunctionCalling`（`:403-555`）与 `class SmartFunctionCalling`（`:598-789`）在活跃应用中**完全死亡**（仅在文件内部与 `tests/` 副本被引用）。`to_llm_format`（`:174-213`）仅经 `list_tools_for_llm`（`:283`）→ 又仅被死掉的 `FunctionCalling`（`:475`）调用，属**传递性死亡**。活跃只用 `ToolResult`、`Tool`、`ToolRegistry`、`create_tool_registry`（`app.py:22/143/297`）。**⚠️ 审计校正 4**：审计说「~55% 死」，更精确的是：两个类完全死、`to_llm_format` 仅传递性死。
- `core/llm_interface.py`（1434 行）：约 690 行 CLI 遗留，仅 `interactive_main.py`（唯一调用者，且不被任何模块 import）使用：`generate_recommendations`（`:944-1280`，仅 `interactive_main.py:141`）、`refine_criteria_with_answer`（`:592-739`，仅 `interactive_main.py:175/180`）及其私有价格/URL 辅助（`:740-943`）。活跃应用只用 `clarify_and_extract_criteria`（`search_properties.py:344`）与 `call_ollama`（`rag/agent_memory.py:37`）。
- `rag/conversation_memory.py`（56 行）：**写死**——`add_interaction`（`:16`）从不被调用；读取 `retrieve_relevant_history` 的输出 `past_context` 在 `search_properties.py:507` 解包后被丢弃。
- `rag/area_knowledge.py`（54 行）：**只 seed 了 1 个区域 Camden**（`:29-35`）；`get_context` 输出 `area_info` 传给 `_hybrid_rank`（`rag_coordinator.py:52-54`），但 `_hybrid_rank`（`:59-137`）函数体**从不引用** `area_info` 形参；在 `search_properties.py:507` 解包后同样被丢弃。
- `data/fake_property_listings1.csv`：孤儿（`grep fake_property_listings1 local_data_demo/` = 0 命中）。真正被引用的是 `fake_property_listings.csv`（`data_loader.py:18`、`get_property_details.py:21`、`scraping/config.py:30`、`scripts/prefetch_osm_data.py:279`）。

### 2.8 配置与依赖

- 3 个配置孤岛：
  - `local_data_demo/config.py`（27 行）：`GEMINI_API_KEY`(:9)、`GOOGLE_MAPS_API_KEY`(:12)、`OPENROUTESERVICE_API_KEY`(:15)、`DEEPSEEK_API_KEY`(:18)、`DEEPSEEK_BASE_URL`(:19)、`DEEPSEEK_MODEL`(:20)、`LLM_PROVIDER`(:23)、`USE_TRAVEL_SERVICE='google'`(:27，硬编码)。
  - `core/llm_config.py`（73 行）：`:19` `LLM_PROVIDER`、`:22-24` DeepSeek 三件套（与 `config.py:18-20` **重复**）、`:27-28` Ollama；工厂 `get_react_llm`(:54)/`get_classification_llm`(:61)/`get_planning_llm`(:68)。运行时 `llm_interface.py` 从 `llm_config` 读 DeepSeek（`config.py` 的那份是冗余副本）。
  - `core/scraping/config.py`（149 行）：路径常量、`RICH_COLUMNS`(:40)、`DEFAULT_SEARCH_TASKS`(:90)、`load_legacy`(:136) + 7 个 `SCRAPER_*` 环境变量。
- **⚠️ 审计校正 5**：审计说「core↔rag 循环依赖仅靠 lazy import 存活」。实际 **rag→core 这一腿是顶层 eager import**：`rag/agent_memory.py:37` `from core.llm_interface import call_ollama` 在模块级。只有 core→rag 两腿是 lazy（`memory_tools.py:11` 在 `_mem()` 内、`search_properties.py:130` 在 `_get_rag_coordinator()` 内）。因此 `import rag.agent_memory` 会**立即**拉起 `core.llm_interface`。`rag/` 确实**无 `__init__.py`**。
- 魔法常数散落（完整表见 §6）：`999` 哨兵在 5 个代码文件（`search_properties.py:27`、`rag_coordinator.py:67/97`、`llm_interface.py:503/532/1303`、`maps_service.py:569/642/643`、`interactive_main.py:67/95/104`）；`1.15` 预算系数在 2 个文件（`search_properties.py:191`、`rag_coordinator.py:89`）；`4.33` 周→月在 1 个文件 2 行（`search_properties.py:468/469`）。**⚠️ 审计校正 6**：审计说 `4.33` 出现在「×2」处，实为同一文件的 2 行，不是 2 个文件。
- chroma 路径风格不一致：CWD 相对 `./chroma_db`（`conversation_memory.py:8`）、`./chroma_db_area`（`area_knowledge.py:11`）→ 因 CWD 不同而在仓库根与 `local_data_demo/` 各生成一份；`__file__` 绝对 `chroma_db_agent_memory`（`agent_memory.py:45-48`）→ 只在 `local_data_demo/` 下生成。
- 依赖（`requirements.txt` 30 行，**全部未固定版本**，无 `==`）：
  - 缺失但被使用：`geopy`（`search_nearby_pois.py:11-12`、`scripts/prefetch_osm_data.py:20-21`）。
  - 声明但未使用：`fastapi`、`uvicorn`、`googlemaps`、`scikit-learn`、`langchain-community`、（`accelerate` 仅传递依赖）。
  - `torch`/`transformers`/`peft` 仅被 `finetuned_parser.py` 导入，且被 `USE_FINETUNED_MODEL=False`（`llm_interface.py:15`，硬编码常量非环境变量）门控——`:332 if USE_FINETUNED_MODEL:` → `:335 from finetuned_parser import ...`，恒不执行。
- 非可安装包：全仓库无 `pyproject.toml`/`setup.py`/`setup.cfg`；导入为 CWD 相对（`from core.X import`、`from rag.X import`），**必须在 `local_data_demo/` 目录下启动**。

### 2.9 服务与前端

- `app.py:731`（尾行）：`app.run(debug=True, host='0.0.0.0', port=5001)`——Werkzeug 开发服务器 + 调试器（RCE 面）+ 绑定全网卡。
- 8 个 `@app.route` 端点**无任何鉴权**；`CORS(app)`（`app.py:26`）**全 origin 放开**；密钥硬编码回落 `"uk-rent-dev-secret-key-do-not-use-in-prod"`（`app.py:35`）。
- 缓存 `core/cache_service.py`：模块级 `_cache = {}`（`:7`，无界、无淘汰、纯内存、重启即失），键为 `create_cache_key` 的 md5（`:17-27`）。
- MCP 客户端 `core/mcp_client.py`（190 行）：单后台事件循环 + 线程（`:67-69/78/87`），所有调用经 `run_coroutine_threadsafe`（`:144`）投递；**⚠️ 审计校正 7**：审计说「串行化所有工具调用」，实际**无锁**，是「单循环但并发协程」，非严格串行。默认每调用超时 `call_timeout=120.0`（`:39/145`）。`close()` 存在（`:109-123`）但**从未注册**（全仓库 `atexit` = 0 命中，`app.py:163-168` 建了 `_mcp_client` 从不 teardown）。
- 日志：≈586 处 `print(`；仅 `langgraph_agent.py:26/35` 用 `logging`（5 处 logger 调用，且无 `basicConfig`，默认不输出）。
- 前端 `unified-ui.html`（1716 行，内联 `<script>` `:692-1715` ≈1024 行 JS），经 `app.py:219 render_template` 服务，只 fetch 活跃端点。两份陈旧 UI 副本：`tests/apartment-finder-ui.html`（调用死端点 `/api/chat`、`/api/search`）、`scrapped_data_demo/apartment-finder-ui.html`（调用死端点 `/api/search`）。
- 无生产 WSGI 服务器（`waitress`/`gunicorn` 均未在依赖中，也未被 import）。

### 2.10 分支

- 本地/远程分支较多。`fengyuan/agent` 领先 16、落后 54（审阅后再决定是否裁剪）；若干已完全合并的分支可安全删除（Phase 0 具体命令见 §4.0）。

---

## §3 目标架构

### 3.1 模块树（可安装包 `uk_rent_agent`）

```
uk_rent_recommendation/                 # 仓库根
├── pyproject.toml                       # 唯一打包 + 依赖声明（见 §9）
├── README.md
├── .gitignore                           # 收紧（含 REFACTOR_IMPLEMENTATION_PLAN.md）
├── src/
│   └── uk_rent_agent/
│       ├── __init__.py
│       ├── config.py                    # 合并后的唯一配置（3 岛 → 1）
│       ├── logging_setup.py             # logging.basicConfig 集中配置
│       ├── domain/                      # 纯数据与常量，零外部依赖
│       │   ├── __init__.py
│       │   ├── constants.py             # 999 / 1.15 / 4.33 / 周月系数 等
│       │   ├── schema.py                # RICH_COLUMNS（唯一 schema）
│       │   └── models.py                # Property, SearchCriteria, ToolDecision (dataclass/TypedDict)
│       ├── data/
│       │   ├── __init__.py
│       │   ├── repository.py            # PropertyRepository（合并 5 个加载入口）
│       │   ├── parsing.py               # parse_price, extract_postcode, filter_by_budget
│       │   ├── cache.py                 # 持久化缓存（sqlite/JSON，沿用 md5 键）
│       │   └── scraping/                # openrent/rightmove/zoopla/provider/normalize/config
│       │       └── legacy_scrapers/     # 从 scrapped_data_demo/scrapper 迁入
│       ├── rag/
│       │   ├── __init__.py              # 新增（补齐包）
│       │   ├── coordinator.py           # RAGCoordinator（注入式，单实例）
│       │   ├── embeddings.py            # PropertyEmbeddingStore (FAISS)
│       │   └── memory.py                # AgentMemory（保留）
│       ├── tools/
│       │   ├── __init__.py              # registry 工厂
│       │   ├── base.py                  # Tool, ToolResult, ToolRegistry（去 FunctionCalling）
│       │   ├── search_properties.py     # 5 段澄清折叠为 1；查 PROPERTY_SOURCE 数据
│       │   ├── get_property_details.py
│       │   ├── check_safety.py
│       │   ├── search_nearby_pois.py    # 改返回 dict
│       │   ├── web_search.py            # 改返回 dict
│       │   ├── calculate_commute.py
│       │   ├── calculate_commute_cost.py
│       │   ├── check_transport_cost.py
│       │   ├── get_weather.py
│       │   └── memory_tools.py          # schema 补 user_id/session_id
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── state.py                 # AgentState（含 user_id）+ create_initial_state
│       │   ├── router.py               # 唯一决策模块（合并 4 函数）
│       │   ├── prompts.py               # 集中所有 prompt
│       │   ├── nodes.py                 # graph 节点
│       │   └── graph.py                 # build_agent_graph
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── server.py                # 独立 MCP 入口（对外 MCP 客户端）
│       │   └── client.py                # MCPToolClient（含 atexit close）
│       └── web/
│           ├── __init__.py
│           ├── app.py                   # create_app() 工厂
│           ├── session_store.py         # SessionStore（TTL/LRU）
│           ├── routes.py
│           ├── templates/unified-ui.html
│           └── static/{js,css}/         # 拆出内联 JS
├── scripts/
│   ├── build_scraped_dataset.py
│   └── prefetch_osm_data.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/properties_sample.csv
│   ├── test_tool_contract.py
│   ├── test_router.py
│   ├── test_api_alex.py
│   └── test_search_pipeline.py
├── docs/                                # 合并所有 md/报告
└── legacy/                              # 可选隔离区（interactive_main.py 等）
```

### 3.2 单向依赖规则（强制）

```
web  →  agent  →  tools  →  {data, rag}  →  domain  →  config
```

- 上层可依赖下层，**下层禁止依赖上层**。
- `domain` 只依赖 `config`（甚至可零依赖）；`data`/`rag` 依赖 `domain`；`tools` 依赖 `data`/`rag`/`domain`；`agent` 依赖 `tools`；`web`/`mcp` 依赖 `agent`。
- **打破现有 core↔rag 环**：`rag.memory` 需要的 LLM 调用（现 `call_ollama`）改为在 `config`/一个独立 `llm/` 薄层提供，`rag` 依赖它而非依赖 `tools`/`agent`。具体：把 `call_ollama` 及 `_call_deepseek`（现 `llm_interface.py:28/50`）下沉为 `uk_rent_agent/llm.py`（位于 domain 与 rag 之间的最低可用层），`rag.memory` 只 `from uk_rent_agent.llm import call_llm`。这样 `rag` 不再 import `tools`/`agent`，环彻底断开。
- 用 `import-linter` 或一条 CI grep 规则守护（见 Phase 1）。

### 3.3 现状→目标 迁移映射（总表，细节在各 Phase）

| 现状路径 | 目标路径 | 处置 |
|----------|----------|------|
| `local_data_demo/app.py` | `src/uk_rent_agent/web/app.py` (+`routes.py`,`session_store.py`) | 拆分为 app 工厂 |
| `local_data_demo/config.py` + `core/llm_config.py` + `core/scraping/config.py`(非 schema 部分) | `src/uk_rent_agent/config.py` | 合并去重 |
| `core/scraping/config.py:40 RICH_COLUMNS` | `src/uk_rent_agent/domain/schema.py` | 迁移 |
| `core/langgraph_agent.py`（1080 行） | `agent/{state,router,prompts,nodes,graph}.py` | 拆分 |
| `core/llm_interface.py`（1434 行，活跃部分） | `uk_rent_agent/llm.py` + 保留 `clarify_and_extract_criteria` | 拆分，遗留入 `legacy/` |
| `core/tool_system.py`（活跃部分） | `tools/base.py` | 删死代码后迁移 |
| `core/tools/*.py` | `tools/*.py` | 迁移（修双重包装/schema） |
| `core/data_loader.py` + 5 个加载入口 | `data/repository.py` + `data/parsing.py` | 合并 |
| `core/scraping/*` | `data/scraping/*` | 迁移 |
| `scrapped_data_demo/scrapper/{rightmove_scraper,scrape_zoopla_listings}.py` | `data/scraping/legacy_scrapers/` | 迁入后删源 |
| `core/cache_service.py` | `data/cache.py` | 加持久化 |
| `rag/{rag_coordinator,property_embeddings,agent_memory}.py` | `rag/{coordinator,embeddings,memory}.py` | 迁移 |
| `rag/{conversation_memory,area_knowledge}.py` | — | 删除 |
| `mcp_server.py` / `core/mcp_client.py` | `mcp/{server,client}.py` | 迁移 |
| `unified-ui.html` | `web/templates/` + `web/static/` | 拆分内联 JS |
| `interactive_main.py`, `finetuned_parser.py` | `legacy/` 或删除 | 隔离 |
| `tests/`(根)、`scrapped_data_demo/`(顶层)、`map_visualization/`、`maps/`、根 `student_model_lora/` | — | 删除 |

---

## §4 分阶段实施

> 通用命令前缀：本节命令均以 PowerShell（Windows）为准。仓库根记为 `$repo = "C:\Users\shuhan\Desktop\uk_rent_recommendation"`。凡涉及历史改写（`git filter-repo`）务必先备份整仓。

---

### Phase 0 — 止血（安全 + 仓库瘦身）

#### 目标
1. 轮换 3 个已泄露的 Google/相关 key。
2. 用 `git filter-repo` 清除历史中的 `.env` blob、chroma sqlite、LoRA 大文件。
3. `git rm --cached` 51 个 `.pyc` + 被跟踪的 `.env` + 生成物。
4. 删除工作树垃圾（根 `student_model_lora/` 298 MB、根 `maps/`、报告 PDF、`quick_test_results.csv`、`diagnose_geocoding.py`）。
5. 裁剪已合并分支。
6. **验收：`.git` < 50 MB，`git status` 干净。**

#### 前置条件
- 安装 `git-filter-repo`：`pip install git-filter-repo`（或 `pipx install git-filter-repo`）。
- 对所有能改写历史感到不安的协作者先沟通（filter-repo 会重写 SHA，需 force-push + 各自重新 clone）。
- **先整仓备份**（filter-repo 不可逆）。

#### 详细步骤

**0.1 轮换密钥（最优先，独立于代码）**
- 到 Google Cloud Console 撤销/重建：泄露于历史 blob `62b38ae6`（`local_data_demo/.env` 的 `GEMINI_API_KEY`、`GOOGLE_MAPS_API_KEY`）与 `c6b3b011`（`fine_tuning/.env` 的 `GEMINI_API_KEY`），以及 OpenRouteService token。
- 新 key 只放进**未跟踪**的 `.env`（确保 `.gitignore` 覆盖，见 0.5）。
- 若 DeepSeek key 也曾进过历史（核对 `git log -p -- **/.env`），一并轮换。

**0.2 备份**
```powershell
$repo = "C:\Users\shuhan\Desktop\uk_rent_recommendation"
Copy-Item -Recurse -Force $repo "$repo-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

**0.3 用 filter-repo 清历史**（一次性给出所有待清路径）
```powershell
cd $repo
# 先看还有哪些 .env/敏感 blob
git rev-list --all --objects | Select-String -Pattern '\.env$'

# 用 --invert-paths 从所有历史中抹除
git filter-repo --force `
  --invert-paths `
  --path fine_tuning/.env `
  --path scrapped_data_demo/.env `
  --path tests/.env `
  --path local_data_demo/.env `
  --path-glob 'chroma_db/*' `
  --path-glob 'chroma_db_area/*' `
  --path-glob 'local_data_demo/chroma_db/*' `
  --path-glob 'local_data_demo/chroma_db_area/*' `
  --path-glob 'local_data_demo/chroma_db_agent_memory/*' `
  --path-glob 'map_visualization/chroma_db/*' `
  --path-glob 'map_visualization/chroma_db_area/*' `
  --path-glob 'fine_tuning/student_model_lora/*'
```
> 备选：若只想按扩展名批量清 LoRA 权重与所有 sqlite：
> `git filter-repo --force --path-glob '*.safetensors' --path-glob '*.bin' --path-glob '*chroma*.sqlite3' --invert-paths`

**0.4 从索引移除生成物（保留工作树文件）**
```powershell
cd $repo
git rm -r --cached --ignore-unmatch **/__pycache__
git rm --cached --ignore-unmatch fine_tuning/.env scrapped_data_demo/.env tests/.env
# 生成物（若仍被跟踪）
git rm --cached --ignore-unmatch UK_Rent_Agent_Technical_Report.pdf UK_Rent_Agent_Technical_Report.html quick_test_results.csv
git rm --cached --ignore-unmatch local_data_demo/data/scraped_property_listings.csv
```
> 51 个 `.pyc` 全在各 `__pycache__/` 下，上面第一条 `git rm -r --cached **/__pycache__` 会一并移除。可用 `git ls-files '*.pyc'` 复核归零。

**0.5 收紧 `.gitignore`（在现有基础上追加，勿重排）**
追加：
```
# 打包
/build/
/dist/
/src/*.egg-info/

# 本地模型权重（历史已清，防再次误提交）
student_model_lora/
fine_tuning/student_model_lora/
*.safetensors
*.bin

# 生成的报告 / 临时产物
UK_Rent_Agent_Technical_Report.*
quick_test_results.csv
diagnose_geocoding.py

# 所有 .env（含子目录）
**/.env

# 本手册（仅本地）
REFACTOR_IMPLEMENTATION_PLAN.md
```

**0.6 删除工作树垃圾**
```powershell
cd $repo
Remove-Item -Recurse -Force student_model_lora, maps, map_visualization
Remove-Item -Force quick_test_results.csv, diagnose_geocoding.py
Remove-Item -Force UK_Rent_Agent_Technical_Report.pdf, UK_Rent_Agent_Technical_Report.html
Remove-Item -Recurse -Force local_data_demo/data/fake_property_listings1.csv
```
> `student_model_lora/`（根）0 文件被跟踪，直接删；`fine_tuning/` 的 LoRA 权重在 0.3 已从历史清除、0.5 已忽略，工作树是否保留取决于是否还要训练；不再训练则 `Remove-Item -Recurse -Force fine_tuning/student_model_lora`。

**0.7 gc 压缩**
```powershell
cd $repo
git reflog expire --expire=now --all
git gc --prune=now --aggressive
du -sh .git   # 若装了 GNU du；否则： (Get-ChildItem .git -Recurse | Measure-Object Length -Sum).Sum/1MB
```

**0.8 裁剪已合并分支**
```powershell
cd $repo
git branch --merged main            # 先看哪些已并入 main
# 逐个删除已确认合并且无用的（保留 main / 当前工作分支 / fengyuan/agent 待审）
git branch -d docs/readme-mcp-memory feature/mcp-refactor local-data-demo baseline/pre-openrent-scraper
# fengyuan/agent 领先16/落后54 —— 审阅后再定，勿在本 Phase 删
```

#### 涉及文件清单（old → 处置）
| 路径 | 处置 |
|------|------|
| `**/*.pyc`, `**/__pycache__/` | `git rm --cached` + 忽略 |
| `fine_tuning/.env`, `scrapped_data_demo/.env`, `tests/.env`, 历史 `local_data_demo/.env` | filter-repo 清历史 + rm --cached |
| `chroma_db*/`, `local_data_demo/chroma_db*/`, `map_visualization/chroma_db*/` | filter-repo 清历史 |
| `fine_tuning/student_model_lora/*`（LoRA 权重） | filter-repo 清历史 + 忽略 |
| 根 `student_model_lora/`（298 MB 未跟踪） | 删工作树 |
| 根 `maps/`, `map_visualization/` | 删工作树 |
| `UK_Rent_Agent_Technical_Report.{pdf,html}`, `quick_test_results.csv`, `diagnose_geocoding.py` | 删 + 忽略 |
| `local_data_demo/data/fake_property_listings1.csv` | 删 |

#### 测试与验收标准
- `git ls-files '*.pyc'` → 空。
- `git ls-files | Select-String '\.env$'` → 空。
- `git rev-list --all --objects | Select-String 'chroma.*\.sqlite3|\.safetensors'` → 空。
- `.git` 体积 < 50 MB。
- `git status` 干净（除新 `.gitignore`）。
- 应用仍能启动（`cd local_data_demo; python app.py` 正常监听 5001，功能未变）。

#### 风险与回滚
- **风险**：filter-repo 重写全历史 SHA；协作者需重新 clone；开放 PR 会失效。
- **缓解**：先在 0.2 的备份上演练；与协作者约定时间窗；改写后 `git push --force-with-lease` 到远端并通知全员重新 clone。
- **回滚**：直接从 0.2 备份恢复整个仓库目录。

---

### Phase 1 — 安全网（特征化测试 + 打包）

#### 目标
1. 在**任何重构之前**，先写覆盖当前行为的**特征化测试**（characterization tests），锁住现有可观测行为（含已知 bug 的现状），作为后续所有阶段的安全网。
2. 固定所有依赖版本 + 补 `geopy` + 移除未用依赖 + 把 torch 栈设为可选 extra。
3. 引入 `pyproject.toml` 打包，消除 CWD 相对导入，使应用可从任意目录启动。
4. **验收：`pytest` 全绿；应用可从任意目录 `python -m uk_rent_agent.web` 启动。**

> 说明：Phase 1 的测试断言的是**当前真实行为**。对已知 bug（如双重包装），测试先断言「现状」（例如 POI 返回被包成 `ToolResult`），到 Phase 4 修复时再把该断言翻转为「期望」。这样每个阶段都能靠 diff 看清行为变化。

#### 前置条件
- Phase 0 已完成（干净仓库）。
- 建虚拟环境：`python -m venv .venv; .\.venv\Scripts\Activate.ps1`。
- 装测试依赖：`pip install pytest pytest-asyncio`。

#### 详细步骤

**1.1 建测试骨架**
- 新建 `tests/conftest.py`、`tests/fixtures/properties_sample.csv`（从 `local_data_demo/data/fake_property_listings.csv` 拷 6~8 行，列必须 == `RICH_COLUMNS`）。
- `conftest.py` 提供：把 `local_data_demo/` 加入 `sys.path` 的 fixture（打包完成前的过渡）、`monkeypatch` 掉网络（`calculate_travel_time`、`get_search_snippets`、DeepSeek `invoke`）的 fixture。

**1.2 工具契约测试**（`tests/test_tool_contract.py`，细节见 §10）
对全部 11 个工具断言 `registry.get(name)` 存在、`execute()` 返回 `ToolResult`、以及 `result.data` 的形状。**关键**：为 `web_search`/`search_nearby_pois` 断言「现状」——`result.data` 是被包装的对象（记录双重包装现象），Phase 4 翻转。

**1.3 路由决策表测试**（`tests/test_router.py`）
对固定 query 断言 `_compute_decision`/`_majority_vote`（monkeypatch 掉 LLM `invoke` 返回定值）路由到的工具，覆盖：greeting→`direct_answer`、property_context→`reasoning_property`、recall 关键词→`direct_answer`、`find me...`→`search_properties`、安全无地址→`clarification` 等。

**1.4 `/api/alex` 金标准测试**（`tests/test_api_alex.py`）
用 Flask test client（monkeypatch 掉 graph 的 LLM 与网络），断言 3 种 `response_type`：`search`（有 recommendations）、`clarification`、`chat`。

**1.5 搜索管线测试**（`tests/test_search_pipeline.py`）
用 fixture CSV，断言 `search_properties_impl` 在给定 `location/max_budget/max_commute_time` 下返回 `status='found'`、`recommendations` 结构、`summary` 无 `999`、周租→月租换算（`4.33`）正确。

**1.6 打包（pyproject + src 布局过渡）**
- 先**不**移动文件，仅加根 `pyproject.toml`（§9 骨架），用 `[tool.setuptools.packages.find]` 暂时指向 `local_data_demo`，或用 `pythonpath`（pytest.ini/pyproject `[tool.pytest.ini_options] pythonpath=["local_data_demo"]`）让测试可运行。真正的 `src/` 迁移在 Phase 2~5 逐步做（避免一次性大爆炸）。
- 固定依赖：把 `requirements.txt` 内容迁入 `pyproject` 的 `dependencies`，逐个 `pip freeze` 出精确版本回填（目标见 §8）。补 `geopy`；移除 `fastapi/uvicorn/googlemaps/scikit-learn/langchain-community`；`torch/transformers/peft/accelerate/torchvision/torchaudio` 归入 `[project.optional-dependencies] finetune`。

**1.7 依赖守护**
- 加 `import-linter`（`pip install import-linter`）合约文件 `importlinter.ini`，声明 §3.2 的分层，Phase 2+ 逐步收紧。此阶段可先只声明「rag 不得 import tools/agent/web」。

#### 涉及文件清单
| 新建 | 说明 |
|------|------|
| `pyproject.toml` | 打包 + 依赖 + pytest 配置 |
| `tests/conftest.py`, `tests/fixtures/properties_sample.csv` | 夹具 |
| `tests/test_tool_contract.py` / `test_router.py` / `test_api_alex.py` / `test_search_pipeline.py` | 特征化测试 |
| `importlinter.ini`（可选） | 依赖守护 |

#### 具体命令
```powershell
cd $repo
pip install pytest pytest-asyncio import-linter
# 运行全部测试
python -m pytest -q
# 只跑契约测试
python -m pytest tests/test_tool_contract.py -q
# 从任意目录验证可启动（打包后）
pip install -e .
python -c "import uk_rent_agent" 2>$null; if ($?) { "package importable" }
```

#### 测试与验收标准
- `python -m pytest -q` 全绿，且**覆盖 11 个工具 + 3 种 response_type + 搜索管线**。
- `pip install -e .` 成功；应用可从仓库根启动而非必须 `cd local_data_demo`（打包 entry point 到位后）。
- `requirements`/`pyproject` 依赖与实际 import 一致（`geopy` 在、`fastapi` 等移除或标注）。

#### 风险与回滚
- **风险**：特征化测试若断言了「理想行为」而非「现状」，会在后续阶段误报。**务必断言现状**。
- **回滚**：测试与 pyproject 是新增文件，删除即回滚，不影响应用。

---

### Phase 2 — 删除优先（死代码清理）

#### 目标
先删无争议的死代码，缩小后续重构面。删除后**特征化测试仍须全绿**。

#### 前置条件
- Phase 1 完成，`pytest` 绿。
- 关键前置：`scrapped_data_demo/scrapper/` 里 `rightmove_scraper.py`、`scrape_zoopla_listings.py` **先迁入** `core/scraping/legacy_scrapers/`（或目标 `data/scraping/legacy_scrapers/`）并改好 `load_legacy` 的引用路径，**再**删 `scrapped_data_demo/`。

#### 详细步骤

**2.1 迁移存活的 legacy 爬虫（删 `scrapped_data_demo/` 之前）**
- 新建目录 `local_data_demo/core/scraping/legacy_scrapers/`（含 `__init__.py`）。
- 拷入 `scrapped_data_demo/scrapper/rightmove_scraper.py`、`scrape_zoopla_listings.py`（及它们内部互相 import 的 `multi_search.py`、`filter_by_date.py` 若被引用）。
- 修改 `core/scraping/config.py`：把 `SCRAPPER_DIR`（`:34`）指向新目录，或把 `load_legacy`（`:136-148`）改为直接 `from .legacy_scrapers import rightmove_scraper`（更干净，去掉 `sys.path.insert` 黑魔法）。
- 核对调用点：`rightmove.py:161 load_legacy("rightmove_scraper")`、`zoopla.py:54 load_legacy("scrape_zoopla_listings")`。
- 跑 `python scraping_selftest.py` 确认 PAGE_MODEL 解析等仍通过（该自检脚本断言 `rightmove._extract_page_model`、`normalize_property`、CSV roundtrip、`load_properties` auto 回落）。

**2.2 删除死代码**
- `tests/`（仓库根，陈旧快照）——已被 Phase 1 的新 `tests/` 取代，删旧内容（注意：新测试文件若也放 `tests/`，需先把旧快照清空只留新测试；建议 Phase 1 就把新测试放好，此处删除旧的 `.py` 快照/`.env`/`apartment-finder-ui.html`/`core/`/`rag/`/`tool_system/` 子树）。
- `scrapped_data_demo/`（顶层）——2.1 迁移后整删。
- `map_visualization/`——已在 Phase 0 删。
- `fine_tuning/` 的生成物（`dataset_raw.json`、`train.jsonl`、`test.jsonl`、`student_model_lora/` 权重、评测报告）——若不再训练，整个 `fine_tuning/` 移入 `legacy/` 或删。
- `core/tool_system.py`：删 `class FunctionCalling`（`:403-555`）与 `class SmartFunctionCalling`（`:598-789`）；`to_llm_format`（`:174-213`）与 `list_tools_for_llm`（`:283-333`）随之删（传递性死）。保留 `ToolResult`、`Tool`、`ToolRegistry`、`extract_json_from_text`（若无人用亦删）、`create_tool_registry`。
- `core/llm_interface.py`：把 `generate_recommendations`（`:944-1280`）、`refine_criteria_with_answer`（`:592-739`）及其私有辅助（`_get_property_url`:740、`_normalize_price_format`:747、`_validate_and_fix_price_in_explanations`:781、`create_fallback_recommendations`:1281-1434）连同 `interactive_main.py` 一起移入 `legacy/`（或删）。活跃只保留 `clarify_and_extract_criteria`（`:328`）、`call_ollama`（`:50`）、`_call_deepseek`（`:28`）及其直接依赖。
- `rag/conversation_memory.py`——删（写死）。同步删 `RAGCoordinator.__init__` 里的 `self.conversation_memory`（`rag_coordinator.py:9`）与 `enhanced_search` 中 `past_context` 相关（`:34-42`），返回值从三元组改二元组（连带改 `search_properties.py:507` 解包）。
- `rag/area_knowledge.py`——删（仅 1 行 Camden 且输出丢弃）。同步删 `self.area_knowledge`（`rag_coordinator.py:10`）、`get_context` 调用（`:43-49`）、`_hybrid_rank` 的 `area_info` 形参（`:52-54/59`）。
- 陈旧 chroma 目录：仓库根 `chroma_db/`、`chroma_db_area/`（已在 Phase 0 从历史清；工作树删），`map_visualization` 的（已随目录删）。
- `data/fake_property_listings1.csv`——已在 Phase 0 删。

**2.3 更新 registry 导入**
删 `conversation_memory`/`area_knowledge` 后，确认 `rag_coordinator.py` 顶部 import（`:3-4`）已移除，`enhanced_search` 只 `return scored_results`（或 `scored_results, {}`）。

#### 涉及文件清单（old → 处置）
| 路径 | 处置 |
|------|------|
| `scrapped_data_demo/scrapper/{rightmove_scraper,scrape_zoopla_listings}.py` | 迁入 `core/scraping/legacy_scrapers/` |
| `scrapped_data_demo/`（顶层其余） | 删 |
| `tests/`（旧快照子树） | 删（保留 Phase 1 新测试） |
| `map_visualization/`, 根 `maps/` | 删（Phase 0 已做） |
| `fine_tuning/`（生成物 + 权重） | 移 `legacy/` 或删 |
| `core/tool_system.py` `FunctionCalling`/`SmartFunctionCalling`/`to_llm_format`/`list_tools_for_llm` | 删 |
| `core/llm_interface.py` `generate_recommendations`/`refine_criteria_with_answer`/私有价格辅助 | 移 `legacy/` 或删 |
| `interactive_main.py` | 移 `legacy/` 或删 |
| `rag/conversation_memory.py`, `rag/area_knowledge.py` | 删 |
| `rag/rag_coordinator.py`（去两个 memory/area 引用） | 改 |
| `core/tools/search_properties.py:507`（解包三元组→二元组） | 改 |

#### 具体命令
```powershell
cd "$repo\local_data_demo"
# 迁移 legacy 爬虫后自检
python scraping_selftest.py
# 删除
Remove-Item -Recurse -Force ..\scrapped_data_demo, ..\map_visualization -ErrorAction SilentlyContinue
Remove-Item -Force rag\conversation_memory.py, rag\area_knowledge.py
# 回归
cd $repo
python -m pytest -q
```

#### 测试与验收标准
- `python scraping_selftest.py` 打印 `ALL SELFTESTS PASSED`。
- `python -m pytest -q` 仍全绿（特征化测试未回归）。
- `grep -r FunctionCalling local_data_demo/` = 0；`grep -r conversation_memory local_data_demo/` = 0；`grep -r area_knowledge local_data_demo/` = 0。
- 应用启动正常。

#### 风险与回滚
- **风险**：删 `conversation_memory`/`area_knowledge` 会改 `enhanced_search` 签名（三元组→二元组）；漏改 `search_properties.py:507` 会 `ValueError: too many values to unpack`。
- **风险**：legacy 爬虫迁移后路径没接对，`load_legacy` 抛 ImportError；`scraping_selftest.py` 会立刻暴露。
- **回滚**：本 Phase 全在版本控制内，`git restore` 或撤销 PR 即可。

---

### Phase 3 — 统一数据层

#### 目标
1. 建**唯一** `PropertyRepository`，合并 §2.3 的 5 个加载入口。
2. `search_properties` 最终消费 `PROPERTY_SOURCE` 选中的数据（不再恒用假 CSV）。
3. 以 `RICH_COLUMNS` 为唯一 schema，在加载边界做一次大小写归一化，删除所有 `prop.get('X', prop.get('x'))` 回退。
4. **唯一** `RAGCoordinator` 实例，依赖注入，每进程只构建一次 FAISS。
5. 缓存持久化到磁盘（sqlite/JSON，沿用现有 md5 键）。
6. 常数集中到 `domain/constants`。
7. `source` 字段贯通到 UI，让「假数据 vs 爬取数据」可见。
8. `PROPERTY_SOURCE=auto` 为默认 + 基于 TTL 的刷新策略。

#### 前置条件
- Phase 2 完成，`pytest` 绿。
- **爬虫链路修复（见 3.9）已由并行 agent 处理并验证**——本 Phase 依赖「能产出符合 `RICH_COLUMNS` 的 CSV」。

#### 详细步骤

**3.1 `PropertyRepository` 接口**（`data/repository.py`）
```python
# data/repository.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uk_rent_agent.domain.schema import RICH_COLUMNS
from uk_rent_agent.config import Config

@dataclass(frozen=True)
class LoadResult:
    properties: list[dict]     # 每个 dict 的键已按 RICH_COLUMNS 归一化
    source: str                # 'scraped' | 'fake'
    csv_path: Path
    is_stale: bool

class PropertyRepository:
    """所有房源数据的唯一入口。合并旧 load_properties / load_mock_properties_from_csv /
    get_live_properties / provider.get_active_property_csv / get_property_details._active_data_path。"""

    def __init__(self, config: Config):
        self._cfg = config
        self._cache: Optional[LoadResult] = None   # 进程内一次性加载

    def load(self, *, force_refresh: bool = False) -> LoadResult:
        """按 PROPERTY_SOURCE(=csv|scraper|auto) 返回归一化房源。auto: 有新鲜缓存用之，
        否则假 CSV；SCRAPE_ON_STARTUP 时才允许阻塞式爬取。结果进程内缓存，除非 force_refresh。"""

    def active_csv_path(self) -> Path:
        """当前后端 CSV 路径（scraped 缓存存在则用之，否则假 CSV）——供 get_property_details
        与 search 保持同源。"""

    def get_by_address(self, address: str) -> Optional[dict]:
        """按地址前缀/包含匹配单条房源（替代 app.py:324-347 与 :485-491 的内联查找）。"""

    @staticmethod
    def _normalize_keys(row: dict) -> dict:
        """在加载边界把每行归一化到 RICH_COLUMNS 的规范大小写，补全缺列为默认值，
        parse 出 parsed_price / postcode。此后全链路只用规范键，删除 prop.get('X', prop.get('x'))。"""
```
- 迁移点：
  - `data_loader.load_properties`（`data_loader.py:34`）→ `repository.load`。
  - `load_mock_properties_from_csv`（`:9`）→ 内部私有 `_read_fake_csv`。
  - `get_live_properties`（`:90`，仅 legacy）→ 随 `interactive_main` 入 `legacy/`。
  - `provider.get_active_property_csv`（`provider.py:30`）→ `repository.active_csv_path`。
  - `get_property_details._active_data_path`（`get_property_details.py:24`）→ 改用注入的 `repository.active_csv_path()`。

**3.2 search_properties 消费统一数据源**
- 删 `_get_rag_coordinator`（`search_properties.py:124-140`）里的 `load_mock_properties_from_csv()`（`:133`）与主流程的 `load_mock_properties_from_csv()`（`:489`）。
- 改为接收注入的 `PropertyRepository` + 单例 `RAGCoordinator`（见 3.4）；房源来自 `repository.load().properties`。
- 把 `search_properties.py` 里 4~5 段近乎重复的澄清返回块（`:398 / :414 / :430 / :447`，形状高度一致）折叠为一个「缺失字段 → 澄清」循环（见 Phase 4.5，本 Phase 可先只统一数据源，折叠留 Phase 4）。

**3.3 唯一 schema + 归一化，删双大小写回退**
- `RICH_COLUMNS` 从 `scraping/config.py:40` 迁到 `domain/schema.py`。
- 在 `_normalize_keys` 里统一大小写；随后删除散落的 `prop.get('Images', prop.get('images', []))`（`search_properties.py:665/735`）、`prop.get('Geo_Location', prop.get('geo_location', ''))`（`:668/740`）、`prop.get('Address', prop.get('address', ...))`（`:672/749` 等）、`prop.get('URL', prop.get('url',...))` 等所有双写回退。
- 同步改 `rag_coordinator._hybrid_rank`（`rag_coordinator.py:74-92`）里 `prop.get('parsed_price')`/`prop.get('Price')` 的兜底解析——归一化后 `parsed_price` 恒在。

**3.4 唯一 RAGCoordinator（依赖注入，单次 FAISS）**
- 删 app 启动那次「从不被查询」的 FAISS 构建（`app.py:181/202-211`），或反过来——**保留** app 启动构建的这一份为**唯一**实例，注入给 agent 与 search 工具，删掉 `search_properties._get_rag_coordinator` 的私有单例。二选一，推荐后者（启动构建 + 注入）。
- `RAGCoordinator` 由 `create_app()`/组合根构造一次，经 `build_agent_graph(tool_provider, rag_coordinator=...)` 与工具注册一路注入。
- MCP 子进程若仍作为独立入口存在，它内部会各自构建一次 FAISS（不可避免，进程隔离）；但默认拓扑改为 web 直连进程内 registry（见 Phase 5），从而消除「MCP 子进程 + 主进程」的双份构建。

**3.5 缓存持久化**（`data/cache.py`）
```python
# data/cache.py —— 替换 core/cache_service.py 的纯内存 _cache
class PersistentCache:
    def __init__(self, path: Path, max_entries: int = 5000): ...
    def get(self, key: str): ...            # 沿用 create_cache_key 的 md5 键
    def set(self, key: str, value) -> None: ...  # 写 sqlite（或 JSON），进程重启后仍在
    @staticmethod
    def make_key(func_name: str, *args, **kwargs) -> str: ...  # == 旧 create_cache_key
```
- 键方案与 `cache_service.py:17-27` 完全一致（`json.dumps(sort_keys=True)` + `md5`），确保旧调用点无缝切换。
- 后端建议 sqlite（`sqlite3` 标准库，单文件、并发安全、带 LRU 淘汰）。

**3.6 常数集中**（`domain/constants.py`）见 §6 迁移表。全部替换为 `from uk_rent_agent.domain import constants as C`；`C.NO_COMMUTE_LIMIT`、`C.BUDGET_SOFT_MULTIPLIER`、`C.WEEKS_PER_MONTH` 等。

**3.7 source 字段贯通 UI**
- `LoadResult.source`（'scraped'|'fake'）→ 在 `search_properties` 的每条 `recommendation` 里加 `'source'` 字段 → `format_output`/`/api/alex` 响应携带 → 前端在卡片上标注「实时房源 / 示例数据」。

**3.8 PROPERTY_SOURCE=auto 默认 + TTL 刷新**
- `Config.PROPERTY_SOURCE` 默认 `'auto'`（已是 `data_loader.py:49` 现状）。
- TTL 沿用 `SCRAPER_CACHE_TTL_HOURS`（`scraping/config.py:60`，默认 24h）；`repository.load` 判断缓存新鲜度（`provider._is_fresh` 逻辑 `provider.py:37-41`）。
- 提供后台刷新路径（可选）：一个 `scripts/build_scraped_dataset.py` 定时任务，或应用内 TTL 到期触发的异步重爬（非阻塞）。

**3.9 爬虫链路修复（前置子任务，验证程序）**
> 一个并行 agent 正在诊断/修复实时爬虫（用户报告「所有房源获取都挂了」）。本小节**不假设其结论**，只给出对接与验证程序。

- 依赖关系：Phase 3 的统一数据层要在 `PROPERTY_SOURCE=scraper|auto` 下真正拿到爬取数据，必须先让爬虫链路产出合法 CSV。
- 验证程序（爬虫修复完成后逐条执行）：
  1. `cd local_data_demo; python build_scraped_dataset.py`（默认 tasks，OpenRent 源；`build_scraped_dataset.py:40-92`）。期望：非空、打印 `[OK] Wrote N properties to .../data/scraped_property_listings.csv`、按 platform 的 breakdown、`With coordinates: k/N`。
  2. `python scraping_selftest.py`（`scraping_selftest.py`）。期望：`ALL SELFTESTS PASSED`；其中断言 `normalize_property` 产出的键集合 **== `RICH_COLUMNS`**（`:48`）、`Enhanced_Description` 含 `Room Type:`/`Amenities:`、CSV roundtrip 的 `Images` 复原为 list。
  3. 期望 CSV 形状：列头 == `RICH_COLUMNS`（14 列：`Price, Address, Description, URL, Available From, Platform, Images, geo_location, Room_Type_Category, Detailed_Amenities, Guest_Policy, Payment_Rules, Excluded_Features, Enhanced_Description`）；`Images` 列是 Python list 的字符串表示（`ast.literal_eval` 可解，见 `data_loader.py:23`）；`geo_location` 形如 `"51.5237, -0.1585"`。
  4. 设 `PROPERTY_SOURCE=scraper`，`python -c "from core.data_loader import load_properties; print(len(load_properties()))"`，期望 > 0 且样本含 `Address/Price/Enhanced_Description`。
- 若爬虫仍不可用：`auto` 模式会回落假 CSV（`data_loader.py:62-66` / `provider.py:161-168`），应用不阻断；`source` 字段标 `'fake'`，UI 明示。Phase 3 的统一数据层与此解耦——它只依赖「Repository 能给出归一化房源」，来源可 scraped 可 fake。

#### 涉及文件清单（old → new）
| old | new |
|-----|-----|
| `core/data_loader.py`（`load_properties`/`load_mock...`/`parse_price`/`extract_postcode`/`filter_by_budget`） | `data/repository.py` + `data/parsing.py` |
| `core/data_loader.py`（`get_live_properties`） | `legacy/`（随 interactive_main） |
| `core/scraping/provider.py`（`get_active_property_csv`） | `data/repository.active_csv_path` |
| `core/tools/get_property_details.py`（`_active_data_path`） | 改用注入 repository |
| `core/scraping/config.py:40 RICH_COLUMNS` | `domain/schema.py` |
| `core/cache_service.py` | `data/cache.py`（持久化） |
| `core/tools/search_properties.py`（`_get_rag_coordinator`/两处 load_mock/双大小写回退） | 消费注入 repository + 单例 RAG |
| `rag/rag_coordinator.py`（多实例隐患） | 单例注入 |
| 散落常数（§6） | `domain/constants.py` |

#### 具体命令
```powershell
cd "$repo\local_data_demo"
# 构建/刷新爬取缓存（爬虫修复后）
python build_scraped_dataset.py
python scraping_selftest.py
# 验证统一数据层
$env:PROPERTY_SOURCE = "auto"
python -c "from core.data_loader import load_properties; ps=load_properties(); print(len(ps), ps[0].keys() if ps else 'EMPTY')"
cd $repo
python -m pytest -q
```

#### 测试与验收标准
- 新增 `tests/test_repository.py`：断言 `PropertyRepository.load()` 返回归一化键（== `RICH_COLUMNS`）、`source` 正确、`csv` 与 `active_csv_path` 同源。
- 新增/改 `tests/test_search_pipeline.py`：断言 search 消费的房源与 repository 同源（不再恒假 CSV）。
- 断言进程内 FAISS 只构建一次（可用计数 spy / 日志断言）。
- 缓存持久化：写入后重启进程仍能命中（`tests/test_cache.py`）。
- 全量 `pytest` 绿。

#### 风险与回滚
- **风险**：归一化改键后，凡是仍按旧大小写取值的地方会取空。**对策**：`_normalize_keys` 保证输出严格 == `RICH_COLUMNS`，并写一条测试断言键集合；grep 清除所有 `prop.get('x', prop.get('X'`。
- **风险**：把「唯一 RAGCoordinator」注入路径接错会导致 FAISS 未构建、搜索空结果。**对策**：组合根构造后立即 `build_index`，测试断言 `index is not None`。
- **回滚**：保留旧 `data_loader.py` 一个发布周期（标 `@deprecated` 转调 repository），出问题快速切回。

---

### Phase 4 — Agent 编排层

#### 目标
1. 统一工具返回契约：所有 `func` 返回**普通 dict**，`Tool.execute` 统一包装——**修双重包装 bug**。
2. 合并 4 个路由函数为**一个决策模块**，词表 == registry 工具名 + 显式非工具结局（`direct_answer`/`clarification`/`reasoning_property`），让 5 个不可达工具可达。
3. `AgentState` 加 `user_id`，在 `execute_tool_node` 注入工具 params——**修记忆桶泄漏**。
4. 集中 prompt 到 `agent/prompts.py`。
5. 合并 3 个配置岛为 `config.py`。
6. 拆分巨文件。

#### 前置条件
- Phase 3 完成，`pytest` 绿。

#### 详细步骤

**4.1 统一工具返回契约**
- 约定：**所有工具 `func` 返回 `dict`**（禁止返回 `ToolResult`）。`Tool.execute`（`tool_system.py:94-153`）保持「把 dict 包成 `ToolResult(success=True, data=dict)`」。
- 修 `web_search_func`（`core/tools/web_search.py:93/107/121`）：把 3 处 `return ToolResult(...)` 改为 `return {...}`（成功返回 `{"success": True, "query":..., "results":..., "detailed_data":...}`；失败返回 `{"success": False, "error":...}`）。去掉 `from core.tool_system import ... ToolResult`。
- 修 `search_nearby_pois_impl`（`core/tools/search_nearby_pois.py:442/486/511/524`）：4 处 `return ToolResult(...)` → `return {...}`（成功含 `"pois"` 键与 `"address"`）。
- **翻转特征化断言**：Phase 1 中对这两个工具「现状是被包装对象」的断言，改为「`result.data` 是含预期键的 dict」。
- 连带验证下游现在能生效：`format_output_node`（`langgraph_agent.py:828`）的 `isinstance(raw_data, dict) and raw_data.get('pois')` 现在为真 → `_format_pois`（`:876`）触发；`_route_after_execution`（`:936-941`）能正确路由到 `format_output`。

**4.2 统一决策模块**（`agent/router.py`）
```python
# agent/router.py
from dataclasses import dataclass, field
from enum import Enum

class Outcome(str, Enum):
    # 非工具结局
    DIRECT_ANSWER = "direct_answer"
    CLARIFICATION = "clarification"
    REASONING_PROPERTY = "reasoning_property"
    MULTI_SEARCH = "multi_search"
    # 真实注册工具（词表必须 == registry.list_tool_names()）
    SEARCH_PROPERTIES = "search_properties"
    GET_PROPERTY_DETAILS = "get_property_details"
    CHECK_SAFETY = "check_safety"
    SEARCH_NEARBY_POIS = "search_nearby_pois"
    CALCULATE_COMMUTE = "calculate_commute"
    CALCULATE_COMMUTE_COST = "calculate_commute_cost"
    CHECK_TRANSPORT_COST = "check_transport_cost"
    GET_WEATHER = "get_weather"
    WEB_SEARCH = "web_search"
    RECALL_MEMORY = "recall_memory"
    REMEMBER = "remember"

@dataclass
class ToolDecision:
    outcome: Outcome
    params: dict = field(default_factory=dict)
    reason: str = ""
    clarification_message: str = ""

class Router:
    def __init__(self, registry, classification_llm):
        self._registry = registry
        self._llm = classification_llm
        # 启动即校验：分类词表覆盖每个注册工具
        assert set(t for t in registry.list_tool_names()) <= {o.value for o in Outcome}

    def decide(self, state) -> ToolDecision:
        """合并旧 _compute_decision(389-425) / _majority_vote(444-490) /
        _heuristic_fallback(581-595) / _build_tool_params(598-645) 为一处。
        顺序：recall 关键词→DIRECT_ANSWER；property_context→REASONING_PROPERTY；
        greeting→DIRECT_ANSWER；否则 LLM 投票（词表=全部 Outcome）→ 参数装配。"""
```
- 关键改动：
  - `CLASSIFICATION_PROMPT`（现 `langgraph_agent.py:278-293`，8 项）扩展到覆盖所有真实工具 + 显式非工具结局；`_majority_vote` 的优先匹配列表（`:459-461`）同步扩展。
  - `_build_tool_params`（`:598-645`）改为按 `Outcome` 装配；为原先不可达的 `calculate_commute`（时间-only）、`check_transport_cost`、`get_property_details`、`recall_memory`、`remember` 补装配分支。
  - `get_property_details` 装配：从 `_resolve_target_address`（`:501`）解析地址 → `{"address": addr}`。
  - `recall_memory`/`remember` 装配：**注入 `user_id`/`session_id`**（来自 state，见 4.3）。
- `decide_tool_node`（`langgraph_agent.py:427-439`）改为调用 `Router.decide`，`Command(goto=...)` 映射不变。

**4.3 user_id 贯通 + 修记忆桶泄漏**
- `AgentState`（`langgraph_agent.py:58-76`）加字段 `user_id: str`、`session_id: str`。
- `create_initial_state`（`:1055-1080`）加参数 `user_id="default"`, `session_id="default"`，`app.py:430` 调用处传入 `resolve_identity` 的结果。
- `execute_tool_node`（`:693-767`）：对 `recall_memory`/`remember`（及任何需要身份的工具），在 `params` 里注入 `state["user_id"]`/`state["session_id"]`，再 `registry.execute_tool(...)`。
- `memory_tools.py` 的 `Tool.parameters` schema（`:42-49 / :61-67`）**补上** `user_id`、`session_id`（可选，默认 `"default"`）——这样即便 LLM 直接调也不会丢身份；但主注入点在 `execute_tool_node`（不依赖 LLM 填）。

**4.4 集中 prompts**（`agent/prompts.py`）
- 把 `CLASSIFICATION_PROMPT`（`:278`）、`REASONING_PROPERTY_PROMPT`（`:295`）、`SYNTHESIS_PROMPT`（`:313`）、`_plan_web_searches` 内联 prompt（`:652-656`）、`generate_response_node` 的直答 prompt（`:798`）、`agent_memory` 的评分/抽取/整合/反思 prompt（`agent_memory.py:131/146/178/237`）全部集中到 `agent/prompts.py`（记忆相关可留 `rag/prompts.py`）。

**4.5 折叠 search_properties 的重复澄清块**
- `search_properties_impl` 里 4 段 `status='need_clarification'` 返回（`:398 / :414 / :430 / :447`）折叠为：
```python
REQUIRED = [("location", "destination/location ..."),
            ("max_budget", "budget ..."),
            ("max_commute_time", "max commute time ...")]
missing = [(k, hint) for k, hint in REQUIRED if not _present(locals()[k])]
if missing:
    return _clarify(missing, extracted_so_far=...)
```
> **⚠️ 审计校正 8**：审计称「5 段近乎重复的澄清块」。实际在 `impl` 内是 **4 段**结构一致的 `need_clarification` 返回（`:398/:414/:430/:447`）；另有 `:398` 前的抽取分支与 `no_results`/`no_exact_match_but_similar`（`:688/:713`）是不同形状，不计入折叠。折叠目标是那 4 段。

**4.6 合并配置岛**（`config.py`）
```python
# config.py —— 合并 local_data_demo/config.py + core/llm_config.py + core/scraping/config.py(非schema)
from dataclasses import dataclass
import os
from dotenv import load_dotenv

@dataclass(frozen=True)
class Config:
    # LLM
    llm_provider: str            # LLM_PROVIDER, default 'deepseek'
    deepseek_api_key: str
    deepseek_base_url: str       # default https://api.deepseek.com
    deepseek_model: str          # default deepseek-chat
    ollama_base_url: str
    ollama_model: str
    # Maps / travel
    google_maps_api_key: str
    openrouteservice_api_key: str
    tfl_app_key: str
    use_travel_service: str      # 'google' | 'openroute'
    # Data / scraping
    property_source: str         # 'auto' | 'csv' | 'scraper'
    scrape_on_startup: bool
    scraper_cache_ttl_hours: float
    scraper_sources: list[str]
    scraper_limit_per_task: int
    scraper_price_band: tuple[int, int]
    # Serving
    flask_secret_key: str
    use_mcp_tools: bool
    # LoRA（可选）
    use_finetuned_model: bool

    @classmethod
    def from_env(cls) -> "Config": ...
```
- 删 `config.py:18-20/23` 与 `llm_config.py:22-24/19` 的 DeepSeek 重复；LLM 工厂 `get_react_llm`/`get_classification_llm`/`get_planning_llm`（`llm_config.py:54/61/68`）保留，改为读 `Config`。
- 把 `USE_FINETUNED_MODEL`（`llm_interface.py:15`，硬编码）改成 `Config.use_finetuned_model`（读 env，默认 False）。

**4.7 拆巨文件**
- `langgraph_agent.py`（1080）→ `agent/state.py`（`AgentState`+`create_initial_state`）、`agent/router.py`（决策）、`agent/prompts.py`、`agent/nodes.py`（各 `_make_*_node` + 格式化 helper `_format_safety/_format_pois/_format_commute_cost`）、`agent/graph.py`（`build_agent_graph`）。
- `llm_interface.py`（1434）→ `uk_rent_agent/llm.py`（`call_ollama`/`_call_deepseek`/`clarify_and_extract_criteria`）+ `legacy/`（CLI 部分，Phase 2 已迁）。
- `search_properties.py`（852）→ 折叠澄清（4.5）后拆出 `PropertyFilter` 到 `tools/_filters.py`，主流程瘦身。
- `maps_service.py`（819）、`tool_system.py`（Phase 2 删死代码后 ~360 行）按职责再拆（可选）。

#### 涉及文件清单（old → new）
| old | new |
|-----|-----|
| `core/tools/web_search.py`（返回 ToolResult） | `tools/web_search.py`（返回 dict） |
| `core/tools/search_nearby_pois.py`（返回 ToolResult） | `tools/search_nearby_pois.py`（返回 dict） |
| `core/langgraph_agent.py`（决策 4 函数） | `agent/router.py`（`Router.decide`+`ToolDecision`） |
| `core/langgraph_agent.py`（AgentState/create_initial_state） | `agent/state.py`（+user_id） |
| `core/langgraph_agent.py`（prompts） | `agent/prompts.py` |
| `core/langgraph_agent.py`（nodes/graph） | `agent/nodes.py` / `agent/graph.py` |
| `core/tools/memory_tools.py`（schema 缺 user_id） | 补 user_id/session_id |
| `core/tools/search_properties.py`（4 段澄清） | 折叠为 1 |
| `local_data_demo/config.py`+`core/llm_config.py`+`core/scraping/config.py`(非schema) | `config.py` |
| `core/llm_interface.py`（活跃部分） | `uk_rent_agent/llm.py` |

#### 具体命令
```powershell
cd $repo
python -m pytest tests/test_tool_contract.py -q     # 断言 web_search/pois 现在返回 dict
python -m pytest tests/test_router.py -q             # 断言 5 个原不可达工具现在可路由
python -m pytest -q                                  # 全量
# 端到端手测 POI 卡片路径已复活
cd local_data_demo; python app.py  # 触发一个「XX 附近有什么超市」查询，确认返回 poi_results 结构而非 repr 字符串
```

#### 测试与验收标准
- `tests/test_tool_contract.py`：`web_search`/`search_nearby_pois` 的 `result.data` 是 dict，含 `results`/`pois` 键；`result.success` 正确反映内部成败（不再被外层吞）。
- `tests/test_router.py`：新增用例断言 `calculate_commute`（纯时间）、`check_transport_cost`、`get_property_details`、`recall_memory`、`remember` 可被 `Router.decide` 选中并装配出合法 params。
- 记忆隔离测试：两个不同 `user_id` 分别 `remember` 后 `recall`，互不串桶（`tests/test_memory_isolation.py`）。
- POI 端到端：`/api/alex` 对 POI 查询返回 `tool_data.poi_results`（结构化）而非 repr。
- 全量 `pytest` 绿。

#### 风险与回滚
- **风险**：翻转双重包装断言的同时若下游还有别处假设 `raw_data` 是 `ToolResult`，会连锁。**对策**：grep `raw_data`/`result.data` 全部使用点，逐一核对。
- **风险**：扩大分类词表可能让 LLM 更易误选生僻工具。**对策**：保留 `_majority_vote` 的 tie-break 规则（`:477-488`）并加针对性测试。
- **回滚**：Phase 4 拆分较大，按 4.1/4.2/4.3 独立小 PR 分批合并，任一出问题只回滚该 PR。

---

### Phase 5 — 服务层

#### 目标
1. `create_app()` 工厂 + `SessionStore`（TTL/LRU 淘汰）。
2. per-user 收藏/历史（走 `user_id` 轴）。
3. 生产 WSGI（Windows 用 `waitress`），`debug=False`。
4. `atexit` 注册 `MCPToolClient.close`。
5. `logging` 替换 print。
6. **默认 MCP 拓扑变更**：web 应用直接用进程内 registry（快、并发、单 FAISS），`mcp_server.py` 仅作对外 MCP 客户端的独立入口保留。
7. 拆 `unified-ui.html` 为静态资源，删 2 份陈旧 UI 副本。

#### 前置条件
- Phase 4 完成，`pytest` 绿。

#### 详细步骤

**5.1 app 工厂 + SessionStore**
```python
# web/session_store.py
import time, threading
from collections import OrderedDict

class SessionStore:
    """替换 app.py:73-76 的裸 dict（无界）+ user_session.py:6 的进程全局 _session_data。
    per-user 隔离 L2 会话状态 + 收藏 + 历史，带 TTL 与 LRU 上限。"""
    def __init__(self, max_users: int = 10_000, ttl_seconds: int = 7*24*3600):
        self._data: OrderedDict[str, UserSession] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_users
        self._ttl = ttl_seconds

    def get(self, user_id: str) -> "UserSession": ...   # 懒建 + LRU touch + 过期淘汰
    def clear(self, user_id: str) -> None: ...
    def _evict_if_needed(self) -> None: ...             # 超上限淘汰最旧；超 TTL 淘汰

class UserSession:
    persistent_state: dict     # 原 _user_states 内容（default_persistent_state 形状）
    history: list              # 原 _user_histories
    last_results: list         # 原 _user_last_results
    favorites: dict            # 原 _session_data['favorites']，改为 per-user
    search_history: list       # 原 _session_data['search_history']，改为 per-user
```
- `web/app.py` 提供 `create_app(config: Config) -> Flask`，在其中构造 `SessionStore`、`PropertyRepository`、单例 `RAGCoordinator`、`tool_registry`、`agent_graph`，用 `app.config`/闭包注入路由。

**5.2 per-user 收藏/历史**
- `core/user_session.py` 的 `add_to_favorites`/`get_favorites`/`add_to_history`（`:23/35/13`）改为接收 `session: UserSession`（或 `user_id`）参数。
- 端点 `app.py:555-595`（`/api/favorites` POST/GET/DELETE、`/api/history` GET）改为先 `resolve_identity` → `store.get(user_id)` → 操作该用户的 `favorites`/`search_history`。删除对进程全局 `_session_data` 的所有引用（`app.py:19/581/582/592`）。

**5.3 生产 WSGI + debug=False**
- 加 `waitress` 依赖。入口 `web/__main__.py`：
```python
from waitress import serve
from uk_rent_agent.config import Config
from uk_rent_agent.web.app import create_app
app = create_app(Config.from_env())
serve(app, host="127.0.0.1", port=5001)   # 默认绑本地；对外部署再显式放开
```
- 删 `app.py:729-731` 的 `app.run(debug=True, host='0.0.0.0', ...)`。开发用 `flask --debug run` 显式开启，绝不进生产默认。
- 补最简鉴权：至少给写端点加一个 API token 中间件（`before_request` 校验 header），或部署在反代后。CORS 收窄到已知 origin（`CORS(app, origins=[...])`，替换 `app.py:26` 的裸 `CORS(app)`）。`FLASK_SECRET_KEY` 必须来自环境，移除硬编码回落（`app.py:32-36`）——无 env 时启动即失败（fail-fast）而非用弱密钥。

**5.4 atexit 注册 MCP close**
- 在构造 `MCPToolClient` 处（现 `app.py:163-168`）注册：`import atexit; atexit.register(_mcp_client.close)`（`close` 已存在于 `mcp_client.py:109-123`）。

**5.5 logging 替换 print**
- 新增 `logging_setup.py`：`logging.basicConfig(level=..., format=...)`，在 `create_app`/入口调用一次。
- 批量把 `print(...)` 换成 `logger.info/debug/warning`（≈586 处，可分模块渐进；startup/错误路径优先）。`traceback.print_exc()` → `logger.exception(...)`。

**5.6 MCP 拓扑变更**
- 默认：`create_app` 直接把**进程内 registry** 作为 `agent_tool_provider`（快、天然并发、与 web 共享同一单例 FAISS/Repository）。把 `USE_MCP_TOOLS` 默认改为 `0`（现 `app.py:159` 默认 `"1"`）。
- `mcp/server.py`（现 `mcp_server.py`）保留为**独立入口**，仅供外部 MCP 客户端（如 Claude Desktop）连接；不再是 web app 的默认工具通道。这样消除「web→MCP 子进程串行/双 FAISS」的开销与 §2.9 的 close 泄漏隐患（子进程仅在显式启用时存在）。
- `mcp_client.py` 保留（供需要时启用），并落实 5.4 的 atexit。

**5.7 拆前端 + 删陈旧 UI**
- `unified-ui.html`（1716 行，内联 JS `:692-1715`）→ `web/templates/unified-ui.html`（仅 HTML/模板）+ `web/static/js/app.js`（抽出的 ~1024 行 JS）+ `web/static/css/`。
- 删 `tests/apartment-finder-ui.html`（调用死端点 `/api/chat`、`/api/search`）与 `scrapped_data_demo/apartment-finder-ui.html`（Phase 2 删 `scrapped_data_demo/` 时已随之删）。

#### 涉及文件清单（old → new）
| old | new |
|-----|-----|
| `app.py:73-76`（无界 per-user dict）+ `user_session.py:6`（全局 `_session_data`） | `web/session_store.py`（`SessionStore`/`UserSession`） |
| `app.py`（脚本式，含 `app.run(debug=True...)`） | `web/app.py`（`create_app`）+ `web/__main__.py`（waitress） |
| `app.py:555-595`（收藏/历史端点） | `web/routes.py`（per-user） |
| `core/user_session.py`（全局函数） | `web/session_store.py` 方法 |
| `mcp_server.py` / `core/mcp_client.py` | `mcp/server.py` / `mcp/client.py`（+atexit） |
| `unified-ui.html`（内联 JS） | `web/templates/` + `web/static/` |
| `tests/apartment-finder-ui.html` | 删 |
| 全局 `print(...)` | `logging` |

#### 具体命令
```powershell
cd $repo
pip install waitress
# 生产式启动（本地绑定，debug off）
python -m uk_rent_agent.web
# 或验证 waitress
python -c "from uk_rent_agent.web.app import create_app; from uk_rent_agent.config import Config; print('app ok' if create_app(Config.from_env()) else 'fail')"
python -m pytest -q
```

#### 测试与验收标准
- `tests/test_session_isolation.py`：两个 `user_id` 的收藏/历史互不可见；超 `max_users` 触发 LRU 淘汰；超 TTL 过期。
- 启动断言 `app.debug is False`；无 `FLASK_SECRET_KEY` env 时 `create_app` 抛错（fail-fast）。
- `atexit` 关停：进程退出时 `MCPToolClient.close` 被调用（可用日志/spy 验证）。
- 默认拓扑：`USE_MCP_TOOLS` 未设时走进程内 registry；置 `1` 时才起子进程。
- 前端：`/` 正常渲染，静态资源分离加载；无对死端点的 fetch。
- 全量 `pytest` 绿；`waitress` 下手测主链路正常。

#### 风险与回滚
- **风险**：per-user 收藏迁移会改端点契约（原先全局收藏对所有人可见）；前端若假设全局需同步调整。
- **风险**：默认关 MCP 后，依赖 MCP 的外部集成需显式开启。
- **回滚**：`create_app` 与旧 `app.py` 可并存一个周期；`USE_MCP_TOOLS` 可随时切回。

---

## §5 附录 A：完整删除清单

> 删除前均已在 §2 核对「不在活跃调用图内」。带 † 者需先迁移再删（见对应 Phase）。

**仓库根（工作树 + 历史）**
- `student_model_lora/`（298 MB，0 文件被跟踪）— 删工作树
- `maps/`、`map_visualization/`（含其 `chroma_db*/`）— 删
- `chroma_db/`、`chroma_db_area/`（根，CWD 相对生成）— 删 + filter-repo 清历史
- `UK_Rent_Agent_Technical_Report.pdf`、`UK_Rent_Agent_Technical_Report.html` — 删（入 docs 或删）
- `quick_test_results.csv`、`diagnose_geocoding.py` — 删
- `tests/`（根，陈旧快照：`*.py` 副本、`core/`、`rag/`、`tool_system/`、`.env`、`apartment-finder-ui.html`、`requirements.txt`、`fake_property_listings.csv`）— 删（Phase 1 新测试另起）
- `scrapped_data_demo/`（顶层全部）†（先迁 `scrapper/rightmove_scraper.py`、`scrape_zoopla_listings.py`）
- `fine_tuning/`（`dataset_raw.json`、`train.jsonl`、`test.jsonl`、`student_model_lora/` 权重、评测报告）— 移 `legacy/` 或删 + filter-repo 清历史

**git 索引/历史**
- 51 个 `*.pyc`（各 `__pycache__/`）— `git rm --cached`
- 历史 `.env` blob：`fine_tuning/.env`、`scrapped_data_demo/.env`、`tests/.env`、`local_data_demo/.env` — filter-repo `--invert-paths`
- 36 个 `*chroma*.sqlite3` + `data_level0.bin` 等 chroma 二进制 — filter-repo
- `fine_tuning/student_model_lora/*`（tokenizer.json 10.9MB 等）— filter-repo

**`local_data_demo/` 内（源码级）**
- `rag/conversation_memory.py`（写死）
- `rag/area_knowledge.py`（1 行 Camden，输出丢弃）
- `data/fake_property_listings1.csv`（孤儿）
- `core/tool_system.py`：`class FunctionCalling`(:403-555)、`class SmartFunctionCalling`(:598-789)、`Tool.to_llm_format`(:174-213)、`ToolRegistry.list_tools_for_llm`(:283-333)
- `core/llm_interface.py`：`generate_recommendations`(:944-1280)、`refine_criteria_with_answer`(:592-739)、`_get_property_url`(:740)、`_normalize_price_format`(:747)、`_validate_and_fix_price_in_explanations`(:781)、`create_fallback_recommendations`(:1281-1434)
- `interactive_main.py`†、`finetuned_parser.py`（LoRA，`USE_FINETUNED_MODEL=False`）— 移 `legacy/`
- `core/data_loader.py::get_live_properties`(:90) — 随 interactive_main
- `local_data_demo/chroma_db/`、`chroma_db_area/`（陈旧运行时目录）
- `core/tools/script.md`、`core/tools/script.pdf`（无关文档产物，git status 未跟踪）
- `test_prompt.txt`（未跟踪临时文件）

---

## §6 附录 B：魔法常数迁移表

目标：全部集中到 `src/uk_rent_agent/domain/constants.py`，各处改为 `from uk_rent_agent.domain import constants as C`。

| 现值 | 语义 | 现出现点（file:line） | 目标常数名 |
|------|------|----------------------|------------|
| `999` | 无通勤限制哨兵 | `search_properties.py:27`(NO_COMMUTE_LIMIT)、`:83`、`:93`、`:705` | `C.NO_COMMUTE_LIMIT = 999` |
| `999` | 缺失通勤/距离默认 | `rag_coordinator.py:97`、`llm_interface.py:1303`、`maps_service.py:642` | `C.MISSING_MINUTES_SENTINEL`（语义拆分，勿与上者混用） |
| `999999` | 缺失预算/距离默认 | `rag_coordinator.py:67`、`maps_service.py:569/643` | `C.UNBOUNDED_SENTINEL` |
| `9999` | 缺失价格默认 | `llm_interface.py:1304` | `C.MISSING_PRICE_SENTINEL` |
| `999` | 提示词里「set to 999」 | `llm_interface.py:503/532` | 引用 `C.NO_COMMUTE_LIMIT` 拼字符串 |
| `999` | CLI 遗留 | `interactive_main.py:67/95/104` | 随 interactive_main 入 legacy |
| `1.15` | 预算软超阈值（+15%） | `search_properties.py:191`(+注释:164)、`rag_coordinator.py:89`(+:91 debug) | `C.BUDGET_SOFT_MULTIPLIER = 1.15` |
| `4.33` | 周→月（平均每月周数） | `search_properties.py:469`(+注释:468) | `C.WEEKS_PER_MONTH = 4.33` |
| `1.5` | 相似搜索通勤放宽系数 | `search_properties.py:638` | `C.SIMILAR_COMMUTE_SLACK = 1.5` |
| `1.05` | 建议预算余量（+5%） | `search_properties.py:654` | `C.SUGGESTED_BUDGET_MARGIN = 1.05` |
| `1.3` | 建议加预算（+30%） | `search_properties.py:706` | `C.BUDGET_BUMP_SUGGESTION = 1.3` |
| `0.995` | 记忆 recency 每小时衰减 | `agent_memory.py:40`(RECENCY_DECAY) | 保留于 `rag`（记忆域常数） |
| `30` | 反思触发累计重要度阈值 | `agent_memory.py:42`(REFLECT_IMPORTANCE_THRESHOLD) | 建议**提高**（见 §设计决策）：`rag` 常数 |
| `25` | 检索候选数 | `agent_memory.py:41`(RETRIEVE_CANDIDATES) | `rag` 常数 |

> **设计决策（反思阈值）**：当前 `REFLECT_IMPORTANCE_THRESHOLD=30`，默认重要度 semantic=7/reflection=8/episodic=5（`agent_memory.py:49`），导致 reflection（每条累加 8）比 episodic（5）更快触发、且反思自身又计入累计形成复利，容易反思过频。建议把阈值提高（如 60~90）或**反思不计入累计**（`maybe_reflect` 里 add reflection 时不 `_accum += importance`）。此为行为变更，放 Phase 4 并加测试。

---

## §7 附录 C：审计摘要校正清单（代码核对结果）

| # | 审计原述 | 核对结论 | 证据 |
|---|----------|----------|------|
| 1 | web_search bug 在 `core/web_search.py:107-115` | **文件错误**。已注册工具是 `core/tools/web_search.py`（`web_search_func` 返回 `ToolResult` 于 `:93/:107/:121`）。`core/web_search.py` 只提供 `get_search_snippets`（返回 str）。行号 107 巧合一致。 | `tools/__init__.py:9`；`core/tools/web_search.py:23/93/107/121` |
| 2 | git HEAD clean（无秘钥） | **部分不符**。HEAD 仍跟踪 `.env`（`fine_tuning/.env`、`scrapped_data_demo/.env`、`tests/.env`），只是疑似占位值；真实 key 在历史 blob `c6b3b011`(fine_tuning/.env)、`62b38ae6`(local_data_demo/.env)。 | `git ls-files`；`git cat-file -p 62b38ae6` |
| 3 | chroma sqlite 提交约 12 次 | **低估**。历史含 36 个 `*chroma*.sqlite3` blob。 | `git rev-list --all --objects \| grep 'chroma.*sqlite3' \| wc -l` = 36 |
| 4 | core↔rag 循环依赖仅靠 lazy import 存活 | **部分不符**。rag→core 腿是**顶层 eager**（`agent_memory.py:37 from core.llm_interface import call_ollama`）；仅 core→rag 两腿 lazy。`import rag.agent_memory` 会立即拉起 core.llm_interface。 | `agent_memory.py:37`；`memory_tools.py:11`；`search_properties.py:130` |
| 5 | MCP「串行化所有工具调用」 | **部分不符**。单后台事件循环+线程，但**无锁**，是「单循环并发协程」，非严格串行。 | `mcp_client.py:67-69/78/87/144`（无 Lock） |
| 6 | `4.33` week→month「×2」 | 实为**同一文件 2 行**（`search_properties.py:468` 注释 + `:469` 代码），非 2 个文件。 | `search_properties.py:468-469` |
| 7 | tool_system.py「~55% 死」（含 to_llm_format） | 更精确：`FunctionCalling`(:403-555)/`SmartFunctionCalling`(:598-789) 完全死；`to_llm_format`(:174-213) 仅经 `list_tools_for_llm` 被死掉的 `FunctionCalling` 调，属**传递性死**。 | `tool_system.py:174/283/475` |
| 8 | search_properties「5 段近乎重复澄清块」 | 实为 `impl` 内 **4 段** `need_clarification`（`:398/:414/:430/:447`）；`no_results`/`no_exact_match_but_similar` 形状不同不计。 | `search_properties.py:398/414/430/447` |
| 9 | `get_property_details` 读爬取缓存 | **确认**。`_active_data_path`(:24-31) 调 `provider.get_active_property_csv`(provider.py:30)。同时它 `import ToolResult`(:17) 但**返回 dict**，不触发双重包装。 | `get_property_details.py:17/24-31/181` |
| 10 | `app.py:731 debug=True` | **确认**（尾行）。`wc -l`=730，Read 显示内容至 731 行，属尾行有/无换行的计数差，非实质出入。 | `app.py:729-731` |
| — | 其余审计条目（split-brain 加载入口、5 个不可达工具、`_session_data` 全局、memory schema 缺 user_id、AgentState 无 user_id、4 个路由函数行号、3 配置岛、cache 无界、close 未注册、debug/CORS/secret、`load_mock` 在 :133/:489、`RICH_COLUMNS` 在 scraping/config.py:40 等） | **全部逐一核对为真**，行号与审计一致。 | 见 §2 各处 |

---

## §8 附录 D：`requirements.txt` 目标内容

> 目标：全部固定版本（`pip freeze` 出精确值回填），补 `geopy`，移除未用项，torch 栈移入可选 extra。以下为**去向标注**，版本号在实施时用 `pip freeze` 锁定。

**核心运行时（保留，需固定版本）**
```
numpy==<freeze>
pandas==<freeze>
chromadb==<freeze>
sentence-transformers==<freeze>
faiss-cpu==<freeze>
beautifulsoup4==<freeze>
requests==<freeze>
python-dotenv==<freeze>
flask==<freeze>
flask-cors==<freeze>
waitress==<freeze>          # 新增：生产 WSGI（Windows）
geopy==<freeze>            # 新增：search_nearby_pois / prefetch_osm_data 已用但缺声明
folium==<freeze>
ddgs==<freeze>
langgraph==<freeze>
langchain==<freeze>
langchain-core==<freeze>
langchain-ollama==<freeze>
langchain-openai==<freeze>
openai==<freeze>
mcp==<freeze>
```

**移除（声明但未使用）**
```
fastapi          # 无 import
uvicorn          # 无 import
googlemaps       # 无 import（用的是 TfL / OSM / OpenRouteService）
scikit-learn     # 无 import
langchain-community  # 无 import
```

**可选 extra（LoRA，仅 USE_FINETUNED_MODEL=True 时需要）→ 放 pyproject `[project.optional-dependencies].finetune`**
```
torch
torchvision
torchaudio
transformers
peft
accelerate
```

---

## §9 附录 E：`pyproject.toml` 骨架

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "uk-rent-agent"
version = "0.1.0"
description = "UK student rental recommendation agent"
requires-python = ">=3.10,<3.11"
dependencies = [
  "numpy", "pandas", "chromadb", "sentence-transformers", "faiss-cpu",
  "beautifulsoup4", "requests", "python-dotenv",
  "flask", "flask-cors", "waitress", "geopy", "folium", "ddgs",
  "langgraph", "langchain", "langchain-core", "langchain-ollama",
  "langchain-openai", "openai", "mcp",
]  # 实施时逐个钉版本

[project.optional-dependencies]
finetune = ["torch", "torchvision", "torchaudio", "transformers", "peft", "accelerate"]
dev = ["pytest", "pytest-asyncio", "import-linter"]

[project.scripts]
uk-rent-web = "uk_rent_agent.web.__main__:main"
uk-rent-mcp = "uk_rent_agent.mcp.server:main"
uk-rent-build-data = "uk_rent_agent.scripts.build_scraped_dataset:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]              # 迁移期若仍在 local_data_demo，改成 ["local_data_demo"]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.importlinter]
root_package = "uk_rent_agent"

[[tool.importlinter.contracts]]
name = "Layered architecture (web>agent>tools>{data,rag}>domain>config)"
type = "layers"
layers = [
  "uk_rent_agent.web",
  "uk_rent_agent.agent",
  "uk_rent_agent.tools",
  "uk_rent_agent.rag | uk_rent_agent.data",
  "uk_rent_agent.domain",
  "uk_rent_agent.config",
]
```

---

## §10 附录 F：特征化测试用例清单

> 命名约定 `test_<模块>_<行为>`。Phase 1 断言**现状**（含 bug）；标 [FLIP@P4] 者在 Phase 4 修复后翻转为期望。所有网络/LLM 调用用 `monkeypatch` 打桩。

### `tests/test_tool_contract.py`（工具契约）
- `test_registry_has_all_eleven_tools` — `create_tool_registry().list_tool_names()` 恰含 11 个：search_properties, calculate_commute, calculate_commute_cost, check_safety, get_weather, web_search, search_nearby_pois, get_property_details, check_transport_cost, recall_memory, remember。
- `test_execute_returns_toolresult` — 每个工具 `await tool.execute(**minimal)` 返回 `ToolResult`，含 `success/data/error/tool_name/execution_time_ms` 字段。
- `test_search_properties_returns_dict_data` — `data` 为 dict，含 `status`/`recommendations` 或 `question`。
- `test_check_safety_data_has_safety_score` — `data` 含 `safety_score`（int）。
- `test_calculate_commute_cost_data_shape` — `data` 含 `success`、（成功时）`commute`+`transport_cost`。
- `test_get_weather_data_has_success` — `data` 含 `success`。
- `test_web_search_double_wrap_current` [FLIP@P4] — 现状：`result.data` 是被包一层的对象（记录双重包装）；P4 后：`result.data` 是含 `results` 的 dict，且 `result.success` 反映真实成败。
- `test_search_nearby_pois_double_wrap_current` [FLIP@P4] — 现状：`result.data` 非 dict；P4 后：`result.data` 是含 `pois`/`address` 的 dict。
- `test_memory_tools_schema_omits_user_id_current` [FLIP@P4] — 现状：`recall_memory_tool.parameters['properties']` 无 `user_id`；P4 后：含 `user_id`/`session_id`。

### `tests/test_router.py`（路由决策表）
- `test_greeting_routes_direct_answer` — `"hello"` → `direct_answer`。
- `test_recall_keyword_routes_direct_answer` — `"do you remember what I'm looking for"` → `direct_answer`。
- `test_property_context_routes_reasoning` — `extracted_context={'property_address': '...'}` + 非 POI 问句 → `reasoning_property`。
- `test_property_context_poi_overrides_reasoning` — 同上但问句含「nearby supermarket」→ 不走 reasoning，进入投票。
- `test_find_me_routes_search_properties` — `"find me a flat near UCL under 1800"` → `search_properties`。
- `test_safety_without_address_routes_clarification` — 安全问句但无可解析地址 → `clarification`。
- `test_commute_cost_missing_endpoint_clarifies` — 缺 from/to → `clarification`。
- `test_calculate_commute_reachable` [P4] — 纯通勤时间查询可路由到 `calculate_commute`（当前不可达，P4 新增）。
- `test_check_transport_cost_reachable` [P4]、`test_get_property_details_reachable` [P4]、`test_recall_remember_reachable` [P4] — 原 5 个不可达工具在 P4 后可路由。

### `tests/test_api_alex.py`（`/api/alex` 金标准）
- `test_alex_search_response_type` — 触发搜索 → `response_type == "search"` 且 `recommendations` 非空。
- `test_alex_clarification_response_type` — 缺参 → `response_type == "clarification"`，`agent_state == "waiting_for_input"`。
- `test_alex_chat_response_type` — 普通问答 → `response_type == "chat"`。
- `test_alex_requires_message` — 空 body → 400。
- `test_alex_user_isolation` [P4/P5] — 两个 `user_id` 的历史互不串。

### `tests/test_search_pipeline.py`（搜索管线，fixture CSV）
- `test_search_found_shape` — 给全 `location/max_budget/max_commute_time` → `status=='found'`，每条 rec 含 `rank/address/price/travel_time/explanation`。
- `test_summary_never_shows_999` — `summary` 不含 `999`（`_found_summary`/`_commute_phrase` 正确抑制哨兵）。
- `test_week_to_month_conversion` — `budget_period='week'`、`max_budget=300` → 内部按 `300*4.33` 过滤。
- `test_soft_violation_within_15pct` — 价格在 `budget*1.15` 内进 soft，超出被排除。
- `test_missing_params_returns_clarification` — 缺 location/budget/commute → `status=='need_clarification'`，`extracted_so_far` 保留已知字段。

### `tests/test_repository.py`（Phase 3 新增）
- `test_load_normalizes_to_rich_columns` — `load().properties[0].keys()` ⊇ `RICH_COLUMNS`，无遗留双大小写键。
- `test_source_label` — auto 模式无缓存时 `source=='fake'`；有新鲜缓存时 `source=='scraped'`。
- `test_active_csv_matches_search_source` — `active_csv_path()` 与 search 消费同一文件。
- `test_single_faiss_build` — 进程内 `build_index` 只被调用一次（spy）。

### `tests/test_cache.py`（Phase 3）
- `test_cache_key_matches_legacy_md5` — `make_key` 输出 == 旧 `create_cache_key`（同参数）。
- `test_cache_persists_across_instances` — set 后新建实例仍能 get（磁盘持久化）。

### `tests/test_memory_isolation.py`（Phase 4）
- `test_two_users_do_not_share_bucket` — `user_a` remember 的事实，`user_b` recall 不到。

### `tests/test_session_isolation.py`（Phase 5）
- `test_favorites_per_user` — `user_a` 收藏对 `user_b` 不可见。
- `test_lru_eviction` — 超 `max_users` 淘汰最旧。
- `test_ttl_expiry` — 超 TTL 的会话被清。

---

*（手册结束。执行顺序：Phase 0 →（可并行 Phase 1）→ 2 → 3 → 4 → 5；每阶段以「特征化测试全绿」为放行门槛。）*
