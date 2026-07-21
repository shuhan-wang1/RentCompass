#!/usr/bin/env bash
# RentCompass health monitor — runs every 5 min via systemd timer (see the .timer
# unit next to this file). READ-ONLY probes only: /health endpoints, docker
# inspect, free/df/stat, and log greps. It never calls /api/* or an LLM, so it
# adds no cost and cannot pollute agent state.
#
# Output contract:
#   * A one-line status summary is appended to $LOG_FILE every run (rotated by
#     logrotate — see rentcompass-monitor.logrotate).
#   * ONLY anomalies are written to stdout, prefixed with a systemd log-level
#     "<N>" token (3=err, 4=warning), so under the systemd service they land in
#     the journal at the right priority and OK runs stay silent.
#
# All paths/thresholds are env-overridable (see below) for manual dry-runs, e.g.
#   MON_LOG=/tmp/m.log MON_STATE=/tmp/m.state MON_LOCK=/tmp/m.lock \
#     bash deploy/monitoring/rentcompass-monitor.sh
set -u

REPO="${MON_REPO:-/home/shuhan/uk_rent_recommendation}"
RUNTIME="${MON_RUNTIME:-$REPO/.runtime}"
LOG_FILE="${MON_LOG:-/var/log/rentcompass/monitor.log}"
STATE="${MON_STATE:-/run/rentcompass-monitor.state}"
LOCK="${MON_LOCK:-/run/rentcompass-monitor.lock}"

PUBLIC_URL="${MON_PUBLIC_URL:-https://rentcompass.co.uk:8443/health}"
LEGACY_URL="${MON_LEGACY_URL:-http://127.0.0.1:5001/health}"
FC_URL="${MON_FC_URL:-http://127.0.0.1:5002/health}"

MEM_FREE_MIN_MB="${MON_MEM_FREE_MIN_MB:-800}"
DISK_USE_MAX_PCT="${MON_DISK_USE_MAX_PCT:-90}"
WAL_MAX_MB="${MON_WAL_MAX_MB:-200}"
LOG_SCAN_WINDOW="${MON_LOG_SCAN_WINDOW:-6m}"

# --- single-instance guard (flock; prevents overlapping 5-min runs) ---------
exec 9>"$LOCK" 2>/dev/null || exit 0
flock -n 9 || exit 0

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
ts="$(date -Is)"
alerts=0
summary=""

emit_alert() { # <priority> <message...>
  local pri="$1"; shift
  alerts=$((alerts + 1))
  printf '<%s>ALERT %s\n' "$pri" "$*"                     # -> journal (systemd reads <N>)
  printf '%s ALERT[%s] %s\n' "$ts" "$pri" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

# --- load previous state (restart counts, telemetry line counts) ------------
declare -A PREV
if [ -r "$STATE" ]; then
  while read -r k v; do [ -n "${k:-}" ] && PREV["$k"]="$v"; done < "$STATE"
fi
declare -A NOW

# --- probe a /health endpoint: echoes "code arch version" -------------------
probe() {
  local url="$1" hdr code arch ver
  hdr="$(curl -s -k --max-time 8 -D - -o /dev/null "$url" 2>/dev/null)"
  code="$(printf '%s' "$hdr" | awk 'NR==1{print $2; exit}')"
  arch="$(printf '%s' "$hdr" | awk 'tolower($1)=="x-agent-arch:"{gsub(/\r/,"",$2);print $2; exit}')"
  ver="$(printf '%s'  "$hdr" | awk 'tolower($1)=="x-agent-version:"{gsub(/\r/,"",$2);print $2; exit}')"
  printf '%s %s %s' "${code:-000}" "${arch:-none}" "${ver:-none}"
}

# 1) PUBLIC edge MUST stay legacy --------------------------------------------
read -r p_code p_arch _p_ver <<<"$(probe "$PUBLIC_URL")"
summary+="pub=$p_code/$p_arch "
if [ "$p_code" != "200" ]; then
  emit_alert 3 "public $PUBLIC_URL returned HTTP $p_code (expected 200)"
elif [ "$p_arch" != "legacy" ]; then
  emit_alert 3 "public edge x-agent-arch=$p_arch — MUST be legacy (fc leaked to public!)"
fi

# 2) local legacy pool -------------------------------------------------------
read -r l_code l_arch l_ver <<<"$(probe "$LEGACY_URL")"
summary+="legacy=$l_code/$l_arch/$l_ver "
if [ "$l_code" != "200" ]; then
  emit_alert 3 "legacy $LEGACY_URL returned HTTP $l_code (expected 200)"
elif [ "$l_arch" != "legacy" ]; then
  emit_alert 3 "legacy pool x-agent-arch=$l_arch (expected legacy)"
fi

# 3) local fc pool (internal canary; lower severity) -------------------------
read -r f_code f_arch f_ver <<<"$(probe "$FC_URL")"
summary+="fc=$f_code/$f_arch/$f_ver "
if [ "$f_code" != "200" ]; then
  emit_alert 4 "fc pool $FC_URL returned HTTP $f_code (internal canary; expected 200 while running)"
elif [ "$f_arch" != "fc_loop" ]; then
  emit_alert 4 "fc pool x-agent-arch=$f_arch (expected fc_loop)"
fi

# 4) containers: health + restart delta --------------------------------------
for c in uk-rent-app uk-rent-app-fc uk-rent-searxng uk-rent-valkey; do
  info="$(docker inspect -f '{{.State.Health.Status}} {{.RestartCount}}' "$c" 2>/dev/null)"
  if [ -z "$info" ]; then
    if [ "$c" = "uk-rent-app-fc" ]; then emit_alert 4 "container $c not found (internal canary)"
    else emit_alert 3 "container $c not found / not running"; fi
    continue
  fi
  health="${info%% *}"; restarts="${info##* }"
  NOW["restart_$c"]="$restarts"
  prev="${PREV[restart_$c]:-$restarts}"
  if [ "$health" != "healthy" ] && [ "$health" != "<no value>" ]; then
    if [ "$c" = "uk-rent-app-fc" ]; then emit_alert 4 "container $c health=$health"
    else emit_alert 3 "container $c health=$health"; fi
  fi
  if [ "$restarts" -gt "$prev" ] 2>/dev/null; then
    emit_alert 3 "container $c restarted ($prev -> $restarts) since last check"
  fi
done

# 5) host free memory (11GB box, no swap -> OOM risk) ------------------------
mem_avail="$(free -m | awk '/^Mem:/{print $7}')"
summary+="mem_avail=${mem_avail:-?}MB "
if [ -n "${mem_avail:-}" ] && [ "$mem_avail" -lt "$MEM_FREE_MIN_MB" ] 2>/dev/null; then
  emit_alert 3 "host available memory ${mem_avail}MB < ${MEM_FREE_MIN_MB}MB (no swap — OOM risk)"
fi

# 6) disk --------------------------------------------------------------------
disk_pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
summary+="disk=${disk_pct:-?}% "
if [ -n "${disk_pct:-}" ] && [ "$disk_pct" -gt "$DISK_USE_MAX_PCT" ] 2>/dev/null; then
  emit_alert 3 "disk / at ${disk_pct}% (> ${DISK_USE_MAX_PCT}%)"
fi

# 7) SQLite -wal sizes (large WAL = checkpoint stalled / lock contention) -----
for wal in "$RUNTIME"/checkpoints.sqlite3-wal "$RUNTIME"/checkpoints_fc.sqlite3-wal "$RUNTIME"/conversations.sqlite3-wal; do
  [ -f "$wal" ] || continue
  sz=$(( $(stat -c%s "$wal" 2>/dev/null || echo 0) / 1048576 ))
  if [ "$sz" -gt "$WAL_MAX_MB" ] 2>/dev/null; then
    emit_alert 4 "WAL $(basename "$wal") ${sz}MB (> ${WAL_MAX_MB}MB — checkpoint may be stalled/locked)"
  fi
done

# 8) SQLite lock errors in recent container logs -----------------------------
for c in uk-rent-app uk-rent-app-fc; do
  n="$(docker logs --since "$LOG_SCAN_WINDOW" "$c" 2>&1 | grep -icE 'database is locked|SQLITE_BUSY' || true)"
  if [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; then
    emit_alert 3 "$c logged $n SQLite lock error(s) in last $LOG_SCAN_WINDOW"
  fi
done

# 9) canary telemetry logs: size, mtime, line count (+growth since last run) --
for name in canary-legacy canary-fc_loop; do
  f="$RUNTIME/logs/$name.jsonl"
  if [ ! -f "$f" ]; then
    emit_alert 4 "telemetry log $name.jsonl missing"
    continue
  fi
  lines="$(wc -l < "$f" 2>/dev/null | tr -dc '0-9')"; lines="${lines:-0}"
  mtime="$(date -d "@$(stat -c%Y "$f" 2>/dev/null || echo 0)" +%H:%M:%S 2>/dev/null)"
  NOW["lines_$name"]="$lines"
  prevl="${PREV[lines_$name]:-$lines}"
  summary+="$name=${lines}(+$(( lines - prevl )))@${mtime:-?} "
done

# --- persist state ----------------------------------------------------------
{ for k in "${!NOW[@]}"; do printf '%s %s\n' "$k" "${NOW[$k]}"; done; } > "$STATE" 2>/dev/null || true

# --- always append one status line ------------------------------------------
status="OK"; [ "$alerts" -gt 0 ] && status="ALERTS=$alerts"
printf '%s %s %s\n' "$ts" "$status" "$summary" >> "$LOG_FILE" 2>/dev/null || true
exit 0
