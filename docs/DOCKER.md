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
| `app`     | built from `Dockerfile`      | `5001`      | Agent web app (uvicorn/ASGI)              |
| `searxng` | `searxng/searxng:latest`     | `8080`      | Search backend for the `web_search` tool  |
| `valkey`  | `valkey/valkey:8-alpine`     | —           | Cache / rate-limit store for SearXNG      |

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

Then open <http://localhost:5001>.

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

## Notes

- Default `LLM_PROVIDER=deepseek` (cloud) needs outbound internet only. If you
  switch to `LLM_PROVIDER=ollama`, point `OLLAMA_BASE_URL` at
  `http://host.docker.internal:11434` and add
  `extra_hosts: ["host.docker.internal:host-gateway"]` to the `app` service.
- The first app start downloads the embedding model into `hf_cache`; give it a
  minute before the health check goes green.
