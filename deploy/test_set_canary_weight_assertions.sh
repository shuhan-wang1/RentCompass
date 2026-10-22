#!/usr/bin/env bash
# Rehearse deploy/set_canary_weight.sh's weighted-routing path end to end.
# Offline, free, no docker, no root, no nginx, NO NETWORK, NO BILLED TURN.
#
#   bash deploy/test_set_canary_weight_assertions.sh
#
# WHY THIS FILE EXISTS
# --------------------
# set_canary_weight.sh is the lever that decides how much of the public sees the
# candidate, and the only weight-0 emergency rollback on a weighted host. It
# used to have NO offline exerciser: switch_pool.sh, update.sh and release.sh
# each had one, and the controller they all delegate to had none. An earlier
# review had to build a throwaway sandbox to answer basic
# questions about it ("does weight 0 ever wait on the answer probe?"), and that
# sandbox could not model the public endpoint correctly, so its weight-0 run died
# at the post-reload verification on a fake's limitation rather than on anything
# the product did. This file is that sandbox, done properly and shipped.
#
# WHAT IS INJECTED. Everything that could touch the world:
#   CANARY_{ROUTE_CONF,ENV_FILE,REPO_DIR}      - config, in a mktemp sandbox
#   CANARY_{TEST_CMD,RELOAD_CMD}               - `nginx -t` / `systemctl reload`
#   CANARY_{WRITE_CMD,MOVE_CMD}                - `sudo tee` / `sudo mv`
#   CANARY_CURL_CMD                            - every /ready probe
#   CANARY_{PUBLIC,LEGACY,CANDIDATE}_URL       - .invalid hosts, never :5001/:5002
#   CANARY_ANSWER_PROBE_CMD                    - the ONE thing that drives a
#                                                billed turn; here it only logs
#   RENTCOMPASS_{DEPLOY_LOCK_FILE,MAINTENANCE_MARKER}
#
# A `curl` stub is also put first on PATH that records any use and fails: if the
# real binary were ever reached, the last assertion fails loudly instead of a
# probe silently reaching 127.0.0.1:5001/5002. Same for `sudo`, `nginx` and
# `systemctl`.
#
# THE PUBLIC-ENDPOINT FAKE is the part the throwaway sandbox got wrong. The real
# public endpoint answers as whichever pool the route file the script JUST WROTE
# selects, and reports it in `X-Rentcompass-Pool`. So the fake reads
# `# rentcompass-canary-weight:` back out of $ROUTE_CONF and assigns per cookie:
# fixed legacy at 0, fixed candidate at 100, and a deterministic split in between.
# Without that, weight 0 "fails" on the fake, not on the product.
set -u
PASS=0; FAIL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

check() { if [ "$2" = "$3" ]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
          else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }
contains() { if grep -qF -- "$2" <<<"$1"; then printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1))
             else printf '\033[31mFAIL\033[0m %s\n       missing: %s\n       got:     %s\n' "$3" "$2" "$1"; FAIL=$((FAIL+1)); fi; }
lacks()  { if grep -qF -- "$2" <<<"$1"; then printf '\033[31mFAIL\033[0m %s\n       unexpectedly present: %s\n' "$3" "$2"; FAIL=$((FAIL+1))
           else printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1)); fi; }

SHA_LEGACY=0123456789abcdef0123456789abcdef01234567
SHA_CANDIDATE=0123456789abcdef0123456789abcdef01234567
SANDBOX=""; VIOLATIONS="$(mktemp -u)/violations"

setup() {   # setup <weight the route is currently on>
  SANDBOX="$(mktemp -d)"; mkdir -p "$SANDBOX/bin"
  VIOLATIONS="$SANDBOX/../set-canary-weight-violations"
  ROUTE_CONF_PATH="$SANDBOX/canary-routing.conf"
  printf '# rentcompass-canary-weight: %s\n# rentcompass-canary-salt: rentcompass-v1\n# rentcompass-rollout-id: seed\n# rentcompass-rollout-stage: seed\n' \
    "$1" > "$ROUTE_CONF_PATH"

  # The candidate identity the deploy path resolves, and the two pins the
  # controller verifies against — rebuilt pools, both stating specialists 0.
  {
    printf 'CANARY_AGENT_ARCH=fc_loop\n'
    printf 'CANARY_MANAGER_V1_SPECIALISTS=0\n'
    printf 'CANARY_USE_MCP_TOOLS=0\n'
    printf 'LEGACY_APP_SHA=%s\n' "$SHA_LEGACY"
    printf 'FC_CANARY_SHA=%s\n' "$SHA_CANDIDATE"
  } > "$SANDBOX/root.env"

  # `-` in the specialists slot = the pool sends NO X-Agent-Specialists header
  # (a pre-2026-08-31 image). The DEFAULT models rebuilt pools: both state 0,
  # both state a full 40-char sha.
  : "${PLEGACY:=legacy $SHA_LEGACY 0}"
  : "${PCANDIDATE:=fc_loop $SHA_CANDIDATE 0}"

  cat > "$SANDBOX/bin/fakecurl" <<'EOF'
#!/usr/bin/env bash
# Args end with the URL. A "Cookie: session=..." header may precede it.
url="${@: -1}"
cookie=""
prev=""
for a in "$@"; do
  case "$prev" in -H) case "$a" in Cookie:*) cookie="${a#Cookie: session=}" ;; esac ;; esac
  prev="$a"
done
echo "curl $url cookie=$cookie" >> "$CALLS"
emit() { # arch sha specialists [pool]
  read -r arch sha specialists <<<"$1"
  [ "$arch" = "DOWN" ] && exit 1
  printf 'HTTP/1.1 200 OK\r\nx-agent-arch: %s\r\nx-agent-version: %s\r\n' "$arch" "$sha"
  [ "$specialists" = "-" ] || printf 'x-agent-specialists: %s\r\n' "$specialists"
  [ -n "${2:-}" ] && printf 'x-rentcompass-pool: %s\r\n' "$2"
  printf '\r\n'
}
case "$url" in
  *legacy.invalid*)    emit "$PLEGACY" ;;
  *candidate.invalid*) emit "$PCANDIDATE" ;;
  *public.invalid*)
    # Answer as whichever pool the CURRENT route file selects for this cookie.
    # PUBLIC_LIE=1 models the edge NOT having taken the new route: the only way to
    # reach the post-reload verification failure without breaking the pools first.
    w="$(sed -n 's/^# rentcompass-canary-weight: \([0-9][0-9]*\)$/\1/p' "$ROUTE_CONF_PATH" | head -1)"
    [ "${PUBLIC_LIE:-0}" = 1 ] && { emit "$PCANDIDATE" candidate; exit 0; }
    case "$w" in
      0)   emit "$PLEGACY" legacy ;;
      100) emit "$PCANDIDATE" candidate ;;
      *)   # deterministic per-cookie split, so both cohorts appear within a few probes
           h=$(( $(cksum <<<"$cookie" | awk '{print $1}') % 100 ))
           if [ "$h" -lt "${w:-0}" ]; then emit "$PCANDIDATE" candidate
           else emit "$PLEGACY" legacy; fi ;;
    esac ;;
  *) exit 1 ;;
esac
EOF
  cat > "$SANDBOX/bin/faketee" <<'EOF'
#!/usr/bin/env bash
cat > "$1"
EOF
  # The answer probe is the ONLY injected command that would drive a billed turn.
  # It records that it was called and succeeds, so "did the probe run at all?" is
  # an observable fact rather than an inference.
  cat > "$SANDBOX/bin/fakeprobe" <<'EOF'
#!/usr/bin/env bash
echo "probe $*" >> "$PROBE_LOG"
echo "PASS answer-probe: stub"
exit "${PROBE_RC:-0}"
EOF
  # If any REAL privileged or networking binary is reached, record it and fail.
  for real in curl sudo nginx systemctl; do
    cat > "$SANDBOX/bin/$real" <<EOF
#!/usr/bin/env bash
echo "REAL $real INVOKED: \$*" >> "$VIOLATIONS"
exit 7
EOF
  done
  chmod +x "$SANDBOX"/bin/*
  # A `nginx -t` / `reload` that fails ONLY the first time: the realistic shape
  # of "the NEW config is bad, the old one is fine". A command that always fails
  # makes the RESTORE fail too, which is a different (also correct) branch.
  cat > "$SANDBOX/bin/failonce" <<EOF
#!/usr/bin/env bash
marker="$SANDBOX/failonce.\$1"
if [ ! -e "\$marker" ]; then : > "\$marker"; exit 1; fi
exit 0
EOF
  chmod +x "$SANDBOX"/bin/*
  export CALLS="$SANDBOX/calls.log"; : > "$CALLS"
  export PROBE_LOG="$SANDBOX/probe.log"; : > "$PROBE_LOG"
  export ROUTE_CONF_PATH PLEGACY PCANDIDATE
}
teardown() { [ -n "$SANDBOX" ] && rm -rf "$SANDBOX"; SANDBOX=""
             unset PLEGACY PCANDIDATE PROBE_RC PUBLIC_LIE 2>/dev/null || true; }

run_weight() {
  ( PATH="$SANDBOX/bin:$PATH" \
    CANARY_REPO_DIR="$SANDBOX" \
    CANARY_ENV_FILE="$SANDBOX/root.env" \
    CANARY_ROUTE_CONF="$ROUTE_CONF_PATH" \
    CANARY_TEST_CMD="${TEST_CMD_OVERRIDE:-true}" \
    CANARY_RELOAD_CMD="${RELOAD_CMD_OVERRIDE:-true}" \
    CANARY_WRITE_CMD="$SANDBOX/bin/faketee" \
    CANARY_MOVE_CMD=mv \
    CANARY_CURL_CMD="$SANDBOX/bin/fakecurl" \
    CANARY_CURL_OPTS="-s" \
    CANARY_PUBLIC_URL="http://public.invalid/ready" \
    CANARY_LEGACY_URL="http://legacy.invalid/ready" \
    CANARY_CANDIDATE_URL="http://candidate.invalid/ready" \
    CANARY_LEGACY_ANSWER_URL="http://legacy.invalid" \
    CANARY_CANDIDATE_ANSWER_URL="http://candidate.invalid" \
    CANARY_ANSWER_PROBE_CMD="$SANDBOX/bin/fakeprobe" \
    CANARY_PROBE_COUNT=64 \
    RENTCOMPASS_DEPLOY_LOCK_FILE="$SANDBOX/deploy.lock" \
    RENTCOMPASS_MAINTENANCE_MARKER="$SANDBOX/maintenance-marker" \
    bash "$ROOT/deploy/set_canary_weight.sh" "$@" ) > "$SANDBOX/out.txt" 2>&1
  RC=$?
  OUT="$(cat "$SANDBOX/out.txt")"
}
weight_now() { sed -n 's/^# rentcompass-canary-weight: \([0-9][0-9]*\)$/\1/p' "$ROUTE_CONF_PATH" | head -1; }
probe_ran()  { [ -s "$PROBE_LOG" ] && echo ran || echo never; }

# ══════════════════════════════════════════════════════════════════════════
echo "--- 1. weight 0 is the emergency rollback: it never probes, it never blocks ---"
# The single most load-bearing property in the file. A candidate that cannot
# answer is the exact reason to remove its cohort, so making weight 0 wait on an
# answer probe would mean a broken pool could not be rolled back.
setup 50
run_weight --weight 0
check    "weight 0 succeeds"                              0 "$RC"
check    "and the route is now 0"                         0 "$(weight_now)"
check    "THE ANSWER PROBE WAS NEVER RUN"                 never "$(probe_ran)"
contains "$OUT" "nginx -t passed for candidate weight 0"  "nginx -t ran for the new route"
contains "$OUT" "emergency rollback active"               "and the rollback is announced"
teardown

# Even when the candidate pool is completely down. Requiring the candidate to be
# healthy before removing its traffic is the deadlock this asserts against.
setup 50
PCANDIDATE="DOWN - -" run_weight --weight 0
check    "weight 0 succeeds with the candidate DOWN"      0 "$RC"
check    "and the route is 0"                             0 "$(weight_now)"
check    "still no answer probe"                          never "$(probe_ran)"
teardown

# ...and when the candidate answers as the wrong architecture entirely.
setup 50
PCANDIDATE="manager_v1 $SHA_CANDIDATE 1" run_weight --weight 0
check    "weight 0 ignores the candidate's identity"      0 "$RC"
check    "the route is 0"                                 0 "$(weight_now)"
teardown

# The rollback pool itself must still be verified — rolling onto a dead legacy
# pool is not a rollback.
setup 50
PLEGACY="DOWN - -" run_weight --weight 0
check    "but a DEAD legacy pool refuses the rollback"    1 "$RC"
contains "$OUT" "legacy pool is not ready"                "by name"
check    "and the route is left where it was"             50 "$(weight_now)"
teardown
echo

echo "--- 2. escalation DOES probe, and a failing probe blocks it ---"
setup 0
run_weight --weight 5 --rollout-id r-esc-1 --allow-public-candidate
check    "5% succeeds when both pools answer"             0 "$RC"
check    "the route is 5"                                 5 "$(weight_now)"
check    "the answer probe ran"                           ran "$(probe_ran)"
contains "$(cat "$PROBE_LOG")" "--url http://legacy.invalid"    "legacy was proven able to answer"
contains "$(cat "$PROBE_LOG")" "--url http://candidate.invalid" "and so was the candidate"
contains "$OUT" "the answer probe is INJECTED"            "an injected probe is announced, never silent"
teardown

setup 0
PROBE_RC=1 run_weight --weight 5 --rollout-id r-esc-2 --allow-public-candidate
check    "a pool that cannot answer blocks the escalation" 1 "$RC"
contains "$OUT" "cannot answer a real turn"               "by name"
check    "and the route is untouched"                     0 "$(weight_now)"
teardown

# The --allow-public-candidate gate is scoped to a route file under /etc/nginx:
# `[[ "$ROUTE_CONF" == /etc/nginx/* && "$WEIGHT" != 0 && "$ALLOW_PUBLIC" != 1 ]]`.
# A sandbox conf is by definition not public, so the flag is not required here —
# asserting otherwise would be asserting a guard this run cannot reach. The gate
# on the REAL path is covered by tests/test_canary_weight_controller.py.
setup 0
run_weight --weight 5 --rollout-id r-esc-3
check    "a NON-public route file needs no --allow-public-candidate" 0 "$RC"
check    "and the route moved"                            5 "$(weight_now)"
teardown

setup 0
run_weight --weight 5 --allow-public-candidate
check    "a non-zero weight needs a --rollout-id"         1 "$RC"
contains "$OUT" "requires --rollout-id"                   "by name"
teardown

setup 0
run_weight --weight 7 --rollout-id r-esc-4 --allow-public-candidate
check    "an unlisted weight is refused"                  1 "$RC"
contains "$OUT" "usage:"                                  "with the accepted set"
teardown
echo

echo "--- 3. DE-ESCALATION never waits on a probe either ---"
# 50 -> 5 with a broken candidate must succeed: a pool that cannot answer has to
# stay de-escalatable, or the only reachable move is 0 and every partial step is
# lost.
setup 50
PROBE_RC=1 run_weight --weight 5 --rollout-id r-de-1 --allow-public-candidate
check    "50 -> 5 succeeds even though the probe would fail" 0 "$RC"
check    "the route is 5"                                 5 "$(weight_now)"
check    "THE PROBE WAS NEVER RUN on a decrease"          never "$(probe_ran)"
contains "$OUT" "the answer probe is SKIPPED"             "and the skip is announced"
teardown

# The identity checks are NOT skipped on a decrease: the candidate keeps serving
# the smaller cohort, so it still has to be the pool we think it is.
setup 50
PCANDIDATE="manager_v1 $SHA_CANDIDATE 1" run_weight --weight 5 --rollout-id r-de-2 --allow-public-candidate
check    "a decrease still verifies the candidate's identity" 1 "$RC"
contains "$OUT" "reports arch 'manager_v1'"               "by name"
check    "and the route is untouched"                     50 "$(weight_now)"
teardown

# An INCREASE is not a de-escalation, however small.
setup 5
PROBE_RC=1 run_weight --weight 20 --rollout-id r-de-3 --allow-public-candidate
check    "5 -> 20 is an escalation and the probe DOES block it" 1 "$RC"
check    "the route is untouched"                         5 "$(weight_now)"
teardown
echo

echo "--- 4. the 100% policy stop ---"
setup 50
run_weight --weight 100 --stage flip --rollout-id r-flip-1 --allow-public-candidate
check    "100 at stage 'flip' without CANARY_ALLOW_FLIP is refused" 1 "$RC"
contains "$OUT" "without CANARY_ALLOW_FLIP=1"             "and names the door"
contains "$OUT" "50% is the highest authorised rollout stage" "and the policy"
check    "the route is untouched"                         50 "$(weight_now)"
check    "no probe was run: the flip is decided before anything is probed" never "$(probe_ran)"
teardown

setup 50
CANARY_ALLOW_FLIP=1 run_weight --weight 100 --stage flip --rollout-id r-flip-2 --allow-public-candidate
check    "...and accepted with it"                        0 "$RC"
check    "the route is 100"                               100 "$(weight_now)"
unset CANARY_ALLOW_FLIP
teardown

setup 50
run_weight --weight 100 --stage c3 --rollout-id r-flip-3 --allow-public-candidate
check    "100 at an ordinary rollout stage is refused"    1 "$RC"
contains "$OUT" "only '--stage maintenance'"              "naming the only two doors"
teardown

# `maintenance` is the deploy drain, and it is machine-only: without the deploy
# lock a human cannot park the candidate at 100%.
setup 50
run_weight --weight 100 --stage maintenance --rollout-id deploy-maintenance-0123456 --allow-public-candidate
check    "stage 'maintenance' without the deploy lock is refused" 1 "$RC"
contains "$OUT" "machine-only"                            "by name"
teardown

# ...and even holding the lock, without the marker update.sh writes.
setup 50
RENTCOMPASS_DEPLOY_LOCK_HELD=1 run_weight --weight 100 --stage maintenance \
  --rollout-id deploy-maintenance-0123456 --allow-public-candidate
check    "stage 'maintenance' without an active drain marker is refused" 1 "$RC"
contains "$OUT" "requires an active drain marker"         "by name"
unset RENTCOMPASS_DEPLOY_LOCK_HELD
teardown

# ...and with a marker naming a DIFFERENT deploy.
setup 50
printf 'deploy-maintenance-deadbee' > "$SANDBOX/maintenance-marker"
RENTCOMPASS_DEPLOY_LOCK_HELD=1 run_weight --weight 100 --stage maintenance \
  --rollout-id deploy-maintenance-0123456 --allow-public-candidate
check    "a marker for another rollout id is refused"     1 "$RC"
contains "$OUT" "names a different rollout id"            "by name"
unset RENTCOMPASS_DEPLOY_LOCK_HELD
teardown

# The whole gate, satisfied: lock held, machine-shaped id, marker naming it.
setup 50
printf 'deploy-maintenance-0123456' > "$SANDBOX/maintenance-marker"
RENTCOMPASS_DEPLOY_LOCK_HELD=1 run_weight --weight 100 --stage maintenance \
  --rollout-id deploy-maintenance-0123456 --allow-public-candidate
check    "a real deploy drain IS allowed to reach 100"    0 "$RC"
check    "the route is 100"                               100 "$(weight_now)"
contains "$OUT" "100% is a temporary drain"               "and says it must be restored"
unset RENTCOMPASS_DEPLOY_LOCK_HELD
teardown
echo

echo "--- 5. --skip-answer-probe is explicit, and says so ---"
setup 0
run_weight --weight 5 --rollout-id r-skip-1 --allow-public-candidate --skip-answer-probe
check    "the escalation succeeds with the probe skipped" 0 "$RC"
check    "the route is 5"                                 5 "$(weight_now)"
check    "and the probe really was not run"               never "$(probe_ran)"
contains "$OUT" "was NOT proven able to answer a turn"    "the skip is WARNed, not silent"
teardown

setup 0
CANARY_SKIP_ANSWER_PROBE=1 run_weight --weight 5 --rollout-id r-skip-2 --allow-public-candidate
check    "the env door works too"                         0 "$RC"
check    "and skips the probe"                            never "$(probe_ran)"
unset CANARY_SKIP_ANSWER_PROBE
teardown

# Skipping the probe does NOT skip the identity checks.
setup 0
PCANDIDATE="DOWN - -" run_weight --weight 5 --rollout-id r-skip-3 --allow-public-candidate --skip-answer-probe
check    "--skip-answer-probe still requires a READY candidate" 1 "$RC"
check    "the route is untouched"                         0 "$(weight_now)"
teardown
echo

echo "--- 6. an empty URL override must never resolve to the live pools ---"
# `-` and not `:-` on every URL default, plus an explicit refusal: this is what
# stops a rehearsal that meant to inject a stub from driving turns at :5001/:5002.
setup 50
( PATH="$SANDBOX/bin:$PATH" CANARY_REPO_DIR="$SANDBOX" CANARY_ENV_FILE="$SANDBOX/root.env" \
  CANARY_ROUTE_CONF="$ROUTE_CONF_PATH" CANARY_CANDIDATE_ANSWER_URL="" \
  bash "$ROOT/deploy/set_canary_weight.sh" --weight 0 ) > "$SANDBOX/out.txt" 2>&1
RC=$?; OUT="$(cat "$SANDBOX/out.txt")"
check    "an EMPTY answer-URL override is refused"        1 "$RC"
contains "$OUT" "must never resolve to the live pools"    "saying exactly why"
teardown

setup 50
( PATH="$SANDBOX/bin:$PATH" CANARY_REPO_DIR="$SANDBOX" CANARY_ENV_FILE="$SANDBOX/root.env" \
  CANARY_ROUTE_CONF="$ROUTE_CONF_PATH" CANARY_ANSWER_PROBE_CMD="" \
  bash "$ROOT/deploy/set_canary_weight.sh" --weight 0 ) > "$SANDBOX/out.txt" 2>&1
RC=$?; OUT="$(cat "$SANDBOX/out.txt")"
check    "an EMPTY probe-command override is refused"     1 "$RC"
contains "$OUT" "set but empty"                           "by name"
teardown
echo

echo "--- 7. --allow-unidentified-target is weight-0 only ---"
# P2-8: a rebuilt legacy pool states a full 40-char commit, so the flag is not
# routine. The restriction below is what stops the habit leaking onto a forward
# escalation, where it is exactly the wrong instinct.
setup 0
run_weight --weight 5 --rollout-id r-flag-1 --allow-public-candidate --allow-unidentified-target
check    "the flag is refused on an escalation"           1 "$RC"
contains "$OUT" "restricted to the weight-0 emergency rollback" "by name"
teardown

setup 50
run_weight --weight 0 --allow-unidentified-target
check    "and accepted on the rollback"                   0 "$RC"
teardown

# A rebuilt legacy pool names its commit, so a plain rollback needs no flag
# and prints no provenance warning.
setup 50
run_weight --weight 0
check    "a rebuilt legacy pool rolls back with no flag"  0 "$RC"
lacks    "$OUT" "no full configured SHA"                  "and no provenance warning"
teardown

# An un-rebuilt legacy host (pre-2026-09): the pool answers 'unknown' AND the root
# .env carries no LEGACY_APP_SHA — those two went together, which is why the flag
# relaxes only the "no full CONFIGURED sha" branch. A pool that answers 'unknown'
# against a REAL pin is a different, still-fatal condition (asserted next).
setup 50
sed -i '/^LEGACY_APP_SHA=/d' "$SANDBOX/root.env"
PLEGACY="legacy unknown -" run_weight --weight 0
check    "an unidentified legacy pool can still be rolled onto" 0 "$RC"
contains "$OUT" "no full configured SHA"                  "loudly"
check    "the route is 0"                                 0 "$(weight_now)"
teardown

# ...but the flag does NOT excuse a pool that contradicts a real pin. That is a
# rollback target which is not the image the .env says it is.
setup 50
PLEGACY="legacy unknown -" run_weight --weight 0 --allow-unidentified-target
check    "a pool contradicting a REAL pin is refused even with the flag" 1 "$RC"
contains "$OUT" "!= configured pin"                       "by name"
check    "and the route is untouched"                     50 "$(weight_now)"
teardown
echo

echo "--- 8. a failing nginx -t restores the previous route ---"
setup 50
TEST_CMD_OVERRIDE="$SANDBOX/bin/failonce test" run_weight --weight 0
check    "a rejected config fails the run"                1 "$RC"
contains "$OUT" "nginx -t rejected candidate weight 0"    "by name"
contains "$OUT" "restoring previous routing include"      "and the restore is announced"
check    "and the route is back where it was"             50 "$(weight_now)"
unset TEST_CMD_OVERRIDE
teardown

setup 50
RELOAD_CMD_OVERRIDE="$SANDBOX/bin/failonce reload" run_weight --weight 0
check    "a failed reload also restores"                  1 "$RC"
contains "$OUT" "nginx reload failed"                     "by name"
check    "the route is restored"                          50 "$(weight_now)"
unset RELOAD_CMD_OVERRIDE
teardown

# If the RESTORE itself cannot pass nginx -t, that is a different and louder
# failure: the operator is told the backup path and left to act.
setup 50
TEST_CMD_OVERRIDE=false run_weight --weight 0
check    "a config broken for BOTH routes still fails"    1 "$RC"
contains "$OUT" "RESTORED CONFIG FAILS nginx -t"          "naming the backup so it is recoverable"
unset TEST_CMD_OVERRIDE
teardown
echo

echo "--- 9. the post-reload public check proves the route really took ---"
setup 0
run_weight --weight 5 --rollout-id r-pub-1 --allow-public-candidate
contains "$OUT" "candidate weight is now 5%"              "a weighted route proves BOTH cohorts"
contains "$(cat "$CALLS")" "public.invalid"               "by sampling the public endpoint"
teardown

# A public endpoint that did NOT take the new route must not be accepted: the
# reload "succeeded" but the edge is still sending everyone to the candidate.
setup 50
PUBLIC_LIE=1 run_weight --weight 0
check    "a public endpoint that ignored the new route is refused" 1 "$RC"
contains "$OUT" "previous route restored"                 "and the previous route is restored"
check    "the route file is back at 50"                   50 "$(weight_now)"
unset PUBLIC_LIE
teardown

# The identity checks run BEFORE anything is written, so a broken legacy pool
# never reaches the route file at all.
setup 50
PLEGACY="fc_loop $SHA_CANDIDATE 0" run_weight --weight 0
check    "a legacy pool answering as the wrong arch is refused" 1 "$RC"
contains "$OUT" "reports arch 'fc_loop', expected 'legacy'" "before anything is written"
check    "and the route was never touched"                50 "$(weight_now)"
teardown
echo

echo "--- 10. nothing real was touched ---"
check "no real curl/sudo/nginx/systemctl was invoked" "absent" \
      "$([ -s "$VIOLATIONS" ] && echo present || echo absent)"
rm -f "$VIOLATIONS"
echo

printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
