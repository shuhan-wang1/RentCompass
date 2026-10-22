# Deploy pin + health monitoring (root install)

Two pieces of operational hardening for a host that runs the compose stack:

1. **Deploy pin gate** — `deploy/update.sh` refuses to build unless `HEAD` is *exactly*
   the commit named in an untracked, host-local file, and refuses to report success
   unless the service it deployed answers `/ready` with the pinned architecture and the
   full 40-character sha. `--status` prints the pin and both services without changing
   anything; `--both` also refreshes the standby (rollback) service.
2. **Health monitor** — a systemd timer runs `rentcompass-monitor.sh` every 5 minutes.
   Its probes are read-only and never call `/api/*`, so it cannot pollute agent state or
   the canary telemetry the gate reads. It does make one direct provider call, at most
   hourly (check 10, below).

Everything below needs root.

## 1. Pin file — `/etc/rentcompass/deploy.env`

The gate reads `DEPLOY_PINNED_SHA` (full 40-character sha) and `DEPLOY_PYTHON_IMAGE` (an
immutable `python@sha256:...` base image) from this file:

```bash
sudo install -d -m 0755 /etc/rentcompass
printf 'DEPLOY_PINNED_SHA=%s\nDEPLOY_PYTHON_IMAGE=%s\n' \
  "<full-commit-sha>" "python@sha256:<64-hex-digest>" | sudo tee /etc/rentcompass/deploy.env
sudo chmod 0644 /etc/rentcompass/deploy.env
```

`bash deploy/release.sh` performs the whole re-pin for you against the tip of the remote
mainline: fetch, CI check, confirmation, a scoped repair of the persistent bind-mount
ownership (`deploy/preflight_runtime_permissions.sh --repair`), checkout, re-pin, then
`update.sh`. A repair failure aborts before checkout or re-pin. It does not weaken the
gate: `update.sh` still enforces `HEAD == DEPLOY_PINNED_SHA`. `--dry-run` prints the plan
and changes nothing.

By hand: edit `DEPLOY_PINNED_SHA` **and** `git checkout <sha>` to the same commit, then run
`bash deploy/update.sh`. The tree must be committed-clean.

## 2. Health monitor (systemd timer + logrotate)

```bash
sudo install -d -m 0755 /var/log/rentcompass
sudo cp deploy/monitoring/rentcompass-monitor.service /etc/systemd/system/
sudo cp deploy/monitoring/rentcompass-monitor.timer   /etc/systemd/system/
sudo cp deploy/monitoring/rentcompass-monitor.logrotate /etc/logrotate.d/rentcompass-monitor
sudo systemctl daemon-reload
sudo systemctl enable --now rentcompass-monitor.timer
```

### 2a. The stable copy at `/usr/local/bin`

The tracked unit's `ExecStart` names the script inside the deploy checkout. A drop-in
redirects the timer to a stable copy at `/usr/local/bin/rentcompass-monitor.sh`, so the
monitor can be fixed without re-pinning the product — and so the installed copy is a
documented, verifiable step rather than shell history:

```bash
# 1. install the committed monitor as the stable copy
sudo install -m 0755 deploy/monitoring/rentcompass-monitor.sh /usr/local/bin/rentcompass-monitor.sh

# 2. point the unit at it (idempotent; this drop-in must survive rebuilds)
sudo install -d -m 0755 /etc/systemd/system/rentcompass-monitor.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/local/bin/rentcompass-monitor.sh\nEnvironment=MON_EXPECTED_SRC_SHA=%s\n' \
  "$(awk '{print $1}' deploy/monitoring/rentcompass-monitor.sha256)" \
  | sudo tee /etc/systemd/system/rentcompass-monitor.service.d/override.conf
sudo systemctl daemon-reload

# 3. prove it took
bash deploy/monitoring/check_install_drift.sh
```

Step 2 also declares the build it installed via `MON_EXPECTED_SRC_SHA`, taken from the
repo manifest, so the monitor alerts (once per state change) if it is ever running
something else. Unset, the check is inert.

Neither `release.sh` nor `update.sh` reinstalls the monitor; re-run step 1 after a release
that changes it.

### 2b. Verify

```bash
systemctl list-timers rentcompass-monitor.timer      # next/last run
sudo systemctl start rentcompass-monitor.service     # run once now
tail -n 5 /var/log/rentcompass/monitor.log           # status lines
journalctl -u rentcompass-monitor.service -p warning --since -1h
bash deploy/monitoring/check_install_drift.sh        # git == installed == what systemd runs
```

Every status line begins with `src=<12 hex>`, the sha256 prefix of the script that
produced it. Compare it to the manifest:

```bash
grep -o 'src=[0-9a-f]*' /var/log/rentcompass/monitor.log | tail -1
awk '{print substr($1,1,12)}' deploy/monitoring/rentcompass-monitor.sha256
```

**After editing `rentcompass-monitor.sh`, regenerate the manifest** or CI fails:

```bash
bash deploy/monitoring/check_install_drift.sh --write-manifest   # then commit
```

## What it checks (every 5 minutes)

| Area | Check | Alert priority |
|------|-------|----------------|
| Monitor build | running `src=` matches `MON_EXPECTED_SRC_SHA`, when declared | err (3) |
| Public `/ready` | HTTP 200; arch/sha/specialist bit match the configured routing and pins | err (3) |
| Control `/ready` | 200 + legacy arch, version, specialists off | err (3) |
| Candidate `/ready` | 200 + configured candidate arch and specialist bit | warning (4) / err (3) |
| Service identity | candidate vs `FC_CANARY_SHA`, control vs `LEGACY_APP_SHA` | err (3) / warning (4) |
| Containers | health status + restart-count delta | err / warning |
| Host memory | `MemAvailable` above a threshold | err (3) |
| Disk | `/` usage below a threshold | err (3) |
| SQLite | `-wal` size; `database is locked` in recent logs | warning / err |
| Telemetry | canary log size, mtime, line count | warning if missing |
| Provider (check 10) | one-token completion direct to the provider, at most hourly; 4xx = configured model rejected | err (3) / warning (4) on 429/5xx |

- `flock` on `/run/rentcompass-monitor.lock` prevents overlapping runs.
- OK runs are silent in the journal; every run appends one `src=`-prefixed line to
  `/var/log/rentcompass/monitor.log` (rotated daily, 7 kept).
- Thresholds, URLs and paths are environment-overridable (`MON_*`); see the script
  header.
- Optional `MON_WEBHOOK_URL`, `MON_EMAIL_TO` and `MON_HEARTBEAT_URL` go in
  `/etc/rentcompass/monitor.env`. Alerts exit non-zero; the heartbeat is a dead-man
  signal for the monitor itself.
- Check 10 is the only paid call (`max_tokens=1`, at most hourly). It exists because
  `/health` and `/ready` stay green when the provider rejects the configured model.

## Known blind spot

`/run` is tmpfs, so a reboot clears the state file, and every delta-based check (restart
counts, the telemetry-shrink assertion) silently no-ops once per boot. When asking why the
monitor did not catch something, check `uptime` first.
