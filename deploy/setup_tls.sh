#!/usr/bin/env bash
# Issue a Let's Encrypt cert and serve rentcompass.co.uk over HTTPS on port 443.
# certbot validates over port 80 (webroot HTTP-01), so it never needs to bind 443.
# Run with sudo:  sudo bash deploy/setup_tls.sh
# Idempotent — safe to re-run.
#
# History: this was setup_tls_8443.sh and put the site on 8443 because Xray held 443.
# deploy/migrate_ports_443.sh swapped them — Xray is on 8443 now. On a host where Xray
# still holds 443, run that migration first or step [4/5] here cannot bind.
#
# WARNING: step [2/5] overwrites the live vhost and weighted include with the
# committed fail-safe templates. That intentionally resets candidate traffic to 0%.
# Re-advance only through set_canary_weight.sh after the rollout gate passes again.
set -euo pipefail

REPO=/home/shuhan/uk_rent_recommendation
CONF=rentcompass.co.uk.conf
SSL_SRC="$REPO/deploy/nginx/rentcompass.co.uk.ssl.conf"
ROUTE_SRC="$REPO/deploy/nginx/rentcompass-canary-routing.conf"
ROUTE_DST=/etc/nginx/snippets/rentcompass-canary-routing.conf
EMAIL=a980026243@gmail.com
DOMAINS="-d rentcompass.co.uk -d www.rentcompass.co.uk"

echo "===== [0/5] Who holds 443 right now ====="
ss -ltnp | grep ':443' || true

echo "===== [1/5] Obtain certificate (HTTP-01 over port 80; does NOT bind 443) ====="
# webroot authenticator: the running port-80 vhost already serves
# /.well-known/acme-challenge/ from /var/www/certbot. No 443 server block is
# created and no certbot plugin is required.
mkdir -p /var/www/certbot
certbot certonly --webroot -w /var/www/certbot $DOMAINS \
  --non-interactive --agree-tos -m "$EMAIL" --no-eff-email \
  --deploy-hook 'systemctl reload nginx'

echo "===== [2/5] Install the HTTPS(:443) vhost ====="
[ -f "$SSL_SRC" ] || { echo "ERROR: $SSL_SRC missing"; exit 1; }
[ -f "$ROUTE_SRC" ] || { echo "ERROR: $ROUTE_SRC missing"; exit 1; }
install -d -m 0755 /etc/nginx/snippets
# Re-installing the vhost is an explicit fail-safe reset to 0% candidate.
install -m 0644 "$ROUTE_SRC" "$ROUTE_DST"
install -m 0644 "$SSL_SRC" "/etc/nginx/sites-available/$CONF"
ln -sf "/etc/nginx/sites-available/$CONF" "/etc/nginx/sites-enabled/$CONF"

echo "===== [3/5] Open firewall for 443 (if ufw active) ====="
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 443/tcp || true
  echo "  ufw: allowed 443/tcp"
else
  echo "  ufw inactive/absent — make sure your CLOUD firewall allows inbound TCP 443"
fi

echo "===== [4/5] Test + reload nginx ====="
nginx -t
systemctl reload nginx
echo "  nginx active: $(systemctl is-active nginx)"

echo "===== [5/5] Verify HTTPS on :443 ====="
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 https://rentcompass.co.uk/health || true)
echo "  https://rentcompass.co.uk/health -> HTTP $code  (200 = TLS works)"
echo "===== DONE — site now on https://rentcompass.co.uk ====="
