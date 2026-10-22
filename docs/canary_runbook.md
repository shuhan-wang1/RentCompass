# Canary rollout tooling

How the scripts in `deploy/` and `scripts/` roll a candidate agent architecture out next
to a stable control service and roll it back. It describes the mechanism; every host,
domain and path is a placeholder you supply. `$REPO` is your checkout.

The offline gate is `scripts/canary_report.py`: it reads the per-turn `canary.turn`
telemetry and returns an exit code (0 proceed, 2 hold, 3 zero-tolerance breach on a valid
measurement). §5 has the contract.

---

## 0. The release procedure

0.1. **Pin.** `deploy/update.sh` builds only the commit named by `DEPLOY_PINNED_SHA` in a
     root-owned pin file outside the repository (see `deploy/monitoring/README.md` §1), so
     no commit can change what a host runs.

0.2. **Clean tree.** `git status --porcelain -uall` must be empty; `release.sh` and
     `update.sh` refuse a dirty or untracked build context.

0.3. **Release.**

     ```bash
     bash deploy/release.sh --dry-run    # print the plan, change nothing
     bash deploy/release.sh              # remote mainline tip → CI check → confirm → re-pin → deploy
     bash deploy/release.sh --ref <sha>  # a specific commit (rollback, hotfix)
     ```

     `release.sh` targets the remote mainline (never a local commit), requires every
     declared CI check to have passed, shows `old-pin → new-pin`, asks for confirmation,
     and hands off to `update.sh`.

0.4. **Drain order.** The default `update.sh --both --drain` refreshes the standby
     service, moves public traffic onto it (stage `maintenance`, §2), rebuilds the other
     service, and restores the recorded routing — on success, on failure and on
     interruption (EXIT trap).

0.5. **Exposure gate.** The preflight refuses a run only when an architecture that is not
     at 100% when the run starts would end there; that cutover needs `CANARY_ALLOW_FLIP=1`.
     Rebuilding the architecture that already serves 100% is routine and needs no flag.

0.6. **Emergency lever.** `sudo bash deploy/switch_pool.sh --to legacy` (or
     `set_canary_weight.sh --weight 0` on a weighted install) works against the running
     containers at any point. `--allow-unidentified-target` relaxes the target's
     full-SHA provenance check; use it only when a target image genuinely cannot state its
     commit, and never on a forward switch (`set_canary_weight.sh` restricts it to
     weight 0).

0.7. **Verify.** `bash deploy/update.sh --status` prints the pin and both services. Each
     service's `/ready` must answer 200 with the expected `X-Agent-Arch`,
     `X-Agent-Specialists` and full 40-character `X-Agent-Version`.

0.8. **Monitor.** Neither script reinstalls the health monitor. After both services
     verify, reinstall it and run `bash deploy/monitoring/check_install_drift.sh`
     (`deploy/monitoring/README.md` §2a).

0.9. **Rehearsals.** `deploy/switch_pool_rehearse.sh` starts a real nginx and probes the
     real services, so it is not for automated contexts. The offline exercisers
     (`deploy/test_*_assertions.sh`, `deploy/monitoring/test_monitor_assertions.sh`) open
     no sockets.

0.10. **Gate interpreter.** `deploy/run_canary_gate.sh` resolves its interpreter in the
      order `CANARY_GATE_PYTHON` → `PYTHON` → `$REPO/.venv/bin/python` →
      `$REPO/venv/bin/python` → `python3` → `python`. If none exists it prints
      `CANARY_GATE_UNRUNNABLE` and exits 69, outside the verdict set. Set
      `CANARY_GATE_PYTHON` to use a virtualenv elsewhere; `canary_report.py` is
      stdlib-only.

**Testing a service directly over loopback.** The session cookie is issued `Secure`, so a
plain-HTTP client drops it and every turn starts a new user and conversation; relay the
cookie by hand. Label test traffic with an explicit `X-Request-Id`
(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$`, otherwise it is replaced by a digest) so its
telemetry can be excluded; a repeated id replays the stored response.

---

## 1. Two services

| Service | Role | `AGENT_ARCH` | Image |
|---|---|---|---|
| `app` | control and rollback target | `legacy` | built from the tree |
| `app-fc` (compose profile `canary`) | candidate | `${CANARY_AGENT_ARCH:-fc_loop}` | immutable pre-built tag `uk-rent-agent:canary-<arch>-<sha>` |

The name `app-fc` and the `FC_*` variables are historical: they mean "candidate", not
"always `fc_loop`". Both services publish on loopback ports set in `docker-compose.yml`;
only the edge proxy is public.

### Env wiring

| Env | control (`app`) | candidate (`app-fc`) |
|---|---|---|
| `AGENT_ARCH` | `legacy` | `${CANARY_AGENT_ARCH:-fc_loop}` |
| `MANAGER_V1_SPECIALISTS` | off | `${CANARY_MANAGER_V1_SPECIALISTS:-0}` |
| `USE_MCP_TOOLS` | app setting | `${CANARY_USE_MCP_TOOLS:-0}` |
| `DEEPSEEK_STRICT` | unset | `1` |
| `APP_CANDIDATE_SHA` | `${LEGACY_APP_SHA:-}` | `${FC_CANARY_SHA:?}` |
| `CHECKPOINT_DB_PATH` | `checkpoints.sqlite3` | derived from the identity (below) |
| `CONVERSATION_DB_PATH` | `conversations.sqlite3` | `conversations.sqlite3` (**shared**) |
| `CANARY_LOG_PATH` | `logs/canary-legacy.jsonl` | `logs/canary-<arch>.jsonl` |

The accepted candidate identities are exactly `fc_loop` / specialists `0` / MCP `0` and
`manager_v1` / specialists `1` / MCP `0`. Write the specialist flag as `0` or `1`;
`manager_v1` + specialists + MCP is refused at config load.

The two compose substitution operators differ on purpose: candidate pins are `:?` (no
candidate without an explicit pin), the control pin is `:-` so a missing value can never
stop the rollback target from starting.

### Routing, cohorts and provenance

- `deploy/set_canary_weight.sh` generates an nginx include that splits public traffic
  0/5/20/50/100 with `split_clients`; a committed weight-0 template lives in
  `deploy/nginx/`. A host without the include has a single upstream, switched by
  `deploy/switch_pool.sh`.
- The cohort key is the session cookie, then `X-Conversation-ID`, then remote address +
  user agent. Keep `CANARY_COHORT_SALT` unchanged while advancing so each stage is a
  superset of the last.
- The edge overwrites rollout and request-id headers, so client copies are never trusted.
  Requests that bypass the edge are recorded as `traffic_source=direct` and drop out of
  a report filtered by rollout id.
- A conversation keeps the architecture that first served it; changing the weight only
  affects new conversations. Message history is shared, so either service can rebuild a
  conversation after a switch.

### State isolation

Checkpoints are separate per architecture and specialist mode; the transcript is shared.

#### The checkpoint identity stamp

On open the runtime stamps `{agent_arch, manager_v1_specialists}` into a side table
`rentcompass_runtime_identity`. An unstamped database is stamped in place; a matching one
opens normally; a different identity fails startup and `/ready` with
`CheckpointIdentityError`.

#### Checkpoint database names

`docker-compose.yml` derives the candidate's file from the identity:

```text
checkpoints_${CANARY_AGENT_ARCH:-fc_loop}_specialists-${CANARY_MANAGER_V1_SPECIALISTS:-0}.sqlite3
```

`config::_HISTORICAL_CHECKPOINT_NAMES` aliases `checkpoints_fc_loop_specialists-0.sqlite3`
to the older `checkpoints_fc.sqlite3`, so an existing database keeps being used and there
is no migration step. Two guards back this up: if both files exist for one identity,
startup refuses rather than orphaning one; if the resolved path is missing while a
database that identity owns sits beside it, startup refuses with
`OrphanedCheckpointError`. `CHECKPOINT_ALLOW_NEW_DB=1` deliberately starts on a new empty
database (the old one is not migrated and not reachable by account erasure).

### Image build

`update.sh` does this automatically for the pinned commit. To build a candidate by hand:

```bash
git worktree add /tmp/candidate-<sha> <candidate-ref>
docker build -t uk-rent-agent:canary-<arch-with-hyphens>-<sha> /tmp/candidate-<sha>
git worktree remove /tmp/candidate-<sha>
```

Then set `FC_CANARY_IMAGE` and `FC_CANARY_SHA` in the root `.env`. Never reuse `:latest`
or rebuild a tag in place; a new candidate gets a new tag. The candidate service has no
`build:` in compose, so the working tree can never silently become what it runs.

### Bring-up (internal stage)

```bash
docker compose --profile canary up -d app-fc
curl -s -D- -o /dev/null http://127.0.0.1:<candidate-port>/ready   # X-Agent-* headers
python scripts/canary_report.py --input .runtime/logs/ --stage internal --since <stage-start-ISO>
```

---

## 2. Stage table

A stage advances only when both minima clear. Use a unique rollout id per stage window.

| Stage | Candidate traffic | Min hold | Min candidate turns | Authorised |
|---|---|---|---|---|
| `internal` | weight 0 | 24h | 50 | yes |
| `c1` | 5% | 24h | 200 | yes |
| `c2` | 20% | 48h | 500 | yes |
| `c3` | 50% | 72h | 1000 | yes — highest operator stage |
| `flip` | 100% | 7d | 2000 | only with `CANARY_ALLOW_FLIP=1` and a frozen/shadow control gate |
| `maintenance` | 100% (temporary) | — | — | machine-only (deploy drain) |

```bash
sudo bash deploy/set_canary_weight.sh --weight 5 \
  --rollout-id <unique-id> --stage c1 --allow-public-candidate
bash deploy/set_canary_weight.sh --status
```

**The candidate slot is exclusive.** There are two service slots and `app` is fixed to
`legacy`, so starting a `manager_v1` candidate replaces whatever the candidate slot ran
before. The comparison arm and every rollback target is then `legacy`, and returning to
`fc_loop` is a new release. `release.sh` and `switch_pool.sh` warn when the configured
candidate differs from what the slot runs.

### The 100% policy stop

At 100% there is no concurrent control arm, so every relative gate loses its denominator.
`set_canary_weight.sh` accepts weight 100 only as:

| Route | Accepted when | Caller |
|---|---|---|
| `--stage maintenance` | the deploy lock is held, the rollout id is `deploy-maintenance-<sha>`, and an active-drain marker names that id | `deploy/update.sh` during a drain |
| `--stage flip` | the environment carries `CANARY_ALLOW_FLIP=1` | an operator performing a gated cutover |

Anything else is refused before the route file changes. `switch_pool.sh --to fc` applies
the same rule on a single-upstream host.

#### The `maintenance` stage and its automatic restore

The drain reads the pre-drain weight, rollout id and stage from the routing include's
`# rentcompass-*` markers, switches under its own `deploy-maintenance-<sha>` rollout id,
and restores the recorded values when the update finishes, fails or is interrupted. A
recorded `100 @ maintenance` is debris from an unfinished drain and is never replayed.
Pass `--rollout-id` to `canary_report.py`; `--since` alone also counts drain turns.

#### `X-Agent-Specialists`: absent means 0 unless 1 is expected

Images built before the header existed omit it. The scripts and monitor require it only
when the expected identity has `specialists=1`; otherwise an absent header counts as 0 and
the exemption is printed.

### The answer probe

`/ready` proves identity and wiring, not that a service can answer. Before any weight
above 0, `deploy/probe_pool_answer.py` drives one real `POST /api/alex` turn against each
service and requires HTTP 200 with `X-Agent-Outcome: ok`, the expected identity headers,
an answer that is not a canned or fallback renderer, the grounding substring the query's
tool must produce, and no announced-but-unexecuted lookup.

```bash
python3 deploy/probe_pool_answer.py --url http://127.0.0.1:<port> \
  --expect-arch manager_v1 --expect-specialists 1 \
  --canary-log .runtime/logs/canary-manager_v1.jsonl
```

The probe is skipped for weight 0 and for any decrease, so reducing exposure never waits
on a model call. A clarification reply is inconclusive (exit 2) and is retried once. Each
probe is a real, billed turn and creates an anonymous conversation.
`--skip-answer-probe` (or `CANARY_SKIP_ANSWER_PROBE=1`) opts out with a warning.

### The rollback-coupled gate

```bash
bash deploy/run_canary_gate.sh --input .runtime/logs/ \
  --stage c1 --since <stage-start-ISO> \
  --candidate-arch <arch> --rollout-id <id> --rollout-stage c1 \
  --configured-weight 5 --expect-rollout-turns <edge-turn-count>
```

It forwards every argument to `canary_report.py`. Exit 3 alone invokes
`set_canary_weight.sh --weight 0`; exit 0 never promotes automatically. Exit 70
(`ROLLBACK_FAILED`) means the rollback itself failed — possibly because a deploy holds the
lock — and exit 69 means the gate could not start (§0.10).

---

## 3. Gate metrics

**Zero-tolerance (exit 3, on a valid measurement):** a tainted/unauthorised memory write
executed; a forbidden write executed; a DSML/tool-markup leak; systematic schema/API 400s.
A denied attempt is the designed safe path and does not count.

**Stage-pause (exit 2):**

- candidate p50 > 6000 ms or p95 > 30000 ms;
- degraded-turn rate (`partial` ∪ `soft_wrapped`) > 10%;
- forbidden-read, no-evidence-number or 5xx rate more than 1pp worse than control;
- for `manager_v1`, specialist non-delivery `(failed + skipped) / planned` > 5%.

Precedence: measurement-invalid (2) ▸ zero-tolerance (3) ▸ instrumentation-hold (2) ▸
stage-pause (2) ▸ proceed (0).

---

## 4. Rollback

```bash
sudo bash deploy/set_canary_weight.sh --weight 0     # weighted install
sudo bash deploy/switch_pool.sh --to legacy          # single-upstream install
```

Weight 0 needs only a ready `legacy` service and skips the answer probe. Any decrease
(for example 50 → 5) is always available. The control service rebuilds affected
conversations from the shared transcript into its own checkpoint namespace and never
reads candidate checkpoints.

---

## 5. `canary_report.py` usage

```bash
python scripts/canary_report.py --input .runtime/logs/canary-fc_loop.jsonl
python scripts/canary_report.py --input .runtime/logs/canary-manager_v1.jsonl \
  --candidate-arch manager_v1 --require-specialists
python scripts/canary_report.py --input .runtime/logs/ --window 24 --json out/canary.json
python scripts/canary_report.py --input .runtime/logs/ --stage c1 --since <ISO> \
  --rollout-id <id> --candidate-sha <full-sha>
```

- `--input` is repeatable (file, directory or glob). Directories and globs read only
  `*.jsonl` / `*.log` / `*.ndjson`, so keep log backups under another suffix or outside
  the log directory.
- `--window HOURS` and `--since ISO` both bound the records; the later bound wins. The
  applied cutoff is printed on the `record filter :` line.
- `--candidate-sha` restricts zero-tolerance to one build; other builds and the control
  arm are reported beside it and never gated on.
- `--require-specialists` asserts at least one specialist task was planned, started and
  delivered (completed or partial).

| Exit | Meaning |
|---|---|
| `0` | proceed / stage-progress-ok |
| `2` | hold, stage-pause or instrumentation-hold (including an invalid measurement) |
| `3` | zero-tolerance breach on a valid measurement |
| `1` | input/runtime error — nothing measured |
| `64` | CLI usage error — nothing measured |

**An invalid measurement can never roll anything back.** Mixed
`telemetry_schema_version`, mixed candidate builds, a record contract-violation rate above
5%, or an empty window make the verdict an instrumentation-hold that outranks
zero-tolerance. The breach counters are still printed, labelled not actionable, and
`run_canary_gate.sh` independently refuses a rollback handed a 3 with
`measurement_valid=False`.

Read the `[INSTRUMENTATION]` block printed under `decision :` before the verdict. Rotate
telemetry only while the service is stopped: the logger holds its file descriptor.
`scripts/canary_prune_fixture_rows.py` removes provable test-fixture rows from a log
(`--dry-run` by default, `--apply` after a backup).
