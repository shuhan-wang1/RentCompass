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
# ROUTING IS PRESERVED BY DEFAULT (R3-M6). Step [2/5] installs the committed
# vhost and weighted include ONLY when they are not already present: the committed
# include defaults to candidate weight 0, and the committed vhost drops the
# `upstream rentcompass_app` block that is the routing state on a single-upstream
# host, so an unconditional re-install silently downgraded production from the
# candidate architecture to legacy on every cert repair. Pass --force-route-reset
# to overwrite them deliberately; the script then prints the exact command that
# puts the candidate back.
set -euo pipefail

REPO=/home/shuhan/uk_rent_recommendation
CONF=rentcompass.co.uk.conf
SSL_SRC="$REPO/deploy/nginx/rentcompass.co.uk.ssl.conf"
ROUTE_SRC="$REPO/deploy/nginx/rentcompass-canary-routing.conf"
ROUTE_DST=/etc/nginx/snippets/rentcompass-canary-routing.conf
EMAIL=a980026243@gmail.com
DOMAINS="-d rentcompass.co.uk -d www.rentcompass.co.uk"

# ---------------------------------------------------------------------------
# R3-M6: re-running this script must not move public traffic
# ---------------------------------------------------------------------------
# These installers used to overwrite BOTH the vhost and the weighted routing
# include unconditionally. The committed include's default is weight 0 — all
# traffic to `legacy` — and the committed vhost replaces the `upstream
# rentcompass_app { server 127.0.0.1:PORT; }` block that IS the routing state on a
# single-upstream host (and the block update.sh, release.sh and switch_pool.sh all
# parse). So a cert repair, a rebuild, or any casual re-run silently DOWNGRADED
# production from the fc_loop architecture to legacy, and removed the tooling's
# only view of the route at the same time.
#
# Existing routing state is therefore preserved by default. --force-route-reset
# is the deliberate "reset the rollout to 0%" action, and it prints how to get
# back.
FORCE_ROUTE_RESET=0
for _arg in "$@"; do
  case "$_arg" in
    --force-route-reset) FORCE_ROUTE_RESET=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown argument: $_arg (try --help)" >&2; exit 1 ;;
  esac
done

print_route_recovery() {
  cat <<RECOVERY
  RECOVERY — the candidate is now at 0% (all public traffic on the legacy pool).
  To put it back, on a WEIGHTED host:
      sudo bash $REPO/deploy/set_canary_weight.sh --weight <0|5|20|50> \
           --allow-public-candidate --rollout-id <id> --stage <c1|c2|c3>
      (a 100% cutover additionally needs --stage flip AND CANARY_ALLOW_FLIP=1)
  On a SINGLE-UPSTREAM host the equivalent lever is:
      sudo CANARY_ALLOW_FLIP=1 bash $REPO/deploy/switch_pool.sh --to fc \
           --allow-public-fc --stage flip
  Neither is automatic: docs/canary_runbook.md section 2 is the authority on
  which weight you are allowed to be at.
RECOVERY
}

install_route_include() {
  if [ -e "$ROUTE_DST" ] && [ "$FORCE_ROUTE_RESET" -ne 1 ]; then
    _w="$(sed -n 's/^# rentcompass-canary-weight: //p' "$ROUTE_DST" | head -1)"
    echo "  KEEPING the existing routing include $ROUTE_DST (candidate weight ${_w:-unknown}%)."
    echo "  Re-installing it would reset the candidate to 0% — a production DOWNGRADE, not a repair."
    echo "  Pass --force-route-reset to overwrite it deliberately."
  else
    install -m 0644 "$ROUTE_SRC" "$ROUTE_DST"
    echo "  installed the fail-safe routing include at candidate weight 0"
    print_route_recovery
  fi
}

install_vhost() {   # $1 = source template
  local dst="/etc/nginx/sites-available/$CONF" existing_port=""
  if [ -e "$dst" ]; then
    existing_port="$(awk '/^upstream rentcompass_app[[:space:]]*\{/,/^\}/' "$dst" \
      | sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' | head -1)"
  fi
  if [ -n "$existing_port" ] && [ "$FORCE_ROUTE_RESET" -ne 1 ]; then
    echo "  KEEPING the existing vhost $dst: it carries a single-upstream route on 127.0.0.1:$existing_port."
    echo "  The committed template replaces that block with the weighted include, which would"
    echo "  move public traffic AND remove the line deploy/update.sh and deploy/switch_pool.sh read."
    echo "  Pass --force-route-reset to overwrite it deliberately."
  else
    install -m 0644 "$1" "$dst"
    echo "  installed $CONF"
  fi
  ln -sf "$dst" "/etc/nginx/sites-enabled/$CONF"
}

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
install_route_include
install_vhost "$SSL_SRC"

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
