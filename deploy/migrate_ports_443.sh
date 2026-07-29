#!/usr/bin/env bash
# Swap the two public TLS ports:  RentCompass 8443 -> 443,  Xray 443 -> 8443.
#
# WHY A SWAP, rather than parking Xray on a fresh port:
#   8443 is already open in ufw *and* in the provider firewall — the site has been
#   publicly served on it. A brand-new port needs both opened, and a silent drop in
#   the cloud firewall only surfaces AFTER the client has been repointed, i.e. once
#   the proxy is already unreachable. The swap needs no firewall change on either side.
#
# WHY THE LIVE VHOST IS EDITED IN PLACE, never overwritten from deploy/nginx/:
#   the live file has drifted from the template on purpose. switch_pool.sh rewrites
#   its upstream (live 5002, template 5001) and client_max_body_size is 15m live vs
#   256k in the template. Copying the template over it would silently roll the public
#   pool back to legacy. Only the two listen lines and the :80 redirect are touched.
#
# ORDER MATTERS: nginx holds 8443 and Xray holds 443, so they have to cross. Xray
# stops first (frees 443), nginx restarts onto 443 (frees 8443), then Xray returns on
# 8443. The proxy is down for that window. That is why this is ONE non-interactive
# script: if your shell reaches this box THROUGH the proxy, running the steps by hand
# loses the session halfway and leaves both ports unbound.
#
# Run with sudo:  sudo bash deploy/migrate_ports_443.sh
# Idempotent — re-running after a successful migration only re-verifies.
set -euo pipefail

# Rehearsal seams: the paths and the root check are the only things that must be
# faked to run this identical code path against throwaway copies with no root and
# no public traffic. Everything else (systemctl, nginx, sqlite3, curl, openssl, ss,
# ufw) is resolved through PATH, so the rehearsal stubs them there.
# See deploy/migrate_ports_443_rehearse.sh.
CONF="${MIG_CONF:-/etc/nginx/sites-available/rentcompass.co.uk.conf}"
XUI_DB="${MIG_XUI_DB:-/etc/x-ui/x-ui.db}"
CERT="${MIG_CERT:-/etc/letsencrypt/live/rentcompass.co.uk/fullchain.pem}"
SITE_URL="${MIG_SITE_URL:-https://127.0.0.1/health}"
XRAY_PROBE="${MIG_XRAY_PROBE:-127.0.0.1:8443}"
REQUIRE_ROOT="${MIG_REQUIRE_ROOT:-1}"
MON_OVERRIDE="${MIG_MON_OVERRIDE:-/etc/systemd/system/rentcompass-monitor.service.d/override.conf}"
MON_URL="${MIG_MON_URL:-https://rentcompass.co.uk/health}"
STAMP=$(date +%Y%m%d-%H%M%S)
CONF_BAK="$CONF.bak-port443-$STAMP"
DB_BAK="$XUI_DB.bak-port443-$STAMP"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
say(){ printf '\033[36m==>\033[0m %s\n' "$*"; }
note(){ printf '    %s\n' "$*"; }

# ---------- [0/7] pre-flight ----------
say "[0/7] Pre-flight"
if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then
  red "must run as root:  sudo bash $0"; exit 1
fi
for b in nginx sqlite3 curl ss systemctl openssl; do
  command -v "$b" >/dev/null || { red "missing required binary: $b"; exit 1; }
done
[ -f "$CONF" ]   || { red "missing nginx vhost: $CONF"; exit 1; }
[ -f "$XUI_DB" ] || { red "missing x-ui database: $XUI_DB"; exit 1; }
[ -f "$CERT" ]   || { red "missing certificate: $CERT — run deploy/setup_tls.sh first"; exit 1; }

# The x-ui DB is the source of truth. /usr/local/x-ui/bin/config.json is REGENERATED
# from it on every x-ui restart, so editing that file would be silently reverted.
xray_port=$(sqlite3 "$XUI_DB" "select port from inbounds where protocol='vless' limit 1;")
[ -n "$xray_port" ] || { red "no vless inbound found in $XUI_DB"; exit 1; }
note "xray vless inbound currently on port: $xray_port"

nginx_on_443=0
grep -qE '^[[:space:]]*listen[[:space:]]+443[[:space:]]+ssl' "$CONF" && nginx_on_443=1
note "nginx vhost currently on: $([ $nginx_on_443 = 1 ] && echo 443 || echo 8443)"

if [ "$nginx_on_443" = 1 ] && [ "$xray_port" = 8443 ]; then
  grn "Already migrated — skipping to verification."
  SKIP_WORK=1
else
  SKIP_WORK=0
fi

verify() {
  say "[6/7] Verify"
  local rc=0
  ss -ltn | grep -E ':(443|8443)\b' | sed 's/^/    /' || true

  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "$SITE_URL" || echo 000)
  if [ "$code" = 200 ]; then
    grn "  site  $SITE_URL -> HTTP 200"
    curl -sk -D- -o /dev/null --max-time 15 "$SITE_URL" \
      | grep -i '^x-agent-' | sed 's/^/      /' || true
  else
    red "  site  $SITE_URL -> HTTP $code   (expected 200)"; rc=1
  fi

  # A live REALITY inbound relays an unauthenticated handshake to its masquerade
  # target, so a genuine www.apple.com cert coming back off :8443 proves Xray is up.
  if timeout 15 openssl s_client -connect "$XRAY_PROBE" -servername www.apple.com \
       </dev/null 2>/dev/null | grep -q 'CN *= *www\.apple\.com'; then
    grn "  xray  $XRAY_PROBE REALITY relaying to www.apple.com — inbound is live"
  else
    red "  xray  $XRAY_PROBE did not relay a www.apple.com handshake"; rc=1
  fi
  return $rc
}

# The health monitor defaults to probing :8443 — which is Xray after this swap, so it
# would alert on every run. Fix it by OVERRIDING THE URL, not by reinstalling the
# script: /usr/local/bin/rentcompass-monitor.sh has drifted from the committed copy,
# and overwriting it could regress fixes that exist only there. Confirm with
# deploy/monitoring/check_install_drift.sh before reconciling those two properly.
fix_monitor() {
  say "[7/7] Point the health monitor at the new URL"
  if [ ! -f "$MON_OVERRIDE" ]; then
    note "no monitor override at $MON_OVERRIDE — nothing to repoint"
    return 0
  fi
  if grep -q '^Environment=MON_PUBLIC_URL=' "$MON_OVERRIDE"; then
    note "already set: $(grep '^Environment=MON_PUBLIC_URL=' "$MON_OVERRIDE")"
    return 0
  fi
  printf 'Environment=MON_PUBLIC_URL=%s\n' "$MON_URL" >> "$MON_OVERRIDE"
  systemctl daemon-reload
  note "added Environment=MON_PUBLIC_URL=$MON_URL"
}

if [ "$SKIP_WORK" = 1 ]; then
  verify || exit 1
  fix_monitor || note "could not repoint the monitor — set MON_PUBLIC_URL by hand"
  exit 0
fi

# ---------- [1/7] backups ----------
say "[1/7] Backups"
cp -a "$CONF" "$CONF_BAK";   note "$CONF_BAK"
cp -a "$XUI_DB" "$DB_BAK";   note "$DB_BAK"

rolled_back=0
crossed=0          # set once x-ui has been stopped / the DB has been written
rollback() {
  [ "$rolled_back" = 1 ] && return
  rolled_back=1
  red ""
  red "FAILED — rolling back to Xray:443 / nginx:8443"
  cp -a "$CONF_BAK" "$CONF" || red "  could not restore $CONF from $CONF_BAK"
  if [ "$crossed" = 1 ]; then
    # Both services were in flight: put each one back where it started.
    systemctl stop x-ui         || true
    cp -a "$DB_BAK" "$XUI_DB"   || red "  could not restore $XUI_DB from $DB_BAK"
    systemctl restart nginx     || red "  nginx did not come back — check: nginx -t"
    systemctl start x-ui        || red "  x-ui did not come back — check: journalctl -u x-ui"
  else
    # Nothing was ever applied — the running nginx still holds the original config
    # and Xray never moved, so restoring the file on disk is the whole rollback.
    # Bouncing either service here would be a self-inflicted outage.
    note "nothing had been applied yet — no service was restarted"
  fi
  red "Rollback done. Nothing on your clients needs changing."
}
trap rollback ERR

# ---------- [2/7] firewall ----------
say "[2/7] Firewall (both ports are already open; these are idempotent)"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 443/tcp  >/dev/null 2>&1 || true
  ufw allow 8443/tcp >/dev/null 2>&1 || true
  note "ufw: 443/tcp and 8443/tcp allowed"
else
  note "ufw inactive — ensure the CLOUD firewall allows inbound TCP 443 and 8443"
fi

# ---------- [3/7] rewrite the vhost (in place, ports only) ----------
say "[3/7] Move the nginx vhost onto 443"
sed -i \
  -e 's|^\([[:space:]]*\)listen[[:space:]]\+8443[[:space:]]\+ssl|\1listen 443 ssl|' \
  -e 's|^\([[:space:]]*\)listen[[:space:]]\+\[::\]:8443[[:space:]]\+ssl|\1listen [::]:443 ssl|' \
  -e 's|https://\$host:8443\$request_uri|https://$host$request_uri|' \
  -e 's|# rentcompass.co.uk — HTTPS on port 8443 (port 443 is reserved for Xray).|# rentcompass.co.uk — HTTPS on the default port 443. Xray moved to 8443.|' \
  -e 's|# Installed by deploy/setup_tls_8443.sh AFTER the certificate is issued.|# Installed by deploy/setup_tls.sh AFTER the certificate is issued.|' \
  -e 's|# Access: https://rentcompass.co.uk:8443|# Access: https://rentcompass.co.uk|' \
  -e 's|# --- HTTP :80 — answer ACME challenges, redirect everything else to HTTPS:8443 ---|# --- HTTP :80 — answer ACME challenges, redirect everything else to HTTPS ---|' \
  -e 's|# --- HTTPS :8443 — the real site (443 stays with Xray) ---|# --- HTTPS :443 — the real site ---|' \
  "$CONF"

grep -qE '^[[:space:]]*listen[[:space:]]+443[[:space:]]+ssl' "$CONF" \
  || { red "rewrite did not take — $CONF still has no 'listen 443 ssl'"; false; }
# Comments may legitimately still say 8443 (that is where Xray went). Only a
# DIRECTIVE still naming 8443 would mean the rewrite missed a line.
if grep -vE '^[[:space:]]*#' "$CONF" | grep -q '8443'; then
  red "leftover 8443 directive in $CONF:"
  grep -nE '8443' "$CONF" | grep -vE ':[[:space:]]*#' | sed 's/^/    /'
  false
fi
note "vhost rewritten (upstream and client_max_body_size untouched)"

# nginx -t parses only; it binds nothing, so this passes while Xray still holds 443.
nginx -t
note "nginx -t OK"

# ---------- [4/7] cross over ----------
say "[4/7] Crossing the ports — proxy is briefly down from here"
# From here on a failure must also put the x-ui inbound back, not just the vhost.
crossed=1
systemctl stop x-ui
note "x-ui stopped — 443 free"

sqlite3 "$XUI_DB" "UPDATE inbounds SET port=8443 WHERE protocol='vless' AND port=443;"
now=$(sqlite3 "$XUI_DB" "select port from inbounds where protocol='vless' limit 1;")
[ "$now" = 8443 ] || { red "x-ui DB still reports port $now"; false; }
note "x-ui inbound moved to 8443 (tag left as-is so traffic stats stay attached)"

systemctl restart nginx
note "nginx restarted — now on 443, 8443 released"

systemctl start x-ui
note "x-ui started"

# ---------- [5/7] settle ----------
say "[5/7] Waiting for both listeners"
for _ in $(seq 1 20); do
  if ss -ltn | grep -qE ':443\b' && ss -ltn | grep -qE ':8443\b'; then break; fi
  sleep 1
done

trap - ERR
if ! verify; then
  rollback
  exit 1
fi

# Past the point of no return — the swap is good. A failure to repoint the monitor
# is worth a warning, never a rollback of a working migration.
fix_monitor || note "could not repoint the monitor — set MON_PUBLIC_URL by hand"

grn ""
grn "DONE — site is on https://rentcompass.co.uk (443), Xray is on 8443."
note "Change ONE field in each client: port 443 -> 8443. Everything else"
note "(UUID, flow, SNI www.apple.com, shortId, public key) is unchanged."
note ""
note "Rollback if needed:"
note "  sudo cp -a $CONF_BAK $CONF"
note "  sudo systemctl stop x-ui && sudo cp -a $DB_BAK $XUI_DB"
note "  sudo systemctl restart nginx && sudo systemctl start x-ui"
