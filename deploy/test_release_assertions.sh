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
  printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=0\nCANARY_USE_MCP_TOOLS=0\n' \
    > "$SANDBOX/root.env"
  # The rollout preflight reads the routing include, never the host's /etc copy.
  install_route 0 rollback rollback
  # ...and, when there is no weighted include, the single upstream line instead.
  mkdir -p "$SANDBOX/nginx"
  install_upstream 5001

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

# install_route <weight> <rollout-id> <stage> — the state the maintenance drain
# restores, and therefore the state a release ENDS on.
install_route() {
  ROUTE_CONF_PATH="$SANDBOX/canary-routing.conf"
  printf '# rentcompass-canary-weight: %s\n# rentcompass-rollout-id: %s\n# rentcompass-rollout-stage: %s\n' \
    "$1" "$2" "$3" > "$ROUTE_CONF_PATH"
}
# remove_route — the shape of a host that has NOT been migrated to the weighted
# include (the untracked snippet is absent from /etc/nginx/snippets). The release
# preflight must survive it AND still resolve where the traffic ends up.
remove_route() { ROUTE_CONF_PATH="$SANDBOX/no-weighted-include.conf"; rm -f "$ROUTE_CONF_PATH"; }
# install_upstream <port> — the single `server 127.0.0.1:PORT;` line switch_pool.sh
# owns; on a single-upstream host it IS the whole rollout state.
install_upstream() {
  SITE_CONF_PATH="$SANDBOX/nginx/site.conf"
  printf 'upstream rentcompass_app {\n    server 127.0.0.1:%s;\n}\n' "$1" > "$SITE_CONF_PATH"
}
remove_upstream() { SITE_CONF_PATH="$SANDBOX/nginx/absent-site.conf"; rm -f "$SITE_CONF_PATH"; }
# manager_v1_candidate — the only OTHER identity the whitelist accepts. On this
# two-pool host it takes the single candidate slot away from fc_loop (R3-M8).
manager_v1_candidate() {
  printf 'CANARY_AGENT_ARCH=manager_v1\nCANARY_MANAGER_V1_SPECIALISTS=1\nCANARY_USE_MCP_TOOLS=0\n' \
    > "$SANDBOX/root.env"
}

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
    RELEASE_ENV_FILE="$SANDBOX/root.env" \
    RELEASE_ROUTE_CONF="$ROUTE_CONF_PATH" \
    RELEASE_SITE_CONF="$SITE_CONF_PATH" \
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

echo "--- 9. the flip gate fires on a CHANGE in exposure, never on a rebuild ---"
# K4: `bash deploy/release.sh` carries no rollout flag at all, yet `--both --drain`
# used to be able to leave the candidate on 100% of the public.
#
# R3-H3: the first version of that gate compared only the END state, so it also
# refused every ROUTINE release on a host where the candidate was already the sole
# public pool — which is this box since 07-27. The gate now compares the START and
# END exposure and fires only when an architecture that is not at 100% ends there.
setup green
install_route 0 rollback rollback
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a weight-0 host releases normally"     0 "$RC"
contains "$OUT" "candidate  arch=fc_loop specialists=0 mcp=0" "the resolved candidate identity is shown"
contains "$OUT" "ends at    candidate weight 0% stage rollback" "so is the weight/stage the run ends on"
teardown

# The pool that is ALREADY at 100% staying at 100% is not a flip.
setup green
install_route 100 r-flip flip
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a rebuild of the pool already at 100% is NOT a flip" 0 "$RC"
contains "$OUT"       "routing    UNCHANGED"         "and the preflight says the routing does not change"
contains "$OUT"       "No cutover happens"           "spelling out why no flag is needed"
contains "$CALLS_TXT" "update --both --drain"        "so the routine release proceeds with no CANARY_ALLOW_FLIP"
teardown

# ...but the SAME weight with a DIFFERENT architecture in the candidate slot is.
setup green
install_route 100 r-flip flip
manager_v1_candidate
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a release that puts a NEW arch on 100% is refused" 1 "$RC"
contains "$OUT"       "END with the candidate"       "and says exactly what it refused"
contains "$OUT"       "100% currently runs 'fc_loop'" "naming the architecture being displaced"
contains "$OUT"       "50% is the highest authorised rollout stage" "citing the runbook rule"
lacks    "$OUT"       "switch_pool.sh --to legacy"   "and never suggests demoting production to the legacy arch"
check    "the pin did NOT move"                  "$OLD_SHA" "$(pin_now)"
check    "HEAD did NOT move"                     "$OLD_SHA" "$(head_now)"
lacks    "$CALLS_TXT" "update"                   "update.sh is never reached"
lacks    "$CALLS_TXT" "preflight"                "and runtime state is left alone"
teardown

setup green
install_route 100 r-flip flip
manager_v1_candidate
CANARY_ALLOW_FLIP=1 run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "CANARY_ALLOW_FLIP=1 authorises the flip release" 0 "$RC"
contains "$OUT"       "authorising a cutover"          "and the authorisation is announced"
contains "$CALLS_TXT" "update --both --drain"          "and the deploy proceeds"
teardown

# A recorded 100%@maintenance is the debris of an interrupted drain: update.sh
# refuses to replay it, so the run ENDS on legacy. That is a DECREASE, not a flip.
setup green
install_route 100 deploy-maintenance-abc1234 maintenance
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "an unfinished drain is not treated as a flip" 0 "$RC"
contains "$OUT"       "UNFINISHED drain"               "the debris is named"
contains "$OUT"       "ENDS with public traffic on legacy" "and the real end state is stated"
contains "$CALLS_TXT" "update --both --drain"          "and the release proceeds"
teardown

setup green
install_route 0 rollback rollback
printf 'CANARY_AGENT_ARCH=manager_v1\nCANARY_MANAGER_V1_SPECIALISTS=0\nCANARY_USE_MCP_TOOLS=0\n' > "$SANDBOX/root.env"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a compatibility-shell manager_v1 candidate is refused" 1 "$RC"
contains "$OUT"       "unsupported candidate identity" "the whitelist is named"
lacks    "$CALLS_TXT" "update"                   "and nothing ships"
teardown
echo

echo "--- 9b. R3-M4: the documented boolean spellings must not kill a release ---"
setup green
install_route 0 rollback rollback
printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=false\nCANARY_USE_MCP_TOOLS=no\n' > "$SANDBOX/root.env"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "false/no normalise to 0 instead of dying" 0 "$RC"
contains "$OUT"       "arch=fc_loop specialists=0 mcp=0" "and are reported in canonical form"
contains "$CALLS_TXT" "update --both --drain"           "so the release proceeds"
teardown

setup green
install_route 0 rollback rollback
printf 'CANARY_AGENT_ARCH=manager_v1\nCANARY_MANAGER_V1_SPECIALISTS=true\nCANARY_USE_MCP_TOOLS=off\n' > "$SANDBOX/root.env"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "true/off normalise to 1/0 instead of dying" 0 "$RC"
contains "$OUT"       "arch=manager_v1 specialists=1 mcp=0" "and reach the whitelist in canonical form"
teardown

setup green
install_route 0 rollback rollback
printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=maybe\nCANARY_USE_MCP_TOOLS=0\n' > "$SANDBOX/root.env"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a value that is neither true nor false is refused" 1 "$RC"
contains "$OUT"       "neither a true (1/true/yes/on) nor a false" "loudly, by name"
lacks    "$CALLS_TXT" "update"                                    "and nothing ships"
teardown
echo

echo "--- 10. TODAY'S HOST: single upstream on :5002, no weighted include ---"
# R3/H2 (round 2): the preflight read these markers WITHOUT `|| true`. `set -euo
# pipefail` turns `sed` on a missing file into exit 2, which errexit turns into a
# silent abort — the entire release died after "Release plan" with no output.
setup green
remove_route
install_upstream 5001
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "no weighted include does not abort the release" 0 "$RC"
contains "$OUT" "SINGLE-UPSTREAM mode"        "the routing mode is named, not silently skipped"
contains "$OUT" "127.0.0.1:5001 = legacy"     "and the sole upstream is resolved to a pool"
contains "$OUT" "candidate weight 0%"         "and to the candidate exposure it means"
contains "$CALLS_TXT" "update --both --drain" "so the release proceeds"
teardown

# THE R3-H3 REGRESSION TEST. This is the exact live state of the production box:
# no /etc/nginx/snippets/rentcompass-canary-routing.conf, and the site conf's sole
# upstream is 127.0.0.1:5002. `bash deploy/release.sh` must work with no new flags.
setup green
remove_route
install_upstream 5002
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "TODAY'S HOST: a routine release needs NO flag"  0 "$RC"
contains "$OUT" "SINGLE-UPSTREAM mode"        "the routing mode is named"
contains "$OUT" "routing    UNCHANGED"        "and the run is identified as a rebuild, not a cutover"
contains "$CALLS_TXT" "update --both --drain" "so update.sh is handed the deploy"
check    "the pin advanced"                   "$NEW_SHA" "$(pin_now)"
teardown

setup green
remove_route
install_upstream 5002
run_release --dry-run; CALLS_TXT="$(cat "$CALLS")"
check    "TODAY'S HOST: --dry-run exits 0"    0 "$RC"
contains "$OUT" "routing    UNCHANGED"        "and previews the unchanged routing"
check    "the pin is untouched by a dry run"  "$OLD_SHA" "$(pin_now)"
lacks    "$CALLS_TXT" "update"                "and nothing is deployed"
teardown

# The manager_v1 cutover on the same host IS a flip: :5002 is the ONE candidate
# slot, so a manager_v1 canary REPLACES the fc_loop pool that serves the public.
setup green
remove_route
install_upstream 5002
manager_v1_candidate
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "a manager_v1 cutover on the live pool is refused" 1 "$RC"
contains "$OUT" "END with the candidate"      "and says exactly what it refused"
contains "$OUT" "single-upstream mode"        "naming the routing mode it resolved"
contains "$OUT" "CANDIDATE SLOT IS EXCLUSIVE" "R3-M8: the slot consequence is stated"
contains "$OUT" "is REPLACED, not compared against" "including the loss of the fc_loop control arm"
contains "$OUT" "rollback target stays the 'legacy' pool" "and what rollback actually means here"
lacks    "$OUT" "switch_pool.sh --to legacy"  "and the remedy never demotes production to legacy"
check    "the pin did NOT move"               "$OLD_SHA" "$(pin_now)"
lacks    "$CALLS_TXT" "update"                "update.sh is never reached"
teardown

setup green
remove_route
install_upstream 5002
manager_v1_candidate
run_release --dry-run; CALLS_TXT="$(cat "$CALLS")"
check    "--dry-run NEVER dies on a policy gate" 0 "$RC"
contains "$OUT" "WOULD BE REFUSED"               "it says what would happen"
contains "$OUT" "the flag it needs is CANARY_ALLOW_FLIP=1" "and names the flag"
check    "the pin is untouched"                  "$OLD_SHA" "$(pin_now)"
lacks    "$CALLS_TXT" "update"                   "and still deploys nothing"
teardown

# A slot that ALREADY runs manager_v1 (its image tag says so) is a rebuild again.
setup green
remove_route
install_upstream 5002
manager_v1_candidate
printf 'FC_CANARY_IMAGE=uk-rent-agent:canary-manager-v1-abc1234\n' >> "$SANDBOX/root.env"
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "re-releasing the arch the slot already runs is not a flip" 0 "$RC"
contains "$OUT" "routing    UNCHANGED"        "the live arch is read from FC_CANARY_IMAGE"
lacks    "$OUT" "CANDIDATE SLOT IS EXCLUSIVE" "and there is no slot-replacement warning"
teardown

setup green
remove_route
remove_upstream
run_release --yes; CALLS_TXT="$(cat "$CALLS")"
check    "neither routing file readable still does not abort silently" 0 "$RC"
contains "$OUT" "upstream UNKNOWN"            "the unresolved end state is stated"
contains "$OUT" "could not be resolved"       "with a warning rather than silence"
contains "$CALLS_TXT" "update --both --drain" "and update.sh, which refuses to guess, decides"
teardown
echo

echo "--- 11. R3-M2: --skip-answer-probe reaches update.sh ---"
setup green
remove_route
install_upstream 5002
run_release --yes --skip-answer-probe; CALLS_TXT="$(cat "$CALLS")"
check    "exit 0"                             0 "$RC"
contains "$CALLS_TXT" "update --both --drain --skip-answer-probe" \
         "the opt-out is appended to the default update policy"
teardown

setup green
remove_route
install_upstream 5002
run_release --yes --skip-answer-probe -- --pool fc; CALLS_TXT="$(cat "$CALLS")"
contains "$CALLS_TXT" "update --pool fc --skip-answer-probe" \
         "and to an explicit one"
teardown
echo

printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
