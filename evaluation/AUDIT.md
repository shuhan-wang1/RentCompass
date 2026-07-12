# RentCompass — Phase 1 Repository Audit (Evaluation Framework)

Read-only reconnaissance. No code modified. Secrets are referenced by location only,
never by value. Date: 2026-07-11.

## 0. Orientation / where the code actually lives

There are **two source trees** and they are both live:

- `app/` — the domain code: LangGraph agent graph, tools, RAG/memory, scraping,
  external-data services, and the Flask route module (`app/app.py`).
- `src/uk_rent_agent/` — the installable package (`pip install -e .`, `pyproject.toml`):
  the web/ASGI layer, LLM router, critic, guardrails, state/contracts, persistence,
  observability, and an `evals/` module.

**Active entry point** (`pyproject.toml:28`, `Dockerfile:53`, `docker-compose.yml:76`):
`uk_rent_agent.web.asgi:create_asgi_app` (uvicorn, port 5001). That factory calls
`uk_rent_agent.web.app.create_app` (`src/uk_rent_agent/web/app.py:13`), which dynamically
loads `app/app.py` as the "legacy" module and returns its Flask `app`. So **`app/app.py`
is the real request handler**, and `src/uk_rent_agent` supplies the router/critic/state it
imports. Both trees are added to `sys.path` (`pyproject.toml:36`, `web/app.py:23-25`).

Note the split-brain: `app/app.py` (1452 lines) is the running app; `src/uk_rent_agent/web/app.py`
is a thin loader. When instrumenting, target `app/app.py` + `app/core/langgraph_agent.py`.

---

## 1. Agent graph — entry point and main nodes

Graph is built in **`build_agent_graph`** at `app/core/langgraph_agent.py:2102` (a
`StateGraph(AgentState)`), compiled at `:2144`. It is compiled lazily on first request in
`app/app.py:762-768` and invoked with `agent_graph.ainvoke(...)` at `app/app.py:907`.
State schema `AgentState` / `create_initial_state` come from
`src/uk_rent_agent/agent/state.py` (imported at `langgraph_agent.py:34`).

Nodes registered (`langgraph_agent.py:2118-2126`):

| Node | Factory / function | Location |
|---|---|---|
| `extract_preferences` | `_make_extract_preferences_node` → `extract_preferences_node` | `langgraph_agent.py:606` |
| `decide_tool` (supervisor/router) | `_make_decide_tool_node` → `decide_tool_node` (logic in `_compute_decision`) | `:788` / `_compute_decision :796` / `decide_tool_node :912` |
| `execute_tool` | `_make_execute_tool_node` → `execute_tool_node` | `:1572` / `:1580` |
| `dispatch_searches` | `_make_dispatch_searches_node` | `:2024` |
| `search_worker` (parallel retrieval worker) | `_make_search_worker_node` | `:2044` |
| `gather_searches` (reduce) | `_make_gather_searches_node` | `:2069` |
| `generate_response` (synthesis) | `_make_generate_response_node` | `:1759` |
| `critic` (grounding critic) | `_make_critic_node` | `:1814` |
| `format_output` | `_make_format_output_node` | `:1870` |

Edges (`:2129-2137`): `START → extract_preferences → decide_tool`. `decide_tool` and
`execute_tool` route dynamically via `Command(goto=...)` (no static edges). Map-reduce:
`dispatch_searches -(fan_out_searches Send)-> search_worker×N → gather_searches →
generate_response`. Then `generate_response → critic → format_output → END`.

Routing helpers: `decide_tool_node` chooses `generate_response` / `format_output` /
`dispatch_searches` / `execute_tool` (`:922-939`); `_route_after_execution` (`:1997`)
decides `format_output` vs `generate_response` after a tool runs; `fan_out_searches`
(`:2033`) emits the `Send` fan-out.

---

## 2. Supervisor, retrieval workers, synthesis, critic — implementations

- **Supervisor / intent routing**: `_compute_decision` (`langgraph_agent.py:796`). It is a
  layered router: deterministic guards run FIRST (fair-housing screen `:808`, memory-recall
  `:818`, property context `:825`, follow-up interception `:841-857`, greetings `:860`,
  no-commute `:877`, live-transport `:888`, soft-criteria gate `:899`), then an LLM
  classifier `_majority_vote` (`:1011`) as the fallback. The intent catalog + prompt are
  static (`_INTENT_CATALOG :464`, `INTENT_CLASSIFICATION_PROMPT :516`). Despite the name
  "majority vote", it is a **single** structured LLM call (`:1034`).
- **Retrieval workers**:
  - *Property retrieval*: the `search_properties` branch inside `execute_tool_node`
    (`:1612-1680`) → tool `app/core/tools/search_properties.py` (1745 lines) →
    `core.scraping.on_demand.get_listings` (`on_demand.py:753`).
  - *Web/market retrieval (parallel)*: `dispatch_searches`/`search_worker`/`gather_searches`
    map-reduce (`:2024-2095`); each worker runs `web_search` (`app/core/tools/web_search.py`).
  - *Location tools* (`check_safety`, `search_nearby_pois`, `calculate_commute*`,
    `get_transport_info`) run through the generic branch of `execute_tool_node` (`:1682-1706`).
- **Synthesis**: `generate_response_node` (`:1762`) using `_build_generation_prompt`
  (`:1724`, prompts `SYNTHESIS_PROMPT :550` / `REASONING_PROPERTY_PROMPT :532`, guarded by
  `SECURITY_DIRECTIVE :594`). LLM from `get_react_llm()`.
- **Grounding critic**: node `critic_node` (`:1824`) delegates to
  **`enforce_grounding`** at `src/uk_rent_agent/agent/critic.py:302`
  (rubric `evaluate_grounding :208`). Deterministic price-grounding check; on failure it does
  **one** regeneration pass (re-invokes `get_react_llm`, `langgraph_agent.py:1834-1841`), never
  a hard replacement; appends a caveat if still ungrounded. Verdict is logged via `_on_verdict`
  (`:1843`).

---

## 3. Tools — count, list, and where registered

**12 tools**, registered in `create_tool_registry` at `app/core/tool_system.py:650`
(register calls `:672-683`). The same registry is the single source of truth for both the
in-process path and the MCP server (`app/mcp_server.py:46`).

| # | Tool name | Definition file | Registered |
|---|---|---|---|
| 1 | `search_properties` | `app/core/tools/search_properties.py` | tool_system.py:672 |
| 2 | `calculate_commute` | `app/core/tools/calculate_commute.py` | :673 |
| 3 | `calculate_commute_cost` | `app/core/tools/calculate_commute_cost.py` | :674 |
| 4 | `check_safety` | `app/core/tools/check_safety.py` | :675 |
| 5 | `get_weather` | `app/core/tools/get_weather.py` | :676 |
| 6 | `web_search` | `app/core/tools/web_search.py` | :677 |
| 7 | `search_nearby_pois` | `app/core/tools/search_nearby_pois.py` | :678 |
| 8 | `get_property_details` | `app/core/tools/get_property_details.py` | :679 |
| 9 | `check_transport_cost` | `app/core/tools/check_transport_cost.py` | :680 |
| 10 | `get_transport_info` | `app/core/tools/get_transport_info.py` | :681 |
| 11 | `recall_memory` | `app/core/tools/memory_tools.py:43` | :682 |
| 12 | `remember` | `app/core/tools/memory_tools.py:65` | :683 |

**Two tool-execution layers:**
- In-process: `ToolRegistry.execute_tool` (`tool_system.py:401`) → `Tool.execute` (`:143`).
- MCP: `MCPToolClient` (`app/core/mcp_client.py:30`) duck-types `execute_tool`, calling
  `app/mcp_server.py` over stdio. Enabled by env `USE_MCP_TOOLS` (default off in
  `app/app.py:364`; the web `create_app` sets it from `Config.use_mcp_tools`,
  `web/app.py:18`). On any MCP failure/timeout it **falls back to the in-process registry**
  (`mcp_client.py:191`). So evaluation can force in-process by `USE_MCP_TOOLS=0`.

Pseudo-routes that are NOT registry tools but appear in routing: `market_info`,
`direct_answer`, `multi_search`, `reasoning_property`, `clarification` (handled inside the
graph, `_INTENT_CATALOG` / `_build_tool_params :1455`).

---

## 4. Read-only vs. side-effecting tools

Side-effect intent is declared on each `Tool` via `side_effect` (default `"none"`,
`tool_system.py:95,121`). Grep shows **only `remember` sets `side_effect="write"`**
(`memory_tools.py:84`, `retry_safe=False`, `cacheable=False`). Write tools are gated by
`tool_allowed` (`src/uk_rent_agent/agent/guardrails.py:42`, enforced at
`execute_tool_node :1686`) and require an idempotency key (`tool_system.py:161-174`,
idempotency store under `.runtime/idempotency.sqlite3`).

| Tool | Declared side_effect | Real-world effects |
|---|---|---|
| `remember` | **write** | **Writes to ChromaDB** (`chroma_db_agent_memory`) via `AgentMemory.add`; may make an LLM importance-rating call. |
| `recall_memory` | none | Read of ChromaDB (vector query). Read-only, but does `_touch` metadata update (`agent_memory.py:353`). |
| `search_properties` | none | **Live scrape of OnTheMarket** + Nominatim geocode + an LLM place-classification call, backed by a 12h SQLite cache (`on_demand.py`). Writes to `listing_cache.sqlite3`. |
| `web_search` | none | **External HTTP** to SearXNG (`web_search.py:341`, `SEARXNG_URL`). |
| `check_safety` | none | **External HTTP** to `data.police.uk` (`maps_service.py:388`) + geocode; caches. |
| `search_nearby_pois` | none | **External HTTP** to Overpass (`maps_service.py:31-35` / `amenity_map_generator.py:185`) + geocode; caches. |
| `calculate_commute` / `calculate_commute_cost` | none | Geocode (Nominatim) + TfL journey (`maps_service.py:213/237`); static fare tables; caches. |
| `get_transport_info` | none | **Live TfL API** (`get_transport_info.py:40` `https://api.tfl.gov.uk`); caches. |
| `get_weather` | none | **External HTTP** to open-meteo (`get_weather.py:34,43`). |
| `check_transport_cost` | none | Static zone/fare table; no network. |
| `get_property_details` | none | Reads local property DB/CSV. |

Additional non-tool side effects per turn (in `app/app.py`, outside the tool layer):
long-term memory **read** (`app.py:880`), memory **write** in a background thread
(`app.py:938` → `remember_turn_async` → LLM extract/consolidate/reflect), and the LangGraph
**SQLite checkpointer write** (`app.py:766`, `persistence.py:24`).

Bottom line: every tool except `remember` is nominally read-only, but 7 of 12 perform **live
external network I/O** (scrape/HTTP) that mutates local caches. Only `remember` mutates
durable user state.

---

## 5. Model-routing logic and tiers

Two indirections:

- **Per-task factories** in `app/core/llm_config.py`: `get_react_llm` (`:54`, response
  generation + critic regen), `get_classification_llm` (`:62`, intent routing),
  `get_planning_llm` (`:70`, web-search planning). Each branches on `LLM_PROVIDER`
  (`:19`, default `deepseek`; alt `ollama`).
- **DeepSeek route table** `ModelRouter` in `src/uk_rent_agent/llm/router.py:15`
  (`route :23`, `create :39`):

| Purpose | Model | Temp | max_tokens | Used by |
|---|---|---|---|---|
| `intent` / `classification` | `deepseek-chat` (cheap) | 0.0 | 256 | `get_classification_llm` → `decide_tool` |
| `planner` / `critic` | `deepseek-chat`, or `deepseek-reasoner` if `complex_task` | 0.0 | 2000 | `get_planning_llm` → market/web planning |
| `responder` / `synthesis` | **`deepseek-reasoner` (strong/expensive)** by default; `deepseek-chat` if `low_latency` | 0.1 | 4000 | `get_react_llm` → `generate_response` + critic regen |
| `memory` / `judge` | `deepseek-chat` | 0.0 | 1500 | (route exists; not wired to memory path — see below) |
| `pro` | `deepseek-v4-pro` | 0.0 | 8000 | unused |

Model IDs come from env: `DEEPSEEK_MODEL`/`DEEPSEEK_CHAT_MODEL`, `DEEPSEEK_REASONER_MODEL`,
`DEEPSEEK_PRO_MODEL` (`router.py:19-21`).

**Important**: the memory subsystem and the on-demand place classifier do NOT use
`ModelRouter`. They call `app/core/llm_interface.py:call_ollama` (`:50`) → `_call_deepseek`
(`:28`), which uses the plain `DEEPSEEK_MODEL` (`deepseek-chat`) via the OpenAI client. So
memory extraction/rating/consolidation/reflection (`agent_memory.py:190,207,239,309`) and
`_llm_classify` (`on_demand.py:398`) all hit **deepseek-chat**, bypassing the router.

Tier summary: **cheap = deepseek-chat** (intent, planning, memory, place-classify);
**strong = deepseek-reasoner** (response synthesis + critic regeneration — the dominant cost).

---

## 6. Existing instrumentation (present/absent, with locations)

| Concern | Status | Location / notes |
|---|---|---|
| **Distributed tracing** | Absent (framework only) | No LangSmith/OTel. `LANGCHAIN_TRACING` not set. A local span helper `node_span` exists at `src/uk_rent_agent/observability.py:50` but is **never called** anywhere (grep confirms only the definition). |
| **Structured request logging** | Partial-present | `JsonFormatter` (`observability.py:31`) + `request_context` contextvars, wired at request boundary (`app.py:573,1146`), enabled via `logging_setup.configure_logging` (`web/__main__.py:2`). Emits `request_id`/`user_id`; schema also allows `node,tool,latency_ms,cache_hit,input_tokens,output_tokens` but nothing populates the latter set. |
| **Token-usage accounting** | **Absent** | No reads of `usage_metadata`/`response.usage`/`get_openai_callback` anywhere. JSON log schema has `input_tokens`/`output_tokens` fields but no emitter. |
| **Latency timing** | Partial | Per-tool wall time in `Tool.execute` (`tool_system.py:192`, stored in `ToolResult.execution_time_ms`) and aggregated in `ToolRegistry._stats` (`:417-424`). **No per-node or per-LLM-call latency** (the `node_span` that would provide it is unused). |
| **Tool success/failure logging** | Present | Per-tool counters in `ToolRegistry._stats` (`tool_system.py:355,417`, `get_stats :428`, `print_stats :432`). Also `logger.info/warning` inside `Tool.execute`. Not persisted/exported — in-memory only, reset per process. |
| **Critic-result logging** | Present | `_on_verdict` logs `critic.verdict` with grounded/issues (`langgraph_agent.py:1843-1848`); `verdict` + `critic_attempts` written to state (`:1859-1862`). |
| **Retry logging** | Present (tool-level only) | Exponential backoff + logs in `Tool.execute` (`tool_system.py:218-221`); attempts gated by `retry_safe`. No retry on LLM calls. |
| **Caching** | Present (scattered) | Generic `PersistentCache` (`app/core/cache_service.py` → `src/uk_rent_agent/data/cache.py`, `data/runtime_cache.sqlite3`); listing cache (`on_demand.py`, 12h TTL, `.runtime/listing_cache.sqlite3`); in-mem `_CLASSIFY_CACHE` (`on_demand.py:244`); scraper CSV cache (`scraping/config.py`). Used by maps/pois/transport/safety/web_search modules. **No LLM-response cache.** |
| **Benchmark harness** | Partial-present | `src/uk_rent_agent/evals/harness.py` (`run_intent_eval`, `run_retrieval_eval`), `metrics.py` (recall@k/mrr/ndcg), `ci_gate.py` (threshold gate, entry point `uk-rent-eval-gate`). Golden set in `evals/golden_set/*.jsonl` (tiny: 4 intents, 1 retrieval, 1 e2e), thresholds `evals/thresholds.json`. **No e2e/graph runner** and **no wiring** from these to the real agent — they take injected `classify`/`retrieve` callables. |
| **Unit tests** | Present | ~30 files in `tests/` (e.g. `test_intent_router.py`, `test_critic_grounding.py`, `test_soft_criteria_gate.py`, `test_agent_memory_isolation.py`) + `tests_refactor/` (`test_tool_contract.py`, `test_cache.py`, ...). Configured in `pyproject.toml:35-37`. |
| **Integration tests** | Weak/absent | `test_maps_poi_pipeline.py`, `test_on_demand_listings.py` touch pipelines, but there is no full-graph end-to-end test driving `build_agent_graph` with mocked LLMs. |

---

## 7. Best hook points for evaluation instrumentation (minimal intrusion)

1. **Single LLM wrapper** — wrap `ModelRouter.create` (`src/uk_rent_agent/llm/router.py:39`)
   to return a `ChatOpenAI` subclass / callback that records model, tokens
   (`response_metadata['token_usage']`), and latency. **Caveat**: this misses the memory +
   place-classify calls that go through `app/core/llm_interface.py:_call_deepseek` (`:28`) —
   wrap **that function too** to cover 100% of model calls. Two wrappers = full coverage.
2. **Tool decorator** — wrap `ToolRegistry.execute_tool` (`tool_system.py:401`), which already
   centralizes every in-process tool call and computes `execution_time_ms`; add
   success/latency/args-hash emission there. For the MCP path, also wrap
   `MCPToolClient.execute_tool` (`mcp_client.py:137`).
3. **Graph-node hook** — the `node_span` context manager
   (`observability.py:50`) is purpose-built and currently unused; wrap each node factory's
   returned callable in `build_agent_graph` (`langgraph_agent.py:2118-2126`) with it to get
   per-node latency/error spans without touching node bodies.
4. **Critic metrics** — extend the existing `_on_verdict` hook (`langgraph_agent.py:1843`) —
   it already receives every verdict at both `initial` and `regenerated` stages.
5. **Turn-level record** — `app/app.py:907-943` is the one place a full turn is assembled
   (state in, final_state out, memory read/write); wrap it to emit one eval row per turn
   (route taken via `final_state['tool_decision']`, `critic_attempts`, `verdict`,
   `response_type`).
6. **Deterministic offline switch** — there is no global "dry-run" flag today; the cleanest
   injection points are the two LLM factories (`llm_config.get_*` / `router.create`) and
   `llm_interface.call_ollama`, plus the network tools listed in §8.

---

## 8. Unstable / live external data sources (fixtures needed for offline determinism)

All of these are live and non-deterministic; each should be snapshotted to run offline:

| Source | Where | Key? | Cache today |
|---|---|---|---|
| **OnTheMarket scrape** (primary listing source) | `app/core/scraping/on_demand.py:753` (`get_listings`), providers in `app/core/scraping/onthemarket.py` | none | 12h SQLite (`.runtime/listing_cache.sqlite3`) |
| OpenRent / Rightmove / Zoopla scrapers | `app/core/scraping/*` + `legacy_scrapers/` | none | **DEAD** per project memory (WAF/decommissioned); OnTheMarket is the only working source |
| **TfL Unified API** (journeys, fares, line status) | `app/core/tools/get_transport_info.py:40`, `app/core/maps_service.py:206,213,231,237` | optional free `TFL_APP_KEY` | yes (cache_service) |
| **data.police.uk** (crime → safety) | `app/core/maps_service.py:388` | none | yes |
| **Overpass / OSM** (POIs) | `app/core/maps_service.py:31-35`, `app/core/amenity_map_generator.py:185`, `app/scripts/prefetch_osm_data.py:24` | none | yes; prefetch script exists |
| **Nominatim geocoding** | `app/core/maps_service.py:166`, `app/core/scraping/on_demand.py:361` | none | yes |
| **open-meteo weather** | `app/core/tools/get_weather.py:34,43` | none | not obviously cached |
| **SearXNG metasearch** (web_search) | `app/core/tools/web_search.py`, `app/core/web_search.py:341` | self-hosted (`SEARXNG_URL`) | partial |

**Yes — fixtures/snapshots are required** for deterministic offline eval. The good news: most
callers already route through `cache_service`/SQLite, so a "record-then-replay" harness can be
built by (a) pre-warming caches with a golden query set, or (b) monkeypatching the HTTP calls
(`requests.get/post`) and the scraper `get_listings` with recorded fixtures. Note the caches
are keyed by query/params, so they only help for the exact golden queries; arbitrary
adversarial queries will miss and hit the network.

---

## 9. Can a FULL offline eval run without paid/live calls?

**Not out of the box** — a normal turn makes a paid DeepSeek call and (for searches) a live
scrape. But every paid/live call has a mockable seam, so a fully offline run is achievable
with mocks/fixtures. Places that make a **paid model call**:

- Intent routing: `get_classification_llm` → `ModelRouter.create("intent")`
  (`llm_config.py:62`) — 1 call/turn (skipped when a deterministic guard short-circuits).
- Web-search planning (market_info): `get_planning_llm` (`llm_config.py:70`,
  `_plan_web_searches :1535`).
- Response synthesis: `get_react_llm` (`llm_config.py:54`) — `generate_response_node:1769`.
- Critic regeneration: `get_react_llm` again (`langgraph_agent.py:1837`), only on a failed
  grounding verdict.
- Place classification inside search: `_llm_classify` (`on_demand.py:398` → `call_ollama`).
- Background memory: `_rate_importance`/`_extract_facts`/`_consolidate`/`maybe_reflect`
  (`agent_memory.py:190,207,239,309` → `call_ollama`).

Places that make a **live network write/scrape**: `search_properties` → OnTheMarket scrape
(the only true "write" is to the local listing cache), and the durable `remember` write to
ChromaDB. There are **no third-party paid write endpoints** and **no payment/booking actions**
(explicitly disclaimed in `CAPABILITIES_NOTE :587`).

Existing mock/mitigation paths: `LLM_PROVIDER=ollama` swaps DeepSeek for a local model
(`llm_config.py`); `USE_MCP_TOOLS=0` forces in-process tools; caches + `_CLASSIFY_CACHE`
absorb repeat calls; the checkpointer/memory dirs are bind-mounted and can be pointed at temp
paths (`CONVERSATION_DB_PATH`, `SEARCH_LISTING_CACHE_PATH`, `IDEMPOTENCY_DB`). **What's
missing for a clean offline eval**: a single injectable fake-LLM and fake-network layer, and a
harness that drives the real graph (the current `evals/` harness does not).

---

## A. COST SURFACE

**Paid API providers:**

- **DeepSeek** — the only paid LLM provider actually used. Requires **`DEEPSEEK_API_KEY`**
  (`app/config.py:18`, `llm_config.py:22`, `router.py:45`). Endpoint `DEEPSEEK_BASE_URL`.
  Models: `deepseek-chat` (cheap; intent/planner/memory/place-classify) and
  `deepseek-reasoner` (expensive; synthesis + critic regen). Called from: `decide_tool`
  (router), `_plan_web_searches`, `generate_response`, `critic`, `on_demand._llm_classify`,
  and all four memory LLM steps.
- **Google Maps** — `GOOGLE_MAPS_API_KEY` present and `USE_TRAVEL_SERVICE='google'`
  (`app/config.py:12,26`), but per project memory the key is a **placeholder** and the commute
  path falls back to free OSM/TfL/OpenRouteService; treat Google Maps as configured-but-inert.
- Free/keyless or self-hosted (no billing): TfL (optional free key), data.police.uk, Overpass,
  Nominatim, open-meteo, SearXNG. `GEMINI_API_KEY`/`OPENROUTESERVICE_API_KEY` exist in config
  but are not on the hot path.

**Model calls per end-to-end turn** (traced through the graph + `app.py`):

| Turn type | Foreground model calls | Background (memory) | Total |
|---|---|---|---|
| Greeting / clarification (guard short-circuits, no generate) | 0–1 (intent may be skipped) | ~2–3 | ~2–4 |
| Direct answer / simple Q | 1 intent + 1 responder | ~2–3 | ~4–5 |
| `search_properties` | 1 intent + 1 place-classify + 1 responder (+0–1 critic regen) | ~2–3 | **~5–7** |
| `market_info` / web multi-search | 1 intent + 1 planner + 1 responder (+0–1 critic regen) | ~2–3 | ~5–7 |

Background memory per turn = `_extract_facts` + `_consolidate` (≈2 always) + `_rate_importance`
(episodic writes) + occasional `maybe_reflect` (gated by accrued importance, `agent_memory.py:294`).
The **dominant cost is the reasoner** used for synthesis (and any critic regeneration), each up
to 4000 output tokens (`router.py:33-34`).

**Required env vars/keys** (from `app/config.py`, `llm_config.py`, `router.py`,
`docker-compose.yml`): essential — `DEEPSEEK_API_KEY`, `LLM_PROVIDER`, `FLASK_SECRET_KEY`;
operational — `PROPERTY_SOURCE`, `SEARXNG_URL`, `USE_MCP_TOOLS`, `IDEMPOTENCY_DB`,
`CONVERSATION_DB_PATH`/checkpoint path, `SEARCH_LISTING_CACHE_PATH`; optional —
`TFL_APP_KEY`, `DEEPSEEK_*_MODEL` overrides, `GOOGLE_MAPS_API_KEY` (placeholder),
`OPENROUTESERVICE_API_KEY`, `GEMINI_API_KEY`.

---

## B. OFFLINE-READINESS (per planned eval phase)

| Phase | Offline with fixtures? | Must mock | Genuinely needs live/paid |
|---|---|---|---|
| **Benchmark run (intent/retrieval/e2e golden)** | Intent & retrieval: **yes** (harness already takes injected `classify`/`retrieve`, `harness.py:16,31`). E2E: **not yet** — no runner exists. | DeepSeek (both `router.create` and `llm_interface._call_deepseek`); the network tools in §8 for e2e. | Nothing, once a fake-LLM + fixtures are supplied. Golden set must be expanded (currently 6 rows total). |
| **Model-routing A/B (cheap vs strong)** | **Yes**, structurally. Swap models via `DEEPSEEK_*_MODEL` env or by patching `ModelRouter.route` (`router.py:23`). | The scoring LLM/judge if used; network tools. | To measure *real* quality/latency deltas you need actual DeepSeek calls (chat vs reasoner) — that is the paid part of the experiment. |
| **Parallel-retrieval A/B (fan-out vs sequential)** | **Yes** for the graph mechanics (`dispatch_searches`/`search_worker`/`gather_searches`, `:2024-2095`). | `web_search`/SearXNG + scrape via recorded fixtures so latency/throughput is deterministic. | Only if you want real network latency numbers. |
| **Fault injection** | **Yes** — inject at the mockable seams: raise in `ToolRegistry.execute_tool` / `MCPToolClient.execute_tool`, force MCP-timeout fallback (`mcp_client.py:146`), make `get_react_llm` raise (tests `generate_response` except path `:1772` and critic `except :333`), feed poisoned/tainted listing text (guardrails `sanitize_untrusted`). | LLM + tools (so failures are synthetic, not billed). | None. |
| **Memory tests** | **Yes** — ChromaDB is local/on-disk; point `chroma_db_agent_memory` at a temp dir. Isolation logic (`_valid_user_id`, per-user `where` filter) is deterministic and already unit-tested (`test_agent_memory_isolation.py`). | The memory LLM steps (`agent_memory.py` → `call_ollama`) to make extract/consolidate/reflect deterministic. | Nothing — no external service; only the internal DeepSeek calls, which should be mocked. |

**Net**: with two LLM shims (`ModelRouter.create` + `llm_interface.call_ollama`) and a
record/replay layer over the §8 network calls (plus temp dirs for chroma/sqlite/idempotency),
**every planned phase can run fully offline and unbilled**, except where the *purpose* of the
phase is to measure real model quality or real network latency. The main gaps to close before
that is possible: (1) no e2e/graph eval runner wired to `build_agent_graph`; (2) no global
mock/dry-run switch; (3) no token accounting; (4) the golden set is minimal.

---

### Items I could not fully verify / open questions
- Whether `get_weather` (open-meteo) is cached — no cache call was visible in the grep; treat
  as uncached/live.
- Exact background-memory call count varies (importance rating only on episodic writes;
  reflection is threshold-gated) — the "~2–3" estimates are traced but not measured.
- Google Maps: config says `USE_TRAVEL_SERVICE='google'` yet the code paths I inspected in
  `maps_service.py` use free OSM/TfL. I did not exhaustively read all commute branches to
  confirm the Google path is never taken; project memory states the key is a placeholder.
