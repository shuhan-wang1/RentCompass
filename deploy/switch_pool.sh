#!/usr/bin/env bash
# Compatibility pool switch / rollback lever for the public route.
#
# On a weighted install this is a 0/100 wrapper around set_canary_weight.sh.  The
# older single-upstream implementation below remains only so an emergency rollback
# still works while a host is being migrated to the weighted include.
#
#   switch_pool.sh --to legacy            # 127.0.0.1:5001
#   switch_pool.sh --to fc                # candidate 100% (needs --allow-public-fc,
#                                         #   plus --stage maintenance for a deploy
#                                         #   drain or CANARY_ALLOW_FLIP=1 for a
#                                         #   gated flip — see docs/canary_runbook.md)
#   switch_pool.sh --status
#
# Candidate identity comes from the root .env's CANARY_AGENT_ARCH /
# CANARY_MANAGER_V1_SPECIALISTS — the same pair update.sh, set_canary_weight.sh
# and the monitor read. SWITCH_CANDIDATE_ARCH / SWITCH_CANDIDATE_SPECIALISTS
# remain as an explicit override for rehearsals.
#
# Guarantees, in order:
#   1. only ports 5001/5002 are ever written;
#   2. the TARGET pool must answer /ready with the expected arch and specialist bit
#      BEFORE anything changes;
#   3. on production, the inactive target is restarted and re-probed before cutover;
#      this clears process-local hot session state so the target rehydrates from the
#      shared ConversationStore instead of assuming load-balancer stickiness;
#   4. only the `server 127.0.0.1:PORT;` line inside the upstream block is rewritten —
#      the rest of the file is byte-identical (the live conf has drifted from the repo
#      copy, e.g. client_max_body_size, and that drift must survive a switch);
#   5. `nginx -t` must pass before any reload;
#   6. after reload the public endpoint is re-verified for arch AND the full 40-char sha;
#   7. ANY failure after the write restores the backup, reloads, and re-verifies — the
#      old upstream is what survives a botched switch.
#
# Rehearsal: every external command is injectable, so the identical code path runs
# against a private nginx instance with no root and no public traffic. See
# deploy/switch_pool_rehearse.sh.
set -uo pipefail

CONF="${SWITCH_CONF:-/etc/nginx/sites-available/rentcompass.co.uk.conf}"
TEST_CMD="${SWITCH_TEST_CMD:-sudo nginx -t}"
RELOAD_CMD="${SWITCH_RELOAD_CMD:-sudo systemctl reload nginx}"
VERIFY_URL="${SWITCH_VERIFY_URL:-https://127.0.0.1/ready}"
CURL_OPTS="${SWITCH_CURL_OPTS:--sk}"
WRITE_CMD="${SWITCH_WRITE_CMD:-sudo tee}"          # reads new conf on stdin, writes to $1
HEALTH_FMT="${SWITCH_POOL_HEALTH_FMT:-http://127.0.0.1:%s/ready}"
REFRESH_CMD="${SWITCH_REFRESH_CMD:-docker restart}"
REFRESH_RETRIES="${SWITCH_REFRESH_RETRIES:-30}"
REFRESH_DELAY="${SWITCH_REFRESH_DELAY:-2}"
ROUTE_CONF="${SWITCH_ROUTE_CONF:-/etc/nginx/snippets/rentcompass-canary-routing.conf}"
WEIGHT_SCRIPT="${SWITCH_WEIGHT_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/set_canary_weight.sh}"
SWITCH_REPO_DIR="${SWITCH_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SWITCH_ENV_FILE="${SWITCH_ENV_FILE:-$SWITCH_REPO_DIR/.env}"

# --- candidate identity: ONE source, shared with the rest of the deploy path ---
# This used to read only SWITCH_CANDIDATE_ARCH / SWITCH_CANDIDATE_SPECIALISTS,
# variables nothing else in the repo sets or documents, while update.sh,
# set_canary_weight.sh and deploy/monitoring/rentcompass-monitor.sh all read
# CANARY_AGENT_ARCH / CANARY_MANAGER_V1_SPECIALISTS from the root .env. On a host
# whose .env selects manager_v1 that disagreement made `switch_pool.sh --to fc`
# fail on an arch mismatch against a pool that was in fact correct.
#
# The CANARY_* variables are now the source of truth. SWITCH_CANDIDATE_* remain
# as an explicit, documented override (rehearsals against a private nginx, and
# ad-hoc probes of a pool that is deliberately not what .env selects).
env_value() { # key default; process env wins, then root .env, then default
  local key="$1" fallback="$2" value=""
  if [[ -n "${!key+x}" ]]; then
    value="${!key}"
  elif [[ -r "$SWITCH_ENV_FILE" ]]; then
    value="$(sed -n "s/^${key}=//p" "$SWITCH_ENV_FILE" | tail -1 | tr -d '\r')"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
  fi
  printf '%s' "${value:-$fallback}"
}

bool01() {
  case "${1,,}" in
    1|true|yes|on) printf 1 ;;
    0|false|no|off|'') printf 0 ;;
    *) return 1 ;;
  esac
}

CANDIDATE_ARCH="${SWITCH_CANDIDATE_ARCH:-$(env_value CANARY_AGENT_ARCH fc_loop)}"
CANDIDATE_SPECIALISTS_RAW="${SWITCH_CANDIDATE_SPECIALISTS:-$(env_value CANARY_MANAGER_V1_SPECIALISTS 0)}"
if ! CANDIDATE_SPECIALISTS="$(bool01 "$CANDIDATE_SPECIALISTS_RAW")"; then
  die_early="candidate specialists switch must be a boolean (got '$CANDIDATE_SPECIALISTS_RAW')"
  CANDIDATE_SPECIALISTS=0
fi

case "$CANDIDATE_ARCH:$CANDIDATE_SPECIALISTS" in
  fc_loop:0|manager_v1:1) ;;
  *) die_early="${die_early:-candidate identity must be fc_loop/specialists=0 or manager_v1/specialists=1}" ;;
esac

UPSTREAM_BLOCK='upstream rentcompass_app'
declare -A PORT=( [legacy]=5001 [fc]=5002 )
declare -A ARCH=( [legacy]=legacy [fc]="$CANDIDATE_ARCH" )
declare -A SPECIALISTS=( [legacy]=0 [fc]="$CANDIDATE_SPECIALISTS" )
declare -A CONTAINER=( [legacy]=uk-rent-app [fc]=uk-rent-app-fc )

die() { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m ok \033[0m %s\n' "$*"; }
note(){ printf '     %s\n' "$*"; }

[[ -z "${die_early:-}" ]] || die "$die_early"

current_port() {
  awk "/^${UPSTREAM_BLOCK}[[:space:]]*\{/,/^\}/" "$CONF" \
    | sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' | head -1
}

pool_of_port() { case "$1" in 5001) echo legacy ;; 5002) echo fc ;; *) echo "unknown:$1" ;; esac; }

# The active-drain marker deploy/update.sh writes before it drains and removes
# after it restores; resolved exactly as set_canary_weight.sh resolves it, so both
# routing modes read one file.
maintenance_marker_path() {
  if [[ -n "${RENTCOMPASS_MAINTENANCE_MARKER:-}" ]]; then
    printf '%s' "$RENTCOMPASS_MAINTENANCE_MARKER"; return 0
  fi
  local common
  common="$(git -C "$SWITCH_REPO_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || return 1
  [[ -n "$common" ]] || return 1
  printf '%s/rentcompass-maintenance-drain' "$common"
}

# Identity of whatever is answering a URL: "<arch> <sha> <specialists>", or
# empty on failure.  A missing specialist header is deliberately not guessed.
identity_at() {
  local url="$1" hdrs
  hdrs=$(curl $CURL_OPTS -D- -o /dev/null --max-time 10 "$url" 2>/dev/null) || return 1
  grep -qi '^HTTP/[0-9.]* 200' <<<"$hdrs" || return 1
  printf '%s %s %s\n' \
    "$(grep -i '^x-agent-arch:'    <<<"$hdrs" | tr -d '\r' | awk '{print $2}')" \
    "$(grep -i '^x-agent-version:' <<<"$hdrs" | tr -d '\r' | awk '{print $2}')" \
    "$(grep -i '^x-agent-specialists:' <<<"$hdrs" | tr -d '\r' | awk '{print $2}')"
}

verify_public() { # $1 expected arch, $2 expected specialists
  local want="$1" want_specialists="$2" got arch sha specialists
  got=$(identity_at "$VERIFY_URL") || return 1
  read -r arch sha specialists <<<"$got"
  [[ "$arch" == "$want" ]] || { note "arch mismatch: want '$want', got '$arch'"; return 1; }
  [[ "$specialists" == "$want_specialists" ]] \
    || { note "specialists mismatch: want '$want_specialists', got '${specialists:-<absent>}'"; return 1; }
  if [[ ${#sha} -ne 40 && "${ALLOW_UNIDENTIFIED:-0}" -ne 1 ]]; then
    note "sha is not a full 40-char commit: '${sha:-<absent>}'"; return 1
  fi
  echo "$arch $sha specialists=$specialists"; return 0
}

write_upstream() {                     # $1 port — surgical, single-line substitution
  local port="$1" tmp; tmp=$(mktemp)
  awk -v port="$port" -v blk="$UPSTREAM_BLOCK" '
    $0 ~ "^" blk "[[:space:]]*\\{" { inblk=1 }
    inblk && /^[[:space:]]*server[[:space:]]+127\.0\.0\.1:[0-9]+;/ && !done {
      sub(/127\.0\.0\.1:[0-9]+/, "127.0.0.1:" port); done=1
    }
    inblk && /^\}/ { inblk=0 }
    { print }
  ' "$CONF" > "$tmp" || { rm -f "$tmp"; return 1; }
  grep -q "127.0.0.1:${port};" "$tmp" || { rm -f "$tmp"; note "substitution produced no ${port} line"; return 1; }
  # exactly one line may differ from the original: refuse anything broader
  local changed; changed=$(diff "$CONF" "$tmp" | grep -c '^[<>]')
  [[ "$changed" -eq 2 ]] || { rm -f "$tmp"; note "refusing: $((changed/2)) lines would change, expected 1"; return 1; }
  $WRITE_CMD "$CONF" < "$tmp" >/dev/null || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
}

status() {
  local p; p=$(current_port)
  printf 'conf          %s\n' "$CONF"
  printf 'upstream      127.0.0.1:%s  (%s)\n' "$p" "$(pool_of_port "$p")"
  local got; got=$(identity_at "$VERIFY_URL") && printf 'serving       %s\n' "$got" \
                                             || printf 'serving       <no 200 from %s>\n' "$VERIFY_URL"
  for pool in legacy fc; do
    got=$(identity_at "$(printf "$HEALTH_FMT" "${PORT[$pool]}")") \
      && printf 'pool %-6s   127.0.0.1:%s  %s\n' "$pool" "${PORT[$pool]}" "$got" \
      || printf 'pool %-6s   127.0.0.1:%s  <unreachable>\n' "$pool" "${PORT[$pool]}"
  done
}

TARGET=""; ALLOW_PUBLIC_FC=0; ALLOW_UNIDENTIFIED=0; EXPECT_SHA=""; STATUS_ONLY=0
ROLLOUT_ID=""; ROLLOUT_STAGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to) TARGET="${2:-}"; shift 2 ;;
    --allow-public-fc) ALLOW_PUBLIC_FC=1; shift ;;
    --expect-sha) EXPECT_SHA="${2:-}"; shift 2 ;;
    --rollout-id) ROLLOUT_ID="${2:-}"; shift 2 ;;
    --stage) ROLLOUT_STAGE="${2:-}"; shift 2 ;;
    --allow-unidentified-target) ALLOW_UNIDENTIFIED=1; shift ;;
    --status) STATUS_ONLY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
if [[ -r "$ROUTE_CONF" ]] && grep -q '^# rentcompass-canary-weight:' "$ROUTE_CONF"; then
  [[ -x "$WEIGHT_SCRIPT" ]] || die "weighted route is installed but controller is not executable: $WEIGHT_SCRIPT"
  if [[ "$STATUS_ONLY" == 1 ]]; then
    exec env CANARY_ROUTE_CONF="$ROUTE_CONF" "$WEIGHT_SCRIPT" --status
  fi
  [[ -n "$TARGET" ]] || die "usage: $0 --to <legacy|fc> [--allow-public-fc] | --status"
  [[ "$TARGET" == legacy || "$TARGET" == fc ]] \
    || die "target must be 'legacy' or 'fc' (got '$TARGET')"
  _weight=0; [[ "$TARGET" == fc ]] && _weight=100
  _args=(--weight "$_weight")
  [[ "$ALLOW_PUBLIC_FC" == 1 ]] && _args+=(--allow-public-candidate)
  [[ "$ALLOW_UNIDENTIFIED" == 1 ]] && _args+=(--allow-unidentified-target)
  [[ -n "$EXPECT_SHA" ]] && _args+=(--expect-sha "$EXPECT_SHA")
  [[ -n "$ROLLOUT_ID" ]] && _args+=(--rollout-id "$ROLLOUT_ID")
  [[ -n "$ROLLOUT_STAGE" ]] && _args+=(--stage "$ROLLOUT_STAGE")
  exec env CANARY_ROUTE_CONF="$ROUTE_CONF" "$WEIGHT_SCRIPT" "${_args[@]}"
fi

[[ "$STATUS_ONLY" == 0 ]] || { status; exit 0; }
[[ -n "$TARGET" ]] || die "usage: $0 --to <legacy|fc> [--allow-public-fc] | --status"
[[ -n "${PORT[$TARGET]:-}" ]] || die "target must be 'legacy' or 'fc' (got '$TARGET')"

if [[ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" != 1 ]]; then
  DEPLOY_LOCK_FILE="${RENTCOMPASS_DEPLOY_LOCK_FILE:-}"
  if [[ -z "$DEPLOY_LOCK_FILE" ]]; then
    SWITCH_REPO_DIR="${SWITCH_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    GIT_COMMON_DIR="$(git -C "$SWITCH_REPO_DIR" rev-parse --path-format=absolute --git-common-dir)" \
      || die "cannot resolve the shared git metadata directory"
    DEPLOY_LOCK_FILE="$GIT_COMMON_DIR/rentcompass-deploy.lock"
  fi
  mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
  exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open deploy lock: $DEPLOY_LOCK_FILE"
  flock -n 9 || die "another release/update/switch/retirement operation is running"
  export RENTCOMPASS_DEPLOY_LOCK_HELD=1
fi

TO_PORT="${PORT[$TARGET]}"; WANT_ARCH="${ARCH[$TARGET]}"; WANT_SPECIALISTS="${SPECIALISTS[$TARGET]}"
[[ "$TO_PORT" == "5001" || "$TO_PORT" == "5002" ]] || die "refusing port $TO_PORT (only 5001/5002)"
[[ -r "$CONF" ]] || die "conf not readable: $CONF"

FROM_PORT=$(current_port)
[[ -n "$FROM_PORT" ]] || die "could not find a server line in the '$UPSTREAM_BLOCK' block"
if [[ "$FROM_PORT" == "$TO_PORT" ]]; then ok "already on $TARGET (127.0.0.1:$TO_PORT)"; exit 0; fi

# The candidate is at STAGE-PAUSE. Pointing public traffic at it is a policy decision,
# not a mechanical one, so it takes an explicit flag even when everything else is green.
if [[ "$TARGET" == "fc" && "$CONF" == /etc/nginx/* && "$ALLOW_PUBLIC_FC" -ne 1 ]]; then
  die "refusing to put candidate on the PUBLIC upstream without --allow-public-fc (candidate is at STAGE-PAUSE)"
fi

# --to fc on a single-upstream host is 100% of public traffic, i.e. the same
# decision set_canary_weight.sh gates on a weighted host. Keep one policy: a
# deploy drain says --stage maintenance and restores afterwards; a real cutover
# sets CANARY_ALLOW_FLIP=1 deliberately. Anything else is refused here too, so
# migrating a host between routing modes cannot change what a release may do.
if [[ "$TARGET" == "fc" && "$CONF" == /etc/nginx/* ]]; then
  case "${ROLLOUT_STAGE:-flip}" in
    maintenance)
      # Identical and-gates to set_canary_weight.sh's maintenance branch: the
      # deploy lock, update.sh's machine rollout-id shape, and the active-drain
      # marker update.sh writes before the drain and removes after the restore.
      # Without them `--stage maintenance` is a permanent, unlogged 100% flip that
      # never has to meet CANARY_ALLOW_FLIP, on the one host shape where nothing
      # else gates it either.
      [[ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" == 1 ]] \
        || die "stage 'maintenance' is machine-only: it is the drain deploy/update.sh takes, and this caller does not hold the deploy lock (docs/canary_runbook.md section 2)"
      [[ "$ROLLOUT_ID" =~ ^deploy-maintenance-[0-9a-f]{7,}$ ]] \
        || die "stage 'maintenance' requires --rollout-id deploy-maintenance-<sha> (got '${ROLLOUT_ID:-<none>}')"
      MAINTENANCE_MARKER="$(maintenance_marker_path)" \
        || die "stage 'maintenance' cannot resolve its marker path; set RENTCOMPASS_MAINTENANCE_MARKER"
      [[ -r "$MAINTENANCE_MARKER" ]] \
        || die "stage 'maintenance' requires an active drain marker at $MAINTENANCE_MARKER; deploy/update.sh writes it before the drain and removes it after the restore. A 100% cutover is '--stage flip' with CANARY_ALLOW_FLIP=1 (docs/canary_runbook.md section 2)"
      [[ "$(cat "$MAINTENANCE_MARKER" 2>/dev/null || true)" == "$ROLLOUT_ID" ]] \
        || die "the drain marker at $MAINTENANCE_MARKER names a different rollout id than '$ROLLOUT_ID'; refusing a 100% exposure no running deploy asked for"
      note "stage 'maintenance': temporary drain onto the candidate; the caller must restore the previous upstream"
      ;;
    flip)
      [[ "${CANARY_ALLOW_FLIP:-0}" == 1 ]] \
        || die "refusing 100% public candidate traffic without CANARY_ALLOW_FLIP=1; 50% is the highest authorised rollout stage (docs/canary_runbook.md section 2)"
      ;;
    *) die "refusing 100% public candidate traffic at stage '$ROLLOUT_STAGE'; use --stage maintenance (deploy drain) or --stage flip with CANARY_ALLOW_FLIP=1 (docs/canary_runbook.md section 2)" ;;
  esac
fi

if [[ "$TARGET" == fc && "$CANDIDATE_ARCH" != fc_loop ]]; then
  die "candidate arch '$CANDIDATE_ARCH' requires the weighted routing include; refusing legacy single-upstream switch"
fi

note "switching $(pool_of_port "$FROM_PORT") (:$FROM_PORT) -> $TARGET (:$TO_PORT)"

# --- 2. target must be healthy BEFORE anything is touched --------------------------
TARGET_URL="$(printf "$HEALTH_FMT" "$TO_PORT")"
TARGET_ID=$(identity_at "$TARGET_URL") \
  || die "target pool 127.0.0.1:$TO_PORT is not answering /ready with 200 — nothing changed"
read -r t_arch t_sha t_specialists <<<"$TARGET_ID"
[[ "$t_arch" == "$WANT_ARCH" ]] || die "target reports arch '$t_arch', expected '$WANT_ARCH' — nothing changed"
[[ "$t_specialists" == "$WANT_SPECIALISTS" ]] \
  || die "target reports specialists='${t_specialists:-<absent>}', expected '$WANT_SPECIALISTS' — nothing changed"

# The durable ConversationStore is shared, but SessionStore is process-local.
# Restarting the inactive target clears any old hot slice and forces its first
# post-cutover request to rehydrate from durable snapshots/history. Rehearsal
# configs outside /etc/nginx skip by default; SWITCH_REFRESH_TARGET=0/1 overrides.
REFRESH_TARGET="${SWITCH_REFRESH_TARGET:-auto}"
if [[ "$REFRESH_TARGET" == auto ]]; then
  [[ "$CONF" == /etc/nginx/* ]] && REFRESH_TARGET=1 || REFRESH_TARGET=0
fi
if [[ "$REFRESH_TARGET" == 1 ]]; then
  note "refreshing inactive target ${CONTAINER[$TARGET]} to clear process-local session state"
  $REFRESH_CMD "${CONTAINER[$TARGET]}" >/dev/null \
    || die "target refresh failed — public upstream was not changed"
  TARGET_ID=""; i=0
  while [[ "$i" -lt "$REFRESH_RETRIES" ]]; do
    TARGET_ID="$(identity_at "$TARGET_URL" || true)"
    [[ -n "$TARGET_ID" ]] && break
    i=$((i + 1)); sleep "$REFRESH_DELAY"
  done
  [[ -n "$TARGET_ID" ]] \
    || die "target did not become ready after refresh — public upstream was not changed"
  read -r t_arch t_sha t_specialists <<<"$TARGET_ID"
  [[ "$t_arch" == "$WANT_ARCH" ]] \
    || die "refreshed target reports arch '$t_arch', expected '$WANT_ARCH' — nothing changed"
  [[ "$t_specialists" == "$WANT_SPECIALISTS" ]] \
    || die "refreshed target reports specialists='${t_specialists:-<absent>}', expected '$WANT_SPECIALISTS' — nothing changed"
fi
# Provenance is directional. A FORWARD switch onto a pool that cannot name its commit is
# refused. A ROLLBACK onto one is allowed with --allow-unidentified-target, loudly: being
# unable to prove what you rolled back to beats staying on something known to be broken.
# The public legacy pool currently reports 'unknown'. The compose wiring now exists
# (`app`'s APP_CANDIDATE_SHA: "${LEGACY_APP_SHA:-}"), but it is INERT until that
# container is next recreated, and `app` must NOT be recreated while it is the standing
# escape hatch. So --allow-unidentified-target stays necessary for legacy rollback until
# the next PLANNED public rebuild sets LEGACY_APP_SHA in the root .env. Once it does,
# rollback passes the 40-char check on its own and this flag should stop being routine.
if [[ ${#t_sha} -ne 40 ]]; then
  if [[ "$ALLOW_UNIDENTIFIED" -eq 1 ]]; then
    printf '\033[33mWARN\033[0m target cannot state its commit (x-agent-version: %s) — proceeding under --allow-unidentified-target\n' "${t_sha:-<absent>}"
  else
    die "target reports a non-full sha '${t_sha:-<absent>}'; pass --allow-unidentified-target to switch anyway — nothing changed"
  fi
fi
if [[ -n "$EXPECT_SHA" && "$t_sha" != "$EXPECT_SHA" ]]; then
  die "target sha '$t_sha' != --expect-sha '$EXPECT_SHA' — nothing changed"
fi
ok "target healthy: $t_arch ${t_sha:-<unidentified>}"

BACKUP=$(mktemp /tmp/switch_pool.backup.XXXXXX); cp "$CONF" "$BACKUP"
note "backup: $BACKUP"

rollback() {
  printf '\033[33m  ->\033[0m restoring previous upstream (:%s)\n' "$FROM_PORT"
  $WRITE_CMD "$CONF" < "$BACKUP" >/dev/null || die "RESTORE WRITE FAILED — conf is $CONF, backup is $BACKUP"
  $TEST_CMD >/dev/null 2>&1 || die "RESTORED CONF FAILS nginx -t — manual intervention needed, backup at $BACKUP"
  $RELOAD_CMD >/dev/null 2>&1 || die "RESTORED CONF RELOAD FAILED — manual intervention needed, backup at $BACKUP"
  _from_pool="$(pool_of_port "$FROM_PORT")"
  if ALLOW_UNIDENTIFIED=1 verify_public "${ARCH[$_from_pool]}" "${SPECIALISTS[$_from_pool]}" >/dev/null; then
    ok "rolled back cleanly to $(pool_of_port "$FROM_PORT")"
  else
    die "ROLLED BACK BUT VERIFY FAILED — check $VERIFY_URL by hand, backup at $BACKUP"
  fi
}

write_upstream "$TO_PORT" || { note "write failed"; rollback; die "switch aborted at write"; }
ok "upstream line rewritten (1 line changed)"

if ! $TEST_CMD >/dev/null 2>&1; then note "nginx -t rejected the new conf"; rollback; die "switch aborted at nginx -t"; fi
ok "nginx -t passed"

if ! $RELOAD_CMD >/dev/null 2>&1; then note "reload failed"; rollback; die "switch aborted at reload"; fi
ok "reloaded"

sleep 1
if ! NEW_ID=$(verify_public "$WANT_ARCH" "$WANT_SPECIALISTS"); then
  note "post-switch verification failed"; rollback; die "switch aborted at verification"
fi
ok "serving $NEW_ID"
ok "switched to $TARGET"
