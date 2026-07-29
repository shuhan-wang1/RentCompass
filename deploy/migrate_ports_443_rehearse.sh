#!/usr/bin/env bash
# Rehearse deploy/migrate_ports_443.sh against throwaway copies — no root, no
# nginx, no x-ui, no public traffic. The REAL script runs; only the paths and the
# root check are faked (MIG_* env), and systemctl/nginx/curl/openssl/ss/ufw are
# stubbed onto PATH.
#
# The script it exercises crosses two live ports and can take the site AND the
# proxy down together, so the failure paths matter more than the happy path:
# cases 3-6 assert that a failure at any stage leaves the box exactly as it started,
# with the proxy back on 443 and nothing for the clients to undo.
#
#   bash deploy/migrate_ports_443_rehearse.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/deploy/migrate_ports_443.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
ok()   { printf '\033[32mPASS\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '\033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }
head_(){ printf '\n--- %s ---\n' "$*"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }

# ---------- fixtures ----------
BIN="$WORK/bin"; mkdir -p "$BIN"

# Stubs record what they were asked to do, and can be told to fail.
cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
[ "${STUB_FAIL_SYSTEMCTL:-}" = "$1 $2" ] && exit 1
exit 0
EOF
cat > "$BIN/nginx" <<'EOF'
#!/usr/bin/env bash
echo "nginx $*" >> "$STUB_LOG"
[ "${STUB_FAIL_NGINX_T:-0}" = 1 ] && { echo "nginx: configuration file test failed" >&2; exit 1; }
exit 0
EOF
cat > "$BIN/ss" <<'EOF'
#!/usr/bin/env bash
printf 'LISTEN 0 511 0.0.0.0:443 0.0.0.0:*\nLISTEN 0 511 0.0.0.0:8443 0.0.0.0:*\n'
EOF
cat > "$BIN/ufw" <<'EOF'
#!/usr/bin/env bash
echo "ufw $*" >> "$STUB_LOG"
[ "${1:-}" = status ] && echo "Status: active"
exit 0
EOF
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
# -w '%{http_code}' form, then the -D- header form.
if [ "${STUB_SITE_CODE:-200}" != 200 ]; then
  case " $* " in *" -w "*) printf '%s' "${STUB_SITE_CODE:-000}"; exit 0;; esac
  exit 7
fi
case " $* " in
  *" -w "*) printf '200';;
  *" -D- "*) printf 'HTTP/2 200\r\nx-agent-arch: fc_loop\r\nx-agent-version: deadbeef\r\n\r\n';;
esac
exit 0
EOF
cat > "$BIN/openssl" <<'EOF'
#!/usr/bin/env bash
[ "${STUB_XRAY_UP:-1}" = 1 ] || exit 1
echo "subject=CN = www.apple.com"
EOF
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"
export STUB_LOG="$WORK/stub.log"

# A byte-for-byte copy of the live vhost shape, INCLUDING the drift that must
# survive: upstream 5002 (fc pool) and client_max_body_size 15m.
make_conf() {
  cat > "$1" <<'EOF'
# rentcompass.co.uk — HTTPS on port 8443 (port 443 is reserved for Xray).
# Installed by deploy/setup_tls_8443.sh AFTER the certificate is issued.
#
# Path: /etc/nginx/sites-available/rentcompass.co.uk.conf  (replaces the HTTP-only one)
# Access: https://rentcompass.co.uk:8443

upstream rentcompass_app {
    server 127.0.0.1:5002;
    keepalive 32;
}

# --- HTTP :80 — answer ACME challenges, redirect everything else to HTTPS:8443 ---
server {
    listen 80;
    listen [::]:80;
    server_name rentcompass.co.uk www.rentcompass.co.uk;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host:8443$request_uri;
    }
}

# --- HTTPS :8443 — the real site (443 stays with Xray) ---
server {
    listen 8443 ssl http2;
    listen [::]:8443 ssl http2;
    server_name rentcompass.co.uk www.rentcompass.co.uk;

    client_max_body_size 15m;

    location / {
        proxy_pass http://rentcompass_app;
    }
}
EOF
}

# The real x-ui schema, reduced to the columns the script touches.
make_db() {
  rm -f "$1"
  sqlite3 "$1" "CREATE TABLE inbounds (id integer PRIMARY KEY AUTOINCREMENT, remark text,
                enable numeric, listen text, port integer, protocol text, tag text,
                CONSTRAINT uni_inbounds_tag UNIQUE (tag));
                INSERT INTO inbounds (remark,enable,listen,port,protocol,tag)
                VALUES ('uk-proxy',1,'',${2:-443},'vless','inbound-443');"
}

setup() {
  rm -rf "$WORK/env"; mkdir -p "$WORK/env"
  make_conf "$WORK/env/site.conf"
  make_db   "$WORK/env/x-ui.db" "${1:-443}"
  : > "$WORK/env/fullchain.pem"
  : > "$STUB_LOG"
  printf '[Service]\nExecStart=\nExecStart=/usr/local/bin/rentcompass-monitor.sh\n' \
    > "$WORK/env/override.conf"
  export MIG_CONF="$WORK/env/site.conf" MIG_XUI_DB="$WORK/env/x-ui.db" \
         MIG_CERT="$WORK/env/fullchain.pem" MIG_REQUIRE_ROOT=0 \
         MIG_SITE_URL="https://127.0.0.1/health" MIG_XRAY_PROBE="127.0.0.1:8443" \
         MIG_MON_OVERRIDE="$WORK/env/override.conf" \
         MIG_MON_URL="https://rentcompass.co.uk/health"
  unset STUB_FAIL_NGINX_T STUB_FAIL_SYSTEMCTL STUB_SITE_CODE STUB_XRAY_UP
}
run() { bash "$SCRIPT" >"$WORK/out.txt" 2>&1; echo $?; }
xport() { sqlite3 "$WORK/env/x-ui.db" "select port from inbounds limit 1;"; }
conf()  { cat "$WORK/env/site.conf"; }

command -v sqlite3 >/dev/null || { echo "SKIP: sqlite3 not installed"; exit 0; }

# ---------- 1. happy path ----------
head_ "1. clean swap: nginx 8443->443, xray 443->8443"
setup 443; rc=$(run)
check "1a exit 0" "$rc" 0
check "1b xray moved to 8443" "$(xport)" 8443
check "1c nginx listens 443" "$(conf | grep -c '^    listen 443 ssl http2;')" 1
check "1d nginx listens [::]:443" "$(conf | grep -c '^    listen \[::\]:443 ssl http2;')" 1
check "1e no 8443 directive left" "$(conf | grep -v '^ *#' | grep -c 8443)" 0
check "1f :80 redirect drops the port" \
      "$(conf | grep -c 'return 301 https://\$host\$request_uri;')" 1
# The whole reason the script seds in place instead of installing the template.
check "1g fc upstream preserved" "$(conf | grep -c 'server 127.0.0.1:5002;')" 1
check "1h body-size drift preserved" "$(conf | grep -c 'client_max_body_size 15m;')" 1
check "1i tag left attached to its stats" \
      "$(sqlite3 "$WORK/env/x-ui.db" 'select tag from inbounds limit 1;')" inbound-443
# The swap is what breaks the monitor (:8443 is Xray now), so the swap must fix it —
# by overriding the URL, never by reinstalling the drifted /usr/local/bin copy.
check "1j monitor repointed off :8443" \
      "$(grep -c '^Environment=MON_PUBLIC_URL=https://rentcompass.co.uk/health$' \
         "$WORK/env/override.conf")" 1
check "1k monitor script itself untouched" \
      "$(grep -c 'install .*rentcompass-monitor' "$STUB_LOG")" 0

head_ "2. ordering: xray must stop BEFORE nginx takes 443, and start after"
order=$(grep -nE 'systemctl (stop|restart|start)' "$STUB_LOG" | sed 's/.*systemctl //' | tr '\n' ',')
check "2a stop x-ui, restart nginx, start x-ui" "$order" "stop x-ui,restart nginx,start x-ui,"

# ---------- 3-5. failure paths must change nothing ----------
head_ "3. nginx -t rejects the rewrite -> restore, never touch the proxy"
setup 443; export STUB_FAIL_NGINX_T=1; rc=$(run)
check "3a non-zero exit" "$([ "$rc" != 0 ] && echo yes)" yes
check "3b xray never moved" "$(xport)" 443
check "3c conf restored to 8443" "$(conf | grep -c '^    listen 8443 ssl http2;')" 1
# Nothing was applied, so neither service may be bounced: the running nginx still
# holds the original config, and a restart here would be a self-inflicted outage.
check "3d x-ui untouched" "$(grep -c 'systemctl.*x-ui' "$STUB_LOG")" 0
check "3e nginx never restarted" "$(grep -c 'systemctl restart nginx' "$STUB_LOG")" 0

head_ "4. nginx restart fails mid-cross -> full rollback, both services back"
setup 443; export STUB_FAIL_SYSTEMCTL="restart nginx"; rc=$(run)
check "4a non-zero exit" "$([ "$rc" != 0 ] && echo yes)" yes
check "4b xray port restored" "$(xport)" 443
check "4c conf restored to 8443" "$(conf | grep -c '^    listen 8443 ssl http2;')" 1
check "4d x-ui was brought back up" "$(grep -c 'systemctl start x-ui' "$STUB_LOG")" 1

head_ "5. ports cross but the site does not answer -> roll back, do not declare success"
setup 443; export STUB_SITE_CODE=502; rc=$(run)
check "5a non-zero exit" "$([ "$rc" != 0 ] && echo yes)" yes
check "5b xray port restored" "$(xport)" 443
check "5c conf restored to 8443" "$(conf | grep -c '^    listen 8443 ssl http2;')" 1

head_ "6. ports cross but xray does not come back -> roll back too"
setup 443; export STUB_XRAY_UP=0; rc=$(run)
check "6a non-zero exit" "$([ "$rc" != 0 ] && echo yes)" yes
check "6b xray port restored" "$(xport)" 443

# ---------- 7. idempotence ----------
head_ "7. re-running an already-migrated box only re-verifies"
setup 8443
sed -i -e 's/listen 8443 ssl/listen 443 ssl/' -e 's/listen \[::\]:8443 ssl/listen [::]:443 ssl/' \
       "$WORK/env/site.conf"
rc=$(run)
check "7a exit 0" "$rc" 0
check "7b no service was bounced" "$(grep -cE 'systemctl (stop|start|restart)' "$STUB_LOG")" 0
check "7c no backup churn" "$(ls "$WORK/env" | grep -c bak-port443)" 0
check "7d said so" "$(grep -c 'Already migrated' "$WORK/out.txt")" 1
# Re-running is also how you repair a monitor that got missed the first time.
check "7e monitor still repointed" \
      "$(grep -c '^Environment=MON_PUBLIC_URL=' "$WORK/env/override.conf")" 1
rc=$(run)   # and a third pass must not stack duplicate Environment lines
check "7f no duplicate override on re-run" \
      "$(grep -c '^Environment=MON_PUBLIC_URL=' "$WORK/env/override.conf")" 1

head_ "8. a box with no monitor override is not a failure"
setup 8443
sed -i -e 's/listen 8443 ssl/listen 443 ssl/' -e 's/listen \[::\]:8443 ssl/listen [::]:443 ssl/' \
       "$WORK/env/site.conf"
rm -f "$WORK/env/override.conf"; rc=$(run)
check "8a exit 0" "$rc" 0
check "8b said it skipped" "$(grep -c 'nothing to repoint' "$WORK/out.txt")" 1

# ---------- 9. pre-flight ----------
head_ "9. refuses to start without a certificate"
setup 443; rm -f "$WORK/env/fullchain.pem"; rc=$(run)
check "9a non-zero exit" "$([ "$rc" != 0 ] && echo yes)" yes
check "9b conf untouched" "$(conf | grep -c '^    listen 8443 ssl http2;')" 1
check "9c xray untouched" "$(xport)" 443

printf '\nrehearsal: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
