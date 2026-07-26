#!/usr/bin/env bash
# Prove the monitor's assertions FIRE. Offline, free, no docker, no provider.
#
# The defect these guard against is not "the check is missing" but "the check
# collects a value and never tests it" -- checks 9, and the l_ver/f_ver/p_ver
# strings, were all in that state. A test that only asserts the happy path would
# reproduce exactly that defect one level up.
set -u
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
          else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi; }

# --- the provider-probe classifier, verbatim from the monitor ----------------
classify() {
  case "$1" in
    OK\ 200*)        echo "ok" ;;
    HTTP\ 429*)      echo "sev4-throttled" ;;
    HTTP\ 4*)        echo "sev3-rejected" ;;
    HTTP\ 5*|NETFAIL*) echo "sev4-unreachable" ;;
    RESOLVE_FAIL*)   echo "sev3-unresolved" ;;
    *)               echo "sev4-unrecognised" ;;
  esac
}

echo "--- provider probe classifier ---"
# The literal body the provider returned on 2026-07-24, reproduced live on 07-26.
INCIDENT='HTTP 400 deepseek-chat {"error":{"message":"The supported API model names are deepseek-v4-pro or deepseek-v4-flash, but you passed deepseek-chat.","type":"invalid_request_error"}}'
check "the 2026-07-24 incident pages at sev3" "sev3-rejected"     "$(classify "$INCIDENT")"
check "a healthy ping is silent"              "ok"                "$(classify 'OK 200 deepseek-v4-flash')"
check "401 (bad key) pages at sev3"           "sev3-rejected"     "$(classify 'HTTP 401 deepseek-v4-flash {"error":"auth"}')"
# Found by writing this test: 429 originally fell into the HTTP 4* branch and
# paged as a configuration fault. Rate limiting is load. An operator trained to
# ignore this check is how the next real 400 gets missed.
check "429 is throttling, NOT a config fault"  "sev4-throttled" "$(classify 'HTTP 429 deepseek-v4-flash')"
check "5xx is only a warning"                 "sev4-unreachable"  "$(classify 'HTTP 503 deepseek-v4-flash')"
check "network failure is only a warning"     "sev4-unreachable"  "$(classify 'NETFAIL deepseek-v4-flash URLError')"
check "unresolvable model pages at sev3"      "sev3-unresolved"   "$(classify 'RESOLVE_FAIL ImportError: no module app.config')"
check "empty output is not silently ok"       "sev4-unrecognised" "$(classify '')"
echo

# --- telemetry line-count assertion -----------------------------------------
# Deliberately silent on zero growth: 2 conversations/day against 288 runs/day
# means "no traffic" and "broken" are indistinguishable here.
tel() { local prev="$1"; local now="$2"; local d=$(( now - prev ))
        if [ "$d" -lt 0 ]; then echo "sev3-shrank"; else echo "silent"; fi; }
echo "--- telemetry log assertion ---"
check "growth is silent"                 "silent"      "$(tel 100 135)"
check "ZERO growth is silent (2 conv/day)" "silent"    "$(tel 135 135)"
check "a SHRINK pages — the 07-25 mv/fd trap" "sev3-shrank" "$(tel 135 2)"
check "truncation to zero pages"         "sev3-shrank" "$(tel 135 0)"
echo

# --- identity assertions ----------------------------------------------------
edge() { [ "$1" != "$2" ] && echo "sev3-edge-mismatch" || echo "silent"; }
drift() { local prev="$1"; local cur="$2"
          if [ -n "$prev" ] && [ "$prev" != "none" ] && [ -n "$cur" ] && [ "$cur" != "none" ] && [ "$prev" != "$cur" ]
          then echo "sev-drift"; else echo "silent"; fi; }
pinmatch() { [ -n "$2" ] && [ "$1" != "none" ] && [ "$1" != "$2" ] && echo "sev3-pin-mismatch" || echo "silent"; }
echo "--- identity assertions (were collected, never tested) ---"
check "edge serving a different build pages" "sev3-edge-mismatch" "$(edge abc123 def456)"
check "edge agreeing with the pool is silent" "silent"            "$(edge abc123 abc123)"
check "an unannounced version change pages"  "sev-drift"          "$(drift 8793c0b d7f6f50)"
check "first run (no previous) is silent"    "silent"             "$(drift '' 8793c0b)"
check "a pool stuck at 'none' is silent here" "silent"            "$(drift none none)"
check "fc not matching FC_CANARY_SHA pages"  "sev3-pin-mismatch"  "$(pinmatch 8793c0b 042c477)"
check "fc matching the pin is silent"        "silent"             "$(pinmatch 8793c0b 8793c0b)"
echo

# --- probe/observation closure ----------------------------------------------
# The name app.config resolves and the name the client puts on the wire are two
# different things. Layer B (a888160) records the latter per call, so they can be
# tied together; disagreement means the probe is validating a model nobody sends.
closure() { local probe="$1"; local observed="$2"
            if [ -n "$probe" ] && [ -n "$observed" ] && [ "$observed" != "$probe" ]
            then echo "sev3-probe-tests-wrong-model"; else echo "silent"; fi; }
echo "--- probe/observation closure ---"
check "agreement is silent"                    "silent" "$(closure deepseek-v4-flash deepseek-v4-flash)"
check "a per-request override would page"      "sev3-probe-tests-wrong-model" "$(closure deepseek-v4-flash deepseek-v4-pro)"
check "a silent fallback would page"           "sev3-probe-tests-wrong-model" "$(closure deepseek-v4-flash deepseek-chat)"
check "no observed call yet is silent (2/day)" "silent" "$(closure deepseek-v4-flash '')"
check "an unresolved probe is silent here"     "silent" "$(closure '' deepseek-v4-flash)"
echo

# --- expected public arch ----------------------------------------------------
# Hard-coding "legacy" here made the check fire sev3 every five minutes about the intended
# state the moment the owner cut the edge over to fc. An always-on alert is an ignored
# alert. Intent is declared; an UNDECLARED change must still page.
pubarch() { [ "$1" != "$2" ] && echo "sev3-unexpected-arch" || echo "silent"; }
edge_ver() { local exp_code="$1"; local p_ver="$2"; local exp_ver="$3"
             if [ "$exp_code" = "200" ] && [ "$p_ver" != "$exp_ver" ]
             then echo "sev3-edge-mismatch"; else echo "silent"; fi; }
echo "--- expected public arch (declared, not assumed) ---"
check "serving what we declared is silent"      "silent"               "$(pubarch fc_loop fc_loop)"
check "an undeclared change still pages"        "sev3-unexpected-arch" "$(pubarch legacy fc_loop)"
check "a rollback we forgot to declare pages"   "sev3-unexpected-arch" "$(pubarch fc_loop legacy)"
# the version comparison must follow the SAME declaration, or it false-alarms after a cutover
check "edge vs the EXPECTED pool is silent"     "silent"               "$(edge_ver 200 abc123 abc123)"
check "edge vs the wrong pool would page"       "sev3-edge-mismatch"   "$(edge_ver 200 abc123 unknown)"
echo

printf 'monitor assertions: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
