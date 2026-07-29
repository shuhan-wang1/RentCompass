# RentCompass — UK Student Rental Agent

A conversational agent that helps students find UK rentals: it searches live
listings, plans commutes against the real TfL network, checks area crime data,
finds nearby amenities, compares neighbourhoods, and remembers what a user told
it across conversations.

The production conversational path is a **native function-calling agent loop**
built on LangGraph: one bound-tools model call per super-step, a batched tool
executor, and a bounded per-turn time budget with deterministic degradation. It
runs behind a **two-pool canary** with per-conversation sticky assignment,
per-turn telemetry, and a deploy gate that refuses to ship anything but a
pinned commit.

The codebase is two cooperating trees. `src/uk_rent_agent/` is the installable
package owning the ASGI entry point and shared infrastructure (state, contracts,
persistence, guardrails, critic, LLM routing, evaluation gate). `app/` is the
domain application (Flask routes, the agent graphs, tools, scraping, RAG) that
the web layer loads at runtime.

---

## Contents

- [1. What it does](#1-what-it-does)
- [2. Live service — and how to tell what is running](#2-live-service--and-how-to-tell-what-is-running)
- [3. Runtime architecture](#3-runtime-architecture)
- [4. The agent loop](#4-the-agent-loop)
- [5. Tools](#5-tools)
- [6. Safety, grounding and guardrails](#6-safety-grounding-and-guardrails)
- [7. Context engineering](#7-context-engineering)
- [8. Search and data pipeline](#8-search-and-data-pipeline)
- [9. Conversations, branching and memory](#9-conversations-branching-and-memory)
- [10. HTTP API](#10-http-api)
- [11. Running locally](#11-running-locally)
- [12. Configuration](#12-configuration)
- [13. Production: the two-pool canary](#13-production-the-two-pool-canary)
- [14. Deploying](#14-deploying)
- [15. Evaluation](#15-evaluation)
- [16. Testing and CI](#16-testing-and-ci)
- [17. Repository map](#17-repository-map)
- [18. Documentation index](#18-documentation-index)
- [Appendix A — the legacy architecture](#appendix-a--the-legacy-architecture)

---

## 1. What it does

- **Live property search.** Scrapes OnTheMarket on demand, normalises listings
  into a rich schema, and caches them in SQLite with a TTL. Criteria accumulate
  across turns: area, budget (weekly or monthly), bedrooms, room type, commute
  destination, travel-time cap, amenities, and soft preferences.
- **Commute planning.** Real TfL Journey Planner itineraries, with journey time
  *and* monthly fare; a straight-line fallback is labelled as an estimate rather
  than passed off as a routed journey.
- **Area intelligence.** Crime/safety scores from data.police.uk, nearby POIs
  from OpenStreetMap Overpass, and an explainable value-for-money ranking of
  candidate areas computed from cached listing prices — never invented.
- **Neighbourhood recommendation.** Given a university or workplace, proposes
  areas to live in: web search → LLM extraction → per-candidate validation
  against real geocoding/commute data → cache. Anything that fails validation is
  dropped before the user sees it.
- **Two search surfaces.** A conversational one (`/api/alex`) and a
  deterministic structured form (`/api/search_direct`) that bypasses the model
  entirely and writes into the same conversation state.
- **In-place refinement.** "Drop anything over £2000 and sort by distance to the
  tube" filters and re-sorts the listings already on screen, deterministically —
  no second scrape, and the prose can never describe a different set than the
  results panel shows.
- **Conversation branching.** Fork a conversation from any completed turn, edit
  an earlier user message to create an alternative branch, and compare two
  branches side by side in a split view.
- **Long-term memory.** Per-user durable facts in ChromaDB, exposed as
  `recall_memory` / `remember` tools, erasable through `/api/forget_me`.
- **Bilingual.** Replies follow an explicit language policy (zh/en) driven by the
  message content and the UI language, not by model whim.

---

## 2. Live service — and how to tell what is running

The public deployment is **`https://rentcompass.co.uk:8443`**, served by host
nginx in front of one of two application pools. TLS is on `8443` rather than
`443` because another service on the box holds `443`; port `80` answers ACME
challenges and 301-redirects everything else to `:8443`.

**Never trust a document (including this one) for what is deployed — ask the
service.** Every response, including `/health`, carries its own provenance:

```bash
curl -sk -D- -o /dev/null https://rentcompass.co.uk:8443/health | grep -i x-agent-
# x-agent-arch:    fc_loop          <- which architecture answered
# x-agent-version: <40-char sha>    <- the exact commit the image was built from
```

The same headers are available per pool on the box (`:5001` legacy, `:5002` fc),
and `bash deploy/update.sh --status` prints the pin, both pools, and which one
the public upstream currently points at. The deployed commit is whatever
`/etc/rentcompass/deploy.env` pins — see [§14](#14-deploying).

At the time of writing the public edge serves the **fc_loop** pool; the legacy
pool stays warm as the rollback target ([Appendix A](#appendix-a--the-legacy-architecture)).

---

## 3. Runtime architecture

```text
                    Browser (app/unified-ui.html)
                              │  https
                              ▼
                   nginx (host, TLS, one upstream)
                              │
         ┌────────────────────┴────────────────────┐
         ▼                                         ▼
  127.0.0.1:5001                            127.0.0.1:5002
  pool "app"  AGENT_ARCH=legacy             pool "app-fc"  AGENT_ARCH=fc_loop
  (rollback target)                         (candidate / current public pool)
         │                                         │
         └────────────────────┬────────────────────┘
                              ▼
              Starlette ASGI shell  (uk_rent_agent.web.asgi)
                 ├── GET /health          served natively
                 └── Mount("/")  ──▶ Flask app (app/app.py)
                              │
              ┌───────────────┼────────────────────────┐
              ▼               ▼                        ▼
        /api/alex       /api/search_direct     conversation / auth /
              │               │                favorites / map routes
              ▼               ▼
    agent graph        search_properties_impl()
    (fc_loop | legacy)   directly, no LLM router
              │
              ▼
      tool provider ── in-process ToolRegistry
                    └─ or MCPToolClient ─▶ app/mcp_server.py (stdio)
                              │
                              ▼
                    tools ─▶ TfL · police.uk · Overpass · Nominatim ·
                             postcodes.io · OnTheMarket · SearXNG

  Compose network:  searxng:8080 (private metasearch)  ·  valkey (its cache)
  Host state:       .runtime/  (checkpoints per arch, shared conversation DB,
                                auth store, idempotency, telemetry logs)
                    chroma_db/ chroma_db_area/ app/chroma_db_agent_memory/
```

`AGENT_ARCH` is the single switch that selects the conversational architecture.
Both architectures share the same tool layer, the same `extract_preferences` and
`critic` nodes, the same state type, and the same output contract — only the
orchestration between them differs.

---

## 4. The agent loop

`AGENT_ARCH=fc_loop` builds the graph in `app/core/agent_loop.py`
(`build_fc_graph`). It replaces classify-then-execute routing with a bounded
tool-use loop driven by the model's own function calls.

```mermaid
flowchart TD
    START([START]) -.->|ENABLE_STORE| HY[hydrate_prefs]
    START --> EP[extract_preferences]
    HY -.-> EP
    EP --> GU[guard]

    GU -->|"fair-housing refusal · greeting · statutory arithmetic"| FO[format_output_fc]
    GU -->|"otherwise — stamps turn_start"| AG[agent]

    AG -->|tool_calls| EX[execute_tools]
    EX -->|ToolMessages + artifacts| AG
    AG -->|"final text · loop cap · turn soft-wrap"| CR[critic]
    AG -->|"ask_user — terminal"| FO
    CR --> FO
    FO --> END([END])
    FO -.->|ENABLE_STORE| PP[persist_prefs]
    PP -.-> END

    HI{{"HITL gate — interrupt before a search_properties batch"}}
    HI -.- EX

    classDef parse fill:#e8f1ff,stroke:#4c78a8,color:#111;
    classDef route fill:#fff4d6,stroke:#d28b00,color:#111;
    classDef tool fill:#e8f7ed,stroke:#2f8f4e,color:#111;
    classDef llm fill:#f1e8ff,stroke:#7b4bb3,color:#111;
    classDef output fill:#ffe8ef,stroke:#c7476b,color:#111;
    classDef opt fill:#f4f4f4,stroke:#999,color:#555,stroke-dasharray:4 3;

    class EP parse;
    class GU route;
    class EX tool;
    class AG,CR llm;
    class FO output;
    class HY,PP,HI opt;
```

Solid edges are the default topology. The dashed nodes are opt-in and off by
default: `hydrate_prefs`/`persist_prefs` appear only when a cross-thread Store is
configured (`ENABLE_STORE`), and the HITL gate pauses the graph with
`interrupt()` before a `search_properties` batch when `ENABLE_HITL` is set and a
checkpointer exists. Nothing has executed at the interrupt point, so resuming
replays nothing.

`guard` and `agent` and `execute_tools` route with `Command(goto=…)`; only
`critic → format_output_fc` needs a static edge. The critic's corrective
regeneration happens **inside** the node (bounded by `critic_attempts`), which is
why no edge loops back from it.

### Nodes

| Node | Responsibility |
|---|---|
| `extract_preferences` | Updates hard/soft preferences, excluded areas, amenities and safety concerns from the current message. Shared verbatim with the legacy arch. |
| `guard` | Deterministic exits taken **before** any model call: fair-housing refusal (Equality Act 2010), greeting fast path, and statutory rent arithmetic (deposit caps, move-in totals) — questions that are decidable in Python and were observably answered wrong when left to the model. |
| `agent` | Exactly **one** bound-tools LLM call per super-step. Emits either tool calls (→ `execute_tools`), a final answer (→ `critic`), or an `ask_user` clarification (→ `format_output_fc`). |
| `execute_tools` | Runs the trailing tool-call batch concurrently (`asyncio.gather`) with per-tool timeouts, idempotency keys, the write/read dispatch policy, and taint tracking. Writes `ToolMessage`s back into `state.messages`. |
| `critic` | Checks the drafted answer against the evidence actually gathered — unsupported money figures and ungrounded station names trigger one corrective regeneration; persistent problems get a caveat rather than a fabricated fix. |
| `format_output_fc` | Builds the frontend contract: recommendations, clarification payloads, safety reports, POI lists, commute-cost cards. |

Because `agent` and `execute_tools` are both real graph nodes, the entire loop
state (`messages`, `tool_artifacts`) lives in the checkpointed `AgentState` —
which is what makes an HITL `interrupt()` a true zero-replay resume.

### The turn around the graph

The graph is the middle of a longer lifecycle. The ordering below is load-bearing
— each step is where it is because the alternative lost something observable.

```text
POST /api/alex
  │
  ├─ validate · resolve identity · resolve or create the conversation
  ├─ persist the USER message, then open a turn (survives a crash mid-generation)
  ├─ open the observation window (ContextVar) and start the turn timer
  │
  ├─ Phase 1 ── under the per-conversation turn lock ─────────────────────────
  │    deep-copy the L2 session state (preferences, accumulated criteria,
  │    full result set) · resolve focus_stack → structured listing records
  │    (session results → recommended registry → SQLite cache → CSV) ·
  │    render the recommended index · detect comparison intent ·
  │    assemble context: history + rolling summary + long-term memory
  │    (retrieved with branch-lineage scoping so a fork sees only what it
  │    actually inherited)
  │
  ├─ Phase 2 ── NO lock held across the slow model call ──────────────────────
  │    graph.ainvoke(state, checkpoint config) → the loop above
  │    HITL safety net if the graph paused · capture fc telemetry signals
  │    from final_state · DSML layer 1 sanitizes the answer BEFORE anything
  │    is persisted or handed to auto-memory
  │
  ├─ Phase 3 ── under the turn lock again ───────────────────────────────────
  │    atomic write-back of L2 state (criteria, results, registry) ·
  │    auto-memory write, hardened when the turn ended tainted
  │
  ├─ persist the ASSISTANT message · complete the turn (or fail it) ·
  │  snapshot the post-turn context for forking
  ├─ DSML layer 2 re-scans the fully serialized body
  └─ emit exactly ONE canary telemetry record — AFTER serialization succeeded,
     so a payload that fails to serialize is never recorded as a 200
```

The lock is held across the two fast phases and deliberately **not** across the
model call, so concurrent turns in the same conversation cannot interleave their
state writes without serialising on the slow path.

### Two channels for tool output

Every tool result is stored twice, on purpose:

- **`tool_artifacts`** — the raw `ToolResult.data`, untouched, used to build
  structured cards and to ground the critic.
- **The model-facing `ToolMessage`** — a derived view, length-capped per tool
  (`web_search` 8000, `search_properties` 6000, `get_property_details` 4000,
  others 4000 chars) and, for untrusted sources, sanitized and taint-marked.

Untrusted set: `web_search`, `search_properties`, `get_property_details`,
`reasoning_property`, `multi_search`. Their content is attacker-reachable (a
listing description is user-supplied text), so it never enters the model channel
unsanitized and never silently authorises a write.

### Turn budget and degradation

A turn has a hard wall-clock ceiling and degrades in a defined order rather than
running long or dying:

| Knob | Default | Meaning |
|---|---|---|
| `FC_TURN_CEILING_S` | `30.0` | Whole-turn ceiling a wrapped turn must close inside. |
| `FC_TURN_SOFT_WRAP_S` | ceiling − reserve − crumb (`23.0`) | After this, the agent opens no new tool batches and must answer from what it has. |
| `FC_FINAL_RESERVE_S` | `6.5` | Time reserved for the final generation. |
| `FC_WRAP_CRITIC_RESERVE_S` | `0.5` | Crumb left for the pure-Python render on a wrapped turn. |
| `FC_BATCH_TOOL_BUDGET_S` | `20` | Budget for one `execute_tools` batch. |
| `FC_TURN_TOOL_BUDGET_S` | `40` | Cumulative tool budget across the turn. |
| `FC_MIN_BATCH_S` | `2.0` | Below this remaining budget, no new batch is started. |
| `FC_LOOP_SOFT_CAP` | `6` | Soft cap on agent↔tools iterations. |

The three knobs that must not drift apart (`ceiling`, `soft wrap`, `final
reserve`) derive from one another unless individually overridden. If the turn
still runs out, the answer is built deterministically from the artifacts already
collected (`_artifact_grounded_fallback_answer`) rather than from a canned
apology — and if the user asked about a dimension no tool ever served, the reply
says so explicitly instead of quietly omitting it.

---

## 5. Tools

`create_tool_registry()` in `app/core/tool_system.py` is the single source of
truth; the same registry is exposed in-process and over MCP.

| Tool | Side effect | Data source |
|---|---|---|
| `search_properties` | none | OnTheMarket on-demand scrape + SQLite listing cache, geo validation, ranking |
| `get_property_details` | none | Listing cache + external description page |
| `compare_or_rank_areas` | none | Cached listing prices (`area_stats`) + commute; explicit "no data" markers |
| `calculate_commute` | none | TfL Journey Planner (transit/cycling/walking) |
| `calculate_commute_cost` | none | TfL journey + fare, combined time-and-money view |
| `get_transport_info` | none | TfL Unified API: journeys, single fares, travelcards, line status |
| `check_transport_cost` | none | Official 2025 TfL fare tables |
| `check_safety` | none | data.police.uk, last 6 months |
| `search_nearby_pois` | none | OpenStreetMap Overpass (paced, multi-mirror) |
| `get_weather` | none | Open-Meteo (geocoding + forecast) |
| `web_search` | none | Private SearXNG instance |
| `recall_memory` | none | Per-user ChromaDB memory |
| `remember` | **write** | Per-user ChromaDB memory — gated, see [§6](#6-safety-grounding-and-guardrails) |
| `ask_user` | none (**terminal**) | Ends the turn with one clarifying question in the user's language |

### MCP

`app/mcp_server.py` exposes the same registry over the Model Context Protocol on
stdio. The web process uses either the in-process registry or `MCPToolClient`,
which calls the MCP server and falls back to the in-process registry when the
subprocess is unavailable (`USE_MCP_TOOLS`, default off).

```bash
cd app && python mcp_server.py
```

> The `mcp` dependency is bounded `<2`: mcp 2.0 removed the low-level `Server`
> decorator API this server is built on, so an unbounded major turns CI red with
> no code change. See `docs/MCP.md`.

---

## 6. Safety, grounding and guardrails

The recurring defect class in this codebase is *a value is computed, stored
where a reader could find it, and then never asserted on*. Most of the controls
below exist because an instance of that shipped.

| Control | What it prevents |
|---|---|
| **Taint tracking** (`agent_loop`, `guardrails.sanitize_untrusted`) | Instructions embedded in scraped/searched text steering the agent. Untrusted output is sanitized into the model channel and marks the turn tainted. |
| **Memory-write gate** (`core/memory_gate.py`) | A tainted, unauthorised `remember` executing. Authorization = the user explicitly asked to save **and** the content is what the user actually stated; tool-derived content under a save cue still routes through explicit confirmation. Pure recall questions can never trigger a write. |
| **Read dispatch policy** (`core/tool_policy.py`) | Pointless and harmful retrieval — e.g. a statutory-limit question with no place named, where the retrieved snippet dropped a threshold and the model led with the wrong number. Reads can be refused before any work happens. |
| **DSML guard** (`core/dsml_guard.py`) | Model control tokens (`<｜tool▁calls▁begin｜>`, `<invoke …>`) reaching a user surface, being persisted, and being replayed as instructions next turn. Layer 1 sanitizes before persistence; layer 2 re-scans the serialized body. A layer-2 hit is recorded as a **leak**, not a block — because it means layer 1 failed. |
| **Critic** (`uk_rent_agent/agent/critic.py`) | Unsupported money figures and invented station names. Both are checked against the evidence surface, not against the model's confidence. |
| **Fair-housing refusal** | Discriminatory search criteria (Equality Act 2010), deterministically, before any model call. |
| **Statutory arithmetic** | Deposit-cap and move-in-total questions answered by arithmetic rather than by a language model that got them wrong. |
| **Idempotency** (`uk_rent_agent/tools/idempotency.py`) | Duplicate durable writes on retry. |
| **Strict schema adapter** (`core/strict_schema.py`) | Provider-side 400s under DeepSeek strict function calling; optional-property semantics are preserved by making them nullable. |
| **Retired-model guard** (`uk_rent_agent/llm/router.py`) | A dead model name reaching the provider. Refused at **import** with the successor named — a stale value here once made both pools return 400 on every real question for a day while `/health` stayed green. |
| **Web hardening** (`web/*`) | Server-issued session identity (client-supplied user IDs are not authorization), sliding-window rate limiting, request-size cap, HTTP-only/SameSite/Secure cookies, HTML-stripped conversation titles. |

Grounding is also a *data* concern, not only a prompt concern:

- `core/place_reference.py` supplies the nearest station from TfL's StopPoint
  index with its distance, so the field exists instead of inviting a guess — and
  the critic then checks the name the answer asserted.
- `core/commute_basis.py` distinguishes a routed TfL journey from a straight-line
  estimate and carries a measured calibration of how wrong the estimate is
  (systematically and grossly low on short trips).
- `core/area_stats.py` returns `sample_size == 0` and null statistics when the
  cache has nothing, so the caller emits an explicit no-data marker.

---

## 7. Context engineering

`app/core/context_assembler.py` owns what the model is allowed to see:

- **Token budget with a deterministic trim order** — what gets dropped first is
  a property of the code, not of the day.
- **Rolling conversation summary**, folded by a dependency-injected completion
  function (the module imports no LLM provider, and makes no network call at
  import time).
- **Turn snapshots with a durable/transient whitelist** — the contract that makes
  forking a conversation reproducible.
- **History-conflict detection** — spotting when the user has contradicted
  something they said earlier, and asking instead of silently picking one.

Around it:

- **`focus_stack`** — an ordered stack of the listings the user has focused on,
  resolved into structured records so "the second one" and 「这套」 anchor to a
  real listing rather than to whatever is nearest in the text.
- **`recommended_registry`** — an accumulated index of every listing already
  recommended in the conversation, so a later reference resolves even after the
  results panel has moved on.
- **`core/refine_results.py`** — recognises a pure narrowing of the on-screen set
  and serves it from state. Anything that is *not* a narrowing (a widened budget,
  a new area, an explicit new-search verb) falls through to a real search.
- **`core/ranking.py`** — multi-objective ranking over eligible listings (price
  0.30, commute 0.28, semantic 0.15, features 0.10, availability 0.10, freshness
  0.07). Missing evidence removes its component and re-normalises the rest,
  rather than scoring unknown information as a perfect match.

---

## 8. Search and data pipeline

```text
user criteria
   │
   ├─ area resolution ── postcodes.io / Nominatim ── city contamination guard
   │
   ├─ on-demand scrape ── OnTheMarket ──▶ SQLite listing cache (TTL, one row per
   │                                       query key, JSON blob of rich listings)
   │
   ├─ hard filters ── budget (weekly→monthly), bedrooms, room type, geo radius
   │
   ├─ enrichment ── descriptions, commute annotation (budget-aware), POI coords
   │
   ├─ semantic pass ── FAISS over listing descriptions
   │
   └─ ranking + diversification ──▶ recommendations + criteria echo
```

- `app/core/scraping/on_demand.py` serves live search; `provider.py` selects the
  startup dataset for index warm-up. OnTheMarket is the active source. Rightmove
  and Zoopla remain opt-in stubs over vendored legacy scrapers — do not expect
  live data from them.
- POI retrieval paces requests across an Overpass mirror pool, caches per type,
  and degrades honestly when the pool is rate-limited.
- The RAG layer combines FAISS listing embeddings, ChromaDB conversation memory,
  and ChromaDB area knowledge.
- Every search-side time budget is configurable (`SEARCH_*`, `POI_*`) so the tool
  can return a smaller honest answer instead of blowing the turn ceiling.

---

## 9. Conversations, branching and memory

- **Durable store** — `ConversationStore` (SQLite) holds conversations,
  messages, turns and favorites. Each turn is opened before generation and
  completed or failed after, so a crash leaves an explicit failed turn rather
  than a half-written conversation. A failed turn is never a valid fork target.
- **Branching** — `POST /api/conversations/<id>/fork` branches from a completed
  turn; `POST …/edit_turn` rewrites an earlier user message into a new branch;
  `GET …/version_map` and `GET …/turns` expose the branch structure. The UI
  stacks branches by root and can open two of them side by side.
- **Checkpoints** — LangGraph SQLite checkpoints, **one file per architecture**
  (`CHECKPOINT_DB_PATH`). The conversation store is deliberately **shared**
  between pools; checkpoints deliberately are not.
- **Long-term memory** — `app/rag/agent_memory.py` over ChromaDB, namespaced per
  user, erasable via `/api/forget_me`.
- **Local auth** — JSON credential store with password hashes
  (`.runtime/users.json`); an authenticated session's identity is authoritative,
  so a client cannot impersonate an account through a header.

---

## 10. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness; served by Starlette, stamps `X-Agent-Arch` / `X-Agent-Version` |
| `GET` | `/` | The web UI |
| `POST` | `/api/alex` | Conversational turn (the agent graph) |
| `POST` | `/api/search_direct` | Deterministic structured search, no LLM router |
| `POST` | `/api/auth/register` · `/api/auth/login` · `/api/auth/logout` | Local auth |
| `GET` | `/api/auth/me` | Current identity |
| `GET`/`POST` | `/api/conversations` | List / create |
| `PATCH`/`DELETE` | `/api/conversations/<id>` | Rename / delete |
| `GET` | `/api/conversations/<id>/messages` | Transcript |
| `POST` | `/api/conversations/<id>/fork` | Branch from a completed turn |
| `POST` | `/api/conversations/<id>/edit_turn` | Edit a user message into a new branch |
| `GET` | `/api/conversations/<id>/version_map` · `/turns` | Branch structure |
| `POST` | `/api/clear_history` | Clear conversation history |
| `POST` | `/api/forget_me` | Erase this user's long-term memory |
| `GET`/`POST`/`DELETE` | `/api/favorites[/<url>]` | Favorites |
| `POST` | `/api/generate_map` | Folium amenity map (HTML) |

Both agent endpoints stamp `X-Request-Id` for log correlation, and every response
carries `X-Agent-Arch` / `X-Agent-Version`. `/api/alex` echoes `conversation_id`
and `turn_id` on every outcome including errors, and always answers HTTP 200 for
an agent-side error so the client can still adopt the freshly created
conversation instead of orphaning it.

---

## 11. Running locally

### Prerequisites

- Python 3.10–3.12
- A DeepSeek API key (default cloud LLM path); Ollama optionally, for local models
- Docker + Compose v2 for the full stack

### Install

```bash
pip install -e .          # runtime
pip install -e '.[dev]'   # + pytest, pytest-asyncio, import-linter
```

### The two env files — both are required, and they are not the same file

| File | Read by | Holds |
|---|---|---|
| `app/.env` | the application (`Config.from_env`, `load_dotenv`) | LLM keys, `FLASK_SECRET_KEY`, provider/tool settings. Bind-mounted read-only into the container. |
| `.env` (repo root) | **docker compose itself**, for `${VAR}` substitution | `SEARXNG_SECRET` (required — compose refuses to start without it), and the canary image pins. |

`app/.env`:

```env
FLASK_SECRET_KEY=replace-with-a-long-random-value

LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_deepseek_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash

USE_MCP_TOOLS=0
PROPERTY_SOURCE=auto
SCRAPER_CACHE_TTL_HOURS=24
SCRAPE_ON_STARTUP=0

# Optional integrations
TFL_APP_KEY=
GOOGLE_MAPS_API_KEY=
OPENROUTESERVICE_API_KEY=
```

> `DEEPSEEK_MODEL` must not name a retired model. `deepseek-chat` and
> `deepseek-reasoner` were retired by the provider and are **refused at startup**
> with the successor named — see `RETIRED_MODEL_NAMES` in
> `src/uk_rent_agent/llm/router.py`.

Root `.env` (copy `.env.example`):

```env
SEARXNG_SECRET=<openssl rand -hex 32>
```

### Run

```bash
python -m uk_rent_agent.web     # production-style ASGI shell (requires FLASK_SECRET_KEY)
python app/app.py               # development Flask entry point (PORT, default 5001)
curl http://127.0.0.1:5001/health
```

### Docker (full stack)

```bash
cp deploy/searxng-settings.yml.example searxng/settings.yml   # first time only
docker compose up -d --build
docker compose ps
```

This starts `app` (:5001), `searxng` (:8080) and `valkey`. The canary `app-fc`
pool is behind a compose profile and does not start by default. The image
pip-installs `src/uk_rent_agent` and copies `app/`; all runtime data (Chroma
indexes, `.runtime/`, `app/.env`) is bind-mounted from the host, never baked in.
See `docs/DOCKER.md`.

---

## 12. Configuration

Read by `src/uk_rent_agent/config.py` (which loads `app/.env`) and by the modules
that own each subsystem. The most important knobs:

**Core**

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | — | Required by the ASGI entry point |
| `LLM_PROVIDER` | `deepseek` | `deepseek` or `ollama` |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Retired names refused at import |
| `USE_MCP_TOOLS` | `0` | Execute tools over the MCP subprocess instead of in-process |
| `PROPERTY_SOURCE` | `auto` | Startup dataset: `auto` / `csv` / `scraper` |
| `SCRAPE_ON_STARTUP` | `0` | Allow scraping at startup |
| `SCRAPER_CACHE_TTL_HOURS` | `24` | Listing cache freshness window |
| `CORS_ORIGINS` | localhost | Comma-separated allowed origins |

**Architecture and canary**

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_ARCH` | `legacy` | `fc_loop` selects the function-calling loop |
| `DEEPSEEK_STRICT` | `0` | Strict function calling for the fc loop |
| `APP_CANDIDATE_SHA` | git sha / `unknown` | Stamped on `X-Agent-Version` and every telemetry record |
| `CANARY_LOG_PATH` | `<runtime>/logs/canary-<arch>.jsonl` | Per-turn telemetry sink; `off` disables |

**State paths**

| Variable | Default | Purpose |
|---|---|---|
| `CHECKPOINT_DB_PATH` | `.runtime/checkpoints.sqlite3` | LangGraph checkpoints — **per architecture** (`CHECKPOINT_PATH` is a back-compat fallback) |
| `CONVERSATION_DB_PATH` | next to the checkpoint DB | Conversation store — **shared between pools** |
| `AUTH_DB_PATH` | `.runtime/users.json` | Local credential store |
| `ENABLE_CHECKPOINTER` | `1` | Enable checkpoints |

**Security / web**

| Variable | Default | Purpose |
|---|---|---|
| `REQUIRE_AUTH` | `0` | Require an authenticated session on `/api/*` |
| `SESSION_COOKIE_SECURE` | `0` | Set behind TLS |
| `ALLOW_LEGACY_CLIENT_USER_ID` | `0` | Accept client-supplied user IDs (migrations only) |
| `MAX_REQUEST_BYTES` | `262144` | Request-size cap |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding-window limiter |

Beyond these, the turn budget (`FC_*`, [§4](#4-the-agent-loop)), the search and
POI budgets (`SEARCH_*`, `POI_*`, `OVERPASS_*`), the area-recommendation cache
(`AREA_RECO_*`) and the geocode caches (`GEOCODE_*`) are all tunable without a
code change; each is documented at its definition.

---

## 13. Production: the two-pool canary

Rolling a new conversational architecture into production is not a deploy — it
is an experiment with a rollback lever. The full procedure lives in
`docs/canary_runbook.md`; this section is the whole shape of it.

### The two pools

| Pool | Service | Port | Config | Image |
|---|---|---|---|---|
| **legacy** | `app` | `127.0.0.1:5001` | `AGENT_ARCH=legacy` | built from the tree (`:latest`) |
| **fc** | `app-fc` | `127.0.0.1:5002` | `AGENT_ARCH=fc_loop`, `DEEPSEEK_STRICT=1` | **immutable pre-built tag** `uk-rent-agent:canary-fc-loop-<sha>` |

The fc service has **no `build:`** in compose, deliberately: the working tree can
never silently become what canary traffic executes. A new candidate gets a new
tag; tags are never rebuilt in place.

```bash
docker compose up -d                              # legacy stack only (unchanged)
docker compose --profile canary up -d app-fc      # add the fc pool
```

### Per-pool environment

| Env | legacy (`app`) | fc (`app-fc`) |
|---|---|---|
| `AGENT_ARCH` | `legacy` | `fc_loop` |
| `DEEPSEEK_STRICT` | unset | `1` |
| `APP_CANDIDATE_SHA` | `${LEGACY_APP_SHA:-}` | `${FC_CANARY_SHA:?}` |
| `CHECKPOINT_DB_PATH` | `checkpoints.sqlite3` | `checkpoints_fc.sqlite3` |
| `CONVERSATION_DB_PATH` | `conversations.sqlite3` | `conversations.sqlite3` (**shared**) |
| `CANARY_LOG_PATH` | `canary-legacy.jsonl` | `canary-fc_loop.jsonl` |

The two substitution operators differ on purpose. The fc pins are `:?` — no fc
pool without an explicit pin. The legacy pin is `:-` with an empty default,
because `app` is the **only rollback target** and a `:?` there would make every
compose command fail while the variable was missing, including the one that
brings the escape hatch back.

### Assignment and state isolation

- **Sticky per conversation.** The serving architecture is persisted on the
  conversation record (a snapshot-whitelisted field, so it survives fork and
  restart). A conversation keeps its arch for life; changing the rollout weight
  only changes what **new** conversations are assigned.
- **Checkpoints are isolated per arch.** Neither pool reads the other's
  LangGraph state — the channels diverge, and a cross-arch resume corrupts the run.
- **Message history is shared.** Either pool can rebuild a conversation from the
  shared transcript, which is what makes rollback survivable.

### Telemetry

Each completed turn emits exactly one JSON line (`event: canary.turn`, schema
v2) carrying arch, candidate sha, endpoint, HTTP status, turn outcome, latency,
security counters (denied vs **executed** — different events), `dsml_blocked` vs
`dsml_leak`, and degradation flags. Observations are accumulated in a ContextVar
*as they happen*, so a turn that crashes still reports what its provider did —
a schema 400 is a plausible cause of a crash and must not vanish with the
final state.

### Gates

`scripts/canary_report.py` reads the stream and returns a verdict as an exit
code. Stages advance only when **both** minima clear:

| Stage | fc traffic | Min hold | Min fc turns |
|---|---|---|---|
| `internal` | internal only | 24h | 50 |
| `c1` | 5% | 24h | 200 |
| `c2` | 20% | 48h | 500 |
| `c3` | 50% | 72h | 1000 |
| `flip` | 100% | 7d | 2000 |

**Zero-tolerance (exit 3, immediate rollback):** a tainted/unauthorised memory
write **executed**; a forbidden write **executed**; a DSML/tool-markup leak; a
systematic schema/API 400. A *denied* attempt is the designed safe path and does
not trip these.

**Stage-pause (exit 2):** fc p50 > 6000 ms or p95 > 30000 ms; degraded-turn rate
(partial ∪ soft-wrapped) > 10%; forbidden-read rate, no-evidence-number rate or
5xx rate more than 1pp worse than legacy.

| Exit | Meaning |
|---|---|
| `0` | proceed / hold-ok |
| `2` | stage-pause **or** instrumentation-hold |
| `3` | zero-tolerance breach |
| `1` | input/runtime error — nothing was measured |
| `64` | CLI usage error — nothing was measured |

```bash
python scripts/canary_report.py --input .runtime/logs/ --stage internal --since <ISO>
python scripts/canary_report.py --input .runtime/logs/ --window 24 --json out/canary.json
```

A turn count that does not reconcile is an **instrumentation hold**, not a pass:
if the telemetry does not describe the run that was driven, every rate in the
report has an unknown denominator. Read the applied window off the report's own
`record filter:` line, never off your shell history.

> Rotate telemetry **while the pool is stopped**. The logger holds its fd; a
> rename moves the inode and the process keeps writing into the file you thought
> you archived — silently mixing two builds into one window.

### Rollback

- **Normal:** stop assigning fc to new conversations; in-flight fc conversations
  finish on fc — sticky assignment is honoured.
- **Emergency:** point the upstream back at legacy immediately. Legacy rebuilds
  affected conversations from the shared transcript into its own checkpoint
  namespace and never reads fc's checkpoints, which are treated as abandoned.

```bash
bash deploy/switch_pool.sh --status
bash deploy/switch_pool.sh --to legacy
```

`switch_pool.sh` verifies the target pool answers with the expected arch
*before* touching nginx, rewrites only the `server 127.0.0.1:PORT;` line inside
the upstream block (the live conf has drifted from the repo copy and that drift
must survive a switch), requires `nginx -t` to pass, re-verifies arch **and** the
full 40-char sha at the public endpoint afterwards, and restores the backup on
any failure. `deploy/switch_pool_rehearse.sh` runs the identical code path
against a private nginx with no root and no public traffic.

---

## 14. Deploying

### One command

```bash
bash deploy/release.sh              # mainline tip → CI check → confirm → re-pin → deploy
bash deploy/release.sh --dry-run    # print the plan, change nothing
bash deploy/release.sh --ref <sha>  # release a specific commit (rollback, hotfix)
```

`release.sh` targets the tip of the **remote** mainline (never a local commit,
never an uncommitted tree), queries that commit's CI conclusion and aborts on a
failure, requires a clean tracked tree, shows you `old-pin → new-pin` and asks
for confirmation, and only then advances the pin.

### The pin gate

`deploy/update.sh` refuses to build anything but the exact sha named in
`/etc/rentcompass/deploy.env` — a root-owned file **outside version control**, so
no commit in this repo can change what production runs. It then deploys
**whichever pool the public nginx upstream is actually serving** (it reads the
upstream line rather than assuming), and refuses to report success unless that
pool answers `/health` with the pinned arch *and* the full 40-char sha.

```bash
bash deploy/update.sh --status      # pin + both pools + which one is public
bash deploy/update.sh --both        # also level the standby (rollback) pool
```

### Monitoring

A systemd timer runs `deploy/monitoring/rentcompass-monitor.sh` every 5 minutes.
Probes are read-only and never touch `/api/*`, so monitoring cannot pollute agent
state or the telemetry the gate reads — with one deliberate exception: at most
hourly it makes a **direct provider call**, because the constraint that made the
script safe is the same one that made it blind to a provider outage that leaves
`/health` green.

Every status line is prefixed with the sha256 of the running script, so install
drift is visible rather than inferred:

```bash
bash deploy/monitoring/check_install_drift.sh
sudo install -m 0755 deploy/monitoring/rentcompass-monitor.sh /usr/local/bin/rentcompass-monitor.sh
```

TLS and nginx provisioning: `deploy/setup_nginx_http.sh`, `deploy/setup_tls_8443.sh`,
`deploy/DEPLOY_DOMAIN.md`.

---

## 15. Evaluation

Two independent mechanisms, with different jobs:

- **`evaluation/`** — a self-contained, offline-first evaluation framework
  (routing, tool use, grounding, cost, latency, resilience, memory).
- **`evals/` + `uk-rent-eval-gate`** — golden datasets and a CI threshold gate
  applied to a generated report.

### The framework

```bash
python -m evaluation.run_benchmark --smoke --offline      # 10 smoke cases, mechanics only
python -m evaluation.run_benchmark --live --config routed_models --max-cost-usd 5
python -m evaluation.run_ablation --study both --offline --smoke
python -m evaluation.fault_injection.run                  # 15 injected-fault scenarios
python -m evaluation.memory_eval                          # needs chromadb
python -m evaluation.report --results evaluation/results --out evaluation/results
```

- **98-case benchmark** across 7 categories (retrieval, money, commute,
  crime/POI, multi-constraint, grounding, memory) exercising 24 distinct
  machine-checkable constraint types out of 29 implemented checkers, plus
  guard-regression, cold-resilience and extension shards, with recorded fixtures
  for deterministic replay.
- **Offline by default and unbilled** (deterministic fake model). `--live` uses
  the real provider and meters spend against a **hard cost cap** that refuses to
  start a case whose estimated cost would exceed it.
- **Guard shard gates** (`--repeat K`): a hard-gate case passes only when **all
  K** repeats pass — never averaged. A separate zero-tolerance sweep fails the
  run on any single `forbidden_tool_executed`, `tainted_write_executed`,
  `budget_breach` or `no_evidence_numbers`. Generation stability is reported as a
  diagnostic and never folded into the gate; the latency SLO is a third,
  independent gate.
- **Honest denominators.** Every reported number carries its `num/den` and its
  source file; a metric that was not produced says so with the reason, and is
  never estimated.

### Results that are safe to cite

From `evaluation/results/CV_METRICS.md`, which classifies each metric and
attaches the caveat it must be quoted with:

| Result | Measured | Caveat it must carry |
|---|---|---|
| Per-node model routing vs an all-strong baseline (98 cases, live) | strong-model calls 165/170 → 78/172 (−52.7%), cost −24.3%, mean e2e −38.4%, grounding held 160/207 | the saving is **token-volume** driven, not a cheaper rate |
| Grounding fidelity (98 cases, live) | verifiable claims grounded 152/204 (74.5%), money claims 121/152 (79.6%), contradicted 1 | heuristic grader, single live run; grounding fidelity ≠ answer quality |
| Retrieval parallelization (16 cases × 3) | retrieval-stage latency mean −57.1%, p95 −42.0%, race anomalies 0/48 | **retrieval-stage** only; end-to-end is synthesis-dominated and ~unchanged |
| Fault injection (15 scenarios) | faults surfaced 15/15, idempotency 3/3 with 0 duplicate writes, fallback 2/2, post-fault completion 13/15 | only the model is mocked; resilience *mechanics*, not accuracy |
| Memory store checks (real ChromaDB) | isolation 5/5, forget 3/3, restart recovery 1/1, identity gate 7/7 | small n; keep separate from the stubbed extraction numbers |

The same file lists what must **not** be cited (raw end-to-end pass rate,
stubbed memory-extraction figures, any n<15 single-run rate) and why.

---

## 16. Testing and CI

```bash
python -m pytest -q          # both trees; ~3,355 tests, ~2 minutes
uk-rent-eval-gate report.json
```

| Tree | Targets |
|---|---|
| `tests/` | The live runtime under `app/` (flat `core` / `rag` modules) |
| `tests_refactor/` | The installable `uk_rent_agent` package under `src/` |

Both trees are hermetic — LLM and network calls are stubbed, and the live
integration tests are env-gated **off** (`RUN_LIVE_OSM`, `RUN_LIVE_SCRAPE`).
A handful of guards deliberately assert against the deployment box itself (for
example, that the installed monitor script matches the committed one); those
skip where their subject does not exist.

CI (`.github/workflows/ci.yml`) runs the full suite on Python 3.11 for every push
to `main` and every PR into `main` or `telemetry/**`, plus a gitleaks secret scan
of the working tree. `.pre-commit-config.yaml` mirrors that secret scan locally
and adds hygiene hooks (trailing whitespace, EOF, merge-conflict markers, a 1 MB
file-size ceiling); hook revisions are pinned deliberately rather than
auto-updated.

Cross-module contracts are guarded by tests rather than by convention — for
example, the evaluator's copy of the dimension vocabulary is allowed to exist but
not allowed to disagree with the product's
(`tests/test_dimension_fanout.py`), and every LLM call must be observable
(`tests/test_all_llm_calls_are_observed.py`).

---

## 17. Repository map

```text
uk_rent_recommendation/
├── src/uk_rent_agent/          installable package (pip install -e .)
│   ├── agent/                  state, contracts, critic, guardrails, persistence
│   ├── data/                   cache, parsing, repository
│   ├── domain/                 schema + constants
│   ├── evals/                  metrics, harness, CI threshold gate
│   ├── llm/                    model router + retired-name guard
│   ├── tools/                  idempotency
│   └── web/                    ASGI shell, Flask wrapper, auth/session/conversation
│                               stores, identity, rate limiting
│                               (streaming.py is an SSE helper; no route wires it yet)
│
├── app/                        domain application
│   ├── app.py                  Flask routes, canary record assembly, focus/registry wiring
│   ├── mcp_server.py           MCP stdio server
│   ├── unified-ui.html         web UI (split-compare, branch stacking, criteria panel)
│   ├── core/
│   │   ├── agent_loop.py       fc_loop graph — the production architecture
│   │   ├── langgraph_agent.py  legacy graph (Appendix A) + shared helpers
│   │   ├── loop_prompts.py     system directive + behaviour rules
│   │   ├── context_assembler.py  context budget, summary, snapshots, conflicts
│   │   ├── tool_policy.py · memory_gate.py · dsml_guard.py · strict_schema.py
│   │   ├── dimensions.py · turn_observations.py · canary_telemetry.py
│   │   ├── ranking.py · refine_results.py · recommend_areas.py · area_stats.py
│   │   ├── place_reference.py · commute_basis.py · safety_reference.py
│   │   ├── scraping/           OnTheMarket provider, cache, normalisation
│   │   └── tools/              the 14 tool implementations
│   ├── rag/                    embeddings, agent memory, coordinator
│   └── scripts/                dataset build, OSM prefetch
│
├── evaluation/                 offline evaluation framework (benchmark, ablation,
│                               fault injection, memory eval, graders, reports)
├── evals/                      golden datasets + CI thresholds
├── tests/ · tests_refactor/    behaviour + architecture/contract tests
├── deploy/                     release.sh, update.sh, switch_pool.sh, nginx, TLS,
│                               monitoring (systemd timer + drift check)
├── scripts/                    canary_report.py, canary_cost.py, calibration samplers
├── docs/                       see below
├── fine_tuning/                optional offline LoRA extraction pipeline (not used at runtime)
├── Dockerfile · docker-compose.yml · pyproject.toml
└── .env.example                root-level compose env template
```

Runtime data is gitignored and lives outside the tracked tree: `.runtime/`
(checkpoints, conversation DB, auth store, idempotency, telemetry logs),
`chroma_db/`, `chroma_db_area/`, `app/chroma_db_agent_memory/`, `app/data/`.

---

## 18. Documentation index

| Document | What it covers |
|---|---|
| `docs/HANDOFF.md` | Master index for the fc_loop programme — status, decisions, open threads. Start here for project history. |
| `docs/canary_runbook.md` | The canary rollout procedure, gates and rollback in full |
| `docs/harness_migration_design.md` | The design that produced the fc_loop architecture |
| `docs/DOCKER.md` | Container runbook |
| `docs/MCP.md` | MCP protocol integration |
| `docs/TESTING.md` | The two test trees and how imports resolve |
| `docs/eval_infrastructure.md` · `docs/evaluator_contract.md` | Measurement infrastructure and the evaluator contract |
| `docs/layered_agent_architecture_proposal.md` | A design proposal — not approved, not running |
| `deploy/DEPLOY_DOMAIN.md` · `deploy/monitoring/README.md` | Domain/TLS provisioning; pin gate + health monitor install |
| `evaluation/README.md` · `evaluation/AUDIT.md` | Evaluation framework guide and repository audit |
| `evaluation/results/REPORT.md` · `CV_METRICS.md` | Generated results and per-claim citation guidance |

---

## Appendix A — the legacy architecture

`AGENT_ARCH=legacy` (the code default) builds the earlier classify-then-execute
graph in `app/core/langgraph_agent.py`. It is retained for one reason: it is the
**rollback target**, and rollback has to be a path that is known to work.

```text
START → extract_preferences → decide_tool
          direct_answer  → generate_response
          clarification  → format_output
          multi_search   → dispatch_searches → search_worker(s) → gather_searches
          any other tool → execute_tool
        generate_response → critic → format_output → END
```

`decide_tool` routes deterministically for memory recall, result follow-ups,
property-detail questions, greetings, explicit no-commute intent and live
transport queries, and falls back to LLM tool classification otherwise;
`dispatch_searches` fans out planned sub-searches with `Send` and reduces them in
`gather_searches`.

It shares the tool layer, `extract_preferences`, `critic` and the output contract
with the fc loop, and the shared dimension vocabulary in `core/dimensions.py`
guarantees both architectures answer the same question about the same nouns.
The structured form path (`/api/search_direct`) sits outside both graphs.

---

## License

Educational and research use.
