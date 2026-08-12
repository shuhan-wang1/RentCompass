# Docker deployment

Runs the whole agent as a containerized stack so the `web_search` tool has a
real search backend (SearXNG) instead of a dead `localhost:8080`.

```
┌────────────┐   http://searxng:8080    ┌────────────┐   cache/limiter   ┌────────┐
│    app     │ ───────────────────────▶ │  searxng   │ ────────────────▶ │ valkey │
│  :5001     │      (JSON API)          │  :8080     │                   │        │
└────────────┘                          └────────────┘                   └────────┘
   agent web app                    private metasearch backend        redis-compatible store
```

| Service   | Image                        | Port (host) | Purpose                                   |
|-----------|------------------------------|-------------|-------------------------------------------|
| `app`     | built from multi-stage `Dockerfile` | `127.0.0.1:5001` | Agent web app (uvicorn/ASGI)       |
| `searxng` | digest-pinned SearXNG image  | `127.0.0.1:8080` | Private search backend                |
| `valkey`  | digest-pinned Valkey image   | —           | Internal SearXNG cache/limiter             |

The app image uses an immutable Python base digest, installs every registry
artifact through `--require-hashes`, builds the local package without dependency
resolution, and runs `pip check` before the runtime stage is emitted.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version`).
- `app/.env` present with at least `FLASK_SECRET_KEY` and your LLM
  credentials (`DEEPSEEK_API_KEY` for the default `LLM_PROVIDER=deepseek`).
  It is mounted read-only into the container — nothing secret is baked into the image.

## Run

```bash
# First time only: create the SearXNG live config (a gitignored runtime file).
cp deploy/searxng-settings.yml.example searxng/settings.yml

docker compose up -d --build      # build the app image + start all three services
docker compose ps                 # all should be Up / healthy
```

`searxng/settings.yml` is gitignored on purpose — the container chowns it to its
own uid, which would otherwise clash with git. `deploy/update.sh` recreates it
from the example automatically if it's missing.

Then open <http://localhost:5001>. `/live` checks only the process; `/ready`
checks required state and reports optional SearXNG/RAG loss as degraded.

> **Port 5001:** if you already run the app on the host (`uvicorn ... --port 5001`),
> stop it first — the container publishes the same port. The container replaces it.

## How the wiring works

- The `web_search` tool reads `SEARXNG_URL` (see `app/core/web_search.py`).
  Compose sets it to `http://searxng:8080`; on the host it defaults to
  `http://localhost:8080`, so the same code works both ways.
- `searxng/settings.yml` enables the **JSON API** (`search.formats: [html, json]`)
  and disables the request `limiter` — both required for a server-to-server caller.
  A default SearXNG install serves HTML only and returns `403` for `format=json`.
- Pre-built RAG indexes, the scraped-data cache, `.runtime` checkpoints, and the
  `.env` are **bind-mounted** from the host, so the container shares the same data
  as a host run and persists writes back.
- The embedding model is cached in the `hf_cache` named volume (downloaded once).
- Both published ports bind to loopback. Nginx overwrites `X-Forwarded-For`
  with the socket peer instead of appending a client-controlled chain.

## Verify the search backend directly

```bash
curl "http://localhost:8080/search?q=London+rent&format=json" | jq '.results | length'
```

A non-zero count means the JSON API is live. Inside the app container the same
call goes to `http://searxng:8080`.

## Common operations

```bash
docker compose logs -f app        # tail app logs
docker compose logs -f searxng    # tail search backend logs
docker compose restart app        # restart after editing .env
docker compose up -d --build app  # rebuild after changing app code
docker compose down               # stop everything (data volumes persist)
```

Persistent bind mounts and databases still require an off-host backup plan. Use
the verified, encrypted workflow in [`runtime_recovery.md`](runtime_recovery.md);
do not treat Docker volumes or a successful container restart as a backup.

## One-time legacy AgentMemory retirement

The current SQLite memory runtime imports the legacy Chroma files read-only and
keeps a verified lineage until the old pool can no longer return. Do not delete
`chroma.sqlite3` or its UUID index directories manually. After one pinned,
Chroma-free commit is deployed and `/ready` on **both** pools reports that same
full SHA, run:

```bash
bash deploy/retire_legacy_agent_memory.sh
```

The script shares the deployment lock, rejects a dirty/unpinned tree, proves both
images lack `chromadb`, seals the exact legacy count and digest, shows the exact
delete/preserve boundary, and asks for confirmation. It preserves
`agent_memory.sqlite3` and rechecks both pools after retirement. From that point,
do not roll back to an image that still depends on Chroma.

## Notes

- Default `LLM_PROVIDER=deepseek` (cloud) needs outbound internet only. If you
  switch to `LLM_PROVIDER=ollama`, point `OLLAMA_BASE_URL` at
  `http://host.docker.internal:11434` and add
  `extra_hosts: ["host.docker.internal:host-gateway"]` to the `app` service.
- The first app start downloads the embedding model into `hf_cache`; give it a
  minute before the health check goes green.
