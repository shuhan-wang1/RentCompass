#!/usr/bin/env bash
# Issue a Let's Encrypt cert and serve rentcompass.co.uk over HTTPS on port 8443.
# Port 443 is left untouched (Xray keeps it). certbot validates over port 80.
# Run with sudo:  sudo bash deploy/setup_tls_8443.sh
# Idempotent — safe to re-run.
set -euo pipefail

REPO=/home/shuhan/uk_rent_recommendation
CONF=rentcompass.co.uk.conf
SSL_SRC="$REPO/deploy/nginx/rentcompass.co.uk.ssl.conf"
EMAIL=a980026243@gmail.com
DOMAINS="-d rentcompass.co.uk -d www.rentcompass.co.uk"

echo "===== [0/5] Sanity: 443 belongs to Xray, we won't touch it ====="
ss -ltnp | grep ':443' || true

echo "===== [1/5] Obtain certificate (HTTP-01 over port 80; does NOT bind 443) ====="
# webroot authenticator: the running port-80 vhost already serves
# /.well-known/acme-challenge/ from /var/www/certbot. No 443 server block is
# created and no certbot plugin is required, so Xray is unaffected.
mkdir -p /var/www/certbot
certbot certonly --webroot -w /var/www/certbot $DOMAINS \
  --non-interactive --agree-tos -m "$EMAIL" --no-eff-email \
  --deploy-hook 'systemctl reload nginx'

echo "===== [2/5] Install the HTTPS(:8443) vhost ====="
[ -f "$SSL_SRC" ] || { echo "ERROR: $SSL_SRC missing"; exit 1; }
install -m 0644 "$SSL_SRC" "/etc/nginx/sites-available/$CONF"
ln -sf "/etc/nginx/sites-available/$CONF" "/etc/nginx/sites-enabled/$CONF"

echo "===== [3/5] Open firewall for 8443 (if ufw active) ====="
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 8443/tcp || true
  echo "  ufw: allowed 8443/tcp"
else
  echo "  ufw inactive/absent — make sure your CLOUD firewall allows inbound TCP 8443"
fi

echo "===== [4/5] Test + reload nginx ====="
nginx -t
systemctl reload nginx
echo "  nginx active: $(systemctl is-active nginx)"

echo "===== [5/5] Verify HTTPS on :8443 ====="
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 https://rentcompass.co.uk:8443/health || true)
echo "  https://rentcompass.co.uk:8443/health -> HTTP $code  (200 = TLS works)"
echo "===== DONE — site now on https://rentcompass.co.uk:8443 (Xray still on 443) ====="
