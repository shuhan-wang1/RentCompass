# Configurable Candidate Canary Runbook

Operational runbook for evaluating and gradually exposing a configurable
candidate (`fc_loop` by default, or `manager_v1` with specialists explicitly
enabled) against the standing `legacy` rollback pool.
The offline gate evaluator is `scripts/canary_report.py`; it reads the `canary.turn`
telemetry stream and returns an exit code (0 proceed, 2 hold/stage-pause, 3 zero-tolerance).

---

## 0. The next release on this host (with PR #83)

`$REPO` below is your checkout (`/…/uk_rent_recommendation`); every path is
relative to it. This section is the whole procedure for the next release on the
production box; the rest of the runbook is the rollout machinery it hands off to.

**Current state, for reference:** no weighted include is installed
(`/etc/nginx/snippets/rentcompass-canary-routing.conf` does not exist), the site
conf's sole upstream is `127.0.0.1:5002`, and the root `.env` contains **no
`CANARY_*` key at all** — so every consumer resolves the candidate identity to
`fc_loop:0:0`, which passes every whitelist.

### Before the release

1. **Leave the root `.env` alone.** The defaults are correct. If you ever do add
   the keys, `0`/`1` and the spellings `true/yes/on` / `false/no/off` are all
   accepted now (they are normalised the same way `config.py` does it); anything
   else is refused **by name** by the release, the update and the monitor.
2. **Do nothing about the checkpoint database.** See "Checkpoint database names"
   in §1: `.runtime/checkpoints_fc.sqlite3` stays exactly where it is and the
   candidate pool keeps opening it. There is no migration step.
3. `git status --porcelain -uall` must be empty. Both `release.sh` and
   `update.sh` refuse a dirty or untracked build context.

### The release

4. ```bash
   cd "$REPO" && bash deploy/release.sh
   ```
   **No flags, no environment prefix.** `CANARY_ALLOW_FLIP=1` is *not* needed:
   this release does not change what serves 100% of the public, and the preflight
   prints `routing UNCHANGED` to say so. Preview it first with
   `bash deploy/release.sh --dry-run`, which is now guaranteed not to die on a
   policy gate.
5. The default `--both --drain` order is: refresh standby `legacy` → drain public
   fc→legacy (`--stage maintenance`, marker-guarded) → rebuild and recreate
   `app-fc` → restore public onto fc. The restore leg never drives a billed turn
   and can never be blocked by an answer probe. Add `--skip-answer-probe` to
   suppress the probe on the drain leg too (it only has an effect on a weighted
   host).
6. If anything goes wrong mid-release, the emergency lever works against the
   still-old containers:
   ```bash
   sudo bash deploy/switch_pool.sh --to legacy --allow-unidentified-target
   ```

### Verify

7. `curl -sI http://127.0.0.1:5001/ready` and `:5002/ready` must both return 200
   with `X-Agent-Arch: legacy` / `fc_loop`, `X-Agent-Specialists: 0`, and the new
   full 40-char `X-Agent-Version`. `bash deploy/update.sh --status` prints the
   same in one place.
8. `.runtime/checkpoints_fc.sqlite3` must still be the file that is growing; no
   `checkpoints_fc_loop_specialists-0.sqlite3` should appear.

### After BOTH pools verify — not before

9. Reinstall the monitor. **Order matters**: the new monitor is tolerant of a pool
   that sends no `X-Agent-Specialists` header, but the rest of it expects the new
   build, and `check_install_drift.sh` is red until this runs.
   ```bash
   sudo install -m 0755 deploy/monitoring/rentcompass-monitor.sh /usr/local/bin/rentcompass-monitor.sh
   bash deploy/monitoring/check_install_drift.sh
   ```
10. `deploy/run_canary_gate.sh` now resolves the interpreter itself (this host has
    no `python` on PATH, only `python3`; the old default exited 127 before running
    the report, so the automatic zero-tolerance rollback silently could not fire).
    If you ever see exit 127 from it, pin the interpreter explicitly with
    `CANARY_GATE_PYTHON=/tmp/rentcompass-venv/bin/python`.

### Never, casually

11. Do **not** re-run `deploy/setup_tls.sh` / `deploy/setup_nginx_http.sh` without
    reading their headers. They install the weighted include (default: candidate
    weight 0) and a vhost that replaces the `upstream rentcompass_app` block —
    together that moves the public edge to the `legacy` architecture AND removes
    the line the deploy tooling reads. Both scripts now **preserve** existing
    routing and print what they kept; `--force-route-reset` is the deliberate
    reset, and it prints the exact command that puts the candidate back.
12. Do **not** run `deploy/switch_pool_rehearse.sh` from an automated context: it
    starts a real nginx and probes the real pools. The offline equivalents are
    `deploy/test_switch_pool_assertions.sh`, `deploy/test_update_assertions.sh`
    and `deploy/test_release_assertions.sh`, which touch no network at all.

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
  are 0, 5, 20, 50 and 100 — and **100 is not an operator-authorised rollout stage**:
  it is reachable only by the deploy drain (`--stage maintenance`) or by a
  deliberately gated flip (`--stage flip` with `CANARY_ALLOW_FLIP=1`). See
  "The 100% policy stop" in §2.

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

#### The checkpoint identity stamp (enforced, not conventional)

The per-pool `CHECKPOINT_DB_PATH` above is only a **naming convention**: an
override, the `CHECKPOINT_PATH` back-compat fallback, or the shared default all point
two architectures at one file, and a cross-arch resume corrupts the run. So the file
now carries its own identity. On open, the runtime stamps
`{agent_arch, manager_v1_specialists}` into a small side table
`rentcompass_runtime_identity`, and:

| File state | Behaviour |
|---|---|
| unstamped (a pre-existing database) | stamped in place on first open; **nothing is moved or migrated** |
| same identity | reopened normally |
| different identity | startup/readiness **fails** with `CheckpointIdentityError` |

```
checkpoint database belongs to a different runtime: file identity
[agent_arch=fc_loop manager_v1_specialists=0] != process identity
[agent_arch=manager_v1 manager_v1_specialists=1] at
/app/.runtime/checkpoints.sqlite3. Point CHECKPOINT_DB_PATH at this runtime's own
file (docker-compose.yml derives it from CANARY_AGENT_ARCH /
CANARY_MANAGER_V1_SPECIALISTS); never let one architecture resume another's
LangGraph checkpoints.
```

`/ready`'s `checkpoint_store` check reports the identity it opened with, and fails
closed when the stamp disagrees.

#### Checkpoint database names (read this before the next deploy)

`docker-compose.yml` DERIVES the candidate pool's file from the identity:

```
CHECKPOINT_DB_PATH: /app/.runtime/checkpoints_${CANARY_AGENT_ARCH:-fc_loop}_specialists-${CANARY_MANAGER_V1_SPECIALISTS:-0}.sqlite3
```

For the identity this box has been running since 2026-07-20 that derivation names
a file **that does not exist**: the live database is `.runtime/checkpoints_fc.sqlite3`
(78 MB, WAL active). Left alone, the next deploy would have opened an empty
database beside it — losing every in-flight graph/HITL resume, and putting the
personal graph state in the old file permanently out of reach of the
account-erasure route, which deletes only from the resolved checkpoint path while
still reporting `deleted`.

So the derived name is an **alias** of the file that identity already owns:

| Identity (`CANARY_AGENT_ARCH` : `CANARY_MANAGER_V1_SPECIALISTS`) | Pool | File under `.runtime/` |
|---|---|---|
| `legacy` : `0` | `app` (:5001) | `checkpoints.sqlite3` (set explicitly in compose) |
| `fc_loop` : `0` | `app-fc` (:5002) | **`checkpoints_fc.sqlite3`** — the derived `checkpoints_fc_loop_specialists-0.sqlite3` resolves to it |
| `manager_v1` : `1` | `app-fc` (:5002) | `checkpoints_manager_v1_specialists-1.sqlite3` (new file, created on first open) |

**There is no one-time migration step and no rename to perform.** `mv`-ing
`checkpoints_fc.sqlite3` to the derived name is *also* correct (a host already
running the derived name keeps it), but it is not required and it is not the
recommended path — leave the file where it is.

Two guards back the mapping up, because a name is only a convention:

- if BOTH files exist for one identity, startup **refuses** rather than picking
  one and orphaning the other (this can only happen on a host that ran the derived
  name before the alias existed: stop the pool, keep the database you want, remove
  or rename the other together with its `-wal`/`-shm`, restart);
- if the resolved path does **not** exist while a database this identity already
  owns sits beside it, startup refuses with `OrphanedCheckpointError`, naming the
  file it expected and the file it found. Set `CHECKPOINT_ALLOW_NEW_DB=1` to accept
  a new empty database deliberately — the old one is **not** migrated and is **not**
  reachable by account erasure.

**Write `CANARY_MANAGER_V1_SPECIALISTS` as `0` or `1`.** Compose interpolation cannot
normalise, so `true` used to interpolate into a *third* database
(`checkpoints_manager_v1_specialists-true.sqlite3`) sharing no state with the
`...-1.sqlite3` the same pool used yesterday. The token is now normalised back to
`0`/`1` when the path is resolved (with a startup warning), and a spelling that is
neither true nor false is refused rather than forking a fourth file.

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

| Stage | Candidate traffic | Min hold | Min candidate turns | Authorised? |
|---|---|---|---|---|
| `internal` | internal only | 24h | 50 | yes |
| `c1` | 5% | 24h | 200 | yes |
| `c2` | 20% | 48h | 500 | yes |
| `c3` | 50% | 72h | 1000 | **yes — highest authorised stage** |
| `flip` | 100% | 7d (168h) | 2000 | **NO** — blocked until a frozen/shadow control gate exists; the controller refuses it without `CANARY_ALLOW_FLIP=1` |
| `maintenance` | 100% (temporary) | n/a | n/a | machine-only, and mechanically so: it needs the deploy lock, a `deploy-maintenance-<sha>` rollout id AND an active-drain marker `deploy/update.sh` owns |

`c3` (50%) is the highest stage an operator may advance to. `flip` and
`maintenance` are the only two stages the controller will accept at weight 100,
and they are described in "The 100% policy stop" below.

> **THE CANDIDATE SLOT IS EXCLUSIVE — read this before starting a `manager_v1`
> canary.** This host has exactly two pool slots. `docker-compose.yml` hardcodes
> `app` (:5001) to `AGENT_ARCH: "legacy"`, and `app-fc` (:5002) is the single
> candidate slot. Since the 07-27 cutover, :5002 runs `fc_loop` and serves **100%
> of public traffic**.
>
> Starting a `manager_v1` canary therefore does not add a third arm — it
> **converts the :5002 pool away from `fc_loop`**, removing today's production
> architecture from the box entirely. Consequences, all of them deliberate
> choices you are making by setting `CANARY_AGENT_ARCH=manager_v1`:
>
> - the comparison arm at every stage is `legacy`, not `fc_loop`;
> - `--weight 0`, `switch_pool.sh --to legacy` and the automatic zero-tolerance
>   rollback all land the public on **`legacy`**, not on `fc_loop`;
> - getting back to `fc_loop` is a new release (set `CANARY_AGENT_ARCH=fc_loop`
>   and re-run `deploy/release.sh`), not a rollback.
>
> `deploy/release.sh` and `deploy/switch_pool.sh` both print this as a preflight
> WARNING whenever the configured candidate arch differs from the arch the slot
> runs today (read from `FC_CANARY_IMAGE`'s tag in the root `.env`). The state
> isolation table above describes which architectures never SHARE state — not
> which of them can be live at the same time. Only two can.

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

### The 100% policy stop

Do not execute a 100% command from this runbook yet. At 100%, there are no live
legacy turns in the rollout window, while the comparative report deliberately
requires a non-empty control arm. It therefore HOLDs rather than treating an empty
control as a clean baseline. A flip requires a separately implemented, immutable
frozen-control artifact (or a shadow control stream) bound to the same candidate;
until that gate exists, **50% is the maximum authorized stage**.

That is now enforced, not only documented. `deploy/set_canary_weight.sh` accepts
weight 100 in exactly two cases:

| How you reach 100 | Accepted when | Who calls it |
|---|---|---|
| `--stage maintenance` | **all three**: `RENTCOMPASS_DEPLOY_LOCK_HELD=1`, `--rollout-id deploy-maintenance-<sha>`, and an active-drain marker naming that same id (plus, on a public route, `--allow-public-candidate`) | `deploy/update.sh` while it redeploys the other pool — nothing else can satisfy all three |
| `--stage flip` | only when the environment carries `CANARY_ALLOW_FLIP=1` | a human performing the gated cutover |

Anything else — including a bare `--weight 100`, which defaults to `--stage flip` —
is refused before the route file is touched, with a one-line reason pointing here.
`deploy/switch_pool.sh --to fc` applies the identical rule on a pre-weighted
single-upstream host, so migrating a host between routing modes cannot change what
a release is allowed to do.

**`maintenance` is a window, not a stage you may enter.** It used to be accepted
unconditionally: `--stage maintenance --rollout-id anything-you-like` put the
candidate on 100% of public traffic with no `CANARY_ALLOW_FLIP`, no TTL and no
record that a restore was owed — a permanent cutover through the one door the flip
gate did not cover. The three and-gates above make it what it claims to be:

- the **deploy lock** must be held, which only `release.sh` / `update.sh` /
  `switch_pool.sh` do while they are actually running;
- the **rollout id** must be `deploy-maintenance-<sha>` (`update.sh`'s own shape),
  so the drain's turns are always filterable out of a stage window;
- an **active-drain marker** must exist and name that exact rollout id.
  `update.sh` writes it (beside the deploy lock, in the shared git metadata
  directory) immediately before the drain and deletes it after the restore, so the
  authorisation expires with the deploy that opened it. A marker left behind by a
  `SIGKILL`ed run is cleared by the next `update.sh` once it holds the lock.

There is no operator procedure that involves typing `--stage maintenance`. A
deliberate 100% is `--stage flip` with `CANARY_ALLOW_FLIP=1`.

```bash
# Refused (this is the shape a routine release used to be able to reach):
sudo bash deploy/set_canary_weight.sh --weight 100 --rollout-id r --allow-public-candidate
#   FAIL refusing candidate weight 100 at stage 'flip' without CANARY_ALLOW_FLIP=1;
#        50% is the highest authorised rollout stage (docs/canary_runbook.md section 2)

# The gated cutover, once the frozen/shadow control gate exists:
sudo CANARY_ALLOW_FLIP=1 bash deploy/set_canary_weight.sh --weight 100 \
  --rollout-id manager-v1-20260910-flip --stage flip --allow-public-candidate
```

#### The `maintenance` stage and its automatic restore

`bash deploy/release.sh` runs `deploy/update.sh --both --drain`, which moves public
traffic onto the standby pool while it recreates the other one. When the standby is
the candidate, that drain IS 100% candidate exposure — legitimate only because it is
temporary. So the drain:

1. reads the pre-drain `weight`, `rollout-id` and `stage` out of the generated
   routing include's own `# rentcompass-*` markers (that file is the rollout state;
   there is no second state store);
2. switches with `--stage maintenance` and its own
   `--rollout-id deploy-maintenance-<short-sha>`, so the drain's turns are always
   **identifiable** — see the warning below about what that does and does not do
   for your report window;
3. restores those recorded values when the pool update finishes, when it **fails**,
   and when it is **interrupted** (SIGINT/SIGTERM/SIGHUP), via an EXIT trap. A
   restore back onto a pool that was already at 100% carries `CANARY_ALLOW_FLIP=1`
   scoped to that single call — restoring an exposure the operator had already
   authorised is not a new flip decision.

> **The drain's turns are NOT excluded from a `--since`-only window.**
> `scripts/canary_report.py` has no notion of `maintenance` (grep it: zero hits).
> Filtering by rollout happens only when you pass `--rollout-id`. So a drain that
> overlaps your stage window puts 100%-candidate, zero-control turns *inside* it
> unless you name the rollout id you are measuring:
>
> ```bash
> # RIGHT: the window is this stage's traffic, drains excluded by construction
> python scripts/canary_report.py --input .runtime/logs/ --stage c1 \
>   --since <stage-start-ISO> --rollout-id manager-v1-20260830-c1
>
> # WRONG during/after a release: --since alone also counts the drain's turns
> python scripts/canary_report.py --input .runtime/logs/ --stage c1 --since <ISO>
> ```

**What is restored, and what is not.** A recorded pre-drain state of 100% at stage
`maintenance` is *not* an authorised exposure — it is the debris of an earlier drain
that never unwound. `update.sh` refuses to replay it (that would launder a temporary
stage into a permanent cutover), leaves public traffic on the drain target, which is
the legacy rollback pool, and prints the deliberate `--stage flip` command instead.

If the restore itself fails, the run says so explicitly and names the command to fix
it by hand; it never exits quietly with production parked on the drain target. An
interrupted run says `INTERRUPTED by signal N ... The redeploy did NOT complete` —
before this it reported the interrupt as `REDEPLOY SUCCEEDED`, demanded the new pin
from a pool it had never rebuilt, and left production on the drain target.

#### What `deploy/release.sh` gates, and what it does not

`deploy/release.sh` prints a preflight showing the resolved candidate identity and
the exposure the run will END on:

```
    candidate  arch=fc_loop specialists=0 mcp=0   (root .env: $REPO/.env)
    ends at    SINGLE-UPSTREAM mode, sole upstream 127.0.0.1:5002 = candidate = candidate weight 100%   ($SITE_CONF)
    routing    UNCHANGED — fc_loop is already serving 100% of public traffic; this release rebuilds it in place.
               No cutover happens, so no CANARY_ALLOW_FLIP is needed (R3-H3).
```

**The gate fires on a CHANGE in exposure, never on a rebuild.** It refuses the run
only when an architecture that is **not** at 100% when the run starts would end
there. Concretely:

| Situation | Gated? |
|---|---|
| this box today: `fc_loop` is the sole upstream, and stays the sole upstream | **no** — routine, no flag |
| weighted host at 0 / 5 / 20 / 50, staying there | **no** |
| weighted host recorded at `100 @ flip`, same candidate arch | **no** — already authorised, routing unchanged |
| weighted host recorded at `100 @ maintenance` | **no** — that is an unfinished drain; `update.sh` refuses to replay it and the run ENDS on `legacy` (a decrease, and the preflight says so) |
| `CANARY_AGENT_ARCH` names an arch the candidate slot does **not** run today, and the candidate ends at 100% | **YES** — needs `CANARY_ALLOW_FLIP=1` |

The first version of this gate compared only the END state, so on this box — where
`fc_loop` on :5002 has been production since 07-27 — it refused **every routine
release** and had to be worked around with `CANARY_ALLOW_FLIP=1` on every run. That
is precisely the habit that disarms the gate for the real `manager_v1` cutover
later, so it was a safety regression dressed as a safety feature.

**`--dry-run` never dies on a policy decision.** It prints what would happen and
names the flag it would need:

```
!!  --dry-run: this release WOULD BE REFUSED. ... 100% currently runs 'fc_loop' ...
!!  --dry-run: the flag it needs is CANARY_ALLOW_FLIP=1
```

The refusal message never tells you to point the public route at the `legacy`
architecture. On this host that is a production **downgrade**, not a remedy — and
the command the old message printed (`switch_pool.sh --to legacy`) was itself
broken by the specialist-header defect below.

**On a host with no weighted include** the snippet is untracked and simply not
installed, and the preflight resolves the end state from the single
`server 127.0.0.1:PORT;` line instead. Until 2026-08-31 that case was worse than
ungated: reading the absent snippet's markers under `set -euo pipefail` exited 2,
`errexit` aborted the whole script, and `bash deploy/release.sh` printed **nothing**
past `Release plan`. Both that crash and the gap in the gate are fixed; if neither
routing file is readable the run says `upstream UNKNOWN`, warns, and lets
`update.sh` (which derives its target from the same line and refuses to guess) make
the call.

#### `X-Agent-Specialists`: absent means 0 unless 1 is expected

The header does not exist in `origin/main`, so **neither container running on this
box emits it**. Every consumer — `switch_pool.sh`, `update.sh`,
`set_canary_weight.sh`, `probe_pool_answer.py` and the monitor — therefore requires
it only when the EXPECTED identity has `specialists=1`; where 0 is expected, an
absent header counts as 0 and the exemption is printed rather than being silent.

This matters most where it is least visible: for a short window a release has the
NEW scripts on disk while both containers are still the OLD image. Demanding the
header there turned `sudo bash deploy/switch_pool.sh --to legacy` — the documented
emergency rollback — and the drain leg of `deploy/update.sh --pool fc` into hard
refusals, exactly when they are needed. A candidate expected to run
`specialists=1` must still state its bit; there is no exemption for that.

The controller preflights pool identity, writes the include with a same-directory
rename, runs `nginx -t`, reloads, and probes both cohorts. Any failure restores the
previous include and reloads it.

### The answer probe (identity is not the same as working)

`/ready` proves a pool's identity and its dependency wiring. It does **not** prove
the pool can answer: `_check_llm_configuration` only asserts the provider credential
is non-empty and reports `connectivity: "not_probed"`. That is exactly the hole the
2026-07-25 incident fell through — a stale `DEEPSEEK_MODEL` in `app/.env` broke both
pools for a day while `/ready` stayed green. A greeting cannot detect it either: the
guard node answers greetings, rent conversions and statutory-money questions
deterministically, with zero model calls.

So before **any** weight > 0, the controller drives one real turn through
`POST /api/alex` against each pool with `deploy/probe_pool_answer.py` and requires:

- HTTP 200 with `X-Agent-Outcome: ok`;
- `X-Agent-Arch` / `X-Agent-Specialists` matching the expected identity;
- an answer that is **not** one of the canned/fallback renderers (time-budget and
  no-reliable-numbers fallbacks, the critic's generic template, the DSML boundary
  fallback, the greeting fast path, the endpoint crash strings);
- the grounding substring the query's tool must produce — the default query is
  benchmark case D1, `Is Peckham (SE15 5DP) safe to live in?`, whose grounded answer
  must cite `data.police.uk`.

```bash
# Run it by hand against a pool before a stage:
python3 deploy/probe_pool_answer.py --url http://127.0.0.1:5002 \
  --expect-arch manager_v1 --expect-specialists 1

# With the telemetry sink mounted, also require that the turn was OBSERVED:
python3 deploy/probe_pool_answer.py --url http://127.0.0.1:5002 \
  --canary-log .runtime/logs/canary-manager_v1.jsonl
#   fails when the record for this request_id says llm_usage_status=not_instrumented
```

`--skip-answer-probe` on `set_canary_weight.sh` opts out; it prints
`WARNING: ... was NOT proven able to answer a turn`. Injection points:
`CANARY_ANSWER_PROBE_CMD`, `CANARY_LEGACY_ANSWER_URL`, `CANARY_CANDIDATE_ANSWER_URL`,
`CANARY_ANSWER_PROBE_TIMEOUT`. An injected probe prints
`WARNING: the answer probe is INJECTED via CANARY_ANSWER_PROBE_CMD=...` on every
use — `CANARY_ANSWER_PROBE_CMD=true` would otherwise turn the gate off in total
silence — and an override set to the empty string is refused outright.

**When the probe does NOT run** (both are deliberate, and both are the same rule —
never make reducing exposure harder than increasing it):

- **weight 0**, the emergency rollback: it must not wait on a model call;
- **any DECREASE** (new weight < the weight currently in the include, e.g. 50 → 5):
  a candidate that cannot answer is the reason you are de-escalating. Probing it
  first would fail and leave the *higher* weight in place, making weight 0 the only
  reachable move. The pool's `/ready` identity is still verified, because it keeps
  serving the smaller cohort. The line to look for is
  `WARNING: lowering candidate exposure 50% -> 5%: the answer probe is SKIPPED`.

**A clarification is INCONCLUSIVE, not a failure.** `app/app.py` returns
`response_type: "clarification"` when the graph asked a follow-up question. That
reply can never carry the tool grounding, and it can be produced by the
deterministic criteria gate with no model call, so it proves nothing either way:
the probe exits **2**, and `set_canary_weight.sh` retries once before failing.

**Cost and side effects — the probe drives REAL turns against production.** Each
weight increase runs it against both pools, so per weight change expect:

- **2 real `POST /api/alex` turns** (4 if one pool clarifies and is retried), each
  paying for real model and tool calls;
- **2 new anonymous users + 2 conversations** in the shared
  `conversations.sqlite3`: `resolve_identity` mints an anonymous `user_id` when
  there is no cookie, and `start_request_turn` then creates the conversation/turn;
- **2 canary records with `traffic_source != edge`**, which the cost report treats
  as unattributable;
- up to `2 × CANARY_ANSWER_PROBE_TIMEOUT` (default 120s, run serially) of added
  wall-clock on every weight change.

That is the price of not repeating 2026-07-25. It is not a reason to make
`--skip-answer-probe` routine, but it is why weight 0 and de-escalations skip it.

### Where `switch_pool.sh` gets the candidate identity

`deploy/switch_pool.sh` reads **`CANARY_AGENT_ARCH` / `CANARY_MANAGER_V1_SPECIALISTS`
from the root `.env`** — the same pair `update.sh`, `set_canary_weight.sh` and
`deploy/monitoring/rentcompass-monitor.sh` read. It previously read only
`SWITCH_CANDIDATE_ARCH` / `SWITCH_CANDIDATE_SPECIALISTS`, which nothing else sets, so
on a host whose `.env` selected `manager_v1` a `switch_pool.sh --to fc` failed on an
arch mismatch against a pool that was in fact correct. `SWITCH_CANDIDATE_*` remain as
an **explicit override** for rehearsals; the accepted pairs are still exactly
`fc_loop`/specialists=0 and `manager_v1`/specialists=1, and boolean spellings
(`true`/`yes`/`on`) normalise to `1`.

**One route file, two variable names.** `update.sh` READS the pre-drain markers from
`UPDATE_ROUTE_CONF`; `switch_pool.sh` WRITES through `SWITCH_ROUTE_CONF` (which it
passes on as `CANARY_ROUTE_CONF`). Their production defaults are the same path, so
this never diverged in production — but a rehearsal that set only one of them
recorded its state from one file and edited another. `update.sh` now passes
`SWITCH_ROUTE_CONF=$ROUTE_CONF` explicitly on both the drain and the restore, so the
file it read is the file that gets written. Override both, or neither.

Boolean spellings normalise in the checkpoint filename too, with one exception: if
`checkpoints_<arch>_specialists-true.sqlite3` **already exists**, it is used as is
and the canonical name is only reported. Silently switching such a host to
`..._specialists-1.sqlite3` would orphan that pool's live checkpoints, and nothing
migrates them. To adopt the canonical name, stop the pool and `mv` the file.

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

**Exit 69 = `CANARY_GATE_UNRUNNABLE`: no verdict was produced and no rollback was
attempted.** The wrapper resolves its own interpreter — `CANARY_GATE_PYTHON` → `PYTHON`
→ `$REPO/.venv/bin/python` → `$REPO/venv/bin/python` → `python3` → `python`, each tested
with `command -v` — and exits 69 (sysexits `EX_UNAVAILABLE`) if none exists and no
`CANARY_GATE_REPORT_CMD` is set. 69 is deliberately outside the verdict set `{0, 2, 3}`
and distinct from 127, so a driver that branches on "not 0" cannot read "the gate could
not start" as "the gate held". Until 2026-09-01 the default was a bare `python`, which
does not exist on this host: every documented invocation exited **127** without running
the report, so the automatic zero-tolerance rollback silently could not fire.

**Exit 70 may just be a deploy in flight.** The weight controller takes the deploy
lock with `flock -n`, and a `release.sh` drain holds it for minutes, so an automatic
rollback that lands inside one dies with `another release/update/switch/retirement
operation is running` → 70. Before escalating, check whether a deploy is running;
then run `sudo bash deploy/set_canary_weight.sh --weight 0` by hand.

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
- **specialist non-delivery rate > 5%** — `manager_v1` candidates only.
  `specialist.non_success_rate = (failed + skipped) / planned`, against the same
  `SPECIALIST_FAILURE_RATE_LIMIT` knob as the failed rate (one threshold, not two).
  `partial` is **not** non-delivery: it answered with a stated gap. This replaces the
  failed rate as the gating specialist metric — the failed rate is still printed but no
  longer decides anything on its own, because a runtime whose every dispatch is refused
  reports `failed = 0`.

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
  strand traffic on a broken candidate. Weight 0 also **skips the answer probe**:
  rollback must never wait on a model call.

- Partial de-escalation (50% → 5%) is equally always available: any weight
  **decrease** skips the answer probe for the same reason. Going straight to 0 is
  still the emergency verb; stepping down is for a candidate that is degraded
  rather than dead.

### Emergency rollback

- Run the weight-0 command above. It is the emergency rollback verb.
- On a **single-upstream** host (this box today) the verb is
  `sudo bash deploy/switch_pool.sh --to legacy --allow-unidentified-target`. The
  `--allow-unidentified-target` flag is still needed while the deployed legacy pool
  cannot name its commit; it relaxes the SHA check only. It works against a pool
  that sends no `X-Agent-Specialists` header (see above) — that exemption is what
  keeps this lever usable during a release.
- The last-resort manual path, if the lever itself is broken: edit the
  `server 127.0.0.1:5002;` line in `/etc/nginx/sites-available/rentcompass.co.uk.conf`
  to `5001`, then `sudo nginx -t && sudo systemctl reload nginx`.
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

- `--input` is repeatable and accepts a file, a directory (searched recursively), or a
  glob. **The `*.jsonl` / `*.log` / `*.ndjson` suffix allowlist
  (`canary_report.LOG_SUFFIXES`) applies to a glob as well as to a directory walk**, so a
  `canary-legacy.jsonl.bak-<date>` sitting beside the live log cannot double the record
  population (duplicate `request_id`s, and a re-introduced mixed-schema HOLD). A file named
  **explicitly** is still read whatever its suffix — that is an operator stating an intent.
  The reader is a seatbelt, not a filing system: keep canary-log backups **outside**
  `.runtime/logs/`.
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

### `--require-specialists` requires a specialist runtime that actually ran

It used to assert only that a plan was made (`not specialist.get("planned")`). Because
`agent_loop::_note_specialist_terminal_once` rewrites the terminal of any task that never
started to `skipped`, a candidate whose every dispatch was refused reported
`planned=180, started=0, skipped=180, failed=0` — and reached **PROCEED, exit 0**, with a
printed `specialist failed rate 0.00%`. The flag now asserts three separately-reported
things, and names which one failed:

1. at least one task **planned**;
2. at least one task **started**;
3. at least one task **completed or partial** — something was actually delivered.

New report rows sit beside `specialist planned`: `specialist started`,
`specialist skipped`, `specialist non-delivery` (the §3 stage-pause metric) and
`specialist skip codes`, a `{code: count}` breakdown walked out of the lifecycle events.
Every `skipped` terminal carries an `error_code` from
`turn_observations.SPECIALIST_ERROR_CODES` (`budget_exhausted` when the turn or soft
budget stopped it, `dispatch_denied` otherwise); an event with no code buckets as
`unspecified` and still counts, so an older record still contributes.

### A crashed, timed-out or cancelled turn is UNMEASURED, not broken instrumentation

Every canary record now carries `llm_observer_installed`: whether the LangChain
callback observer was attached while that turn ran. When a turn ends in an unobservable
outcome (`crash` or `server_error`; graph timeouts and client cancellations are recorded
as `crash`) **and** the observer was installed, the record reports
`llm_usage_status: partial` — its token spend is unknown, so it is counted as
chargeable-and-unmeasured by `canary_cost` and is never priced at zero — but it is **not**
a telemetry-contract violation and does not by itself produce an INSTRUMENTATION-HOLD. The
crash still counts against the 5xx/outcome rates it always counted against, and the report
prints `unmeasured spend turns` with an `of which unobservable` sub-row so the size of
that population stays visible.

When the observer was **not** installed, the record is a violation and the window holds —
whatever the outcome. That is the 2026-07-25 class (a stale `DEEPSEEK_MODEL` produced
clean-looking zero-call telemetry from an unwatched process for a day), and it is the
defect the instrumentation gate exists to catch. A record written by a producer that
predates the flag has no flag, gets no exemption, and keeps the stricter reading.

> Rule of thumb for an operator reading a HOLD: `unmeasured spend turns` high and
> `of which unobservable` **equally high** → the candidate is crashing, look at the
> 5xx/outcome rates. `of which unobservable` **low** while unmeasured is high → the
> observer is not wired, fix the instrumentation before trusting any number in the report.

The producer half is what stops a positive false zero: `unknown_turn_signals` no longer
copies `no_llm_calls` through on an unobservable outcome, so a turn killed with a provider
call in flight can never assert a measured zero spend. `search_direct` keeps its provable
zero — that endpoint makes no call. A **cancelled** turn (client disconnect) is now
recorded rather than vanishing from the denominator: `turn_outcome: crash`,
`http_status: 499`, so a wave of disconnects counts toward `--expect-turns` without being
charged to the 5xx rate.

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
`endpoint=alex`, one single `candidate_sha`, and **contract-valid for that record's own
`telemetry_schema_version`** (`validate_record` picks the rules by version; the printed
ineligibility reason still reads `record violates the v2 contract`, a stale string in
`scripts/canary_report.py`). legacy turns, `search_direct` turns, v1 records from a
rotated log and malformed records are each reported as ineligible and can never make up
the count. Request IDs are reconciled
one for one, so a duplicated record cannot stand in for a turn that emitted nothing.

A mismatch is an **INSTRUMENTATION-HOLD (exit 2)** — the telemetry does not describe
the run that was driven, so every rate in the report has an unknown denominator. It
ranks *below* zero-tolerance: a run that both lost turns and committed a real breach
still exits **3**.

### Mixed `telemetry_schema_version` in one window is also an INSTRUMENTATION-HOLD

`canary_report.py` refuses a window whose records mix
`telemetry_schema_version` **2** and **3**: the two contracts are not comparable, so
any rate computed across both has an undefined denominator.

**This will fire on the day you roll a pool forward.** While the legacy pool still
runs a v2 image and the candidate already emits v3, every window spanning the cutover
HOLDs immediately. That is the check working, not a regression in the candidate.

The correct handling is to **move `--since` to after BOTH pools finished deploying**,
so the window contains a single schema:

```bash
# WRONG — the window straddles the deploy and mixes v2 with v3 records:
bash deploy/run_canary_gate.sh --input .runtime/logs/ --stage c1 --since <before-the-deploy>

# RIGHT — open the stage window after `deploy/update.sh --both` reported both pools live:
bash deploy/run_canary_gate.sh --input .runtime/logs/ --stage c1 --since <both-pools-live-ISO>
```

Do **not** relax the check or hand-edit the logs to make the window pass. The stage
hold clocks (§2) run from the window you open, so a window opened after the deploy is
also the only one whose elapsed-hours figure means anything.

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
