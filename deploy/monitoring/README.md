# Deploy pin + health monitoring (root install)

Two pieces of ops hardening for the frozen production deploy:

1. **Deploy pin gate** — `deploy/update.sh` refuses to build unless `HEAD` is
   *exactly* the commit named in an untracked, server-local file. Production
   deploys the exact pin and nothing else (no "deploy a later commit" escape).
2. **Health monitor** — a systemd timer runs `rentcompass-monitor.sh` every 5
   minutes. Read-only probes, and it never calls `/api/*`, so it cannot pollute
   agent state or the canary telemetry the eval gate reads. It *does* make one
   direct provider call, at most hourly (check 10) — see the table below.

Everything below needs **root** (this box has no passwordless sudo — run each
with `!sudo …` in the Claude session, or as root directly).

## 1. Pin file — `/etc/rentcompass/deploy.env`

The gate reads `DEPLOY_PINNED_SHA` (full 40-char sha) from here.

```bash
sudo install -d -m 0755 /etc/rentcompass
# <PINNED_SHA> = the deployed commit's FULL sha (git rev-parse HEAD)
printf 'DEPLOY_PINNED_SHA=%s\n' "<PINNED_SHA>" | sudo tee /etc/rentcompass/deploy.env
sudo chmod 0644 /etc/rentcompass/deploy.env
```

**Re-pin procedure** (only when intentionally shipping a new frozen release):
edit `DEPLOY_PINNED_SHA` in this file **and** `git checkout <sha>` to the same
commit, then run `bash deploy/update.sh`. The tree must be committed-clean.

## 2. Health monitor (systemd timer + logrotate)

```bash
sudo install -d -m 0755 /var/log/rentcompass
sudo cp deploy/monitoring/rentcompass-monitor.service /etc/systemd/system/
sudo cp deploy/monitoring/rentcompass-monitor.timer   /etc/systemd/system/
sudo cp deploy/monitoring/rentcompass-monitor.logrotate /etc/logrotate.d/rentcompass-monitor
sudo systemctl daemon-reload
sudo systemctl enable --now rentcompass-monitor.timer
```

### 2a. The stable copy at `/usr/local/bin` — READ THIS BEFORE REDEPLOYING

The unit's own `ExecStart` names the script **inside the pinned deploy tree**
(`/home/shuhan/uk_rent_recommendation/deploy/monitoring/`). Production does *not*
run that copy. A drop-in redirects the timer to a stable copy at
`/usr/local/bin/rentcompass-monitor.sh`, so that a frozen deploy pin cannot hold
the monitor hostage — the monitor can be fixed without re-pinning the product.

That split is deliberate, but it is also how three copies drifted apart on
2026-07-26/27 (git, `/usr/local/bin`, and the pinned tree were three *different*
builds, and the one guarding production was in none of the other two). So the
install of the stable copy is a documented step with a verification, not shell
history:

```bash
# 1. install the committed monitor as the stable copy
sudo install -m 0755 deploy/monitoring/rentcompass-monitor.sh /usr/local/bin/rentcompass-monitor.sh

# 2. point the unit at it (idempotent; this is the drop-in that must survive rebuilds)
sudo install -d -m 0755 /etc/systemd/system/rentcompass-monitor.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/local/bin/rentcompass-monitor.sh\nEnvironment=MON_EXPECTED_SRC_SHA=%s\n' \
  "$(awk '{print $1}' deploy/monitoring/rentcompass-monitor.sha256)" \
  | sudo tee /etc/systemd/system/rentcompass-monitor.service.d/override.conf
sudo systemctl daemon-reload

# 3. prove it took
bash deploy/monitoring/check_install_drift.sh
```

Step 2 also **declares** the build it installed via `MON_EXPECTED_SRC_SHA`, taken
straight from the repo manifest — so the monitor itself pages (sev3, once per state
change) if it is ever running something other than the declared build. Unset, the
check is inert; it never invents an alert on a box that was set up by hand.

### 2b. Verify

```bash
systemctl list-timers rentcompass-monitor.timer      # next/last run
sudo systemctl start rentcompass-monitor.service     # run once now
tail -n 5 /var/log/rentcompass/monitor.log           # status lines
journalctl -u rentcompass-monitor.service -p warning --since -1h   # anomalies only
bash deploy/monitoring/check_install_drift.sh        # git == installed == what systemd runs
```

Every status line begins with `src=<12 hex>` — the sha256 prefix of the script that
produced it. Compare it to the manifest at a glance:

```bash
grep -o 'src=[0-9a-f]*' /var/log/rentcompass/monitor.log | tail -1
awk '{print substr($1,1,12)}' deploy/monitoring/rentcompass-monitor.sha256
```

If those two differ, the running monitor is not the committed one. **After editing
`rentcompass-monitor.sh`, regenerate the manifest** or CI fails:

```bash
bash deploy/monitoring/check_install_drift.sh --write-manifest   # then commit
```

## What it checks (every 5 min)

| Area | Check | Alert priority |
|------|-------|----------------|
| Monitor build | running `src=` matches `MON_EXPECTED_SRC_SHA`, when declared | err (3) |
| Public `:8443/health` | HTTP 200 **and** `x-agent-arch` == `MON_EXPECTED_PUBLIC_ARCH` (default `fc_loop`) | err (3) |
| Local `:5001/health` | 200 + `x-agent-arch: legacy` (+ version) | err (3) |
| Local `:5002/health` | 200 + `x-agent-arch: fc_loop` (internal) | warning (4) |
| Pool identity | fc vs `FC_CANARY_SHA`, legacy vs `LEGACY_APP_SHA`, edge vs expected pool | err (3) / warn (4) |
| Containers | health status + restart-count delta | err/warn |
| Host memory | `MemAvailable` ≥ 800 MB (no swap) | err (3) |
| Disk | `/` usage ≤ 90 % | err (3) |
| SQLite | `-wal` size ≤ 200 MB; `database is locked` in recent logs | warn/err |
| Telemetry | `canary-legacy.jsonl` / `canary-fc_loop.jsonl` size, mtime, line count | warn if missing |
| Provider (check 10) | one-token completion **direct to the provider**, ≤ hourly; 4xx = configured model rejected | err (3) / warn (4) on 429/5xx |

- **flock** (`/run/rentcompass-monitor.lock`) prevents overlapping runs.
- OK runs are silent in the journal; every run appends one line to
  `/var/log/rentcompass/monitor.log` (rotated daily, 7 kept), led by `src=`.
- Thresholds/paths are env-overridable (`MON_*`) — see the script header.
- **`MON_EXPECTED_PUBLIC_ARCH` declares which arch the public edge is meant to
  serve.** Update it as part of any cutover or rollback (`deploy/switch_pool.sh`
  moves the upstream; this variable records that the move was on purpose). It was
  hard-coded to `legacy` until 2026-07-26, when cutting the edge to `fc_loop` made
  it fire sev3 every five minutes about the intended state.
- No `/api/*` traffic and no synthetic agent turns: no agent-state pollution and
  nothing written to `.runtime/logs/canary-<arch>.jsonl`, which the eval gate reads.
  Check 10 is the one paid call — `max_tokens=1`, at most hourly, straight to the
  provider — and it exists because on 2026-07-24 a retired `DEEPSEEK_MODEL` made the
  provider reject every real question for a day while checks 1–9 stayed green.

## Known blind spot

`/run` is tmpfs, so a reboot clears the state file. Every delta-based check
(restart counts, the telemetry-shrink assertion) compares a value against itself
and silently no-ops **exactly once per boot**. Documented rather than fixed:
moving state to a persistent path would also let a stale state file outlive a
genuine reset. When asking "why did the monitor not catch X", check `uptime` first.
