#!/usr/bin/env bash
# Set the public candidate cohort to exactly 0, 5, 20, 50 or 100 percent.
#
# The generated nginx include hashes a stable key in this order:
#   1. the opaque Flask `session` cookie (hashed, not signature-validated by nginx);
#   2. X-Conversation-ID for cookie-less API clients;
#   3. remote address + user-agent for the first/bootstrap request.
#
# Safety contract:
#   * 0 is the production default and emergency rollback;
#   * both required pools pass /ready identity checks before exposure;
#   * manager_v1 candidates require specialists=1 and MCP=0;
#   * the route file is replaced with a same-directory atomic rename;
#   * nginx -t runs before reload; every later failure restores the old file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${CANARY_REPO_DIR:-$(cd "$HERE/.." && pwd)}"
ENV_FILE="${CANARY_ENV_FILE:-$REPO/.env}"
ROUTE_CONF="${CANARY_ROUTE_CONF:-/etc/nginx/snippets/rentcompass-canary-routing.conf}"
TEST_CMD="${CANARY_TEST_CMD:-sudo nginx -t}"
RELOAD_CMD="${CANARY_RELOAD_CMD:-sudo systemctl reload nginx}"
WRITE_CMD="${CANARY_WRITE_CMD:-sudo tee}"
MOVE_CMD="${CANARY_MOVE_CMD:-sudo mv}"
CURL_CMD="${CANARY_CURL_CMD:-curl}"
CURL_OPTS="${CANARY_CURL_OPTS:--sk}"
PUBLIC_URL="${CANARY_PUBLIC_URL:-https://127.0.0.1/ready}"
LEGACY_URL="${CANARY_LEGACY_URL:-http://127.0.0.1:5001/ready}"
CANDIDATE_URL="${CANARY_CANDIDATE_URL:-http://127.0.0.1:5002/ready}"
PROBE_COUNT="${CANARY_PROBE_COUNT:-256}"

die()  { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m ok \033[0m %s\n' "$*"; }
note() { printf '     %s\n' "$*"; }

env_value() { # key default; process env wins, then root .env, then default
  local key="$1" fallback="$2" value=""
  if [[ -n "${!key+x}" ]]; then
    value="${!key}"
  elif [[ -r "$ENV_FILE" ]]; then
    value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1 | tr -d '\r')"
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

CANDIDATE_ARCH="$(env_value CANARY_AGENT_ARCH fc_loop)"
CANDIDATE_SPECIALISTS_RAW="$(env_value CANARY_MANAGER_V1_SPECIALISTS 0)"
CANDIDATE_MCP_RAW="$(env_value CANARY_USE_MCP_TOOLS 0)"
CANDIDATE_SPECIALISTS="$(bool01 "$CANDIDATE_SPECIALISTS_RAW")" \
  || die "CANARY_MANAGER_V1_SPECIALISTS must be a boolean"
CANDIDATE_MCP="$(bool01 "$CANDIDATE_MCP_RAW")" \
  || die "CANARY_USE_MCP_TOOLS must be a boolean"
CANDIDATE_SHA="$(env_value FC_CANARY_SHA '')"
LEGACY_SHA="$(env_value LEGACY_APP_SHA '')"
COHORT_SALT="$(env_value CANARY_COHORT_SALT rentcompass-v1)"

case "$CANDIDATE_ARCH" in
  fc_loop)
    [[ "$CANDIDATE_SPECIALISTS" == 0 ]] \
      || die "fc_loop candidate requires CANARY_MANAGER_V1_SPECIALISTS=0"
    [[ "$CANDIDATE_MCP" == 0 ]] \
      || die "fc_loop public rollout requires CANARY_USE_MCP_TOOLS=0"
    ;;
  manager_v1)
    [[ "$CANDIDATE_SPECIALISTS" == 1 ]] \
      || die "manager_v1 rollout requires CANARY_MANAGER_V1_SPECIALISTS=1 (refusing compatibility-shell canary)"
    [[ "$CANDIDATE_MCP" == 0 ]] \
      || die "manager_v1 specialists require CANARY_USE_MCP_TOOLS=0"
    ;;
  *) die "CANARY_AGENT_ARCH must be fc_loop or manager_v1 (got '$CANDIDATE_ARCH')" ;;
esac
[[ "$COHORT_SALT" =~ ^[A-Za-z0-9._:-]{1,64}$ ]] \
  || die "CANARY_COHORT_SALT must use 1-64 safe characters [A-Za-z0-9._:-]"
[[ "$PROBE_COUNT" =~ ^[1-9][0-9]*$ ]] || die "CANARY_PROBE_COUNT must be positive"

WEIGHT=""; STATUS_ONLY=0; ALLOW_PUBLIC=0; ALLOW_UNIDENTIFIED=0; EXPECT_SHA=""
ROLLOUT_ID="${CANARY_ROLLOUT_ID:-}"; ROLLOUT_STAGE="${CANARY_ROLLOUT_STAGE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --weight) WEIGHT="${2:-}"; shift 2 ;;
    --status) STATUS_ONLY=1; shift ;;
    --expect-sha) EXPECT_SHA="${2:-}"; shift 2 ;;
    --rollout-id) ROLLOUT_ID="${2:-}"; shift 2 ;;
    --stage) ROLLOUT_STAGE="${2:-}"; shift 2 ;;
    --allow-public-candidate|--allow-public-fc) ALLOW_PUBLIC=1; shift ;;
    --allow-unidentified-target) ALLOW_UNIDENTIFIED=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

current_weight() {
  sed -n 's/^# rentcompass-canary-weight: \([0-9][0-9]*\)$/\1/p' "$ROUTE_CONF" 2>/dev/null | head -1
}

identity_at() { # URL [synthetic-session-cookie] -> "arch sha specialists pool"
  local url="$1" cookie="${2:-}" headers arch sha specialists pool
  local -a opts=()
  read -r -a opts <<<"$CURL_OPTS"
  if [[ -n "$cookie" ]]; then
    headers="$($CURL_CMD "${opts[@]}" -H "Cookie: session=$cookie" -D- -o /dev/null --max-time 10 "$url" 2>/dev/null)" || return 1
  else
    headers="$($CURL_CMD "${opts[@]}" -D- -o /dev/null --max-time 10 "$url" 2>/dev/null)" || return 1
  fi
  grep -qi '^HTTP/[0-9.]* 200' <<<"$headers" || return 1
  arch="$(grep -i '^x-agent-arch:' <<<"$headers" | tr -d '\r' | awk '{print $2}' | head -1)"
  sha="$(grep -i '^x-agent-version:' <<<"$headers" | tr -d '\r' | awk '{print $2}' | head -1)"
  specialists="$(grep -i '^x-agent-specialists:' <<<"$headers" | tr -d '\r' | awk '{print $2}' | head -1)"
  pool="$(grep -i '^x-rentcompass-pool:' <<<"$headers" | tr -d '\r' | awk '{print $2}' | head -1)"
  printf '%s %s %s %s\n' "${arch:-none}" "${sha:-none}" "${specialists:-none}" "${pool:-none}"
}

status() {
  printf 'route file     %s\n' "$ROUTE_CONF"
  printf 'weight         %s%%\n' "$(current_weight || echo unknown)"
  printf 'candidate      arch=%s specialists=%s mcp=%s\n' \
    "$CANDIDATE_ARCH" "$CANDIDATE_SPECIALISTS" "$CANDIDATE_MCP"
  printf 'legacy local   %s\n' "$(identity_at "$LEGACY_URL" || echo '<unreachable>')"
  printf 'candidate local %s\n' "$(identity_at "$CANDIDATE_URL" || echo '<unreachable>')"
}

if [[ "$STATUS_ONLY" == 1 ]]; then status; exit 0; fi
case "$WEIGHT" in 0|5|20|50|100) ;; *) die "usage: $0 --weight <0|5|20|50|100> | --status" ;; esac
[[ -r "$ROUTE_CONF" ]] || die "route include is missing/unreadable: $ROUTE_CONF"

case "$WEIGHT" in
  0) _default_stage=rollback ;;
  5) _default_stage=c1 ;;
  20) _default_stage=c2 ;;
  50) _default_stage=c3 ;;
  100) _default_stage=flip ;;
esac
ROLLOUT_STAGE="${ROLLOUT_STAGE:-$_default_stage}"
if [[ "$WEIGHT" == 0 ]]; then
  ROLLOUT_ID="${ROLLOUT_ID:-rollback}"
else
  [[ -n "$ROLLOUT_ID" ]] \
    || die "non-zero candidate exposure requires --rollout-id (stage telemetry must not mix)"
fi
[[ "$ROLLOUT_ID" =~ ^[A-Za-z0-9._:-]{1,96}$ ]] \
  || die "rollout id must use 1-96 safe characters [A-Za-z0-9._:-]"
[[ "$ROLLOUT_STAGE" =~ ^[A-Za-z0-9._:-]{1,32}$ ]] \
  || die "rollout stage must use 1-32 safe characters [A-Za-z0-9._:-]"
if [[ "$WEIGHT" != 0 && "$ALLOW_UNIDENTIFIED" == 1 ]]; then
  die "--allow-unidentified-target is restricted to the weight-0 emergency rollback"
fi

if [[ "$ROUTE_CONF" == /etc/nginx/* && "$WEIGHT" != 0 && "$ALLOW_PUBLIC" != 1 ]]; then
  die "refusing public candidate weight $WEIGHT without --allow-public-candidate"
fi

if [[ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" != 1 ]]; then
  DEPLOY_LOCK_FILE="${RENTCOMPASS_DEPLOY_LOCK_FILE:-}"
  if [[ -z "$DEPLOY_LOCK_FILE" ]]; then
    GIT_COMMON_DIR="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)" \
      || die "cannot resolve shared git metadata directory"
    DEPLOY_LOCK_FILE="$GIT_COMMON_DIR/rentcompass-deploy.lock"
  fi
  mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
  exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open deploy lock: $DEPLOY_LOCK_FILE"
  flock -n 9 || die "another release/update/switch/retirement operation is running"
  export RENTCOMPASS_DEPLOY_LOCK_HELD=1
fi

verify_local() { # label url want_arch want_specialists configured_sha allow_unknown
  local label="$1" url="$2" want_arch="$3" want_specialists="$4" configured_sha="$5" allow_unknown="$6"
  local got arch sha specialists pool
  got="$(identity_at "$url")" || die "$label pool is not ready at $url; routing unchanged"
  read -r arch sha specialists pool <<<"$got"
  [[ "$arch" == "$want_arch" ]] || die "$label pool reports arch '$arch', expected '$want_arch'; routing unchanged"
  [[ "$specialists" == "$want_specialists" ]] \
    || { [[ "$label" == legacy && "$specialists" == none && "$want_specialists" == 0 ]] \
      || die "$label pool reports specialists='$specialists', expected '$want_specialists'; routing unchanged"; }
  if [[ "$configured_sha" =~ ^[0-9a-f]{40}$ ]]; then
    [[ "$sha" == "$configured_sha" ]] \
      || die "$label pool sha '$sha' != configured pin '$configured_sha'; routing unchanged"
  elif [[ "$allow_unknown" != 1 ]]; then
    die "$label pool has no full configured sha; pass --allow-unidentified-target only for emergency legacy rollback"
  else
    note "WARNING: $label has no full configured SHA; architecture/readiness verified for emergency rollback"
  fi
  if [[ -n "$EXPECT_SHA" ]]; then
    [[ "$sha" == "$EXPECT_SHA" ]] || die "$label pool sha '$sha' != --expect-sha '$EXPECT_SHA'; routing unchanged"
  fi
  ok "$label ready: arch=$arch sha=$sha specialists=${specialists/none/0}"
}

# 0% is the emergency path: only the rollback pool is required.  Any non-zero
# exposure requires both pools so a rollback remains immediately available.
_legacy_allow_unknown="$ALLOW_UNIDENTIFIED"
[[ "$WEIGHT" == 0 ]] && _legacy_allow_unknown=1
verify_local legacy "$LEGACY_URL" legacy 0 "$LEGACY_SHA" "$_legacy_allow_unknown"
if [[ "$WEIGHT" != 0 ]]; then
  verify_local candidate "$CANDIDATE_URL" "$CANDIDATE_ARCH" \
    "$CANDIDATE_SPECIALISTS" "$CANDIDATE_SHA" 0
fi

render_route() {
  cat <<EOF
# RentCompass weighted candidate routing.  Managed by deploy/set_canary_weight.sh.
# rentcompass-canary-weight: $WEIGHT
# rentcompass-canary-salt: $COHORT_SALT
# rentcompass-rollout-id: $ROLLOUT_ID
# rentcompass-rollout-stage: $ROLLOUT_STAGE

log_format rentcompass_canary escape=json
    '{"ts":"\$time_iso8601","request_id":"\$request_id",'
    '"rollout_id":"\$rentcompass_rollout_id","stage":"\$rentcompass_rollout_stage",'
    '"assigned_pool":"\$rentcompass_pool","configured_weight":\$rentcompass_candidate_weight,'
    '"status":\$status,"method":"\$request_method","uri":"\$uri"}';

map \$cookie_session \$rentcompass_session_cohort_key {
    ""      "";
    default "session:\$cookie_session";
}

map \$http_x_conversation_id \$rentcompass_conversation_cohort_key {
    ""      "fallback:\$binary_remote_addr:\$http_user_agent";
    default "conversation:\$http_x_conversation_id";
}

map \$rentcompass_session_cohort_key \$rentcompass_cohort_key {
    ""      \$rentcompass_conversation_cohort_key;
    default \$rentcompass_session_cohort_key;
}
EOF
  if [[ "$WEIGHT" == 0 || "$WEIGHT" == 100 ]]; then
    local fixed=legacy; [[ "$WEIGHT" == 100 ]] && fixed=candidate
    cat <<EOF

map "" \$rentcompass_pool {
    default $fixed;
}
EOF
  else
    cat <<EOF

# split_clients uses MurmurHash2; keeping the salt and first bucket fixed makes
# 5% a strict subset of 20%, and 20% a subset of 50%.
split_clients "$COHORT_SALT|\${rentcompass_cohort_key}" \$rentcompass_pool {
    $WEIGHT% candidate;
    *        legacy;
}
EOF
  fi
  cat <<EOF

map "" \$rentcompass_candidate_weight {
    default $WEIGHT;
}

map "" \$rentcompass_rollout_id {
    default $ROLLOUT_ID;
}

map "" \$rentcompass_rollout_stage {
    default $ROLLOUT_STAGE;
}
EOF
  cat <<'EOF'

map $rentcompass_pool $rentcompass_backend_port {
    legacy    5001;
    candidate 5002;
}
EOF
}

TMP="$(mktemp)"; BACKUP="$(mktemp /tmp/rentcompass-canary-route.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
render_route > "$TMP"
cp "$ROUTE_CONF" "$BACKUP"

atomic_install() { # source -> ROUTE_CONF, same-directory rename
  local source="$1" stage="${ROUTE_CONF}.new.$$"
  $WRITE_CMD "$stage" < "$source" >/dev/null || return 1
  $MOVE_CMD "$stage" "$ROUTE_CONF" >/dev/null || return 1
}

restore() {
  note "restoring previous routing include from $BACKUP"
  atomic_install "$BACKUP" || die "RESTORE WRITE FAILED; backup remains at $BACKUP"
  $TEST_CMD >/dev/null 2>&1 || die "RESTORED CONFIG FAILS nginx -t; backup at $BACKUP"
  $RELOAD_CMD >/dev/null 2>&1 || die "RESTORED CONFIG RELOAD FAILED; backup at $BACKUP"
}

atomic_install "$TMP" || die "atomic route write failed; original remains at $BACKUP"
if ! $TEST_CMD >/dev/null 2>&1; then
  restore
  die "nginx -t rejected candidate weight $WEIGHT; previous route restored"
fi
ok "nginx -t passed for candidate weight $WEIGHT"
if ! $RELOAD_CMD >/dev/null 2>&1; then
  restore
  die "nginx reload failed; previous route restored"
fi

verify_public_sample() { # cookie -> selected pool; validates identity correspondence
  local cookie="$1" got arch sha specialists pool
  got="$(identity_at "$PUBLIC_URL" "$cookie")" || return 1
  read -r arch sha specialists pool <<<"$got"
  case "$pool" in
    legacy)
      [[ "$arch" == legacy && "$specialists" =~ ^(0|none)$ ]] || return 1
      [[ ! "$LEGACY_SHA" =~ ^[0-9a-f]{40}$ || "$sha" == "$LEGACY_SHA" ]] || return 1
      ;;
    candidate)
      [[ "$arch" == "$CANDIDATE_ARCH" && "$specialists" == "$CANDIDATE_SPECIALISTS" ]] || return 1
      [[ "$sha" == "$CANDIDATE_SHA" ]] || return 1
      ;;
    *) return 1 ;;
  esac
  printf '%s\n' "$pool"
}

seen_legacy=0; seen_candidate=0
if [[ "$WEIGHT" == 0 || "$WEIGHT" == 100 ]]; then
  selected="$(verify_public_sample rentcompass-canary-fixed-probe)" || {
    restore; die "public post-reload identity verification failed; previous route restored";
  }
  [[ "$WEIGHT" == 0 && "$selected" == legacy ]] \
    || [[ "$WEIGHT" == 100 && "$selected" == candidate ]] \
    || { restore; die "public route selected '$selected' at weight $WEIGHT; previous route restored"; }
else
  for ((i=0; i<PROBE_COUNT; i++)); do
    selected="$(verify_public_sample "rentcompass-canary-probe-$i" || true)"
    [[ "$selected" == legacy ]] && seen_legacy=1
    [[ "$selected" == candidate ]] && seen_candidate=1
    [[ "$seen_legacy" == 1 && "$seen_candidate" == 1 ]] && break
  done
  if [[ "$seen_legacy" != 1 || "$seen_candidate" != 1 ]]; then
    restore
    die "weighted route did not prove both cohorts in $PROBE_COUNT read-only /ready probes; previous route restored"
  fi
fi

rm -f "$BACKUP"
ok "candidate weight is now $WEIGHT% (arch=$CANDIDATE_ARCH specialists=$CANDIDATE_SPECIALISTS rollout=$ROLLOUT_ID/$ROLLOUT_STAGE)"
[[ "$WEIGHT" == 0 ]] && note "emergency rollback active: all new requests route to legacy"
exit 0
