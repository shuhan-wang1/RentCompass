#!/usr/bin/env bash
# One-shot nginx cutover for rentcompass.co.uk (HTTP / port 80).
# Run with sudo:  sudo bash deploy/setup_nginx_http.sh
# Idempotent — safe to re-run.
set -euo pipefail

REPO=/home/shuhan/uk_rent_recommendation
CONF=rentcompass.co.uk.conf
SRC="$REPO/deploy/nginx/$CONF"

echo "===== [1/5] Who is listening on :443 (must be freed before HTTPS) ====="
ss -ltnp | grep ':443' || echo "  (:443 is now FREE — good for TLS)"

echo "===== [2/5] Installing vhost ====="
[ -f "$SRC" ] || { echo "ERROR: $SRC not found"; exit 1; }
install -m 0644 "$SRC" "/etc/nginx/sites-available/$CONF"
ln -sf "/etc/nginx/sites-available/$CONF" "/etc/nginx/sites-enabled/$CONF"
rm -f /etc/nginx/sites-enabled/default
echo "  installed + enabled $CONF; removed default site (if present)"

echo "===== [3/5] Testing nginx config ====="
nginx -t

echo "===== [4/5] Reloading nginx ====="
systemctl reload nginx
echo "  nginx active: $(systemctl is-active nginx)"

echo "===== [5/5] Verifying proxy: vhost -> app /health ====="
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: rentcompass.co.uk' http://127.0.0.1/health || true)
echo "  http://rentcompass.co.uk/health -> HTTP $code  (200 = proxy works)"

echo "===== DONE — http://rentcompass.co.uk should now serve the app ====="
