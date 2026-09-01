#!/usr/bin/env bash
# Rehearse deploy/switch_pool.sh's single-upstream path end to end.
# Offline, free, no docker, no root, no nginx, NO NETWORK.
#
#   bash deploy/test_switch_pool_assertions.sh
#
# WHY THIS FILE EXISTS
# --------------------
# switch_pool.sh is the emergency rollback lever and the verb update.sh's drain
# calls, and until now its only exerciser was deploy/switch_pool_rehearse.sh —
# which starts a REAL nginx and probes the REAL pools, so nobody runs it in a
# review. That is how R3-H1 shipped: an `X-Agent-Specialists` equality check with
# no exemption for the pre-2026-08-31 images, which is BOTH containers deployed on
# this host, turning `--to legacy` (the documented emergency rollback) and the
# drain leg of `update.sh --pool fc` into hard refusals.
#
# Every external command is injected, including curl. A `curl` stub is also put
# first on PATH that records any use and fails: if the real binary were ever
# reached, the last assertion here fails loudly instead of a probe silently
# reaching 127.0.0.1:5001/5002.
set -u
PASS=0; FAIL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

check() { if [ "$2" = "$3" ]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
          else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }
contains() { if grep -qF -- "$2" <<<"$1"; then printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1))
             else printf '\033[31mFAIL\033[0m %s\n       missing: %s\n       got:     %s\n' "$3" "$2" "$1"; FAIL=$((FAIL+1)); fi; }
lacks()  { if grep -qF -- "$2" <<<"$1"; then printf '\033[31mFAIL\033[0m %s\n       unexpectedly present: %s\n' "$3" "$2"; FAIL=$((FAIL+1))
           else printf '\033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1)); fi; }

SHA40_A=1111111111111111111111111111111111111111
SHA40_B=2222222222222222222222222222222222222222
SANDBOX=""; NETWORK_VIOLATIONS=""

setup() {   # setup <current upstream port>
  SANDBOX="$(mktemp -d)"; mkdir -p "$SANDBOX/bin"
  NETWORK_VIOLATIONS="${NETWORK_VIOLATIONS:-$SANDBOX/../switch-pool-network-violations}"
  CONF_PATH="$SANDBOX/site.conf"
  printf 'server {\n    listen 80;\n}\n\nupstream rentcompass_app {\n    server 127.0.0.1:%s;\n    keepalive 32;\n}\n' \
    "$1" > "$CONF_PATH"
  ROUTE_CONF_PATH="$SANDBOX/no-weighted-include.conf"
  printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=0\nCANARY_USE_MCP_TOOLS=0\n' > "$SANDBOX/root.env"

  # The identity each port answers with. `-` in the specialists slot means the
  # pool sends NO X-Agent-Specialists header at all, which is what origin/main's
  # image does — i.e. what is running on this box right now.
  : "${P5001:=legacy $SHA40_A -}"
  : "${P5002:=fc_loop $SHA40_B -}"

  cat > "$SANDBOX/bin/fakecurl" <<'EOF'
#!/usr/bin/env bash
url="${@: -1}"
echo "curl $url" >> "$CALLS"
case "$url" in
  *:5001/*) id="$P5001" ;;
  *:5002/*) id="$P5002" ;;
  *)  # the "public" endpoint answers as whichever pool the conf selects now
      p="$(sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' "$SWITCH_CONF" | head -1)"
      case "$p" in 5001) id="$P5001" ;; 5002) id="$P5002" ;; *) exit 1 ;; esac ;;
esac
read -r arch sha specialists <<<"$id"
[ "$arch" = "DOWN" ] && exit 1
printf 'HTTP/1.1 200 OK\r\nx-agent-arch: %s\r\nx-agent-version: %s\r\n' "$arch" "$sha"
[ "$specialists" = "-" ] || printf 'x-agent-specialists: %s\r\n' "$specialists"
printf '\r\n'
EOF
  cat > "$SANDBOX/bin/faketee" <<'EOF'
#!/usr/bin/env bash
cat > "$1"
EOF
  cat > "$SANDBOX/bin/fakeweight" <<'EOF'
#!/usr/bin/env bash
echo "weight $*" >> "$CALLS"
exit 0
EOF
  # If the REAL curl is ever reached, record it and fail: this harness must never
  # touch 127.0.0.1:5001 / :5002 or any host.
  cat > "$SANDBOX/bin/curl" <<EOF
#!/usr/bin/env bash
echo "REAL CURL INVOKED: \$*" >> "$NETWORK_VIOLATIONS"
exit 7
EOF
  chmod +x "$SANDBOX"/bin/*
  export CALLS="$SANDBOX/calls.log"; : > "$CALLS"
  export P5001 P5002
}
teardown() { [ -n "$SANDBOX" ] && rm -rf "$SANDBOX"; SANDBOX=""; unset P5001 P5002; }

install_weighted_route() {   # a host that HAS the generated include
  ROUTE_CONF_PATH="$SANDBOX/canary-routing.conf"
  printf '# rentcompass-canary-weight: 0\n# rentcompass-rollout-id: rollback\n# rentcompass-rollout-stage: rollback\n' \
    > "$ROUTE_CONF_PATH"
}

run_switch() {
  ( PATH="$SANDBOX/bin:$PATH" \
    SWITCH_CONF="$CONF_PATH" \
    SWITCH_ROUTE_CONF="$ROUTE_CONF_PATH" \
    SWITCH_ENV_FILE="$SANDBOX/root.env" \
    SWITCH_REPO_DIR="$SANDBOX" \
    SWITCH_CURL_CMD="$SANDBOX/bin/fakecurl" \
    SWITCH_CURL_OPTS="-s" \
    SWITCH_VERIFY_URL="http://public.invalid/ready" \
    SWITCH_POOL_HEALTH_FMT="http://127.0.0.1:%s/ready" \
    SWITCH_WRITE_CMD="$SANDBOX/bin/faketee" \
    SWITCH_TEST_CMD=true \
    SWITCH_RELOAD_CMD=true \
    SWITCH_REFRESH_TARGET=0 \
    SWITCH_WEIGHT_SCRIPT="$SANDBOX/bin/fakeweight" \
    RENTCOMPASS_DEPLOY_LOCK_FILE="$SANDBOX/deploy.lock" \
    RENTCOMPASS_MAINTENANCE_MARKER="$SANDBOX/maintenance-marker" \
    bash "$ROOT/deploy/switch_pool.sh" "$@" ) > "$SANDBOX/out.txt" 2>&1
  RC=$?
  OUT="$(cat "$SANDBOX/out.txt")"
}
port_now() { sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' "$CONF_PATH" | head -1; }

# ══════════════════════════════════════════════════════════════════════════
echo "--- 1. R3-H1: the pools deployed TODAY send no X-Agent-Specialists ---"
# `git grep X-Agent-Specialists origin/main` -> zero hits, so neither running
# container emits it. The equality check refused the emergency rollback outright.
setup 5002
P5001="legacy $SHA40_A -" P5002="fc_loop $SHA40_B -" run_switch --to legacy
check    "--to legacy succeeds against a headerless pool" 0 "$RC"
check    "and the upstream actually moved"                5001 "$(port_now)"
contains "$OUT" "sends no X-Agent-Specialists header"     "the exemption is announced, not silent"
contains "$OUT" "switched to legacy"                      "and the switch completes"
teardown

setup 5001
P5001="legacy $SHA40_A -" P5002="fc_loop $SHA40_B -" run_switch --to fc
check    "--to fc also succeeds when 0 is expected"       0 "$RC"
check    "and the upstream moved"                         5002 "$(port_now)"
teardown

# The DEPLOYED legacy pool cannot even name its commit; that stays a separate,
# explicitly flagged decision, and it must not be entangled with the header rule.
setup 5002
P5001="legacy unknown -" run_switch --to legacy
check    "an unidentified rollback target still needs the flag" 1 "$RC"
contains "$OUT" "non-full sha"                            "and says which check refused"
check    "the upstream is untouched"                      5002 "$(port_now)"
teardown

setup 5002
P5001="legacy unknown -" run_switch --to legacy --allow-unidentified-target
check    "...and is allowed with it"                      0 "$RC"
check    "the upstream moved"                             5001 "$(port_now)"
contains "$OUT" "cannot state its commit"                 "loudly"
teardown
echo

echo "--- 2. the exemption is for an EXPECTED 0, and nothing else ---"
# A manager_v1 candidate can never reach this path: the single-upstream lever
# refuses it outright and sends the operator to the weighted controller, where
# set_canary_weight.sh::verify_local and probe_pool_answer.py::specialists_match
# hold a specialists=1 candidate to stating its bit (tests/test_r3c_specialist_rule.py).
setup 5001
printf 'CANARY_AGENT_ARCH=manager_v1\nCANARY_MANAGER_V1_SPECIALISTS=1\nCANARY_USE_MCP_TOOLS=0\n' > "$SANDBOX/root.env"
P5002="manager_v1 $SHA40_B -" run_switch --to fc
check    "a manager_v1 candidate never uses the single-upstream lever" 1 "$RC"
contains "$OUT" "requires the weighted routing include"   "and is sent to the controller that gates it"
check    "the upstream is untouched"                      5001 "$(port_now)"
teardown

setup 5001
P5002="fc_loop $SHA40_B 1" run_switch --to fc
check    "a pool claiming 1 where 0 is expected is refused" 1 "$RC"
contains "$OUT" "specialists='1', expected '0'"           "the mismatch is exact, not fuzzy"
check    "the upstream is untouched"                      5001 "$(port_now)"
teardown

setup 5001
P5002="legacy $SHA40_B -" run_switch --to fc
check    "an arch mismatch is still refused"              1 "$RC"
contains "$OUT" "reports arch 'legacy'"                   "by name"
check    "the upstream is untouched"                      5001 "$(port_now)"
teardown

setup 5001
P5002="DOWN - -" run_switch --to fc
check    "an unreachable target is refused"               1 "$RC"
contains "$OUT" "not answering /ready"                    "by name"
check    "the upstream is untouched"                      5001 "$(port_now)"
teardown
echo

echo "--- 3. idempotence and argument handling ---"
setup 5002
run_switch --to fc
check    "switching to the pool already selected is a no-op" 0 "$RC"
contains "$OUT" "already on fc"                           "and says so"
teardown

setup 5002
run_switch --to nowhere
check    "an unknown pool is refused"                     1 "$RC"
teardown
echo

echo "--- 4. R3-M8: the candidate slot is exclusive, and says so ---"
setup 5002
printf 'CANARY_AGENT_ARCH=manager_v1\nCANARY_MANAGER_V1_SPECIALISTS=1\nCANARY_USE_MCP_TOOLS=0\nFC_CANARY_IMAGE=uk-rent-agent:canary-fc-loop-4171d84\n' > "$SANDBOX/root.env"
P5001="legacy $SHA40_A -" run_switch --to legacy
contains "$OUT" "candidate slot (:5002) runs fc_loop today" "the displaced architecture is named"
contains "$OUT" "the slot is EXCLUSIVE"                     "and the consequence is stated"
teardown

setup 5002
printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=0\nCANARY_USE_MCP_TOOLS=0\nFC_CANARY_IMAGE=uk-rent-agent:canary-fc-loop-4171d84\n' > "$SANDBOX/root.env"
P5001="legacy $SHA40_A -" run_switch --to legacy
lacks    "$OUT" "the slot is EXCLUSIVE"                     "and stays quiet when nothing is displaced"
teardown
echo

echo "--- 5. the identity whitelist covers MCP too ---"
setup 5002
printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=0\nCANARY_USE_MCP_TOOLS=1\n' > "$SANDBOX/root.env"
run_switch --to legacy
check    "an MCP-enabled candidate identity is refused"   1 "$RC"
contains "$OUT" "candidate identity must be"              "by the same whitelist the rest of the deploy path uses"
teardown

setup 5002
printf 'CANARY_AGENT_ARCH=fc_loop\nCANARY_MANAGER_V1_SPECIALISTS=perhaps\nCANARY_USE_MCP_TOOLS=0\n' > "$SANDBOX/root.env"
run_switch --to legacy
check    "a non-boolean specialist switch is refused"     1 "$RC"
contains "$OUT" "must be a boolean"                       "by name"
teardown
echo

echo "--- 6. R3-M2: --skip-answer-probe reaches the weighted controller ---"
setup 5002
install_weighted_route
run_switch --to legacy --skip-answer-probe
contains "$(cat "$CALLS")" "weight --weight 0 --skip-answer-probe" \
         "the flag is forwarded to set_canary_weight.sh"
teardown

setup 5002
install_weighted_route
CANARY_SKIP_ANSWER_PROBE=1 run_switch --to legacy
contains "$(cat "$CALLS")" "--skip-answer-probe"          "and the env door works too"
unset CANARY_SKIP_ANSWER_PROBE
teardown
echo

echo "--- 7. no real network was touched ---"
check "the real curl binary was never invoked" "absent" \
      "$([ -s "${NETWORK_VIOLATIONS:-/nonexistent}" ] && echo present || echo absent)"
rm -f "${NETWORK_VIOLATIONS:-/nonexistent}"
echo

printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
