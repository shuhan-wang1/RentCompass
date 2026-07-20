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
| **fc** | `AGENT_ARCH=fc_loop`, `DEEPSEEK_STRICT=1` | **pinned to tag `canary/fc-loop-7db03e7`** — an immutable image tag, **never the branch tip** |

- The fc image is **pinned to a tag**, not to `feat/harness-fc-loop`'s HEAD. Rebuilding the
  branch must not move what canary traffic runs. Cut a new tag to advance the candidate.
- Both pools run the **same** application; only the arch flag, strict flag, and image differ.

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
python scripts/canary_report.py --input logs/canary.jsonl

# Windowed to the last 24h, machine-readable copy for CI:
python scripts/canary_report.py --input logs/ --window 24 --json out/canary.json

# Per stage (both minima checked). --since is the stage start.
python scripts/canary_report.py --input logs/ --stage internal --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input logs/ --stage c1 --window 24  --since 2026-07-20T09:00:00Z
python scripts/canary_report.py --input logs/ --stage c2 --window 48  --since 2026-07-21T09:00:00Z
python scripts/canary_report.py --input logs/ --stage c3 --window 72  --since 2026-07-23T09:00:00Z
python scripts/canary_report.py --input logs/ --stage flip --window 168 --since 2026-07-26T09:00:00Z
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
