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
#   * both required pools also ANSWER a real turn before exposure (see
#     deploy/probe_pool_answer.py); --skip-answer-probe opts out explicitly;
#   * manager_v1 candidates require specialists=1 and MCP=0;
#   * 100 is NOT a routine weight.  docs/canary_runbook.md authorises 50 as the
#     highest rollout stage, so 100 is accepted only for `--stage maintenance`
#     (the temporary drain a pool update takes, and restores afterwards) or for
#     `--stage flip` with CANARY_ALLOW_FLIP=1 explicitly set;
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
# `-`, NOT `:-`, on every URL: an explicitly EMPTY override must stay empty and be
# refused below.  With `:-` an empty value silently resolved to the REAL live pools
# — the exact shape of the incident that motivated the CANARY_ANSWER_PROBE_CMD
# hardening, and the way two real billed turns were driven from a rehearsal.
PUBLIC_URL="${CANARY_PUBLIC_URL-https://127.0.0.1/ready}"
LEGACY_URL="${CANARY_LEGACY_URL-http://127.0.0.1:5001/ready}"
CANDIDATE_URL="${CANARY_CANDIDATE_URL-http://127.0.0.1:5002/ready}"
PROBE_COUNT="${CANARY_PROBE_COUNT:-256}"
# `-`, NOT `:-`: an explicitly EMPTY override must stay empty and be refused
# below. Falling back to the real probe there would silently drive a live turn
# against a real pool from a caller that meant to inject a stub.
ANSWER_PROBE_CMD="${CANARY_ANSWER_PROBE_CMD-python3 $HERE/probe_pool_answer.py}"
# Injecting the probe is how the harnesses rehearse this path without a pool.  It
# is also the only way to disable the gate without saying so, so the injection is
# remembered and announced on every use.
ANSWER_PROBE_INJECTED=0
if [[ -n "${CANARY_ANSWER_PROBE_CMD+x}" ]]; then
  ANSWER_PROBE_INJECTED=1
fi
LEGACY_ANSWER_URL="${CANARY_LEGACY_ANSWER_URL-http://127.0.0.1:5001}"
CANDIDATE_ANSWER_URL="${CANARY_CANDIDATE_ANSWER_URL-http://127.0.0.1:5002}"
ANSWER_PROBE_TIMEOUT="${CANARY_ANSWER_PROBE_TIMEOUT:-120}"
# The grounding substring the probe requires. It defaults to what
# deploy/probe_pool_answer.py has always defaulted to, so behaviour is unchanged —
# but it is now a knob rather than a constant buried in the probe: a citation
# format or localisation change should not silently block every weight increase,
# and an operator should be able to say so in one command. An explicitly EMPTY
# value keeps only the fallback-marker check (see the probe's --expect-substring).
ANSWER_PROBE_SUBSTRING="${CANARY_ANSWER_PROBE_SUBSTRING-data.police.uk}"

die()  { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m ok \033[0m %s\n' "$*"; }
note() { printf '     %s\n' "$*"; }

# An empty override would make `$ANSWER_PROBE_CMD --url ...` run `--url` as a
# command: a failure, but an incomprehensible one. Refuse it where it is set.
[[ "$ANSWER_PROBE_INJECTED" == 0 || -n "${ANSWER_PROBE_CMD// /}" ]] \
  || die "CANARY_ANSWER_PROBE_CMD is set but empty; unset it to use deploy/probe_pool_answer.py, or pass --skip-answer-probe to opt out explicitly"

# Same rule for every URL: an explicitly empty override is a caller mistake, and
# falling back to the real :5001/:5002 would answer it by talking to production.
for _u in CANARY_PUBLIC_URL:PUBLIC_URL CANARY_LEGACY_URL:LEGACY_URL \
          CANARY_CANDIDATE_URL:CANDIDATE_URL CANARY_LEGACY_ANSWER_URL:LEGACY_ANSWER_URL \
          CANARY_CANDIDATE_ANSWER_URL:CANDIDATE_ANSWER_URL; do
  _env="${_u%%:*}"; _var="${_u##*:}"
  [[ -z "${!_env+x}" || -n "${!_var// /}" ]] \
    || die "$_env is set but EMPTY; unset it to use the default, or give it a real URL — an empty override must never resolve to the live pools"
done
unset _u _env _var

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

# --- CANONICAL specialist-header rule (keep every copy byte-identical) -------
# X-Agent-Specialists is REQUIRED only when the EXPECTED identity has
# specialists=1.  When 0 is expected an ABSENT header (reported as '' or 'none')
# counts as 0: every image built before 2026-08-31 omits the header entirely,
# which is BOTH pools deployed on this host today, and the legacy pool is the
# standing rollback escape hatch that must not be recreated just to satisfy a
# header check.  Demanding the header for an expected 0 turned `--to legacy`
# (the emergency rollback) and update.sh's drain leg into hard failures.
# Copies: deploy/update.sh, deploy/set_canary_weight.sh, deploy/switch_pool.sh,
#         deploy/monitoring/rentcompass-monitor.sh
# Python twin: deploy/probe_pool_answer.py::specialists_match
specialists_ok() { # <observed> <expected>
  local observed="${1:-}" expected="${2:-}"
  if [ "$observed" = "$expected" ]; then return 0; fi
  if [ "$expected" = 0 ] && { [ -z "$observed" ] || [ "$observed" = none ]; }; then return 0; fi
  return 1
}
specialists_absent() { # <observed> -> 0 when the pool sent no specialist header
  [ -z "${1:-}" ] || [ "${1:-}" = none ]
}
specialists_shown() { # <observed> -> what to print for it in a message
  if specialists_absent "${1:-}"; then printf '<absent>'; else printf '%s' "$1"; fi
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
# The env door exists so deploy/update.sh and deploy/release.sh can reach the
# opt-out through deploy/switch_pool.sh without every layer growing a flag.
SKIP_ANSWER_PROBE="${CANARY_SKIP_ANSWER_PROBE:-0}"
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
    --skip-answer-probe) SKIP_ANSWER_PROBE=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

current_weight() {
  sed -n 's/^# rentcompass-canary-weight: \([0-9][0-9]*\)$/\1/p' "$ROUTE_CONF" 2>/dev/null | head -1
}

# The active-drain marker deploy/update.sh writes before it drains and removes
# after it restores.  It lives beside the deploy lock in the SHARED git metadata
# directory, so every worktree of this repo — and the sudo'd controller — resolve
# the same path without a second configuration knob.
maintenance_marker_path() {
  if [[ -n "${RENTCOMPASS_MAINTENANCE_MARKER:-}" ]]; then
    printf '%s' "$RENTCOMPASS_MAINTENANCE_MARKER"; return 0
  fi
  local common
  common="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || return 1
  [[ -n "$common" ]] || return 1
  printf '%s/rentcompass-maintenance-drain' "$common"
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

# The weight the route is on right now.  Read once, before anything is written,
# because whether this change RAISES or LOWERS exposure decides which gates apply.
CURRENT_WEIGHT="$(current_weight || true)"
DE_ESCALATION=0
if [[ "$WEIGHT" != 0 && "$CURRENT_WEIGHT" =~ ^[0-9]+$ && "$WEIGHT" -lt "$CURRENT_WEIGHT" ]]; then
  DE_ESCALATION=1
fi

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

# ---------------------------------------------------------------------------
# 100% policy stop
# ---------------------------------------------------------------------------
# docs/canary_runbook.md authorises 50% as the highest ROLLOUT stage: at 100%
# there are no live legacy turns, so the comparative gate has no control arm and
# HOLDs rather than clearing.  100 therefore has exactly two legitimate callers:
#
#   * `--stage maintenance` — the temporary drain deploy/update.sh takes while it
#     recreates the other pool.  update.sh records the pre-drain weight/stage from
#     the route include's own markers and restores them on success AND on failure.
#   * `--stage flip` with CANARY_ALLOW_FLIP=1 — the deliberate, separately gated
#     cutover, which a routine `deploy/release.sh` must never reach on its own.
#
# Every other stage keeps the documented {0,5,20,50} set.
if [[ "$WEIGHT" == 100 ]]; then
  case "$ROLLOUT_STAGE" in
    maintenance)
      # `maintenance` used to be an unconditional 100% with no TTL and no marker:
      # a human could type `--stage maintenance --rollout-id anything` and park the
      # candidate on all public traffic permanently, bypassing CANARY_ALLOW_FLIP.
      # Three and-gates make it what it claims to be — a drain a running deploy takes:
      #   1. the caller holds the deploy lock (only update/release/switch export it);
      #   2. the rollout id has update.sh's machine shape, so the drain's turns are
      #      filterable and can never be mistaken for a stage window;
      #   3. a marker file update.sh creates before the drain and deletes after the
      #      restore exists and names this exact rollout id.  It is the part a bare
      #      human invocation cannot fake from the environment alone, and it makes
      #      the authorisation expire with the deploy that opened it.
      [[ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" == 1 ]] \
        || die "stage 'maintenance' is machine-only: it is the drain deploy/update.sh takes while it recreates a pool, and this caller does not hold the deploy lock (docs/canary_runbook.md section 2)"
      [[ "$ROLLOUT_ID" =~ ^deploy-maintenance-[0-9a-f]{7,}$ ]] \
        || die "stage 'maintenance' requires --rollout-id deploy-maintenance-<sha> (got '$ROLLOUT_ID'); the drain's turns must be filterable out of every stage window"
      MAINTENANCE_MARKER="$(maintenance_marker_path)" \
        || die "stage 'maintenance' cannot resolve its marker path; set RENTCOMPASS_MAINTENANCE_MARKER"
      [[ -r "$MAINTENANCE_MARKER" ]] \
        || die "stage 'maintenance' requires an active drain marker at $MAINTENANCE_MARKER; deploy/update.sh writes it before the drain and removes it after the restore. A 100% cutover is '--stage flip' with CANARY_ALLOW_FLIP=1 (docs/canary_runbook.md section 2)"
      [[ "$(cat "$MAINTENANCE_MARKER" 2>/dev/null || true)" == "$ROLLOUT_ID" ]] \
        || die "the drain marker at $MAINTENANCE_MARKER names a different rollout id than '$ROLLOUT_ID'; refusing a 100% exposure no running deploy asked for"
      _marker_age=$(( $(date +%s) - $(stat -c %Y "$MAINTENANCE_MARKER" 2>/dev/null || echo 0) ))
      [[ "$_marker_age" -ge 0 && "$_marker_age" -le "${RENTCOMPASS_MAINTENANCE_MARKER_TTL:-3600}" ]] \
        || die "the drain marker at $MAINTENANCE_MARKER is ${_marker_age}s old (TTL ${RENTCOMPASS_MAINTENANCE_MARKER_TTL:-3600}s); a drain authorisation must expire with the deploy that opened it — re-run deploy/update.sh instead of reusing it"
      note "stage 'maintenance': 100% is a temporary drain; the caller must restore the recorded weight/stage"
      ;;
    flip)
      [[ "${CANARY_ALLOW_FLIP:-0}" == 1 ]] \
        || die "refusing candidate weight 100 at stage 'flip' without CANARY_ALLOW_FLIP=1; 50% is the highest authorised rollout stage (docs/canary_runbook.md section 2)"
      note "CANARY_ALLOW_FLIP=1: performing the explicitly gated 100% flip"
      ;;
    *)
      die "refusing candidate weight 100 at stage '$ROLLOUT_STAGE'; only '--stage maintenance' (deploy drain) or '--stage flip' with CANARY_ALLOW_FLIP=1 may reach 100% (docs/canary_runbook.md section 2)"
      ;;
  esac
fi

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
  specialists_ok "$specialists" "$want_specialists" \
    || die "$label pool reports specialists='$specialists', expected '$want_specialists'; routing unchanged"
  if specialists_absent "$specialists"; then
    note "$label sends no X-Agent-Specialists header (pre-2026-08-31 image); absent counts as specialists=0"
  fi
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

run_answer_probe() { # base_url want_arch want_specialists -> stdout, probe rc
  # `--expect-specialists 0` is passed for legacy exactly as verify_local does:
  # the DEPLOYED legacy pool predates X-Agent-Specialists (it is the standing
  # rollback escape hatch and must not be recreated), so it sends no such header.
  # probe_pool_answer.py::specialists_match mirrors verify_local's `none` branch —
  # an absent header counts as 0 only when the pool answers as arch 'legacy'.
  $ANSWER_PROBE_CMD --url "$1" --expect-arch "$2" \
    --expect-specialists "$3" --expect-substring "$ANSWER_PROBE_SUBSTRING" \
    --timeout "$ANSWER_PROBE_TIMEOUT" 2>&1
}

verify_answer() { # label base_url want_arch want_specialists
  local label="$1" base="$2" want_arch="$3" want_specialists="$4" out rc=0
  if [[ "$SKIP_ANSWER_PROBE" == 1 ]]; then
    note "WARNING: --skip-answer-probe: $label was NOT proven able to answer a turn"
    return 0
  fi
  # An injected probe is a legitimate rehearsal hook, but it is also the one way
  # to turn this gate off without saying so (CANARY_ANSWER_PROBE_CMD=true exits 0
  # in silence). Name it every time so an injected run can never read as a real one.
  if [[ "$ANSWER_PROBE_INJECTED" == 1 ]]; then
    note "WARNING: the answer probe is INJECTED via CANARY_ANSWER_PROBE_CMD='$ANSWER_PROBE_CMD'"
    note "WARNING: this is NOT deploy/probe_pool_answer.py; $label is being proven by a substitute"
  fi
  # /ready proves identity and dependency wiring; it cannot prove the pool can
  # ANSWER. A stale model name kept both pools green on /ready for a day on
  # 2026-07-25, so one real turn is driven before any cohort is exposed.
  out="$(run_answer_probe "$base" "$want_arch" "$want_specialists")" || rc=$?
  printf '     %s\n' "$out"
  # Exit 2 = the pool asked a clarifying question: a real reply that can carry no
  # tool grounding. Retry once rather than either failing a healthy pool or
  # accepting a turn that proved nothing.
  if [[ "$rc" -eq 2 ]]; then
    note "the probe was INCONCLUSIVE (a clarification); retrying once"
    rc=0
    out="$(run_answer_probe "$base" "$want_arch" "$want_specialists")" || rc=$?
    printf '     %s\n' "$out"
    [[ "$rc" -ne 2 ]] \
      || die "$label pool asked for clarification twice and never produced a grounded answer; routing unchanged (pass a different --query to deploy/probe_pool_answer.py by hand, or --skip-answer-probe if you have another proof)"
  fi
  [[ "$rc" -eq 0 ]] \
    || die "$label pool cannot answer a real turn; routing unchanged. To reduce exposure NOW: sudo bash deploy/set_canary_weight.sh --weight 0 (weight 0 and any weight DECREASE skip this probe). --skip-answer-probe only if you have another proof."
  ok "$label answered a real turn"
}

# 0% is the emergency path: only the rollback pool is required.  Any non-zero
# exposure requires both pools so a rollback remains immediately available.
_legacy_allow_unknown="$ALLOW_UNIDENTIFIED"
[[ "$WEIGHT" == 0 ]] && _legacy_allow_unknown=1
verify_local legacy "$LEGACY_URL" legacy 0 "$LEGACY_SHA" "$_legacy_allow_unknown"
if [[ "$WEIGHT" != 0 ]]; then
  verify_local candidate "$CANDIDATE_URL" "$CANDIDATE_ARCH" \
    "$CANDIDATE_SPECIALISTS" "$CANDIDATE_SHA" 0
  # DE-ESCALATION EXEMPTION.  A candidate that cannot answer is the exact reason
  # to lower its exposure, so requiring it to answer first would strand traffic
  # at the HIGHER weight: 50 -> 5 would die on the probe while 50% of the public
  # kept hitting the broken pool, leaving weight 0 as the only reachable move.
  # Any decrease is therefore treated like weight 0 and skips the probe; the
  # identity/readiness checks above still apply, because the candidate keeps
  # serving the smaller cohort.
  if [[ "$DE_ESCALATION" == 1 ]]; then
    note "WARNING: lowering candidate exposure ${CURRENT_WEIGHT}% -> ${WEIGHT}%: the answer probe is SKIPPED"
    note "         (a pool that cannot answer must stay de-escalatable; --weight 0 removes the cohort entirely)"
  else
    # Exposure means BOTH pools carry public traffic (the cohort split, and the
    # rollback that must remain available), so both must prove they can answer.
    verify_answer legacy "$LEGACY_ANSWER_URL" legacy 0
    verify_answer candidate "$CANDIDATE_ANSWER_URL" "$CANDIDATE_ARCH" "$CANDIDATE_SPECIALISTS"
  fi
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
      [[ "$arch" == legacy ]] && specialists_ok "$specialists" 0 || return 1
      [[ ! "$LEGACY_SHA" =~ ^[0-9a-f]{40}$ || "$sha" == "$LEGACY_SHA" ]] || return 1
      ;;
    candidate)
      [[ "$arch" == "$CANDIDATE_ARCH" ]] \
        && specialists_ok "$specialists" "$CANDIDATE_SPECIALISTS" || return 1
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
