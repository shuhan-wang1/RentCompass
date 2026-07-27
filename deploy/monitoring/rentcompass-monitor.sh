#!/usr/bin/env bash
# RentCompass health monitor — runs every 5 min via systemd timer (see the .timer
# unit next to this file). READ-ONLY probes only: /health endpoints, docker
# inspect, free/df/stat, and log greps. It never calls /api/*, so it cannot
# pollute agent state or the canary telemetry the gate reads.
#
# It DOES make one out-of-band provider call, at most hourly (check 10). That is
# deliberate: the constraint that made this script safe is the same constraint
# that made it blind. On 2026-07-24 a retired DEEPSEEK_MODEL made the provider
# reject every real question for a day while the process stayed healthy, and
# checks 1-9 would all have stayed green. The probe talks to the provider
# DIRECTLY -- never through /api/* -- so it can see that class without writing a
# single row into .runtime/logs/canary-<arch>.jsonl.
#
# Output contract:
#   * A one-line status summary is appended to $LOG_FILE every run (rotated by
#     logrotate — see rentcompass-monitor.logrotate).
#   * ONLY anomalies are written to stdout, prefixed with a systemd log-level
#     "<N>" token (3=err, 4=warning), so under the systemd service they land in
#     the journal at the right priority and OK runs stay silent.
#   * Every status line STARTS with `src=<first 12 hex of this file's sha256>`.
#     See PROVENANCE below — that token is what makes install drift visible.
#
# PROVENANCE, and the defect it closes. This file exists in more than one place, and
# on 2026-07-27 all three copies were DIFFERENT:
#
#   deploy/monitoring/rentcompass-monitor.sh   in git — nominally the source of truth
#   /usr/local/bin/rentcompass-monitor.sh      the deliberate stable copy the timer runs
#   $REPO/deploy/monitoring/…                  the pinned production tree, 6 days behind
#
# The split is INTENTIONAL: the unit's own ExecStart names the pinned tree, and an
# override.conf redirects it to /usr/local/bin precisely so a frozen deploy pin cannot
# hold the monitor hostage. What was NOT intentional is that nothing compared them, so
# "which of these three is actually guarding production" had no answer you could read
# anywhere. A comment saying "keep these in sync" is the thing this project has learned
# to distrust, so instead:
#
#   * this script hashes ITSELF (`sha256sum "$0"`) and puts the result in every status
#     line, so /var/log/rentcompass/monitor.log records which code produced each line;
#   * the repo commits the expected hash to rentcompass-monitor.sha256, and a test
#     (tests/test_monitor_install_provenance.py) keeps that file true of the script;
#   * deploy/monitoring/check_install_drift.sh compares repo ↔ manifest ↔ installed copy
#     ↔ the ExecStart systemd has actually resolved, and exits non-zero on any mismatch.
#
# Divergence is therefore visible in an artifact the operator already tails, instead of
# being invisible until a rebuild silently reverts production to an older monitor.
#
# All paths/thresholds are env-overridable (see below) for manual dry-runs, e.g.
#   MON_LOG=/tmp/m.log MON_STATE=/tmp/m.state MON_LOCK=/tmp/m.lock \
#     bash deploy/monitoring/rentcompass-monitor.sh
set -u

REPO="${MON_REPO:-/home/shuhan/uk_rent_recommendation}"
RUNTIME="${MON_RUNTIME:-$REPO/.runtime}"
LOG_FILE="${MON_LOG:-/var/log/rentcompass/monitor.log}"
# KNOWN BLIND SPOT, documented deliberately rather than fixed: /run is tmpfs, so a
# reboot clears this file. Every delta-based check below reads ${PREV[k]:-<current>},
# which makes the FIRST run after a reboot compare a value against itself: restart
# counts, and the telemetry-shrink assertion, are both silently no-ops exactly once
# per boot. Moving STATE to a persistent path would fix it and would also make a stale
# state file outlive a genuine reset, so it is a trade, not an oversight. Anyone
# investigating "why did the monitor not catch X" should check uptime first.
STATE="${MON_STATE:-/run/rentcompass-monitor.state}"
LOCK="${MON_LOCK:-/run/rentcompass-monitor.lock}"

PUBLIC_URL="${MON_PUBLIC_URL:-https://rentcompass.co.uk:8443/health}"
LEGACY_URL="${MON_LEGACY_URL:-http://127.0.0.1:5001/health}"
FC_URL="${MON_FC_URL:-http://127.0.0.1:5002/health}"

MEM_FREE_MIN_MB="${MON_MEM_FREE_MIN_MB:-800}"
DISK_USE_MAX_PCT="${MON_DISK_USE_MAX_PCT:-90}"
WAL_MAX_MB="${MON_WAL_MAX_MB:-200}"
LOG_SCAN_WINDOW="${MON_LOG_SCAN_WINDOW:-6m}"

# --- self-provenance (see PROVENANCE in the header) -------------------------
# Cheap (one sha256 of a ~22KB file per run), side-effect-free, and it touches neither
# agent state nor canary telemetry, so it is safe under the constraint that makes the
# rest of this script safe. It must NEVER be able to fail a run: if $0 is unreadable or
# sha256sum is missing we report `unknown` and carry on, because a monitor that dies
# while introspecting itself is strictly worse than one that cannot name its own build.
SELF_SHA="$(sha256sum "$0" 2>/dev/null | awk '{print substr($1,1,12)}')"
[ -n "${SELF_SHA:-}" ] || SELF_SHA="unknown"

# Optional DECLARED intent, same idiom as MON_EXPECTED_PUBLIC_ARCH below: if the
# installer tells us which build it installed, a mismatch is an error. Deliberately
# INERT when unset, which is today's state — so this cannot add a second alert to a
# steady state that is meant to sit at exactly one. It is NOT compared against
# $REPO/deploy/monitoring/, and that omission is the whole point: the pinned production
# tree is deliberately older than the installed copy, so keying on it would page every
# five minutes about the intended arrangement. Only an explicit declaration counts.
EXPECTED_SRC_SHA="${MON_EXPECTED_SRC_SHA:-}"

# --- single-instance guard (flock; prevents overlapping 5-min runs) ---------
exec 9>"$LOCK" 2>/dev/null || exit 0
flock -n 9 || exit 0

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
ts="$(date -Is)"
alerts=0
# src= leads the summary so the running build is the first thing on every status line,
# including on runs that alert. `tail -1 monitor.log` now answers "what is guarding
# production right now" without shelling out to compare files.
summary="src=$SELF_SHA "

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

# 0) INSTALL DRIFT: is the copy running the copy the installer declared? -----
# Fires only when MON_EXPECTED_SRC_SHA is declared (see above), and then only once per
# state change, because a wrong build is a standing condition and re-reporting a standing
# condition every five minutes is how the 2026-07-26 public-arch alert trained everyone
# to ignore it. sev3: an unexpected monitor build means every OTHER check in this file is
# of unknown vintage, which is exactly the failure that let a stale copy scream a false
# alarm 365 times after the fc cutover.
NOW["src_sha"]="$SELF_SHA"
if [ -n "$EXPECTED_SRC_SHA" ] && [ "$SELF_SHA" != "unknown" ] \
   && [ "${EXPECTED_SRC_SHA:0:12}" != "$SELF_SHA" ] \
   && [ "${PREV[src_sha]:-}" != "$SELF_SHA" ]; then
  emit_alert 3 "monitor build drift: running src=$SELF_SHA but MON_EXPECTED_SRC_SHA=${EXPECTED_SRC_SHA:0:12} — reinstall from deploy/monitoring (see check_install_drift.sh)"
fi

# --- probe a /health endpoint: echoes "code arch version" -------------------
probe() {
  local url="$1" hdr code arch ver
  hdr="$(curl -s -k --max-time 8 -D - -o /dev/null "$url" 2>/dev/null)"
  code="$(printf '%s' "$hdr" | awk 'NR==1{print $2; exit}')"
  arch="$(printf '%s' "$hdr" | awk 'tolower($1)=="x-agent-arch:"{gsub(/\r/,"",$2);print $2; exit}')"
  ver="$(printf '%s'  "$hdr" | awk 'tolower($1)=="x-agent-version:"{gsub(/\r/,"",$2);print $2; exit}')"
  printf '%s %s %s' "${code:-000}" "${arch:-none}" "${ver:-none}"
}

# 1) PUBLIC edge must serve the arch we intend --------------------------------
# This check used to hard-code "legacy" and treat anything else as a leak. On 2026-07-26 the
# owner deliberately cut the public edge over to fc_loop, so that assertion started firing
# sev3 every five minutes about the intended state. An alert that is always on is an alert
# that gets ignored, which is how the next real one is missed -- the same reasoning that
# demoted provider 429s to sev4.
#
# The intent is now declared, not assumed. MON_EXPECTED_PUBLIC_ARCH must be updated as part
# of any cutover or rollback; a mismatch still pages, because an UNPLANNED change of what the
# public edge serves is exactly what this check is for. deploy/switch_pool.sh is what moves
# the upstream, and this variable is what records that the move was on purpose.
EXPECTED_PUBLIC_ARCH="${MON_EXPECTED_PUBLIC_ARCH:-fc_loop}"

read -r p_code p_arch p_ver <<<"$(probe "$PUBLIC_URL")"
summary+="pub=$p_code/$p_arch "
if [ "$p_code" != "200" ]; then
  emit_alert 3 "public $PUBLIC_URL returned HTTP $p_code (expected 200)"
elif [ "$p_arch" != "$EXPECTED_PUBLIC_ARCH" ]; then
  emit_alert 3 "public edge x-agent-arch=$p_arch but MON_EXPECTED_PUBLIC_ARCH=$EXPECTED_PUBLIC_ARCH — the public pool changed without the monitor being told"
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

# 3b) identity assertions ---------------------------------------------------
# l_ver/f_ver were being written into the summary and never tested, and p_ver was
# discarded outright -- data collected, assertion never written. Same defect class
# as `--since` computing a window it then does not filter on.
NOW["ver_legacy"]="$l_ver"; NOW["ver_fc"]="$f_ver"

# The edge must be serving the pool we think it is serving -- compared against the pool
# named by MON_EXPECTED_PUBLIC_ARCH, not always legacy. This assertion shipped hard-coded to
# the legacy pool and started false-alarming the moment the edge moved to fc: the same
# assumption, in the check written to catch that assumption. Declared intent, one place.
case "$EXPECTED_PUBLIC_ARCH" in
  fc_loop) _exp_code="$f_code"; _exp_ver="$f_ver" ;;
  *)       _exp_code="$l_code"; _exp_ver="$l_ver" ;;
esac
if [ "$p_code" = "200" ] && [ "$_exp_code" = "200" ] && [ "$p_ver" != "$_exp_ver" ]; then
  emit_alert 3 "public edge version '$p_ver' != $EXPECTED_PUBLIC_ARCH pool version '$_exp_ver' — the edge is not serving the pool it should be"
fi

# An unannounced version change means something redeployed without an operator.
for pool in legacy fc; do
  case "$pool" in legacy) cur="$l_ver"; sev=3 ;; fc) cur="$f_ver"; sev=4 ;; esac
  prev="${PREV[ver_$pool]:-}"
  if [ -n "$prev" ] && [ "$prev" != "none" ] && [ -n "$cur" ] && [ "$cur" != "none" ] && [ "$prev" != "$cur" ]; then
    emit_alert "$sev" "$pool pool version changed $prev -> $cur since the last run (unannounced redeploy?)"
  fi
done

# The fc pool must be running the image the compose .env pins. A mismatch means the
# container is not the candidate anyone thinks is under test.
if [ "$f_code" = "200" ] && [ -r "$REPO/.env" ]; then
  pinned="$(sed -n 's/^FC_CANARY_SHA=//p' "$REPO/.env" | tr -d '\r\"' | head -1)"
  if [ -n "$pinned" ] && [ "$f_ver" != "none" ] && [ "$f_ver" != "$pinned" ]; then
    emit_alert 3 "fc pool serves $f_ver but .env pins FC_CANARY_SHA=$pinned"
  fi
fi

# The legacy pool still answers 'unknown', but NOT because the knob is missing any more.
# CORRECTED 2026-07-27: this comment used to say "APP_CANDIDATE_SHA is unset for the 'app'
# service". That stopped being true when the `app` service was wired with
#   APP_CANDIDATE_SHA: "${LEGACY_APP_SHA:-}"
# (docker-compose.yml; DEFAULTING `:-`, deliberately not `:?` like app-fc, so a missing value
# can never block the rollback target from starting). The wiring is INERT today for two
# independent reasons, and both must clear before the pool can name its commit:
#   1. LEGACY_APP_SHA is not set in the root .env, so the container gets "", which app.py
#      reads as indistinguishable from unset and falls through to the git probe -> "unknown";
#   2. even once it IS set, the running `app` container predates the wiring, and ops
#      deliberately does not recreate it — it is the standing escape hatch while fc_loop
#      holds the public edge. It picks the value up at the next PLANNED rebuild.
# The ALERT LOGIC below is unchanged and still correct: it fires on the OBSERVED header value
# ('unknown'/'none'), which is the thing that actually blocks `switch_pool.sh --to legacy`,
# not on the configuration that produces it. Warn once per state change, not every five
# minutes. Only the explanation was wrong.
if [ "$l_code" = "200" ] && { [ "$l_ver" = "unknown" ] || [ "$l_ver" = "none" ]; } \
   && [ "${PREV[ver_legacy]:-}" != "$l_ver" ]; then
  emit_alert 4 "legacy pool cannot state its commit (x-agent-version: $l_ver) — set LEGACY_APP_SHA in the root .env and recreate 'app' at the next planned rebuild"
fi

# Symmetry with the fc pin check above: once the legacy pool CAN name its commit, it must be
# the commit the compose .env pins, or the rollback target is not the image anyone believes
# it is. Added 2026-07-27; deliberately INERT in today's steady state, which is exactly one
# genuine alert (canary-legacy.jsonl missing, because the legacy pool serves no traffic and
# therefore writes no telemetry). It cannot fire today for two independent reasons:
#   * LEGACY_APP_SHA is absent from the root .env, so $pinned is empty; and
#   * l_ver is 'unknown', which is EXCLUDED below.
# That exclusion is not belt-and-braces, it is the contract: "the pool cannot state its
# commit" already has its own alert immediately above, and this check must not double-report
# it. In particular, setting LEGACY_APP_SHA WITHOUT recreating the container — the documented
# intermediate state — leaves l_ver at 'unknown' and must stay silent here. This fires only on
# the genuinely new failure: a legacy pool that names a REAL sha which is not the pinned one.
if [ "$l_code" = "200" ] && [ -r "$REPO/.env" ]; then
  l_pinned="$(sed -n 's/^LEGACY_APP_SHA=//p' "$REPO/.env" | tr -d '\r\"' | head -1)"
  if [ -n "$l_pinned" ] && [ "$l_ver" != "none" ] && [ "$l_ver" != "unknown" ] \
     && [ "$l_ver" != "$l_pinned" ]; then
    emit_alert 3 "legacy pool serves $l_ver but .env pins LEGACY_APP_SHA=$l_pinned"
  fi
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
  delta=$(( lines - prevl ))
  summary+="$name=${lines}(+${delta})@${mtime:-?} "

  # Deliberately NOT alerting on zero growth. The site sees ~2 conversations/day
  # against 288 runs/day, so "no traffic" and "broken" are indistinguishable in
  # telemetry growth -- an alert here would fire almost every run. Check 10 is what
  # covers the broken case, by generating its own signal instead of waiting for one.
  #
  # A DECREASE is unambiguous and is the real failure: on 2026-07-25 a log was moved
  # out from under the running container, which kept appending to the moved inode
  # while .runtime/logs/ sat empty. Records were being written where nothing would
  # aggregate them, and nothing would have said so.
  if [ "$delta" -lt 0 ]; then
    emit_alert 3 "telemetry log $name.jsonl SHRANK by $(( -delta )) lines ($prevl -> $lines) — rotated, truncated, or moved out from under the running pool"
  fi
done

# 10) OUT-OF-BAND PROVIDER PROBE --------------------------------------------
# The 2026-07-24 class: a retired model name makes the provider reject every real
# question while the process stays healthy, never restarts, and answers /health
# 200. Checks 1-9 are all blind to it -- see the header.
#
# Two rules make this probe worth having, and getting either wrong makes it
# theatre:
#
#   1. The model name is resolved FROM THE LIVE CONTAINER, through the same
#      import the serving process uses -- never from the repo. The incident WAS a
#      divergence between container env and repo, so a repo-reading probe reports
#      green all day. This mirrors the pattern the compose file already uses for
#      searxng, whose healthcheck verifies the JSON API is enabled rather than
#      just that the process is up.
#   2. It calls the provider DIRECTLY. Routing synthetic traffic through /api/*
#      would write rows into .runtime/logs/canary-<arch>.jsonl -- the very
#      population the gate reads -- and the sink is fixed at process start, so a
#      request cannot redirect its own telemetry. 24 synthetic turns/day would
#      invalidate every future p50 and pass-rate. Not negotiable.
#
# Cost: one completion with max_tokens=1, at most hourly. At the rates derived
# from the 2026-07-25 round (uncached input $0.139/M) this is far below a cent
# per month.
PROVIDER_PROBE_EVERY_S="${MON_PROVIDER_PROBE_EVERY_S:-3600}"
PROVIDER_PROBE_CONTAINER="${MON_PROVIDER_PROBE_CONTAINER:-uk-rent-app}"
now_s="$(date +%s)"
last_probe="${PREV[provider_probe_at]:-0}"
NOW["provider_probe_at"]="$last_probe"

if [ "$PROVIDER_PROBE_EVERY_S" -gt 0 ] 2>/dev/null \
   && [ $(( now_s - last_probe )) -ge "$PROVIDER_PROBE_EVERY_S" ]; then
  NOW["provider_probe_at"]="$now_s"
  probe_out="$(timeout 60 docker exec -i "$PROVIDER_PROBE_CONTAINER" python - <<'PYPROBE' 2>&1
import json, os, sys, urllib.error, urllib.request
try:
    # The same import the serving process performs: whatever it resolves here is
    # what real questions will be sent with.
    from app.config import DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
except Exception as e:
    print("RESOLVE_FAIL " + type(e).__name__ + ": " + str(e)[:160]); sys.exit(0)

key = os.getenv("DEEPSEEK_API_KEY") or ""
if not key:
    print("RESOLVE_FAIL no DEEPSEEK_API_KEY in the container environment"); sys.exit(0)

req = urllib.request.Request(
    str(DEEPSEEK_BASE_URL).rstrip("/") + "/chat/completions",
    data=json.dumps({"model": DEEPSEEK_MODEL,
                     "messages": [{"role": "user", "content": "ping"}],
                     "max_tokens": 1}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print("OK %s %s" % (r.status, DEEPSEEK_MODEL))
except urllib.error.HTTPError as e:
    # The body names the cause -- on 2026-07-24 it was "The supported API model
    # names are deepseek-v4-pro or deepseek-v4-flash, but you passed deepseek-chat".
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:240].replace("\n", " ")
    except Exception:
        pass
    print("HTTP %s %s %s" % (e.code, DEEPSEEK_MODEL, body))
except Exception as e:
    print("NETFAIL %s %s" % (DEEPSEEK_MODEL, type(e).__name__))
PYPROBE
)"
  # Never let a key reach the log, whatever the provider echoes back.
  probe_out="$(printf '%s' "$probe_out" | sed -E 's/(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+/\1<redacted>/g' | tr '\n' ' ')"
  case "$probe_out" in
    OK\ 200*)
      summary+="provider=ok "
      ;;
    HTTP\ 429*)
      # Rate limiting is load, not misconfiguration. Paging on it would train the
      # operator to ignore this check -- which is how a real 400 gets missed.
      emit_alert 4 "provider rate-limited the probe (transient): $probe_out"
      summary+="provider=throttled "
      ;;
    HTTP\ 4*)
      # Any OTHER 4xx on a one-token ping is a configuration fault, not load. This
      # is the 07-24 signature and it is the whole reason this check exists.
      emit_alert 3 "PROVIDER REJECTS THE CONFIGURED MODEL — real questions are failing while /health stays 200: $probe_out"
      summary+="provider=REJECTED "
      ;;
    HTTP\ 5*|NETFAIL*)
      emit_alert 4 "provider probe could not complete (transient?): $probe_out"
      summary+="provider=unreachable "
      ;;
    RESOLVE_FAIL*)
      emit_alert 3 "cannot resolve the model the live container would use: $probe_out"
      summary+="provider=unresolved "
      ;;
    *)
      emit_alert 4 "provider probe returned an unrecognised result: ${probe_out:-<empty>}"
      summary+="provider=? "
      ;;
  esac

  # CLOSURE: the name `from app.config import DEEPSEEK_MODEL` resolves and the name the
  # client actually put on the wire are two different things -- a per-request override or
  # a fallback would leave the probe testing a model nobody uses. Layer B (a888160) now
  # records the model of every real call in llm_usage.models, so the two can be tied
  # together. Disagreement is itself the alert.
  probe_model="$(printf '%s' "$probe_out" | awk '{print $3}')"
  case "$PROVIDER_PROBE_CONTAINER" in
    *-fc) probe_arch="fc_loop" ;;
    *)    probe_arch="legacy"  ;;
  esac
  obs_log="$RUNTIME/logs/canary-$probe_arch.jsonl"
  if [ -n "$probe_model" ] && [ -r "$obs_log" ]; then
    observed="$(tac "$obs_log" 2>/dev/null | python3 -c '
import json, sys
for line in sys.stdin:                      # newest first
    try:
        models = ((json.loads(line).get("llm_usage") or {}).get("models") or {})
    except Exception:
        continue
    if models:
        print(",".join(sorted(models))); break
' 2>/dev/null)"
    # No observed call yet is not a failure -- at ~2 conversations/day that is the norm.
    if [ -n "$observed" ] && [ "$observed" != "$probe_model" ]; then
      emit_alert 3 "probe tests '$probe_model' but the last real $probe_arch call used '$observed' — the probe is validating a model the app does not send"
      summary+="probe_model_mismatch "
    fi
  fi
fi

# --- persist state ----------------------------------------------------------
{ for k in "${!NOW[@]}"; do printf '%s %s\n' "$k" "${NOW[$k]}"; done; } > "$STATE" 2>/dev/null || true

# --- always append one status line ------------------------------------------
status="OK"; [ "$alerts" -gt 0 ] && status="ALERTS=$alerts"
printf '%s %s %s\n' "$ts" "$status" "$summary" >> "$LOG_FILE" 2>/dev/null || true
exit 0
