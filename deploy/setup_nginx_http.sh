#!/usr/bin/env bash
# One-shot nginx cutover for rentcompass.co.uk (HTTP / port 80).
# Run with sudo:  sudo bash deploy/setup_nginx_http.sh
# Idempotent — safe to re-run, and (R3-M6) a re-run does NOT move public traffic:
# existing routing state is preserved unless --force-route-reset says otherwise.
set -euo pipefail

REPO=/home/shuhan/uk_rent_recommendation
CONF=rentcompass.co.uk.conf
SRC="$REPO/deploy/nginx/$CONF"
ROUTE_SRC="$REPO/deploy/nginx/rentcompass-canary-routing.conf"
ROUTE_DST=/etc/nginx/snippets/rentcompass-canary-routing.conf

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

# nginx should own :443 since the port swap (Xray moved to 8443) — see
# deploy/migrate_ports_443.sh. Anything else here means TLS will not come up.
echo "===== [1/5] Who is listening on :443 (expect nginx, or nothing yet) ====="
ss -ltnp | grep ':443' || echo "  (:443 is FREE — good for TLS)"

echo "===== [2/5] Installing vhost ====="
[ -f "$SRC" ] || { echo "ERROR: $SRC not found"; exit 1; }
[ -f "$ROUTE_SRC" ] || { echo "ERROR: $ROUTE_SRC not found"; exit 1; }
install -d -m 0755 /etc/nginx/snippets
# The include must exist before a vhost that includes it, but an EXISTING one is
# the live rollout state and is never silently replaced.
install_route_include
install_vhost "$SRC"
rm -f /etc/nginx/sites-enabled/default
echo "  enabled $CONF; removed default site (if present)"

echo "===== [3/5] Testing nginx config ====="
nginx -t

echo "===== [4/5] Reloading nginx ====="
systemctl reload nginx
echo "  nginx active: $(systemctl is-active nginx)"

echo "===== [5/5] Verifying proxy: vhost -> app /health ====="
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: rentcompass.co.uk' http://127.0.0.1/health || true)
echo "  http://rentcompass.co.uk/health -> HTTP $code  (200 = proxy works)"

echo "===== DONE — http://rentcompass.co.uk should now serve the app ====="
