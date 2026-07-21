# fc_loop Canary Runbook (2026-07-20 plan)

Operational runbook for rolling the `fc_loop` harness out behind a canary against the
`legacy` (LangGraph) arch. This encodes shuhan's 2026-07-20 plan verbatim in structure.
The offline gate evaluator is `scripts/canary_report.py`; it reads the `canary.turn`
telemetry stream and returns an exit code (0 proceed/hold, 2 stage-pause, 3 zero-tolerance).

---

## 1. Two pools

| Pool | Config | Image |
|---|---|---|
| **legacy** | prod-config (unchanged) | current prod image |
| **fc** | `AGENT_ARCH=fc_loop`, `DEEPSEEK_STRICT=1` | built from the **current candidate tag** `canary/fc-loop-<sha>` — an immutable tag cut on the commit that carries the canary infra + these deployment fixes, **never the branch tip** |

- The fc image is **pinned to an immutable tag**, not to `feat/harness-fc-loop`'s HEAD.
  Rebuilding the branch must not move what canary traffic runs. Cut a **new** tag to advance
  the candidate.
- **Superseded history:** an earlier draft pinned `canary/fc-loop-7db03e7`. Do **not** deploy
  it — `7db03e7` predates the canary infrastructure (sticky per-conversation assignment,
  per-turn telemetry, and the `X-Agent-*` headers landed later, in `3d215fb`/`14312f0`), so an
  image built from it has no canary support at all. The deployable is the **current candidate
  tag** cut on the commit that includes the canary infra together with the compose/env wiring
  in this round.
- Both pools run the **same** application; only the arch flag, strict flag, image, and the
  DB-path / telemetry env pins differ.

### Env wiring

Per-pool environment, as set in `docker-compose.yml`. `CHECKPOINT_DB_PATH` (separate per arch)
is the state-isolation boundary; `CONVERSATION_DB_PATH` is **the same file for both pools** so
either arch can rebuild a conversation from the shared transcript on rollback.

| Env | legacy (`app`) | fc (`app-fc`) |
|---|---|---|
| `AGENT_ARCH` | `legacy` | `fc_loop` |
| `DEEPSEEK_STRICT` | (unset) | `1` |
| `APP_CANDIDATE_SHA` | (unset) | `${FC_CANARY_SHA}` (candidate sha) |
| `CHECKPOINT_DB_PATH` | `/app/.runtime/checkpoints.sqlite3` | `/app/.runtime/checkpoints_fc.sqlite3` |
| `CONVERSATION_DB_PATH` | `/app/.runtime/conversations.sqlite3` | `/app/.runtime/conversations.sqlite3` (**shared**) |
| `CANARY_LOG_PATH` | `/app/.runtime/logs/canary-legacy.jsonl` | `/app/.runtime/logs/canary-fc_loop.jsonl` |

> **⚠ `CHECKPOINT_DB_PATH` was previously mis-documented.** Earlier notes told ops to set the
> checkpoint path via `CHECKPOINT_PATH`, but the code read a different variable — so the pin
> silently had no effect. This is now fixed: **`CHECKPOINT_DB_PATH` is the canonical knob** and
> `CHECKPOINT_PATH` is accepted only as a back-compat fallback. If both are set and differ,
> `CHECKPOINT_DB_PATH` wins (startup logs a one-line warning). Default when neither is set:
> `<root>/.runtime/checkpoints.sqlite3`.
>
> `CANARY_LOG_PATH`: unset → default `<checkpoint dir>/logs/canary-<arch>.jsonl`; `off` disables
> telemetry; any other value is used as the path verbatim.

> **Legacy pool telemetry is not live until recreation.** The pins on the legacy `app` service
> only make current effective defaults explicit and turn on telemetry; they take effect the next
> time that container is **recreated**. Ops keeps the running legacy container as-is for now, so
> the CURRENT legacy prod image predates telemetry and emits nothing — the relative
> (vs-legacy) gate metrics in §3 read *not instrumented* until the legacy pool is recreated on a
> telemetry-capable image. Do that recreation **before advancing past `internal`**; the
> `internal` stage judges fc **absolutes only** and needs no legacy baseline.

### Assignment — sticky, per-conversation

- Assignment is **sticky per conversation** and **persisted on the conversation record**
  (a snapshot-whitelisted field, so it survives fork/restart).
- **No dynamic hash thresholds.** A conversation, once assigned, keeps its arch for its
  whole life regardless of the current rollout weight. Changing the weight only changes
  which arch **new** conversations are assigned.

### State isolation

- **Separate checkpoint DBs per arch.** fc writes only to the fc checkpoint namespace;
  legacy writes only to the legacy namespace. Neither reads the other's checkpoints.
- **Shared message history.** Both arches see the same user-visible message transcript, so
  a conversation reads coherently and either arch can rebuild context from it.

### Image build (immutable fc image)

The fc pool has **no `build:`** in compose — it runs a fixed, pre-built image, so the working
tree can never silently become what canary traffic executes. Build the image out of band from
the current candidate tag and reference it by an immutable tag:

```
# 1. Check the candidate commit out into an isolated tree (does not touch your branch):
git worktree add /tmp/fc-<sha> canary/fc-loop-<sha>
#    (or: git archive canary/fc-loop-<sha> | tar -x -C /tmp/fc-<sha>)

# 2. Build a uniquely, immutably tagged image from it:
docker build -t uk-rent-agent:canary-fc-loop-<sha> /tmp/fc-<sha>

# 3. Clean up the worktree when done:
git worktree remove /tmp/fc-<sha>
```

- **Never** retag or reuse `:latest` for the fc pool, and never rebuild the tag in place — a
  new candidate gets a **new** `<sha>` tag.
- Wire it into the compose `.env` (root-level, next to `docker-compose.yml`):

  ```
  FC_CANARY_IMAGE=uk-rent-agent:canary-fc-loop-<sha>
  FC_CANARY_SHA=<sha>
  ```

  Both are `:?`-required by the `app-fc` service — the fc pool refuses to start if either is
  unset, so it can never come up on an ambiguous image or with an unpinned sha.

### Bring-up (internal stage)

The default `docker compose up -d` is **unchanged** — it brings up the legacy stack only. The
fc pool is behind the `canary` profile:

```
# Start the internal fc pool (loopback :5002, not routed by nginx):
docker compose --profile canary up -d app-fc

# Verify: expect 200 plus the canary response headers.
curl -s -D- http://127.0.0.1:5002/health
#   → X-Agent-Arch: fc_loop
#   → X-Agent-Version: <sha>          (== FC_CANARY_SHA)
```

- Per-turn telemetry lands on the host at `./.runtime/logs/*.jsonl` (fc →
  `canary-fc_loop.jsonl`) via the shared `./.runtime` bind mount.
- Report on the internal stage (fc absolutes only — see the legacy-telemetry note in §1):

  ```
  python scripts/canary_report.py --input .runtime/logs/ --stage internal --since <stage-start-ISO>
  ```

---

## 2. Stage table

Each stage advances **only when BOTH minima clear** — the elapsed-time floor **and** the
fc-turn-count floor. Neither alone is sufficient.

| Stage | fc traffic | Min hold | Min fc turns |
|---|---|---|---|
| `internal` | internal only | 24h | 50 |
| `c1` | 5% | 24h | 200 |
| `c2` | 20% | 48h | 500 |
| `c3` | 50% | 72h | 1000 |
| `flip` | 100% | 7d (168h) | 2000 |

`canary_report.py --stage <name> --since <stage-start-ISO>` reports `turns_ok`, `hours_ok`,
and `eligible = turns_ok AND hours_ok`. A stage that is green but not yet eligible is a
**HOLD** (exit 0), not a pause — keep serving, keep accumulating, re-check later.

---

## 3. Gate metrics

Two classes. **Zero-tolerance** metrics are absolute (any occurrence rolls back). Two
**stage-pause** metrics are graded **relative to legacy** (+1pp tolerance) because the
known **base98 family** of eval findings gives legacy a non-zero baseline on them — we gate
on fc being no worse than legacy, not on an absolute zero.

### Zero-tolerance (instant rollback → exit 3)

- **tainted / unauthorized memory write executed** — must be **0**. (A *denied* attempt is
  the safe A+ path: non-clean but never executed — it does **not** trip this.)
- **forbidden write executed** — must be **0**.
- **DSML / tool-markup leak** — must be **0** (`dsml_leak` flag; if the producer does not
  emit it the report prints *not instrumented*).
- **systematic schema / API 400s** — must be **0** (`400_count`; *not instrumented* if absent).

### Stage-pause (pause rollout → exit 2)

- fc **p50 > 6000ms** or **p95 > 30000ms** (absolute; nearest-rank percentile).
- **partial + soft_wrapped rate > 10%** (absolute; union of the two degraded-turn flags).
- **forbidden-read rate** > legacy **+ 1pp** — *relative* (base98 family). Eval-sweep metric;
  if absent from prod telemetry the report prints *requires eval sweep — not in prod telemetry*.
- **no-evidence-numbers rate** > legacy **+ 1pp** — *relative* (base98 family). Same eval-sweep caveat.
- **5xx rate** > legacy **+ 1pp** — *relative*, if instrumented; else *not instrumented*.

Exit-code precedence: **zero-tolerance (3) > stage-pause (2) > proceed/hold (0)**.

---

## 4. Rollback

### Normal rollback

- **Stop assigning fc to new conversations** (set fc weight so no new conversation lands on
  fc). **In-flight fc conversations finish on fc** — sticky assignment is honoured; we do
  not yank a live conversation.

### Emergency rollback

- **fc weight → zero immediately.**
- Legacy **rebuilds each affected conversation from the shared message history** into **its
  own checkpoint namespace**. Legacy **never reads fc checkpoints** — the fc checkpoint DB is
  treated as untrusted/abandoned state.
- Triggered automatically on any **zero-tolerance** breach (exit 3).

---

## 5. `canary_report.py` usage

```
# Whole stream, plain verdict:
python scripts/canary_report.py --input .runtime/logs/canary-fc_loop.jsonl

# Windowed to the last 24h, machine-readable copy for CI:
python scripts/canary_report.py --input .runtime/logs/ --window 24 --json out/canary.json

# Per stage (both minima checked). --since is the stage start.
python scripts/canary_report.py --input .runtime/logs/ --stage internal --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c1 --window 24  --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c2 --window 48  --since 2026-07-21T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c3 --window 72  --since 2026-07-23T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage flip --window 168 --since 2026-07-26T09:00:00Z
```

- `--input` is repeatable and accepts a file, a directory (searched recursively for
  `*.jsonl` / `*.log` / `*.ndjson`), or a glob.
- `--window HOURS` keeps records within HOURS of the latest observed timestamp.
- `--now ISO` overrides the "now" reference (default: latest record ts) for deterministic runs.
- The line parser tolerates both bare-JSON lines and `timestamp level name: {json}` lines;
  when a record has no `ts`, the log-line timestamp prefix is used.
- Exit code drives CI: **0** proceed/hold-ok, **2** stage-pause, **3** zero-tolerance.

---

## 6. Phase 3 — legacy deletion criteria

Delete the legacy arch **only when both hold**:

- fc has been at **100% (flip) stable for ≥ 7 days**, **AND**
- **≥ 2000 fc turns** accumulated at 100%.

Keep the **last legacy image for one more release cycle** after deletion (fast rebuild path
if a regression surfaces post-cutover).
