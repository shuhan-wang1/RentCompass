#!/usr/bin/env bash
# Rehearse deploy/update.sh end to end. Offline, free, no docker, no root, no nginx.
#
#   bash deploy/test_update_assertions.sh
#
# Every external command update.sh touches is injectable (UPDATE_*_CMD), so the
# REAL script runs here — this is not a re-implementation of its logic.
#
# The defect being guarded against is not "the script is broken" but "the script
# succeeds against the wrong pool". The old update.sh printed
# "Healthy ✅ Live at https://rentcompass.co.uk:8443" after rebuilding :5001
# while the public upstream had been on :5002 for days. So the assertions below
# are mostly about WHICH pool got deployed and whether success is claimed on
# anything less than the pinned commit actually answering.
set -u
PASS=0; FAIL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

check() { if [ "$2" = "$3" ]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
          else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }
contains() { if grep -qF -- "$2" <<<"$1"; then printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1))
             else printf '\033[31mFAIL\033[0m %s\n       missing: %s\n       got:     %s\n' "$3" "$2" "$1"; FAIL=$((FAIL+1)); fi; }
lacks()  { if grep -qF -- "$2" <<<"$1"; then printf '\033[31mFAIL\033[0m %s\n       unexpectedly present: %s\n' "$3" "$2"; FAIL=$((FAIL+1))
           else printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1)); fi; }

# --------------------------------------------------------------------------
# A throwaway git repo + fake docker/compose/curl/switch, rebuilt per scenario.
# --------------------------------------------------------------------------
SANDBOX=""
setup() {                      # setup <upstream_port> <legacy_sha> <fc_sha>
  SANDBOX="$(mktemp -d)"
  local repo="$SANDBOX/repo"; mkdir -p "$repo/deploy" "$repo/searxng" "$repo/app/core" "$SANDBOX/bin"
  cp "$ROOT/deploy/update.sh" "$repo/deploy/update.sh"
  cp "$ROOT/deploy/searxng-settings.yml.example" "$repo/deploy/" 2>/dev/null || true
  : > "$repo/searxng/settings.yml"
  printf 'FC_LOOP_SYSTEM_PROMPT_VERSION = "2.1.0"\n' > "$repo/app/core/loop_prompts.py"
  printf 'SEARXNG_SECRET="keep-me"\nFC_CANARY_IMAGE=uk-rent-agent:canary-fc-loop-old\nFC_CANARY_SHA=%s\n' \
    "0000000000000000000000000000000000000000" > "$repo/.env"

  ( cd "$repo"
    git init -q .; git config user.email t@t; git config user.name t
    echo x > f; git add -A; git commit -qm c1 ) >/dev/null 2>&1
  PIN="$(cd "$repo" && git rev-parse HEAD)"
  printf 'DEPLOY_PINNED_SHA=%s\nDEPLOY_PYTHON_IMAGE=python@sha256:%064d\n' \
    "$PIN" 2 > "$SANDBOX/pin.env"

  mkdir -p "$SANDBOX/nginx"
  printf 'upstream rentcompass_app {\n    server 127.0.0.1:%s;\n}\n' "$1" > "$SANDBOX/nginx/site.conf"
  # Absent by default: the pre-weighted single-upstream shape most scenarios use.
  ROUTE_CONF_PATH="$SANDBOX/nginx/no-weighted-include.conf"

  # Fakes. Each logs its argv to $SANDBOX/calls.log so assertions can read it.
  cat > "$SANDBOX/bin/fakedocker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$1" in
  image)
    if [ "${3:-}" = "--format" ] || [ "${2:-}" = "inspect" ] && [ "${3:-}" = "--format" ]; then
      case "${FAKE_DIGEST_KIND:-bare}" in
        bare)    printf 'sha256:%064d\n' 1 ;;
        repo)    printf 'uk-rent-agent@sha256:%064d\n' 1 ;;
        invalid) printf 'uk-rent-agent:mutable\n' ;;
      esac
      exit 0
    fi
    exit "${FAKE_IMAGE_EXISTS:-1}" ;;   # non-zero = tag absent -> build
  build) exit "${FAKE_BUILD_RC:-0}" ;;
esac
exit 0
EOF
  cat > "$SANDBOX/bin/fakecompose" <<'EOF'
#!/usr/bin/env bash
echo "compose $*" >> "$CALLS"
for key in FC_CANARY_IMAGE FC_CANARY_SHA FC_IMAGE_DIGEST PROMPT_VERSION PROMPT_SCHEMA_SHA RELEASE_METADATA_REQUIRED; do
  if [ -n "${!key:-}" ]; then
    continue
  fi
  grep -Eq "^${key}=.+$" "$UPDATE_ENV_FILE" || {
    echo "compose interpolation is missing required env: $key" >&2
    exit 86
  }
done
exit "${FAKE_COMPOSE_RC:-0}"
EOF
  # Answers /health per port. The sha a pool reports flips to the pin once its
  # service has been brought up, which is what makes the verify step meaningful.
  cat > "$SANDBOX/bin/fakecurl" <<'EOF'
#!/usr/bin/env bash
url="${@: -1}"
port="${url##*:}"; port="${port%%/*}"
case "$port" in
  5001) sha="$LEGACY_SHA"; arch=legacy ;;
  5002) sha="$FC_SHA";     arch=fc_loop ;;
  *) exit 1 ;;
esac
grep -q "compose .*up .*app-fc" "$CALLS" 2>/dev/null && [ "$port" = 5002 ] && sha="$PIN"
grep -q "compose up -d app$" "$CALLS" 2>/dev/null && [ "$port" = 5001 ] && sha="$PIN"
[ "$sha" = "DOWN" ] && exit 1
printf 'HTTP/1.1 200 OK\r\nx-agent-arch: %s\r\nx-agent-version: %s\r\nx-agent-specialists: 0\r\n\r\n' "$arch" "$sha"
EOF
  # Logs the scoped flip authorisation separately from argv, so the existing
  # `switch --to <pool>` assertions keep matching a bare argv line.
  cat > "$SANDBOX/bin/fakeswitch" <<'EOF'
#!/usr/bin/env bash
echo "switchenv CANARY_ALLOW_FLIP=${CANARY_ALLOW_FLIP:-0}" >> "$CALLS"
# The route file update.sh READ the pre-drain state from must be the one
# switch_pool.sh WRITES, and `--stage maintenance` is only accepted while the
# active-drain marker update.sh owns exists and names this run's rollout id.
echo "switchroute ${SWITCH_ROUTE_CONF:-<unset>}" >> "$CALLS"
echo "switchmarker $(cat "${RENTCOMPASS_MAINTENANCE_MARKER:-/nonexistent}" 2>/dev/null || echo '<absent>')" >> "$CALLS"
echo "switch $*" >> "$CALLS"
exit "${FAKE_SWITCH_RC:-0}"
EOF
  chmod +x "$SANDBOX"/bin/*
  export CALLS="$SANDBOX/calls.log"; : > "$CALLS"
  export LEGACY_SHA="$2" FC_SHA="$3" PIN
  REPO="$repo"
}
teardown() { [ -n "$SANDBOX" ] && rm -rf "$SANDBOX"; SANDBOX=""; }

# install_route <weight> <rollout-id> <stage> — the weighted include IS the rollout
# state file; update.sh reads the pre-drain weight/id/stage out of these markers and
# must put the SAME values back when the drain ends.
install_route() {
  ROUTE_CONF_PATH="$SANDBOX/nginx/canary-routing.conf"
  printf '# rentcompass-canary-weight: %s\n# rentcompass-canary-salt: t\n# rentcompass-rollout-id: %s\n# rentcompass-rollout-stage: %s\n' \
    "$1" "$2" "$3" > "$ROUTE_CONF_PATH"
}

# Sets the globals OUT and RC. Deliberately NOT a command substitution at the
# call site: that runs in a subshell, so RC would never reach the assertions.
run_update() {                 # run_update [args...]
  ( cd "$REPO" && \
    UPDATE_REPO_DIR="$REPO" \
    DEPLOY_PIN_ENV="$SANDBOX/pin.env" \
    UPDATE_CONF="$SANDBOX/nginx/site.conf" \
    UPDATE_ROUTE_CONF="$ROUTE_CONF_PATH" \
    UPDATE_ENV_FILE="$REPO/.env" \
    UPDATE_ENV_BACKUP_DIR="$SANDBOX/env-backups" \
    UPDATE_DOCKER_CMD="$SANDBOX/bin/fakedocker" \
    UPDATE_COMPOSE_CMD="$SANDBOX/bin/fakecompose" \
    UPDATE_CURL_CMD="$SANDBOX/bin/fakecurl" \
    UPDATE_SWITCH_CMD="$SANDBOX/bin/fakeswitch" \
    RENTCOMPASS_MAINTENANCE_MARKER="$SANDBOX/maintenance-marker" \
    UPDATE_HEALTH_RETRIES=2 UPDATE_HEALTH_DELAY=0 \
    bash deploy/update.sh "$@" ) > "$SANDBOX/out.txt" 2>&1
  RC=$?
  OUT="$(cat "$SANDBOX/out.txt")"
}

# ══════════════════════════════════════════════════════════════════════════
echo "--- 1. auto-target follows the PUBLIC upstream, not a hardcoded pool ---"
# The exact production shape on 2026-07-28: nginx on :5002 (fc), both pools on
# the old commit. The old script rebuilt `app` here and called it a success.
setup 5002 old-legacy-sha old-fc-sha
run_update; CALLS_TXT="$(cat "$CALLS")"
check   "exit 0"                                   0 "$RC"
contains "$OUT"       "deploying the 'fc' pool"      "auto resolves to fc when the upstream is :5002"
contains "$CALLS_TXT" "compose --profile canary up -d app-fc" "it brings up app-fc"
lacks    "$CALLS_TXT" "up -d --build app"           "it does NOT rebuild legacy — the rollback escape hatch is left alone"
contains "$CALLS_TXT" "switch --to legacy"          "a public deploy drains by default"
contains "$CALLS_TXT" "switch --to fc"              "the default drain returns traffic after verification"
teardown

setup 5001 old-legacy-sha old-fc-sha
run_update; CALLS_TXT="$(cat "$CALLS")"
contains "$OUT"       "deploying the 'legacy' pool" "auto resolves to legacy when the upstream is :5001"
contains "$CALLS_TXT" "docker build"                "it builds legacy"
contains "$CALLS_TXT" "compose up -d app"            "it recreates legacy"
lacks    "$CALLS_TXT" "app-fc"                      "it does NOT touch the fc pool"
teardown
echo

echo "--- 2. the fc image is built from an ISOLATED WORKTREE, never the tree ---"
setup 5002 old-legacy-sha old-fc-sha
run_update; CALLS_TXT="$(cat "$CALLS")"
BUILD_LINE="$(grep '^docker build' "$CALLS" | head -1)"
contains "$BUILD_LINE" "canary-fc-loop-"            "the image tag encodes the pinned commit"
lacks    "$BUILD_LINE" " $REPO"                     "the build context is NOT the working tree"
contains "$BUILD_LINE" "/fc-build-"                 "the build context IS the temp worktree"
check   "no worktree is left behind" "" "$(cd "$REPO" && git worktree list --porcelain | grep -c 'fc-build-' | sed 's/^0$//')"
teardown
echo

echo "--- 3. the pins land in the root .env, and nothing else in it moves ---"
setup 5002 old-legacy-sha old-fc-sha
run_update
contains "$(cat "$REPO/.env")" "FC_CANARY_SHA=$PIN"           "FC_CANARY_SHA is rewritten to the pin"
contains "$(cat "$REPO/.env")" "FC_IMAGE_DIGEST=sha256:"       "FC_IMAGE_DIGEST is non-empty before compose"
contains "$(cat "$REPO/.env")" "PROMPT_VERSION=2.1.0"           "PROMPT_VERSION is the runtime PromptSpec version"
lacks    "$(cat "$REPO/.env")" "PROMPT_VERSION=2.1.0@"          "the source revision is not mixed into PROMPT_VERSION"
contains "$(cat "$REPO/.env")" "SEARXNG_SECRET=\"keep-me\""   "SEARXNG_SECRET survives untouched"
BACKUP_FILE="$(find "$SANDBOX/env-backups" -type f -name 'root-env.*.bak' -print -quit)"
check   "an out-of-tree pre-run backup exists" "present" "$([ -f "$BACKUP_FILE" ] && echo present || echo absent)"
contains "$(cat "$BACKUP_FILE")" "FC_CANARY_SHA=00000000" "the backup holds the PRE-run value"
check   "the secret backup is mode 0600" "600" "$(stat -c %a "$BACKUP_FILE")"
check   "no plaintext .env backup remains in repo" "absent" "$([ -e "$REPO/.env.bak" ] && echo present || echo absent)"
check   "the rewritten root .env is mode 0600" "600" "$(stat -c %a "$REPO/.env")"
teardown

echo "--- 3a. Docker repository digests normalize; invalid digests mutate nothing ---"
setup 5002 old-legacy-sha old-fc-sha
FAKE_DIGEST_KIND=repo run_update
check   "a name@sha256 repository digest is accepted" 0 "$RC"
contains "$(cat "$REPO/.env")" "FC_IMAGE_DIGEST=sha256:" \
  "the repository name is stripped from runtime digest metadata"
teardown

setup 5002 old-legacy-sha old-fc-sha
ENV_BEFORE="$(sha256sum "$REPO/.env" | awk '{print $1}')"
FAKE_DIGEST_KIND=invalid run_update; CALLS_TXT="$(cat "$CALLS")"
check   "an invalid image digest fails closed" 1 "$RC"
contains "$OUT" ".env was not changed" "the operator sees the transactional guarantee"
check   "invalid digest leaves .env byte-identical" "$ENV_BEFORE" \
  "$(sha256sum "$REPO/.env" | awk '{print $1}')"
lacks   "$CALLS_TXT" "compose" "invalid digest never reaches compose"
teardown

echo "--- 3b. a legacy deploy teaches the escape hatch to name its commit ---"
setup 5001 old-legacy-sha old-fc-sha
run_update
check   "legacy deploy survives missing inactive-fc digest" 0 "$RC"
contains "$(cat "$REPO/.env")" "LEGACY_APP_SHA=$PIN"          "LEGACY_APP_SHA is set (kills --allow-unidentified-target on rollback)"
teardown
echo

echo "--- 4. success is claimed ONLY when the pinned commit is answering ---"
setup 5002 old-legacy-sha old-fc-sha
# Freeze the fc pool on the OLD sha: compose "succeeds", the pool stays stale.
cat > "$SANDBOX/bin/fakecurl" <<'EOF'
#!/usr/bin/env bash
url="${@: -1}"; port="${url##*:}"; port="${port%%/*}"
case "$port" in
  5001) printf 'HTTP/1.1 200 OK\r\nx-agent-arch: legacy\r\nx-agent-version: %s\r\nx-agent-specialists: 0\r\n\r\n' "$LEGACY_SHA" ;;
  5002) printf 'HTTP/1.1 200 OK\r\nx-agent-arch: fc_loop\r\nx-agent-version: %s\r\nx-agent-specialists: 0\r\n\r\n' "$FC_SHA" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$SANDBOX/bin/fakecurl"
run_update
check   "a stale pool fails the deploy"            1 "$RC"
contains "$OUT" "the new code is NOT live"          "and says so in those words"
lacks    "$OUT" "Live at https://rentcompass"       "no green banner over a stale pool"
teardown

setup 5002 old-legacy-sha old-fc-sha
# Right COMMIT, WRONG ARCHITECTURE — the shape a half-finished pool switch leaves
# behind. A sha-only "already current" check would skip the deploy here and call
# it success, so the skip must compare the full identity.
cat > "$SANDBOX/bin/fakecurl" <<'EOF'
#!/usr/bin/env bash
url="${@: -1}"; port="${url##*:}"; port="${port%%/*}"
printf 'HTTP/1.1 200 OK\r\nx-agent-arch: legacy\r\nx-agent-version: %s\r\nx-agent-specialists: 0\r\n\r\n' "$PIN"
EOF
chmod +x "$SANDBOX/bin/fakecurl"
run_update
check   "an arch mismatch fails the deploy"        1 "$RC"
contains "$OUT" "expected 'fc_loop'"                "and names the arch it wanted"
teardown
echo

echo "--- 5. the pin gate still governs everything ---"
setup 5002 old-legacy-sha old-fc-sha
echo dirty >> "$REPO/f"
run_update; CALLS_TXT="$(cat "$CALLS")"
check   "a dirty tree is refused"                  1 "$RC"
contains "$OUT"       "working tree is DIRTY"         "with the fail-closed wording"
lacks    "$CALLS_TXT" "compose"                    "and nothing is deployed"
teardown

setup 5002 old-legacy-sha old-fc-sha
echo contamination > "$REPO/untracked-build-input.py"
run_update; CALLS_TXT="$(cat "$CALLS")"
check   "an untracked build-context file is refused" 1 "$RC"
contains "$OUT"       "untracked-build-input.py"       "the contaminating path is named"
lacks    "$CALLS_TXT" "docker build"                   "and no image is built"
teardown

setup 5002 old-legacy-sha old-fc-sha
printf 'DEPLOY_PINNED_SHA=%s\nDEPLOY_PYTHON_IMAGE=python@sha256:%064d\n' \
  "0000000000000000000000000000000000000000" 2 > "$SANDBOX/pin.env"
run_update
check   "an unknown pin is refused"                1 "$RC"
contains "$OUT" "is not in this repo"               "with the original wording"
teardown

setup 5002 old-legacy-sha old-fc-sha
printf 'DEPLOY_PINNED_SHA=%s\nDEPLOY_PYTHON_IMAGE=python:3.12-slim\n' "$PIN" > "$SANDBOX/pin.env"
run_update
check   "a mutable Python base tag is refused"     1 "$RC"
contains "$OUT" "immutable digest reference"        "base image provenance fails closed"
teardown
echo

echo "--- 5b. --status is NOT gated: it is what you run when a deploy was refused ---"
setup 5002 old-legacy-sha old-fc-sha
echo dirty >> "$REPO/f"                             # would refuse a real deploy
run_update --status; CALLS_TXT="$(cat "$CALLS")"
check   "--status exits 0 on a dirty tree"         0 "$RC"
contains "$OUT"       "<- PUBLIC"                   "it marks which pool serves the public"
contains "$OUT"       "5002"                        "and prints the live upstream port"
lacks    "$CALLS_TXT" "compose"                     "and changes nothing"
lacks    "$CALLS_TXT" "docker build"                "and builds nothing"
teardown

setup 5002 old-legacy-sha old-fc-sha
( cd "$REPO" && git checkout -q --detach HEAD && git commit -q --allow-empty -m drift ) >/dev/null 2>&1
run_update --status
contains "$OUT" "a deploy would be REFUSED"         "HEAD off the pin is stated plainly, not just implied"
teardown

setup 5002 old-legacy-sha old-fc-sha
printf 'DEPLOY_PINNED_SHA=%s\n' "$PIN" > "$SANDBOX/pin.env"
run_update --status; CALLS_TXT="$(cat "$CALLS")"
check   "--status survives a missing Python image pin" 0 "$RC"
contains "$OUT" "invalid/missing"                     "the missing immutable base is reported as a deploy blocker"
lacks    "$CALLS_TXT" "docker build"                  "diagnosis still performs no build"
teardown

setup 5002 old-legacy-sha old-fc-sha
mv "$SANDBOX/pin.env" "$SANDBOX/pin.env.absent"
run_update --status; CALLS_TXT="$(cat "$CALLS")"
check   "--status survives a missing pin file"        0 "$RC"
contains "$OUT" "pin unavailable"                    "the absent pin is reported as a deploy blocker"
lacks    "$CALLS_TXT" "docker build"                 "missing metadata cannot trigger a build"
teardown
echo

echo "--- 6. already-current is a no-op, --force overrides ---"
setup 5002 old-legacy-sha old-fc-sha
FC_SHA="$PIN" LEGACY_SHA=old-legacy-sha
setup 5002 old-legacy-sha "$PIN"; FC_SHA="$PIN"
run_update; CALLS_TXT="$(cat "$CALLS")"
contains "$OUT"       "already serves fc_loop"     "a pool already on the pin AND arch is skipped"
lacks    "$CALLS_TXT" "compose"                    "and no container is recreated"
run_update --force; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "app-fc"                     "--force redeploys anyway"
teardown
echo

echo "--- 7. --drain moves public traffic away and puts it back ---"
setup 5002 old-legacy-sha old-fc-sha
run_update --drain; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "switch --to legacy"         "traffic is drained to the standby first"
contains "$CALLS_TXT" "switch --to fc"             "and returned afterwards"
contains "$CALLS_TXT" "--expect-sha $PIN"          "the return switch verifies the pinned sha"
check   "drain order: switch, deploy, switch back" "switch-compose-switch" \
        "$(grep -oE '^(switch|compose)' "$CALLS" | uniq | paste -sd- -)"
teardown

setup 5002 old-legacy-sha old-fc-sha
LEGACY_SHA=DOWN                                     # standby unhealthy
run_update --drain; CALLS_TXT="$(cat "$CALLS")"
contains "$OUT"       "not ready"                  "an unhealthy standby is reported"
lacks    "$CALLS_TXT" "switch"                     "and traffic is NOT moved onto it"
lacks    "$CALLS_TXT" "app-fc"                     "failed drain never mutates the public container"
contains "$OUT"       "no public container was redeployed" "the fail-closed outcome is explicit"
teardown

setup 5002 old-legacy-sha old-fc-sha
LEGACY_SHA=DOWN
run_update --allow-in-place; CALLS_TXT="$(cat "$CALLS")"
check    "explicit in-place override exits 0"       0 "$RC"
contains "$OUT"       "EXPLICIT --allow-in-place"  "the unsafe override is unmistakable"
contains "$CALLS_TXT" "app-fc"                     "only the explicit override mutates the public pool"
teardown
echo

echo "--- 7b. the drain is a MAINTENANCE stage and its pre-drain state is restored ---"
# K4: `release.sh` -> `update.sh --both --drain` -> `switch_pool.sh --to fc` used to
# reach `set_canary_weight.sh --weight 100 --stage flip`, i.e. a routine release could
# leave the candidate serving 100% of the public. The drain now declares
# `--stage maintenance` (the only non-flip stage the controller accepts at 100%) and
# restores the weight/rollout-id/stage it recorded from the routing include.
setup 5001 old-legacy-sha old-fc-sha
install_route 0 rollback rollback
run_update --both; CALLS_TXT="$(cat "$CALLS")"
check    "weighted host at weight 0 deploys cleanly"  0 "$RC"
contains "$CALLS_TXT" "--stage maintenance"            "the drain declares the maintenance stage"
contains "$CALLS_TXT" "--rollout-id deploy-maintenance-" "the drain uses its own maintenance rollout id"
contains "$OUT"       "restored when this pool finishes OR fails" "the operator is told the drain is temporary"
RESTORE_LINE="$(grep -- '^switch --to legacy' "$CALLS" | tail -1)"
contains "$RESTORE_LINE" "--rollout-id rollback"       "the restore replays the RECORDED rollout id"
contains "$RESTORE_LINE" "--stage rollback"            "the restore replays the RECORDED stage"
contains "$RESTORE_LINE" "--expect-sha $PIN"           "a successful restore still demands the pin"
lacks    "$CALLS_TXT" "CANARY_ALLOW_FLIP=1"            "returning to legacy never authorises a flip"
teardown

setup 5002 old-legacy-sha old-fc-sha
install_route 100 r-flip flip
run_update --both; CALLS_TXT="$(cat "$CALLS")"
check    "a host already at 100% is restored to 100%"  0 "$RC"
DRAIN_LINE="$(grep -- '^switch --to legacy' "$CALLS" | head -1)"
contains "$DRAIN_LINE"   "--stage maintenance"         "draining OFF the candidate is also a maintenance stage"
RESTORE_LINE="$(grep -- '^switch --to fc' "$CALLS" | tail -1)"
contains "$RESTORE_LINE" "--rollout-id r-flip"         "the recorded rollout id comes back"
contains "$RESTORE_LINE" "--stage flip"                "the recorded stage comes back"
contains "$CALLS_TXT"    "CANARY_ALLOW_FLIP=1"         "restoring a pre-existing 100% needs the scoped allow-flip"
check    "the allow-flip is scoped to the restore ONLY (drain=0, restore=1)" "0-1" \
         "$(grep -oE 'CANARY_ALLOW_FLIP=[01]' "$CALLS" | sed 's/.*=//' | paste -sd- -)"
contains "$OUT" "scoped to this restore only"          "and the scoping is stated to the operator"
teardown

setup 5001 old-legacy-sha old-fc-sha
install_route 0 rollback rollback
FAKE_COMPOSE_RC=1 run_update --pool legacy; CALLS_TXT="$(cat "$CALLS")"
check    "a failed redeploy still fails"               1 "$RC"
check    "the drain is unwound even when the deploy dies" "fc-legacy" \
         "$(grep -oE '^switch --to (fc|legacy)' "$CALLS" | sed 's/^switch --to //' | paste -sd- -)"
FAILED_RESTORE="$(grep -- '^switch --to legacy' "$CALLS" | tail -1)"
lacks    "$FAILED_RESTORE" "--expect-sha"              "a restore after a failure never demands the undeployed pin"
teardown
echo

echo "--- 7c. the drain carries a marker that expires with the deploy ---"
# R3/M2: `--stage maintenance` was the one route to 100% candidate traffic that
# never had to meet CANARY_ALLOW_FLIP, and nothing bounded it — a human could type
# it and park production there permanently. It is now an ACTIVE-DRAIN window:
# update.sh writes a marker naming this run's rollout id before the drain and
# deletes it after the restore, and the controllers refuse the stage without it.
setup 5001 old-legacy-sha old-fc-sha
install_route 0 rollback rollback
run_update --both; CALLS_TXT="$(cat "$CALLS")"
check    "the marked drain deploys cleanly"          0 "$RC"
contains "$CALLS_TXT" "switchmarker deploy-maintenance-" "the marker exists while the drain switch runs"
contains "$CALLS_TXT" "switchroute $ROUTE_CONF_PATH"     "switch_pool writes the SAME route file update.sh read"
check    "the drain window is closed afterwards" "absent" \
         "$([ -e "$SANDBOX/maintenance-marker" ] && echo present || echo absent)"
teardown

setup 5001 old-legacy-sha old-fc-sha
install_route 0 rollback rollback
: > "$SANDBOX/maintenance-marker"                    # debris from a killed run
run_update --both; CALLS_TXT="$(cat "$CALLS")"
check    "a stale marker does not survive the next deploy" 0 "$RC"
contains "$OUT" "stale maintenance drain marker"     "and the operator is told it was cleared"
check    "and it is gone at the end"          "absent" \
         "$([ -e "$SANDBOX/maintenance-marker" ] && echo present || echo absent)"
teardown
echo

echo "--- 7d. a leftover maintenance route is NOT replayed as an authorised 100% ---"
# R3/M1: the restore hands itself CANARY_ALLOW_FLIP=1 whenever it returns to fc.
# That is defensible for an exposure the operator had already authorised, but the
# recorded state here is weight 100 at stage `maintenance` — the debris of an
# EARLIER drain that never unwound. Replaying it would launder a temporary stage
# into a permanent cutover, which is what the stage exists to prevent.
setup 5002 old-legacy-sha old-fc-sha
install_route 100 deploy-maintenance-abc1234 maintenance
run_update --pool fc; CALLS_TXT="$(cat "$CALLS")"
check    "the deploy itself still succeeds"          0 "$RC"
contains "$CALLS_TXT" "switch --to legacy"           "traffic is still drained off the pool being rebuilt"
lacks    "$CALLS_TXT" "switch --to fc"               "but it is NOT put back onto the candidate"
lacks    "$CALLS_TXT" "CANARY_ALLOW_FLIP=1"          "and no flip authorisation is self-granted"
contains "$OUT" "NOT restoring the pre-drain route"  "the refusal is explicit"
contains "$OUT" "UNFINISHED drain"                   "and names why the recorded state is not authorisation"
contains "$OUT" "--stage flip"                       "with the deliberate command that would do it on purpose"
teardown

# The contrast: a route recorded at 100% under stage `flip` IS an authorised
# exposure, and is restored exactly as before.
setup 5002 old-legacy-sha old-fc-sha
install_route 100 r-flip flip
run_update --pool fc; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "switch --to fc"               "an authorised 100% is still restored"
contains "$CALLS_TXT" "CANARY_ALLOW_FLIP=1"          "with the scoped allow-flip"
teardown
echo

echo "--- 7e. a SIGNAL takes the failure branch, not 'REDEPLOY SUCCEEDED' ---"
# R3/M3: the EXIT trap does run on SIGINT/SIGTERM/SIGHUP, but `$?` inside it is 0.
# The restore therefore took the SUCCESS branch: it demanded `--expect-sha <new
# pin>` from a pool that was never rebuilt, failed, and reported "REDEPLOY
# SUCCEEDED BUT THE UPSTREAM IS STILL ON ..." while production sat on the drain
# target. The signal is now captured and passed to the trap as a failure code.
setup 5001 old-legacy-sha old-fc-sha
install_route 0 rollback rollback
# Interrupt from inside the deploy: the fake signals update.sh itself, so the
# trap fires at exactly the point a Ctrl-C during a real `compose up` would.
cat > "$SANDBOX/bin/fakecompose" <<'EOF'
#!/usr/bin/env bash
echo "compose $*" >> "$CALLS"
kill -TERM "$PPID"
exit 0
EOF
chmod +x "$SANDBOX/bin/fakecompose"
run_update --pool legacy; CALLS_TXT="$(cat "$CALLS")"
check    "an interrupted deploy exits with the signal code" 143 "$RC"
contains "$OUT" "INTERRUPTED by signal 15"           "the interrupt is named"
contains "$OUT" "The redeploy did NOT complete"      "and is not reported as a success"
lacks    "$OUT" "REDEPLOY SUCCEEDED"                 "the success wording never appears"
check    "the drain is still unwound"                "fc-legacy" \
         "$(grep -oE '^switch --to (fc|legacy)' "$CALLS" | sed 's/^switch --to //' | paste -sd- -)"
INTERRUPTED_RESTORE="$(grep -- '^switch --to legacy' "$CALLS" | tail -1)"
lacks    "$INTERRUPTED_RESTORE" "--expect-sha"       "and never demands a pin nothing deployed"
check    "the drain window is closed on the way out" "absent" \
         "$([ -e "$SANDBOX/maintenance-marker" ] && echo present || echo absent)"
teardown
echo

echo "--- 8. the operator is warned when the rollback target drifts ---"
setup 5002 old-legacy-sha old-fc-sha
run_update
contains "$OUT" "It is your rollback target"       "drift on the standby pool is called out"
contains "$OUT" "--pool legacy"                    "with the command that fixes it"
teardown

setup 5002 old-legacy-sha old-fc-sha
run_update --both; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "docker build"                "--both builds legacy"
contains "$CALLS_TXT" "compose up -d app"            "--both deploys legacy"
contains "$CALLS_TXT" "app-fc"                     "--both deploys fc"
DEPLOY_ORDER="$(awk '/^compose / { if ($0 ~ /app-fc/) print "fc"; else if ($0 ~ /up -d app$/) print "legacy" }' "$CALLS" | paste -sd- -)"
check   "when fc is public, --both deploys standby first" "legacy-fc" "$DEPLOY_ORDER"
lacks    "$OUT"       "It is your rollback target" "and then has no drift to warn about"
teardown

setup 5001 old-legacy-sha old-fc-sha
run_update --both; CALLS_TXT="$(cat "$CALLS")"
DEPLOY_ORDER="$(awk '/^compose / { if ($0 ~ /app-fc/) print "fc"; else if ($0 ~ /up -d app$/) print "legacy" }' "$CALLS" | paste -sd- -)"
check   "when legacy is public, --both deploys standby first" "fc-legacy" "$DEPLOY_ORDER"
contains "$CALLS_TXT" "switch --to fc"              "legacy-public release drains to refreshed fc"
contains "$CALLS_TXT" "switch --to legacy"          "and returns only after legacy verifies"
teardown
echo

printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
