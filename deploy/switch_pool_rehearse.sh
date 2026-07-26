#!/usr/bin/env bash
# Rehearse switch_pool.sh against a PRIVATE nginx instance.
#
# Runs as an ordinary user, in its own prefix, on a loopback high port. It never reads
# /etc/nginx, never needs root, and never touches public traffic — so the fc candidate,
# which is at STAGE-PAUSE, is never momentarily exposed. The logic exercised is the
# real script: only CONF / TEST_CMD / RELOAD_CMD / VERIFY_URL / health-URL / write are
# injected, exactly as they would be pointed at the live conf in production.
#
#   deploy/switch_pool_rehearse.sh [port]        # default 8444
#
# Every case establishes its own starting upstream first. Cases that depend on the
# leftovers of the previous case are how a rehearsal passes while the real thing fails.
set -uo pipefail

PORT="${1:-8444}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SW="$HERE/switch_pool.sh"
PREFIX=$(mktemp -d /tmp/nginx-rehearse.XXXXXX)
CONF="$PREFIX/rehearse.conf"
PASS=0; FAIL=0

cleanup() { [[ -f "$PREFIX/nginx.pid" ]] && nginx -p "$PREFIX" -c "$CONF" -s quit 2>/dev/null; sleep 0.3; rm -rf "$PREFIX"; }
trap cleanup EXIT

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then printf '\033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
  else printf '\033[31mFAIL\033[0m %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; FAIL=$((FAIL+1)); fi
}

mkdir -p "$PREFIX/logs" "$PREFIX/tmp"
write_conf() {                      # $1 = upstream port
cat > "$CONF" <<EOF
worker_processes 1;
error_log $PREFIX/logs/error.log;
pid $PREFIX/nginx.pid;
events { worker_connections 64; }
http {
    access_log off;
    client_body_temp_path $PREFIX/tmp;
    proxy_temp_path $PREFIX/tmp/proxy;
    fastcgi_temp_path $PREFIX/tmp/fastcgi;
    uwsgi_temp_path $PREFIX/tmp/uwsgi;
    scgi_temp_path $PREFIX/tmp/scgi;

upstream rentcompass_app {
    server 127.0.0.1:$1;
    keepalive 32;
}

    server {
        listen 127.0.0.1:$PORT;
        # DRIFT MARKER — stands in for the live conf's client_max_body_size 15m, which
        # has drifted from the repo copy (256k). A switch that rewrote the whole file
        # would silently erase production drift; this line proves it does not.
        client_max_body_size 15m;
        location / {
            proxy_pass http://rentcompass_app;
            proxy_set_header Host \$host;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }
    }
}
EOF
}

write_conf 5001
nginx -p "$PREFIX" -c "$CONF" -t >/dev/null 2>&1 || { echo "rehearsal nginx cannot validate its own conf"; exit 1; }
nginx -p "$PREFIX" -c "$CONF"    >/dev/null 2>&1 || { echo "rehearsal nginx failed to start"; exit 1; }
sleep 0.5
echo "rehearsal nginx up: prefix=$PREFIX  listener=127.0.0.1:$PORT"
echo

export SWITCH_CONF="$CONF"
export SWITCH_TEST_CMD="nginx -p $PREFIX -c $CONF -t"
export SWITCH_RELOAD_CMD="nginx -p $PREFIX -c $CONF -s reload"
export SWITCH_VERIFY_URL="http://127.0.0.1:$PORT/health"
export SWITCH_CURL_OPTS="-s"
export SWITCH_WRITE_CMD="tee"

port_now()     { sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' "$CONF" | head -1; }
drift_intact() { grep -q 'client_max_body_size 15m;' "$CONF" && echo yes || echo no; }
conf_hash()    { md5sum "$CONF" | cut -d' ' -f1; }
reset_to()     { write_conf "$1"; nginx -p "$PREFIX" -c "$CONF" -s reload 2>/dev/null; sleep 0.4; }
serving_arch() { curl -s -D- -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null \
                 | grep -i '^x-agent-arch:' | tr -d '\r' | awk '{print $2}'; }
failed()       { [[ $1 -ne 0 ]] && echo yes || echo no; }

echo "--- 1. legacy -> fc: the forward switch ---"
reset_to 5001
out=$(bash "$SW" --to fc 2>&1); rc=$?
check "1a exit 0"                 "0"    "$rc"
check "1b upstream is 5002"       "5002" "$(port_now)"
check "1c drift preserved"        "yes"  "$(drift_intact)"
check "1d now serving fc_loop"    "fc_loop" "$(serving_arch)"
echo

echo "--- 2. fc -> legacy: THE ROLLBACK VERB ---"
# The public legacy pool reports x-agent-version: unknown (compose sets APP_CANDIDATE_SHA
# only for app-fc). Rollback must therefore still work, but only under an explicit flag.
reset_to 5002
out=$(bash "$SW" --to legacy 2>&1); rc=$?
check "2a refused without the flag"        "yes"  "$(failed $rc)"
check "2b conf unchanged after refusal"    "5002" "$(port_now)"
out=$(bash "$SW" --to legacy --allow-unidentified-target 2>&1); rc=$?
check "2c succeeds with the flag"          "0"      "$rc"
check "2d upstream is 5001"                "5001"   "$(port_now)"
check "2e warned about provenance"         "yes"    "$(grep -q 'cannot state its commit' <<<"$out" && echo yes || echo no)"
check "2f now serving legacy"              "legacy" "$(serving_arch)"
echo

echo "--- 3. port outside the 5001/5002 allowlist -> refuse, change nothing ---"
reset_to 5001; before=$(conf_hash)
PATCHED=$(mktemp); sed 's/\[fc\]=5002/[fc]=5999/' "$SW" > "$PATCHED"
bash "$PATCHED" --to fc >/dev/null 2>&1; rc=$?
rm -f "$PATCHED"
check "3a refused"                "yes"     "$(failed $rc)"
check "3b conf untouched"         "$before" "$(conf_hash)"
echo

echo "--- 4. target pool unreachable -> refuse, change nothing ---"
reset_to 5001; before=$(conf_hash)
SWITCH_POOL_HEALTH_FMT="http://127.0.0.1:1/%s" bash "$SW" --to fc >/dev/null 2>&1; rc=$?
check "4a refused"                "yes"     "$(failed $rc)"
check "4b conf untouched"         "$before" "$(conf_hash)"
echo

echo "--- 5. target reports the WRONG arch -> refuse, change nothing ---"
reset_to 5001; before=$(conf_hash)
# point the 'fc' health probe at the legacy pool: right shape, wrong identity
SWITCH_POOL_HEALTH_FMT="http://127.0.0.1:5001/health#%s" bash "$SW" --to fc >/dev/null 2>&1; rc=$?
check "5a refused on arch mismatch" "yes"     "$(failed $rc)"
check "5b conf untouched"           "$before" "$(conf_hash)"
echo

echo "--- 6. --expect-sha mismatch -> refuse, change nothing ---"
reset_to 5001; before=$(conf_hash)
bash "$SW" --to fc --expect-sha 0000000000000000000000000000000000000000 >/dev/null 2>&1; rc=$?
check "6a refused on sha mismatch"  "yes"     "$(failed $rc)"
check "6b conf untouched"           "$before" "$(conf_hash)"
echo

echo "--- 7. nginx -t rejects the new conf -> auto-restore ---"
reset_to 5001
SWITCH_TEST_CMD="false" bash "$SW" --to fc >/dev/null 2>&1; rc=$?
check "7a reported failure"       "yes"  "$(failed $rc)"
check "7b upstream restored"      "5001" "$(port_now)"
check "7c drift preserved"        "yes"  "$(drift_intact)"
echo

echo "--- 8. reload fails mid-switch -> auto-restore the old upstream ---"
reset_to 5001
SWITCH_RELOAD_CMD="false" bash "$SW" --to fc >/dev/null 2>&1; rc=$?
check "8a reported failure"       "yes"  "$(failed $rc)"
check "8b upstream restored"      "5001" "$(port_now)"
check "8c drift preserved"        "yes"  "$(drift_intact)"
nginx -p "$PREFIX" -c "$CONF" -s reload 2>/dev/null; sleep 0.4
check "8d still serving legacy"   "legacy" "$(serving_arch)"
echo

echo "--- 9. idempotence: switching to where you already are is a no-op ---"
reset_to 5001; before=$(conf_hash)
bash "$SW" --to legacy >/dev/null 2>&1; rc=$?
check "9a exit 0"                 "0"       "$rc"
check "9b conf untouched"         "$before" "$(conf_hash)"
echo

printf 'rehearsal: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
