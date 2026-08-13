#!/usr/bin/env bash
# Rehearse deploy/release.sh end to end. Offline, free, no docker, no root, no nginx.
#
#   bash deploy/test_release_assertions.sh
#
# Every external command release.sh touches is injectable (RELEASE_*_CMD), so the
# REAL script runs here — this is not a re-implementation of its logic.
#
# release.sh advances the deploy pin, which is the one thing standing between a
# merge and the public site. So the assertions are mostly about what must NOT
# happen: no re-pin on a dirty tree, no re-pin on red CI, no re-pin without
# confirmation, no source/pin movement when persistent-state maintenance fails,
# and — when the deploy fails after the pin already moved — no silence about the
# pin now naming a commit that is not running.
set -u
PASS=0; FAIL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

check() { if [ "$2" = "$3" ]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
          else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }
contains() { if grep -qF -- "$2" <<<"$1"; then printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1))
             else printf '\033[31mFAIL\033[0m %s\n       missing: %s\n       got:     %s\n' "$3" "$2" "$1"; FAIL=$((FAIL+1)); fi; }
lacks()  { if grep -qF -- "$2" <<<"$1"; then printf '\033[31mFAIL\033[0m %s\n       unexpectedly present: %s\n' "$3" "$2"; FAIL=$((FAIL+1))
           else printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1)); fi; }

SANDBOX=""
# setup <ci_conclusion|none|nogh> — builds a repo whose remote-tracking mainline
# is one commit AHEAD of the checked-out HEAD, i.e. a release is due.
setup() {
  SANDBOX="$(mktemp -d)"
  local repo="$SANDBOX/repo"; mkdir -p "$repo/deploy" "$SANDBOX/bin"
  cp "$ROOT/deploy/release.sh" "$repo/deploy/release.sh"

  ( cd "$repo"
    git init -q .; git config user.email t@t; git config user.name t
    echo one > f; git add -A; git commit -qm c1
    OLD=$(git rev-parse HEAD)
    echo two > f; git add -A; git commit -qm c2
    NEW=$(git rev-parse HEAD)
    # a remote-tracking ref, without needing an actual remote
    git update-ref refs/remotes/origin/main "$NEW"
    git checkout -q --detach "$OLD"
  ) >/dev/null 2>&1
  OLD_SHA="$(cd "$repo" && git rev-parse HEAD)"
  NEW_SHA="$(cd "$repo" && git rev-parse refs/remotes/origin/main)"
  printf 'DEPLOY_PINNED_SHA=%s\n' "$OLD_SHA" > "$SANDBOX/pin.env"

  cat > "$SANDBOX/bin/fakegh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
case "$FAKE_CI" in
  green)   printf 'Tests (Python 3.12)\tcompleted\tsuccess\nCompose smoke\tcompleted\tsuccess\n' ;;
  red)     printf 'Tests (Python 3.12)\tcompleted\tfailure\nCompose smoke\tcompleted\tsuccess\n' ;;
  pending) printf 'Tests (Python 3.12)\tin_progress\tunknown\nCompose smoke\tcompleted\tsuccess\n' ;;
  missing) printf 'Tests (Python 3.12)\tcompleted\tsuccess\n' ;;
  unknown) printf 'Tests (Python 3.12)\tcompleted\tunknown\nCompose smoke\tcompleted\tsuccess\n' ;;
  none)    : ;;
  apifail) exit 1 ;;
esac
EOF
  cat > "$SANDBOX/bin/fakesudo" <<'EOF'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
# emulate `sudo tee FILE` on a root-owned file we are actually allowed to write
if [ "${1:-}" = "tee" ]; then cat > "$2"; fi
EOF
  cat > "$SANDBOX/bin/fakeupdate" <<'EOF'
#!/usr/bin/env bash
echo "update $*" >> "$CALLS"
exit "${FAKE_UPDATE_RC:-0}"
EOF
  cat > "$SANDBOX/bin/fakepreflight" <<'EOF'
#!/usr/bin/env bash
echo "preflight $*" >> "$CALLS"
exit "${FAKE_PREFLIGHT_RC:-0}"
EOF
  chmod +x "$SANDBOX"/bin/*
  export CALLS="$SANDBOX/calls.log"; : > "$CALLS"
  export FAKE_CI="${1:-green}" FAKE_UPDATE_RC=0 FAKE_PREFLIGHT_RC=0
  REPO="$repo"
}
teardown() { [ -n "$SANDBOX" ] && rm -rf "$SANDBOX"; SANDBOX=""; }

# Sets OUT and RC. Not a command substitution: that subshell would swallow RC.
run_release() {
  local stdin_data="${RELEASE_STDIN:-}"
  ( cd "$REPO" && \
    RELEASE_REPO_DIR="$REPO" \
    DEPLOY_PIN_ENV="$SANDBOX/pin.env" \
    RELEASE_GH_CMD="$SANDBOX/bin/fakegh" \
    RELEASE_SUDO_CMD="$SANDBOX/bin/fakesudo" \
    RELEASE_UPDATE_CMD="$SANDBOX/bin/fakeupdate" \
    RELEASE_RUNTIME_MAINTENANCE_CMD="$SANDBOX/bin/fakepreflight" \
    RELEASE_REQUIRED_CHECKS="Tests (Python 3.12),Compose smoke" \
    bash deploy/release.sh --no-fetch "$@" <<<"$stdin_data" ) > "$SANDBOX/out.txt" 2>&1
  RC=$?
  OUT="$(cat "$SANDBOX/out.txt")"
}
pin_now() { grep '^DEPLOY_PINNED_SHA=' "$SANDBOX/pin.env" | cut -d= -f2; }
head_now() { (cd "$REPO" && git rev-parse HEAD); }

# ══════════════════════════════════════════════════════════════════════════
echo "--- 1. the happy path: checkout, re-pin, hand off ---"
setup green
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "exit 0"                          0 "$RC"
check    "HEAD moved to the mainline tip"  "$NEW_SHA" "$(head_now)"
check    "pin advanced to the same commit" "$NEW_SHA" "$(pin_now)"
contains "$CALLS_TXT" "update"              "update.sh was invoked"
contains "$CALLS_TXT" "update --both --drain" "a release refreshes both pools with safe drain by default"
contains "$CALLS_TXT" "preflight --repair"    "persistent runtime maintenance ran in repair mode"
contains "$CALLS_TXT" $'preflight --repair\nupdate --both --drain' "maintenance completed before deployment"
contains "$OUT"       "required CI checks"  "the CI verdict is reported"
teardown
echo

echo "--- 2. the pin does NOT move when the release must not happen ---"
setup red
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "red CI aborts"                   1 "$RC"
check    "pin untouched"                   "$OLD_SHA" "$(pin_now)"
check    "HEAD untouched"                  "$OLD_SHA" "$(head_now)"
contains "$OUT"       "concluded 'failure'" "and says why"
lacks    "$CALLS_TXT" "update"              "update.sh is never reached"
lacks    "$CALLS_TXT" "preflight"           "runtime state is not touched after red CI"
teardown

setup red
run_release --yes --allow-failing-ci
check    "--allow-failing-ci overrides"    0 "$RC"
check    "pin advanced under the override" "$NEW_SHA" "$(pin_now)"
teardown

setup green
echo dirty >> "$REPO/f"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a dirty tree aborts"             1 "$RC"
check    "pin untouched"                   "$OLD_SHA" "$(pin_now)"
contains "$OUT"       "DIRTY"               "and says why"
lacks    "$CALLS_TXT" "update"              "update.sh is never reached"
lacks    "$CALLS_TXT" "preflight"           "runtime state is not touched for a dirty release"
teardown

setup green
RELEASE_STDIN="n" run_release; CALLS_TXT="$(cat "$CALLS")"
check    "answering 'n' aborts"            1 "$RC"
check    "pin untouched"                   "$OLD_SHA" "$(pin_now)"
check    "HEAD untouched"                  "$OLD_SHA" "$(head_now)"
lacks    "$CALLS_TXT" "update"              "nothing is deployed"
lacks    "$CALLS_TXT" "preflight"           "declining leaves runtime state untouched"
teardown

setup green
run_release --dry-run; CALLS_TXT="$(cat "$CALLS")"
check    "--dry-run exits 0"               0 "$RC"
check    "pin untouched"                   "$OLD_SHA" "$(pin_now)"
check    "HEAD untouched"                  "$OLD_SHA" "$(head_now)"
contains "$OUT"       "Release plan"        "but the plan is printed"
lacks    "$CALLS_TXT" "update"              "and nothing is deployed"
lacks    "$CALLS_TXT" "preflight"           "dry-run never repairs persistent state"
teardown
echo

echo "--- 3. persistent-state maintenance fails before source or pin can move ---"
setup green
FAKE_PREFLIGHT_RC=42 run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "maintenance failure aborts"        1 "$RC"
check    "pin untouched"                     "$OLD_SHA" "$(pin_now)"
check    "HEAD untouched"                    "$OLD_SHA" "$(head_now)"
contains "$CALLS_TXT" "preflight --repair"  "the repair was attempted"
lacks    "$CALLS_TXT" "update"              "deployment is never reached"
contains "$OUT" "source, pin and containers were not changed" "the no-mutation guarantee is explicit"
teardown
echo

echo "--- 4. missing, pending and unknown CI all fail closed ---"
setup none
run_release --yes
check    "no reported checks aborts" 1 "$RC"
contains "$OUT" "No CI checks reported" "the missing evidence is explicit"
teardown

setup apifail
run_release --yes
check    "a gh api failure aborts"   1 "$RC"
contains "$OUT" "gh api failed"      "the unavailable gate is explicit"
teardown

setup pending
run_release --yes
check    "a pending required check aborts" 1 "$RC"
contains "$OUT" "not completed" "pending is not treated as green"
teardown

setup missing
run_release --yes
check    "a missing required check aborts" 1 "$RC"
contains "$OUT" "MISSING" "the required check name is reported"
teardown

setup unknown
run_release --yes
check    "an unknown conclusion aborts" 1 "$RC"
contains "$OUT" "unknown/non-success" "unknown is not treated as green"
teardown
echo

echo "--- 5. a failed deploy must not leave the pin lying about what runs ---"
setup green
FAKE_UPDATE_RC=1 run_release --yes
check    "the failure propagates"          1 "$RC"
check    "the pin DID move (the gate needs it before update.sh runs)" "$NEW_SHA" "$(pin_now)"
contains "$OUT" "DEPLOY FAILED after the pin was advanced" "the divergence is stated, not left silent"
contains "$OUT" "may be running that commit without passing /ready" "the process/readiness uncertainty is stated accurately"
contains "$OUT" "update.sh --both --drain" "recovery never recommends an unsafe in-place redeploy"
contains "$OUT" "$OLD_SHA"                 "and the previous pin is quoted for the restore"
teardown
echo

echo "--- 6. it is idempotent: a second run changes nothing ---"
setup green
run_release --yes
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "second run exits 0"              0 "$RC"
check    "pin still the tip"               "$NEW_SHA" "$(pin_now)"
contains "$OUT" "nothing to advance"        "and says it had nothing to do"
contains "$CALLS_TXT" "update"              "while still handing off (update.sh decides if a redeploy is needed)"
contains "$CALLS_TXT" "preflight --repair"  "idempotent releases still maintain persistent state"
teardown
echo

echo "--- 7. --ref off mainline is allowed but flagged; -- passes through ---"
setup green
run_release --yes --ref "$OLD_SHA"
check    "an explicit --ref releases it"   0 "$RC"
check    "pin follows --ref"               "$OLD_SHA" "$(pin_now)"
teardown

setup green
run_release --yes -- --both --drain; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "update --both --drain" "everything after -- reaches update.sh"
teardown

setup green
run_release --yes -- --pool legacy; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "update --pool legacy" "explicit update arguments override the safe release defaults"
teardown
echo

printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
