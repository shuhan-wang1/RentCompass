# Configurable Candidate Canary Runbook

Operational runbook for evaluating and gradually exposing a configurable
candidate (`fc_loop` by default, or `manager_v1` with specialists explicitly
enabled) against the standing `legacy` rollback pool.
The offline gate evaluator is `scripts/canary_report.py`; it reads the `canary.turn`
telemetry stream and returns an exit code (0 proceed, 2 hold/stage-pause, 3 zero-tolerance).

---

## 1. Two pools

| Pool | Config | Image |
|---|---|---|
| **legacy** | prod-config (unchanged) | current prod image |
| **candidate** (`app-fc`, :5002) | default `fc_loop` / specialists off; optional `manager_v1` / specialists on | immutable `uk-rent-agent:canary-<arch>-<sha>` image |

- The candidate image is **pinned to an immutable tag**, not to a branch HEAD.
  Rebuilding the branch must not move what canary traffic runs. Cut a **new** tag to advance
  the candidate.
- **Superseded history:** an earlier draft pinned `canary/fc-loop-7db03e7`. Do **not** deploy
  it — `7db03e7` predates the canary infrastructure (conversation arch provenance,
  per-turn telemetry, and the `X-Agent-*` headers landed later, in `3d215fb`/`14312f0`), so an
  image built from it has no canary support at all. The deployable is the **current candidate
  tag** cut on the commit that includes the canary infra together with the compose/env wiring
  in this round.
- Both pools run the **same** application. `app-fc` is retained as a service name
  for deploy compatibility; it means “candidate”, not “always fc_loop”.
- Production routing starts at **0% candidate**. The only accepted public weights
  are 0, 5, 20, 50 and 100.

### Env wiring

Per-pool environment, as set in `docker-compose.yml`. `CHECKPOINT_DB_PATH` (separate per arch and specialist mode)
is the state-isolation boundary; `CONVERSATION_DB_PATH` is **the same file for both pools** so
either arch can rebuild a conversation from the shared transcript on rollback.

| Env | legacy (`app`) | candidate (`app-fc`) |
|---|---|---|
| `AGENT_ARCH` | `legacy` | `${CANARY_AGENT_ARCH:-fc_loop}` |
| `MANAGER_V1_SPECIALISTS` | off | `${CANARY_MANAGER_V1_SPECIALISTS:-0}` |
| `USE_MCP_TOOLS` | app setting | `${CANARY_USE_MCP_TOOLS:-0}` |
| `DEEPSEEK_STRICT` | (unset) | `1` |
| `APP_CANDIDATE_SHA` | `${LEGACY_APP_SHA}` (required production pin) | `${FC_CANARY_SHA}` (candidate sha) |
| `CHECKPOINT_DB_PATH` | `/app/.runtime/checkpoints.sqlite3` | `/app/.runtime/checkpoints_<arch>_specialists-<0|1>.sqlite3` |
| `CONVERSATION_DB_PATH` | `/app/.runtime/conversations.sqlite3` | `/app/.runtime/conversations.sqlite3` (**shared**) |
| `CANARY_LOG_PATH` | `/app/.runtime/logs/canary-legacy.jsonl` | `/app/.runtime/logs/canary-<arch>.jsonl` |

Safe candidate selections are exactly:

```dotenv
# Existing/default candidate
CANARY_AGENT_ARCH=fc_loop
CANARY_MANAGER_V1_SPECIALISTS=0
CANARY_USE_MCP_TOOLS=0

# Phase-2 manager candidate
CANARY_AGENT_ARCH=manager_v1
CANARY_MANAGER_V1_SPECIALISTS=1
CANARY_USE_MCP_TOOLS=0
```

`manager_v1 + specialists + MCP` is rejected by `Config` at startup. The
weight controller additionally refuses to expose manager_v1 unless `/ready`
reports `X-Agent-Arch: manager_v1` and `X-Agent-Specialists: 1`.

> **⚠ `CHECKPOINT_DB_PATH` was previously mis-documented.** Earlier notes told ops to set the
> checkpoint path via `CHECKPOINT_PATH`, but the code read a different variable — so the pin
> silently had no effect. This is now fixed: **`CHECKPOINT_DB_PATH` is the canonical knob** and
> `CHECKPOINT_PATH` is accepted only as a back-compat fallback. If both are set and differ,
> `CHECKPOINT_DB_PATH` wins (startup logs a one-line warning). Default when neither is set:
> `<root>/.runtime/checkpoints.sqlite3`.
>
> `CANARY_LOG_PATH`: unset → default `<checkpoint dir>/logs/canary-<arch>.jsonl`; `off` disables
> telemetry; any other value is used as the path verbatim.

> Before collecting a comparison window, recreate both pools from their declared pins and
> require `/ready` to report the expected arch, full source SHA, image identity and prompt
> metadata. A pool without telemetry is *not instrumented* and cannot authorize a relative gate.

### Routing, cohorts and conversation provenance

- Nginx performs real 0/5/20/50/100 splitting with `split_clients`.
- This requires the generated include at
  `/etc/nginx/snippets/rentcompass-canary-routing.conf`. A host that has not yet
  installed it still has only the legacy single-upstream 0/100 switch and **must
  not** attempt 5/20/50 staging. `setup_nginx_http.sh` or `setup_tls.sh` installs
  the committed 0% fail-safe include; verify it before the first stage.
- The cohort key is the opaque Flask `session` cookie first, an explicit
  `X-Conversation-ID` second, then remote address + user-agent as bootstrap
  fallback. Nginx hashes but does not signature-validate the cookie (cohorting is
  not an authorization boundary). Cookie-less API clients must send
  `X-Conversation-ID`; nginx never tries to parse a JSON body.
- A first request with neither cookie nor header uses the fallback. After the
  response establishes a signed session cookie, that browser can move cohort
  once; subsequent requests stay on the cookie cohort. Do not claim absolute
  first-request stickiness for anonymous clients.
- Keep `CANARY_COHORT_SALT` unchanged while advancing stages. Because candidate
  is always the first hash bucket, 5% is a subset of 20%, and 20% a subset of 50%.
- Nginx overwrites all `X-RentCompass-Rollout-*` and assigned-pool/source headers;
  client-supplied copies are never trusted. A JSON access log records request ID,
  rollout ID, assigned pool and configured weight as the external denominator.
  It also overwrites `X-Request-ID` with that same Nginx `$request_id`, so edge and
  application records share a directly reconcilable identifier.
- Direct loopback requests that do not carry the edge-injected headers are emitted
  as `traffic_source=direct`, with no rollout ID, and are excluded when the report
  selects an exact public rollout ID.
- Each conversation persists the arch/version/strict triple that last served it as
  provenance. After a pool switch, the serving process reconciles that stamp and rebuilds
  hot state from the shared durable transcript.
- Shared durable conversation history preserves continuity if a cohort assignment
  changes; process-local hot state is never the rollback source of truth.

### State isolation

- **Separate checkpoint DBs per architecture and specialist mode.** Legacy,
  fc_loop, manager_v1-off and manager_v1-on never resume each other's graph state.
- **Shared message history.** Both arches see the same user-visible message transcript, so
  a conversation reads coherently and either arch can rebuild context from it.

### Image build (immutable candidate image)

> `bash deploy/update.sh` now performs this whole section automatically for the pool the
> public route is serving — worktree checkout at the pin, immutable tag, `.env` pins,
> bring-up, and an arch+sha verification of what is actually answering. The manual
> procedure below remains the reference for building a candidate that is *not* the
> current deploy pin.

The candidate pool has **no `build:`** in compose — it runs a fixed, pre-built image, so the working
tree can never silently become what canary traffic executes. Build the image out of band from
the current candidate ref and reference it by an immutable tag:

```
# 1. Check the candidate commit out into an isolated tree (does not touch your branch):
git worktree add /tmp/candidate-<sha> <candidate-git-ref>
#    (or: git archive <candidate-git-ref> | tar -x -C /tmp/candidate-<sha>)

# 2. Build a uniquely, immutably tagged image from it:
#    Use canary-fc-loop-... or canary-manager-v1-... to match CANARY_AGENT_ARCH.
docker build -t uk-rent-agent:canary-<arch-with-hyphens>-<sha> /tmp/candidate-<sha>

# 3. Clean up the worktree when done:
git worktree remove /tmp/candidate-<sha>
```

- **Never** retag or reuse `:latest` for the candidate pool, and never rebuild the tag in place — a
  new candidate gets a **new** `<sha>` tag.
- Wire it into the compose `.env` (root-level, next to `docker-compose.yml`):

  ```
  FC_CANARY_IMAGE=uk-rent-agent:canary-<arch-with-hyphens>-<sha>
  FC_CANARY_SHA=<sha>
  ```

  Both are `:?`-required by the `app-fc` service — the candidate pool refuses to start if either is
  unset, so it can never come up on an ambiguous image or with an unpinned sha.

### Bring-up (internal stage)

The default `docker compose up -d` is **unchanged** — it brings up the legacy stack only. The
candidate pool is behind the `canary` profile:

```
# Start the internal candidate pool (loopback :5002, not routed by nginx at weight 0):
docker compose --profile canary up -d app-fc

# Verify: expect 200 plus the canary response headers.
curl -s -D- http://127.0.0.1:5002/health
#   → X-Agent-Arch: fc_loop (default) or manager_v1
#   → X-Agent-Version: <sha>          (== FC_CANARY_SHA)
#   → X-Agent-Specialists: 0 (fc_loop) or 1 (manager_v1 rollout)
```

- `/health` is served by Starlette directly (it bypasses Flask's `after_request` hook); it
  stamps the pool-identity headers itself as of the current candidate. On an image built from
  an older commit `/health` returns 200 **without** the headers — verify against a
  Flask-served path instead (`curl -s -D- -o /dev/null http://127.0.0.1:5002/`).

- Per-turn telemetry lands on the host at `./.runtime/logs/*.jsonl`
  (`canary-fc_loop.jsonl` or `canary-manager_v1.jsonl`) via the shared
  `./.runtime` bind mount.
- Report on the internal stage together with concurrent contract-valid legacy
  telemetry. If the control arm is absent, the report HOLDs; an empty control must
  never become a false-green comparison:

  ```
  python scripts/canary_report.py --input .runtime/logs/ --stage internal --since <stage-start-ISO>
  ```

---

## 2. Stage table

Each stage advances **only when BOTH minima clear** — the elapsed-time floor and
the candidate-turn-count floor. Neither alone is sufficient. Use one unique,
content-free rollout ID per stage so direct :5002 smoke traffic cannot enter the
public cohort denominator.

| Stage | Candidate traffic | Min hold | Min candidate turns |
|---|---|---|---|
| `internal` | internal only | 24h | 50 |
| `c1` | 5% | 24h | 200 |
| `c2` | 20% | 48h | 500 |
| `c3` | 50% | 72h | 1000 |
| `flip` | 100% | 7d (168h) | 2000 |

Apply a stage only after its offline evaluation gate passes:

```bash
# Examples; use a new immutable rollout id for every stage window.
sudo bash deploy/set_canary_weight.sh --weight 5 \
  --rollout-id manager-v1-20260830-c1 --stage c1 --allow-public-candidate
sudo bash deploy/set_canary_weight.sh --weight 20 \
  --rollout-id manager-v1-20260831-c2 --stage c2 --allow-public-candidate
sudo bash deploy/set_canary_weight.sh --weight 50 \
  --rollout-id manager-v1-20260902-c3 --stage c3 --allow-public-candidate

bash deploy/set_canary_weight.sh --status
```

Do not execute the 100% command from this runbook yet. At 100%, there are no live
legacy turns in the rollout window, while the comparative report deliberately
requires a non-empty control arm. It therefore HOLDs rather than treating an empty
control as a clean baseline. A flip requires a separately implemented, immutable
frozen-control artifact (or a shadow control stream) bound to the same candidate;
until that gate exists, 50% is the maximum authorized stage. The controller retains
the 100% primitive for a future gated cutover, not as evidence that flip is eligible.

The controller preflights pool identity, writes the include with a same-directory
rename, runs `nginx -t`, reloads, and probes both cohorts. Any failure restores the
previous include and reloads it.

`canary_report.py --stage <name> --since <stage-start-ISO>` reports `turns_ok`, `hours_ok`,
and `eligible = turns_ok AND hours_ok`. `--since` also **filters the records** (see §5), so
the turn count is this stage's traffic. Not-yet-eligible is a **HOLD** (exit 2): it is not
an SLO regression, but it must still stop an automated promotion.

Run production gates through the rollback-coupled wrapper:

```bash
# N is the exact count of agent-turn requests for this rollout ID in the trusted
# nginx access log, after filtering out health/static/direct traffic.
EDGE_AGENT_TURNS=200
bash deploy/run_canary_gate.sh --input .runtime/logs/ \
  --stage c1 --since <stage-start-ISO> \
  --candidate-arch manager_v1 --require-specialists \
  --rollout-id <exact-rollout-id> --rollout-stage c1 \
  --configured-weight 5 --expect-rollout-turns "$EDGE_AGENT_TURNS"
```

It forwards every argument to `canary_report.py`. Exit 3 alone invokes
`set_canary_weight.sh --weight 0`; exit 0 never promotes automatically, while
2/1/64 leave routing untouched. If rollback itself fails the wrapper exits 70
and prints `ROLLBACK_FAILED` for immediate operator escalation.

---

## 3. Gate metrics

Two classes. **Zero-tolerance** metrics are absolute (any occurrence rolls back). Two
**stage-pause** metrics are graded **relative to legacy** (+1pp tolerance) because the
known **base98 family** of eval findings gives legacy a non-zero baseline on them — we gate
on the candidate being no worse than legacy, not on an absolute zero.

### Zero-tolerance (instant rollback → exit 3)

- **tainted / unauthorized memory write executed** — must be **0**. (A *denied* attempt is
  the safe A+ path: non-clean but never executed — it does **not** trip this.)
- **forbidden write executed** — must be **0**.
- **DSML / tool-markup leak** — must be **0** (`dsml_leak` flag; if the producer does not
  emit it the report prints *not instrumented*).
- **systematic schema / API 400s** — must be **0** (`400_count`; *not instrumented* if absent).

### Stage-pause (pause rollout → exit 2)

- candidate **p50 > 6000ms** or **p95 > 30000ms** (absolute; nearest-rank percentile).
- **partial + soft_wrapped rate > 10%** (absolute; union of the two degraded-turn flags).
- **forbidden-read rate** > legacy **+ 1pp** — *relative* (base98 family). Eval-sweep metric;
  if absent from prod telemetry the report prints *requires eval sweep — not in prod telemetry*.
- **no-evidence-numbers rate** > legacy **+ 1pp** — *relative* (base98 family). Same eval-sweep caveat.
- **5xx rate** > legacy **+ 1pp** — *relative*, if instrumented; else *not instrumented*.

Exit-code precedence: **zero-tolerance (3) > hold/stage-pause (2) > proceed (0)**.

---

## 4. Rollback

### Normal rollback

- Set candidate weight to zero immediately:

  ```bash
  sudo bash deploy/set_canary_weight.sh --weight 0
  # Compatibility alias (also maps to weight 0 on weighted installs):
  sudo bash deploy/switch_pool.sh --to legacy
  ```

  Weight 0 requires only a ready pool reporting `legacy`; an old rollback image
  without a full SHA may proceed with a loud warning so missing provenance cannot
  strand traffic on a broken candidate.

### Emergency rollback

- Run the weight-0 command above. It is the emergency rollback verb.
- Legacy **rebuilds each affected conversation from the shared message history** into **its
  own checkpoint namespace**. Legacy **never reads candidate checkpoints** — the candidate checkpoint DB is
  treated as untrusted/abandoned state.
- When the report is run through `deploy/run_canary_gate.sh`, a
  **zero-tolerance** breach (exit 3) automatically invokes the weight-0 command.
  Running `canary_report.py` directly never mutates routing.

---

## 5. `canary_report.py` usage

```
# Whole stream, plain verdict (default candidate is fc_loop):
python scripts/canary_report.py --input .runtime/logs/canary-fc_loop.jsonl

# manager_v1 specialist candidate:
python scripts/canary_report.py --input .runtime/logs/canary-manager_v1.jsonl \
  --candidate-arch manager_v1 --require-specialists

# Windowed to the last 24h, machine-readable copy for CI:
python scripts/canary_report.py --input .runtime/logs/ --window 24 --json out/canary.json

# Per stage (both minima checked). --since is the stage start AND bounds the records.
python scripts/canary_report.py --input .runtime/logs/ --stage internal --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c1 --window 24  --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c2 --window 48  --since 2026-07-21T09:00:00Z
python scripts/canary_report.py --input .runtime/logs/ --stage c3 --window 72  --since 2026-07-23T09:00:00Z
# Reserved until an immutable frozen-control/shadow-control gate is implemented:
# python scripts/canary_report.py --input .runtime/logs/ --stage flip ...
```

- `--input` is repeatable and accepts a file, a directory (searched recursively for
  `*.jsonl` / `*.log` / `*.ndjson`), or a glob.
- `--window HOURS` keeps records within HOURS of the "now" reference.
- `--since ISO` **also filters**: records older than ISO are excluded from the population,
  and it supplies the stage elapsed-hours check. `--window` and `--since` are both *lower
  bounds* on a record's `ts`, so passing both applies the **later** of the two (the
  intersection — the tighter bound wins).
- **Read the window off the report, not off your shell history.** Every run prints the
  cutoff it actually applied on its `record filter :` line, and the `--expect-turns` block
  repeats the same string; the JSON carries it as `window_cutoff` / `window_filter`.

  > **Changed 2026-07-26.** Previously `--since` was parsed and then used **only** for the
  > elapsed-hours check — it filtered nothing, only `--window HOURS` did — while the anchor
  > block nevertheless printed `window = the selected --window / --since range` and so
  > claimed a bound it had not applied. Consequences for older evidence: any report produced
  > by the pre-fix tool was computed over **every record in the input**, whatever `--since`
  > said. That is why the first run of the 2026-07-25 internal round counted a pre-window
  > warm-up turn and returned INSTRUMENTATION-HOLD.
- `--now ISO` overrides the "now" reference (default: latest record ts) for deterministic runs.
- The line parser tolerates both bare-JSON lines and `timestamp level name: {json}` lines;
  when a record has no `ts`, the log-line timestamp prefix is used.
- Exit codes drive CI. Only **0 / 2 / 3** are gate verdicts:

  | code | meaning |
  |---|---|
  | **0** | proceed / stage-progress-ok |
  | **2** | hold, stage-pause, **or** instrumentation-hold |
  | **3** | zero-tolerance breach (instant rollback) |
  | **1** | input/runtime error — no `--input`, unparseable `--since`/`--now`, negative `--expect-turns` |
  | **64** | CLI usage error — unknown flag, or an option missing its argument |

  `--json` takes a **PATH**. A bare `--json` used to abort inside argparse with **exit 2**,
  which is indistinguishable from STAGE-PAUSE if only `$?` is checked; it now exits **64**,
  a code no verdict can ever return. If a driver sees 64, the command was mistyped and
  **nothing was measured** — do not treat it as a gate result.

### Rotating telemetry before a stage window

Start every internal/pre-public stage window on a **new** telemetry file, so a previous build's records
cannot land inside the window being judged.

```
# CORRECT — stop first, then move, then start.
docker compose --profile canary stop app-fc
mv .runtime/logs/canary-<arch>.jsonl .runtime/logs/canary-<arch>.<old-sha>.jsonl
docker compose --profile canary up -d app-fc
```

> **Do not `mv` the file while the pool is running.** The logger opens its sink once
> at startup and holds the fd. A rename moves the *inode*, and the fd follows it — so
> the pool keeps writing into the file you just "rotated away" and never creates the
> new one. It looks like it worked: the old path is gone and a fresh file appears to
> be waiting. It is not; the stage window then silently mixes the new build's turns
> into the archived file, and `--expect-turns` reports zero eligible records for a run
> that actually happened. Verify after rotating:
>
> ```
> docker exec uk-rent-app-fc sh -c 'ls -l /proc/1/fd | grep canary'
> ```

### External anchor for the internal 50 rounds

```
python scripts/canary_report.py --input .runtime/logs/canary-fc_loop.jsonl --expect-turns 50

# The pool was warmed with a throwaway turn (cold start costs seconds), so open the
# window AFTER it — --since excludes it from the count and from the percentiles:
python scripts/canary_report.py --input .runtime/logs/canary-fc_loop.jsonl \
    --since <ts-of-the-first-turn-of-record> --expect-turns 50
```

Counts only records that are, all at once: inside the **applied** window (the `--window` /
`--since` cutoff the report prints — see §5), the exact `--candidate-arch`,
`endpoint=alex`, one single `candidate_sha`, and v2 contract-valid. legacy turns,
`search_direct` turns, v1 records from a rotated log and malformed records are each
reported as ineligible and can never make up the count. Request IDs are reconciled
one for one, so a duplicated record cannot stand in for a turn that emitted nothing.

A mismatch is an **INSTRUMENTATION-HOLD (exit 2)** — the telemetry does not describe
the run that was driven, so every rate in the report has an unknown denominator. It
ranks *below* zero-tolerance: a run that both lost turns and committed a real breach
still exits **3**.

> Passing 50 rounds is a **functional** gate only. The `internal` stage still requires
> its **24h minimum elapsed** (see the stage table) — driving 50 turns quickly does not
> satisfy the time gate, and `--stage internal --since <start>` is what checks it.

---

## 6. Phase 3 — legacy deletion criteria

Legacy deletion is not authorized by the current gate. After frozen/shadow control
support is implemented, delete the legacy arch **only when both hold**:

- the selected candidate has been at **100% (flip) stable for ≥ 7 days**, **AND**
- **≥ 2000 candidate turns** accumulated at 100%.

Keep the **last legacy image for one more release cycle** after deletion (fast rebuild path
if a regression surfaces post-cutover).

---

## Legacy pool self-identification (added 2026-07-26, inert)

`APP_CANDIDATE_SHA` for the `app` service now reads `${LEGACY_APP_SHA:-}`. It is **wiring only
and takes effect at the next planned public rebuild** — per HANDOFF §3.10, the rollback target
is deliberately NOT rebuilt just to populate it, because rebuilding the only escape hatch right
after moving traffic onto an unproven candidate is the worst possible timing.

Until it is populated the legacy pool may answer `x-agent-version: unknown`. On a
weighted install the emergency weight-0 controller accepts that condition only for
legacy, after validating readiness, architecture and `X-Agent-Specialists: 0`:

```
sudo bash deploy/set_canary_weight.sh --weight 0
```

On a pre-weighted, single-upstream host only, the compatibility fallback still
requires `--allow-unidentified-target` for such an image. Install the weighted
include before beginning staged rollout.

**Set it to the FULL 40-character sha**, matching the `FC_CANARY_SHA` convention.
`switch_pool.sh` length-checks at 40, so a 7-char value populates the header, *looks* fixed, and
still forces the override flag.

**Deliberately `:-` and not `:?`.** The `FC_CANARY_IMAGE` / `FC_CANARY_SHA` pins use `:?`, and
because compose interpolation is whole-file and ignores profiles, **an unset `FC_CANARY_*` makes
every compose command fail — including `up -d app`, the one that restores the escape hatch**:

```
$ docker compose -f docker-compose.yml config --services      # no --profile canary
error while interpolating services.app-fc.image: required variable FC_CANARY_IMAGE is missing a value
```

Reproducing that pattern on the rollback target would have been a regression dressed as a fix.
Counting `SEARXNG_SECRET:?`, the rollback path currently has **three** hard prerequisites in the
root `.env`; making `app` independent of the `app-fc` pins (e.g. moving `app-fc` into a
canary-only override file) is the real structural fix and is not done.
