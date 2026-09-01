#!/usr/bin/env bash
# Cut a release: put this box on the latest reviewed commit and deploy it.
#
#   cd /home/shuhan/uk_rent_recommendation
#   bash deploy/release.sh
#
# That is the whole procedure. A cached sudo credential may be used to repair the
# five persistent bind-mount trees and to write the root-owned pin; everything
# else is unattended.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS INSTEAD OF "just make update.sh deploy the latest commit"
# ---------------------------------------------------------------------------
# `deploy/update.sh` refuses to deploy anything but the exact sha named in
# `/etc/rentcompass/deploy.env` — a file OUTSIDE version control, so that no
# commit to this repo can change what production runs. That gate is not
# bureaucracy; it is the reason a bad merge cannot reach the public site on its
# own. This script does NOT weaken it, remove it, or route around it. update.sh
# still enforces `HEAD == DEPLOY_PINNED_SHA` exactly as before.
#
# What was actually broken was the *procedure* around the gate. The re-pin was
# three manual steps documented in deploy/monitoring/README.md, and on 2026-07-28
# a merged search fix sat unshipped for hours while the same bug was re-reported,
# because nobody had performed them. A release step people skip is not a safety
# property — it is an outage waiting for a busy day.
#
# So: the gate keeps deciding what MAY ship; this script performs the deliberate
# act of advancing the pin, with the checks that decision deserves:
#
#   1. the target is the tip of the REMOTE mainline (never a local commit, never
#      an uncommitted tree) — everything there went through a PR with required
#      CI and branch protection;
#   2. every declared required check must exist, be completed and be successful;
#   3. the entire working tree (including untracked build-context files) is clean;
#   4. you are shown old-pin -> new-pin and must confirm (unless --yes);
#   5. before source or pin moves, recursively repair and verify the five scoped
#      persistent bind-mount trees for the image's non-root uid:gid;
#   6. only then is the pin advanced, and update.sh rebuilds both pools with a
#      safe drain before refusing to report success unless they answer with that
#      exact sha.
#
# If the deploy fails after the pin moved, the script tells you the pin is ahead
# of what is running and prints the one command that puts it back.
#
# ---------------------------------------------------------------------------
# OPTIONS
# ---------------------------------------------------------------------------
#   --ref <branch|sha>   release this instead of the mainline tip (a rollback to
#                        an older release, a hotfix tag)
#   --yes                do not ask for confirmation (for cron/CI)
#   --no-fetch           use the refs already on disk
#   --allow-failing-ci   release even though the target's CI checks FAILED
#   --dry-run            do everything except re-pin and deploy; print the plan.
#                        NEVER fails on a rollout policy decision: it prints what
#                        would happen and the flag that would be needed.
#   --skip-answer-probe  passed to update.sh: do not drive a real billed turn
#                        against either pool during the drain
#   --                   override the default --both --drain arguments passed
#                        to update.sh
#
# ENVIRONMENT
#   CANARY_ALLOW_FLIP=1  authorise a run that would CHANGE what 100% of the public
#                        runs — i.e. an architecture that is NOT at 100% when the
#                        run starts ends there. Rebuilding the pool that is already
#                        the sole public upstream is NOT that: routing is unchanged,
#                        and a routine release on this host needs no flag. Only a
#                        real cutover (a different candidate architecture taking the
#                        exclusive :5002 slot, or a weighted host being advanced to
#                        100%) is refused without it.
#
# Every external command is injectable so this can be rehearsed with no docker,
# no root and no network (see deploy/test_release_assertions.sh).
set -euo pipefail

# Resolve this script's own path BEFORE cd'ing away — `--help` reads $0, and a
# relative $0 stops resolving the moment the working directory changes.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

REPO_DIR="${RELEASE_REPO_DIR:-/home/shuhan/uk_rent_recommendation}"
cd "$REPO_DIR"

GIT_CMD="${RELEASE_GIT_CMD:-git}"
GH_CMD="${RELEASE_GH_CMD:-gh}"
SUDO_CMD="${RELEASE_SUDO_CMD:-sudo}"
UPDATE_CMD="${RELEASE_UPDATE_CMD:-bash deploy/update.sh}"
RUNTIME_MAINTENANCE_CMD="${RELEASE_RUNTIME_MAINTENANCE_CMD:-bash deploy/preflight_runtime_permissions.sh}"
PIN_ENV_FILE="${DEPLOY_PIN_ENV:-/etc/rentcompass/deploy.env}"
# REMOTE on purpose: a local branch can hold commits that never saw a PR.
TRACK_REF="${RELEASE_TRACK_REF:-origin/main}"
REQUIRED_CHECKS="${RELEASE_REQUIRED_CHECKS:-Tests (Python 3.12),Compose smoke,Eval smoke,Supply chain gates}"
ENV_FILE="${RELEASE_ENV_FILE:-$REPO_DIR/.env}"
ROUTE_CONF="${RELEASE_ROUTE_CONF:-/etc/nginx/snippets/rentcompass-canary-routing.conf}"
SITE_CONF="${RELEASE_SITE_CONF:-/etc/nginx/sites-available/rentcompass.co.uk.conf}"
UPSTREAM_BLOCK='upstream rentcompass_app'

say()  { printf '==> %s\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

bool01() {
  case "${1,,}" in
    1|true|yes|on) printf 1 ;;
    0|false|no|off|'') printf 0 ;;
    *) return 1 ;;
  esac
}

read_root_env() { # key default; explicit process env wins
  local key="$1" fallback="$2" value=""
  if [ -n "${!key+x}" ]; then value="${!key}"
  elif [ -r "$ENV_FILE" ]; then
    value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1 | tr -d '\r')"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
  fi
  printf '%s' "${value:-$fallback}"
}

# The generated routing include is the rollout state file; update.sh restores the
# weight/rollout-id/stage recorded in these markers after its maintenance drain, so
# they are also the state this release will END on.
#
# The `[ -r ]` guard is not decoration. `set -euo pipefail` + a `sed` on a missing
# file = exit 2, which `2>/dev/null` hides without changing, `pipefail` promotes to
# the whole pipeline, and `errexit` turns into a SILENT abort of the entire release
# — no output at all past "Release plan". The weighted include is untracked and is
# NOT installed on every host, so "missing" is the normal case, not an error case.
# deploy/update.sh has always called these markers with `|| true` for this reason.
route_marker() {
  [ -r "$ROUTE_CONF" ] || return 0
  sed -n "s/^# rentcompass-$1: //p" "$ROUTE_CONF" 2>/dev/null | head -1
}

# A host with no weighted include routes through a single `server 127.0.0.1:PORT;`
# line instead (the line deploy/switch_pool.sh owns). That is not "no rollout
# state": :5002 there IS 100% candidate traffic. Read it with the same parser
# update.sh uses so one policy covers both routing modes.
upstream_port() {
  [ -r "$SITE_CONF" ] || return 0
  awk "/^${UPSTREAM_BLOCK}[[:space:]]*\{/,/^\}/" "$SITE_CONF" \
    | sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' | head -1
}

REF=""; ASSUME_YES=0; NO_FETCH=0; ALLOW_FAILING_CI=0; DRY_RUN=0; SKIP_ANSWER_PROBE=0
PASSTHROUGH=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)              REF="${2:-}"; shift 2 ;;
    --yes|-y)           ASSUME_YES=1; shift ;;
    --no-fetch)         NO_FETCH=1; shift ;;
    --allow-failing-ci) ALLOW_FAILING_CI=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --skip-answer-probe) SKIP_ANSWER_PROBE=1; shift ;;
    --)                 shift; PASSTHROUGH=("$@"); break ;;
    -h|--help)          sed -n '2,/^set -euo/p' "$SELF" | sed '$d'; exit 0 ;;
    *)                  die "unknown argument: $1  (try --help; use -- to pass options to update.sh)" ;;
  esac
done

# A release is safe and rollback-complete by default: refresh the non-public
# pool first, drain onto it, then replace and restore the public pool. Callers
# can still pass an explicit update policy after --.
if [ "${#PASSTHROUGH[@]}" -eq 0 ]; then
  PASSTHROUGH=(--both --drain)
fi
# R3-M2: the opt-out has to reach set_canary_weight.sh through update.sh and
# switch_pool.sh, or no caller above set_canary_weight.sh can ever use it.
if [ "$SKIP_ANSWER_PROBE" -eq 1 ]; then
  PASSTHROUGH+=(--skip-answer-probe)
fi

if [ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" != "1" ]; then
  DEPLOY_LOCK_FILE="${RENTCOMPASS_DEPLOY_LOCK_FILE:-}"
  if [ -z "$DEPLOY_LOCK_FILE" ]; then
    GIT_COMMON_DIR="$($GIT_CMD rev-parse --path-format=absolute --git-common-dir)" \
      || die "cannot resolve the shared git metadata directory"
    DEPLOY_LOCK_FILE="$GIT_COMMON_DIR/rentcompass-deploy.lock"
  fi
  mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
  exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open deploy lock: $DEPLOY_LOCK_FILE"
  flock -n 9 || die "another release/update/switch/retirement operation is running"
  export RENTCOMPASS_DEPLOY_LOCK_HELD=1
fi

# ---------------------------------------------------------------------------
# 1. Resolve the target
# ---------------------------------------------------------------------------
if [ "$NO_FETCH" -eq 0 ]; then
  say "Fetching origin..."
  $GIT_CMD fetch origin --prune >/dev/null 2>&1 \
    || warn "git fetch failed — falling back to the refs already on disk"
fi

WANT="${REF:-$TRACK_REF}"
TARGET="$($GIT_CMD rev-parse --verify -q "${WANT}^{commit}" || true)"
[ -n "$TARGET" ] || die "cannot resolve '$WANT' to a commit — check the ref name, and that it has been fetched"
TARGET_SHORT="$($GIT_CMD rev-parse --short "$TARGET")"

if [ -n "$REF" ]; then
  # An explicit --ref may legitimately sit off mainline (rollback, hotfix tag).
  # Say so rather than letting it pass as reviewed code.
  $GIT_CMD merge-base --is-ancestor "$TARGET" "$TRACK_REF" 2>/dev/null \
    || warn "'$REF' is NOT an ancestor of $TRACK_REF — it may never have been through a PR"
fi

# ---------------------------------------------------------------------------
# 2. Refuse a dirty tree BEFORE anything moves
# ---------------------------------------------------------------------------
# The legacy pool builds this tree. Untracked files are build context too, so a
# tracked-only diff is insufficient protection.
tree_status="$($GIT_CMD status --porcelain --untracked-files=all)"
if [ -n "$tree_status" ]; then
  echo "!! Working tree is DIRTY (tracked or untracked) — refusing a contaminated build context:" >&2
  printf '%s\n' "$tree_status" >&2
  echo "!! Commit, ignore, or remove the files above, then re-run." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. CI verdict for the target
# ---------------------------------------------------------------------------
# Advancing the pin is the moment this repo's review process becomes production,
# so "did every required check pass" carries the weight. Missing CLI/API data,
# missing checks, queued/in-progress checks, and unknown conclusions all fail
# closed. --allow-failing-ci applies only to an explicit completed failure.
check_ci() {
  local sha="$1" out name row status conclusion
  local -a required
  command -v "${GH_CMD%% *}" >/dev/null 2>&1 || {
    die "'$GH_CMD' not found — cannot verify required CI for $TARGET_SHORT."; }
  out=$($GH_CMD api "repos/{owner}/{repo}/commits/$sha/check-runs" \
          --jq '.check_runs | sort_by(.started_at) | .[] | [.name,.status,(.conclusion // "unknown")] | @tsv' 2>/dev/null) \
    || die "Could not read required CI status for $TARGET_SHORT (gh api failed)."
  [ -n "$out" ] || die "No CI checks reported for $TARGET_SHORT."

  IFS=',' read -r -a required <<<"$REQUIRED_CHECKS"
  [ "${#required[@]}" -gt 0 ] || die "RELEASE_REQUIRED_CHECKS is empty"
  for name in "${required[@]}"; do
    name="${name#"${name%%[![:space:]]*}"}"; name="${name%"${name##*[![:space:]]}"}"
    [ -n "$name" ] || die "RELEASE_REQUIRED_CHECKS contains an empty name"
    row="$(awk -F '\t' -v wanted="$name" '$1 == wanted {last=$0} END {print last}' <<<"$out")"
    [ -n "$row" ] || die "Required CI check '$name' is MISSING for $TARGET_SHORT."
    IFS=$'\t' read -r _ status conclusion <<<"$row"
    [ "$status" = "completed" ] \
      || die "Required CI check '$name' is '$status' (not completed) for $TARGET_SHORT."
    if [ "$conclusion" != "success" ]; then
      case "$conclusion" in
        failure|timed_out|cancelled|action_required|startup_failure)
          [ "$ALLOW_FAILING_CI" -eq 1 ] \
            || die "Required CI check '$name' concluded '$conclusion' for $TARGET_SHORT."
          warn "Required CI check '$name' concluded '$conclusion' — explicit --allow-failing-ci override"
          ;;
        *) die "Required CI check '$name' has unknown/non-success conclusion '$conclusion' for $TARGET_SHORT." ;;
      esac
    fi
  done
  say "All ${#required[@]} required CI checks on $TARGET_SHORT are completed and successful ✅"
}
check_ci "$TARGET"

# ---------------------------------------------------------------------------
# 4. Show the plan; confirm
# ---------------------------------------------------------------------------
OLD_PIN=""
if [ -r "$PIN_ENV_FILE" ]; then
  # shellcheck source=/dev/null
  DEPLOY_PINNED_SHA=""; . "$PIN_ENV_FILE"; OLD_PIN="${DEPLOY_PINNED_SHA:-}"
else
  warn "pin file '$PIN_ENV_FILE' is not readable — it will be created/replaced"
fi
HEAD_NOW="$($GIT_CMD rev-parse HEAD)"

echo
say "Release plan"
printf '    source     %s  (%s)\n' "$TARGET_SHORT" "${REF:+--ref $REF}${REF:-$TRACK_REF tip}"
printf '    HEAD       %s -> %s\n' "$($GIT_CMD rev-parse --short HEAD)" "$TARGET_SHORT"
printf '    pin        %s -> %s\n' "${OLD_PIN:0:7}${OLD_PIN:+ }${OLD_PIN:-<unset>}" "$TARGET"
printf '    maintain   %s --repair\n' "$RUNTIME_MAINTENANCE_CMD"
printf '    then       %s %s\n' "$UPDATE_CMD" "${PASSTHROUGH[*]:-}"

# ---------------------------------------------------------------------------
# 4a. Rollout preflight — what identity ships, and where the traffic ENDS UP
# ---------------------------------------------------------------------------
# `--both --drain` hands the public route to the standby and back. Before this
# preflight existed the return leg could resolve to `--stage flip` at weight 100,
# so `bash deploy/release.sh` — with no rollout flag anywhere — could end with the
# candidate serving 100% of the public.
#
# R3-H3: the first version of the gate compared only the END state, so it could
# not tell "the pool that is already live stays at 100%" from "this run flips the
# candidate to 100%". On this box the fc_loop pool on :5002 has BEEN the sole
# public upstream since the 07-27 cutover, so every routine release was refused
# and had to be typed as `CANARY_ALLOW_FLIP=1 bash deploy/release.sh` — training
# the exact habit that disarms the gate for the real manager_v1 cutover later.
#
# The gate now fires only on a CHANGE: an architecture that is NOT running at 100%
# when the run starts ends there. A rebuild of the already-live single-upstream
# pool is routine and needs no flag.
CANDIDATE_ARCH="$(read_root_env CANARY_AGENT_ARCH fc_loop)"
# R3-M4: normalise the boolean the same way config.py::_bool_token does, so the
# spellings .env.example and the runbook bless (`true`/`yes`/`on`) cannot kill
# every release while every other consumer accepts them.
CANDIDATE_SPECIALISTS_RAW="$(read_root_env CANARY_MANAGER_V1_SPECIALISTS 0)"
CANDIDATE_MCP_RAW="$(read_root_env CANARY_USE_MCP_TOOLS 0)"
CANDIDATE_SPECIALISTS="$(bool01 "$CANDIDATE_SPECIALISTS_RAW")" \
  || die "CANARY_MANAGER_V1_SPECIALISTS='$CANDIDATE_SPECIALISTS_RAW' in $ENV_FILE is neither a true (1/true/yes/on) nor a false (0/false/no/off) spelling"
CANDIDATE_MCP="$(bool01 "$CANDIDATE_MCP_RAW")" \
  || die "CANARY_USE_MCP_TOOLS='$CANDIDATE_MCP_RAW' in $ENV_FILE is neither a true (1/true/yes/on) nor a false (0/false/no/off) spelling"
case "$CANDIDATE_ARCH:$CANDIDATE_SPECIALISTS:$CANDIDATE_MCP" in
  fc_loop:0:0|manager_v1:1:0) ;;
  *) die "root .env selects an unsupported candidate identity ${CANDIDATE_ARCH}:${CANDIDATE_SPECIALISTS}:${CANDIDATE_MCP}; the only accepted pairs are fc_loop:0:0 and manager_v1:1:0 (docs/canary_runbook.md)" ;;
esac

# What the exclusive candidate SLOT (:5002) runs RIGHT NOW. update.sh tags every
# candidate image `uk-rent-agent:canary-<arch-with-dashes>-<short sha>`, so the
# root .env's own FC_CANARY_IMAGE is a primary source for it — no probe, no
# guess. An unparseable tag falls back to fc_loop, the only architecture that has
# ever occupied the slot on this host.
LIVE_CANDIDATE_ARCH="${RELEASE_LIVE_CANDIDATE_ARCH:-}"
if [ -z "$LIVE_CANDIDATE_ARCH" ]; then
  LIVE_CANDIDATE_ARCH="$(printf '%s' "$(read_root_env FC_CANARY_IMAGE '')" \
    | sed -n 's/.*:canary-\(.*\)-[0-9a-f]\{7,\}$/\1/p' | tr '-' '_')"
fi
LIVE_CANDIDATE_ARCH="${LIVE_CANDIDATE_ARCH:-fc_loop}"

END_WEIGHT="$(route_marker canary-weight || true)"
END_STAGE="$(route_marker rollout-stage || true)"
END_ROLLOUT_ID="$(route_marker rollout-id || true)"
END_MODE=weighted
END_SOURCE="$ROUTE_CONF"
END_NOTE=""
# START = the exposure this host is on before the release. update.sh records the
# same markers before its drain and puts them back afterwards, so START and END
# are equal for every routine release; where they differ is stated below.
START_WEIGHT="$END_WEIGHT"
START_STAGE="$END_STAGE"
if [ -z "$END_WEIGHT" ]; then
  # No weighted include: resolve the end state from the single upstream instead of
  # skipping the gate. Skipping it is what let K4 apply to weighted hosts only.
  END_MODE=single-upstream
  END_SOURCE="$SITE_CONF"
  END_PORT="$(upstream_port || true)"
  case "${END_PORT:-}" in
    5001) END_POOL=legacy;    END_WEIGHT=0 ;;
    5002) END_POOL=candidate; END_WEIGHT=100 ;;
    *)    END_POOL=unknown;   END_WEIGHT="" ;;
  esac
  START_WEIGHT="$END_WEIGHT"
  START_STAGE="$END_STAGE"
elif [ "$END_WEIGHT" = 100 ] && [ "${END_STAGE:-flip}" = maintenance ]; then
  # update.sh REFUSES to replay a recorded `100 @ maintenance`: that is the debris
  # of an interrupted drain, not an authorised exposure. The run therefore ends
  # with the public on legacy, i.e. LOWER exposure than it started with.
  END_WEIGHT=0
  END_STAGE=rollback
  END_NOTE="the recorded 100%@maintenance is an UNFINISHED drain; update.sh will not replay it, so this run ENDS with public traffic on legacy"
fi

# The architecture 100% of the public runs, before and after. Specialists are
# bound to the architecture by the whitelist above (fc_loop=>0, manager_v1=>1),
# so the arch alone identifies the exposure.
START_100_ARCH=""; [ "${START_WEIGHT:-}" = 100 ] && START_100_ARCH="$LIVE_CANDIDATE_ARCH"
END_100_ARCH="";   [ "${END_WEIGHT:-}" = 100 ]   && END_100_ARCH="$CANDIDATE_ARCH"
FLIP=0
if [ -n "$END_100_ARCH" ] && [ "$START_100_ARCH" != "$END_100_ARCH" ]; then FLIP=1; fi

printf '    candidate  arch=%s specialists=%s mcp=%s   (root .env: %s)\n' \
  "$CANDIDATE_ARCH" "$CANDIDATE_SPECIALISTS" "$CANDIDATE_MCP" "$ENV_FILE"
if [ "$END_MODE" = weighted ]; then
  printf '    ends at    candidate weight %s%% stage %s rollout %s   (weighted include: %s)\n' \
    "$END_WEIGHT" "${END_STAGE:-<none>}" "${END_ROLLOUT_ID:-<none>}" "$END_SOURCE"
elif [ -n "$END_WEIGHT" ]; then
  printf '    ends at    SINGLE-UPSTREAM mode, sole upstream 127.0.0.1:%s = %s = candidate weight %s%%   (%s)\n' \
    "$END_PORT" "$END_POOL" "$END_WEIGHT" "$END_SOURCE"
else
  printf '    ends at    SINGLE-UPSTREAM mode, upstream UNKNOWN (no weighted include at %s, no readable upstream in %s)\n' \
    "$ROUTE_CONF" "$SITE_CONF"
  warn "The end state of this release could not be resolved from either routing file."
  warn "update.sh derives its deploy target from the same upstream line and will refuse to guess, so this run is expected to stop there."
fi
[ -z "$END_NOTE" ] || warn "$END_NOTE"

# R3-M8. This host has exactly TWO pool slots: docker-compose.yml hardcodes :5001
# to arch `legacy`, and :5002 is the single candidate slot. A candidate whose arch
# is not what the slot runs today therefore REPLACES today's production
# architecture instead of running beside it, and "rollback" means `legacy` — not
# the architecture being displaced. docs/canary_runbook.md section 2 repeats this.
if [ "$CANDIDATE_ARCH" != "$LIVE_CANDIDATE_ARCH" ]; then
  warn "CANDIDATE SLOT IS EXCLUSIVE: :5002 runs '$LIVE_CANDIDATE_ARCH' today and this release makes it '$CANDIDATE_ARCH'."
  warn "  '$LIVE_CANDIDATE_ARCH' is REPLACED, not compared against: there is no control arm for it and no way back to it except a new release."
  warn "  The rollback target stays the 'legacy' pool on :5001 — rolling back does NOT return you to '$LIVE_CANDIDATE_ARCH'."
fi

if [ "$FLIP" -eq 1 ]; then
  FLIP_MSG="this release would END with the candidate ($CANDIDATE_ARCH, specialists=$CANDIDATE_SPECIALISTS) at 100% of public traffic (${END_MODE} mode, stage '${END_STAGE:-flip}'), and that is a CHANGE: 100% currently runs '${START_100_ARCH:-<not at 100%>}'. 50% is the highest authorised rollout stage, so a routine release must not perform this cutover. Re-run with CANARY_ALLOW_FLIP=1 for a deliberately gated flip (docs/canary_runbook.md section 2). Do NOT 'fix' this by pointing the public route at the legacy architecture — that is a production downgrade, not a remedy."
  if [ "${CANARY_ALLOW_FLIP:-0}" = "1" ]; then
    warn "CANARY_ALLOW_FLIP=1: authorising a cutover from '${START_100_ARCH:-<not at 100%>}' to '$CANDIDATE_ARCH' at 100% of public traffic."
  elif [ "$DRY_RUN" -eq 1 ]; then
    # A dry run prints the plan and changes nothing, so a policy decision must
    # never abort it: the operator loses the preview exactly when they need it.
    warn "--dry-run: this release WOULD BE REFUSED. $FLIP_MSG"
    warn "--dry-run: the flag it needs is CANARY_ALLOW_FLIP=1"
  else
    die "$FLIP_MSG"
  fi
elif [ "${END_WEIGHT:-}" = 100 ]; then
  printf '    routing    UNCHANGED — %s is already serving 100%% of public traffic; this release rebuilds it in place.\n' \
    "$CANDIDATE_ARCH"
  printf '               No cutover happens, so no CANARY_ALLOW_FLIP is needed (R3-H3).\n'
fi
echo

if [ "$OLD_PIN" = "$TARGET" ] && [ "$HEAD_NOW" = "$TARGET" ]; then
  say "Pin and HEAD are already $TARGET_SHORT — nothing to advance; handing straight to update.sh"
fi
if [ "$DRY_RUN" -eq 1 ]; then
  say "--dry-run: stopping here. Nothing was checked out, re-pinned or deployed."
  exit 0
fi
if { [ "$OLD_PIN" != "$TARGET" ] || [ "$HEAD_NOW" != "$TARGET" ]; } && [ "$ASSUME_YES" -eq 0 ]; then
  printf 'Proceed? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) die "aborted — nothing was changed" ;; esac
fi

# ---------------------------------------------------------------------------
# 5. Repair and recursively verify persistent runtime state
# ---------------------------------------------------------------------------
# This is deliberately before checkout and re-pin. A failed repair therefore
# cannot leave the source, pin or containers half-advanced. update.sh runs the
# same script read-only as its final fail-closed gate.
say "Maintaining persistent runtime bind mounts for the non-root app user..."
RUNTIME_PREFLIGHT_REPO="$REPO_DIR" \
RUNTIME_PREFLIGHT_SUDO_CMD="$SUDO_CMD" \
  $RUNTIME_MAINTENANCE_CMD --repair \
  || die "persistent runtime maintenance failed; source, pin and containers were not changed"

# ---------------------------------------------------------------------------
# 6. Check out the target
# ---------------------------------------------------------------------------
if [ "$HEAD_NOW" != "$TARGET" ]; then
  say "Checking out $TARGET_SHORT"
  $GIT_CMD checkout --detach "$TARGET" >/dev/null 2>&1 || die "could not check out $TARGET_SHORT"
fi

# ---------------------------------------------------------------------------
# 7. Advance the pin
# ---------------------------------------------------------------------------
# Written through a temp file + `sudo tee` rather than an in-place `sudo sed`, so
# a half-written pin file cannot survive a failure: the gate reading a truncated
# DEPLOY_PINNED_SHA would refuse every deploy, including the rollback.
repin() {
  local sha="$1" tmp; tmp=$(mktemp)
  if [ -r "$PIN_ENV_FILE" ] && grep -q '^DEPLOY_PINNED_SHA=' "$PIN_ENV_FILE"; then
    awk -v v="$sha" '/^DEPLOY_PINNED_SHA=/ { print "DEPLOY_PINNED_SHA=" v; next } { print }' \
      "$PIN_ENV_FILE" > "$tmp"
  else
    [ -r "$PIN_ENV_FILE" ] && cat "$PIN_ENV_FILE" > "$tmp"
    printf 'DEPLOY_PINNED_SHA=%s\n' "$sha" >> "$tmp"
  fi
  if [ -w "$PIN_ENV_FILE" ]; then
    cat "$tmp" > "$PIN_ENV_FILE"
  else
    say "Re-pinning $PIN_ENV_FILE (this is the sudo password prompt)"
    $SUDO_CMD tee "$PIN_ENV_FILE" < "$tmp" >/dev/null || { rm -f "$tmp"; die "could not write $PIN_ENV_FILE"; }
  fi
  rm -f "$tmp"
}

if [ "$OLD_PIN" != "$TARGET" ]; then
  repin "$TARGET"
  say "Pin advanced: ${OLD_PIN:-<unset>} -> $TARGET"
else
  say "Pin already $TARGET_SHORT — left alone"
fi

# ---------------------------------------------------------------------------
# 8. Hand off to update.sh, which enforces the gate and verifies the result
# ---------------------------------------------------------------------------
echo
say "Handing off to $UPDATE_CMD"
# rc is captured on the SAME command, not from `$?` after an `if`: a bare `if
# cmd; then ... fi` returns 0 when the condition is false, so reading `$?` after
# `fi` reports success for a deploy that just failed. That is the precise shape
# of the defect this whole file exists to stop, one level up.
rc=0
$UPDATE_CMD ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || rc=$?
[ "$rc" -eq 0 ] && exit 0

# A failed readiness gate does not prove whether the process started: the
# candidate can be live on its port while /ready rejects its dependencies or
# release metadata. State that uncertainty instead of telling the operator the
# commit is definitely not running.
echo
warn "DEPLOY FAILED after the pin was advanced to $TARGET_SHORT."
warn "One or more pools may be running that commit without passing /ready."
warn "Inspect both pools, then after fixing the cause re-run safely:"
warn "    bash deploy/update.sh --both --drain"
if [ -n "$OLD_PIN" ] && [ "$OLD_PIN" != "$TARGET" ]; then
  warn "  ...or put the pin back where it was:"
  warn "    sudo sed -i 's/^DEPLOY_PINNED_SHA=.*/DEPLOY_PINNED_SHA=$OLD_PIN/' $PIN_ENV_FILE"
  warn "    git checkout --detach $OLD_PIN"
fi
exit "$rc"
