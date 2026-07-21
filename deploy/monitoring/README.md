# Deploy pin + health monitoring (root install)

Two pieces of ops hardening for the frozen production deploy:

1. **Deploy pin gate** — `deploy/update.sh` refuses to build unless `HEAD` is
   *exactly* the commit named in an untracked, server-local file. Production
   deploys the exact pin and nothing else (no "deploy a later commit" escape).
2. **Health monitor** — a systemd timer runs `rentcompass-monitor.sh` every 5
   minutes (read-only probes; no LLM/`/api` calls).

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

Verify:

```bash
systemctl list-timers rentcompass-monitor.timer      # next/last run
sudo systemctl start rentcompass-monitor.service     # run once now
tail -n 5 /var/log/rentcompass/monitor.log           # status lines
journalctl -u rentcompass-monitor.service -p warning --since -1h   # anomalies only
```

## What it checks (every 5 min)

| Area | Check | Alert priority |
|------|-------|----------------|
| Public `:8443/health` | HTTP 200 **and** `x-agent-arch: legacy` | err (3) |
| Local `:5001/health` | 200 + `x-agent-arch: legacy` (+ version) | err (3) |
| Local `:5002/health` | 200 + `x-agent-arch: fc_loop` (internal) | warning (4) |
| Containers | health status + restart-count delta | err/warn |
| Host memory | `MemAvailable` ≥ 800 MB (no swap) | err (3) |
| Disk | `/` usage ≤ 90 % | err (3) |
| SQLite | `-wal` size ≤ 200 MB; `database is locked` in recent logs | warn/err |
| Telemetry | `canary-legacy.jsonl` / `canary-fc_loop.jsonl` size, mtime, line count | warn if missing |

- **flock** (`/run/rentcompass-monitor.lock`) prevents overlapping runs.
- OK runs are silent in the journal; every run appends one line to
  `/var/log/rentcompass/monitor.log` (rotated daily, 7 kept).
- Thresholds/paths are env-overridable (`MON_*`) — see the script header.
- No real LLM/`/api` heartbeats: zero cost, no agent-state pollution.
