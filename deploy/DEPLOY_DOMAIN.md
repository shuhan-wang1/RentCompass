# Deploying to rentcompass.co.uk

Puts the containerized agent (running on `127.0.0.1:5001`) behind nginx on this
VPS, reachable at `https://rentcompass.co.uk`.

```
Internet ─▶ rentcompass.co.uk (DNS A → 158.220.88.118)
         ─▶ nginx :80/:443  ─proxy─▶  127.0.0.1:5001  (uk-rent-app container)
                                          └─▶ searxng:8080, valkey (compose net)
```

**Server public IP:** `158.220.88.118`
**You run the `sudo` steps** (this box has no passwordless sudo). Tip: in this
Claude session you can prefix a command with `!` to run it here and let me see
the output — e.g. `! sudo nginx -t`.

---

## Step 1 — DNS (do this first; propagation takes minutes–hours)

At your domain registrar / DNS provider for `rentcompass.co.uk`, add:

| Type | Name  | Value              | TTL |
|------|-------|--------------------|-----|
| A    | `@`   | `158.220.88.118`   | 300 |
| A    | `www` | `158.220.88.118`   | 300 |

(If you use IPv6 too, add `AAAA @ 2a02:c204:2326:6337::1` — optional.)

Verify from anywhere:
```bash
dig +short rentcompass.co.uk        # must return 158.220.88.118
dig +short www.rentcompass.co.uk
```

## Step 2 — Open the firewall for 80 + 443

- **Cloud provider console:** ensure the VPS security group / firewall allows
  inbound TCP **80** and **443**.
- **On the box**, if `ufw` is active:
  ```bash
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw status
  ```

## Step 3 — Resolve the port 443 conflict  ⚠️ REQUIRED before HTTPS

Something is already listening on `:443` (it answers `Server: AkamaiGHost`).
nginx cannot bind 443 until that is gone. Identify it:
```bash
sudo ss -ltnp | grep ':443'         # shows the process/pid
sudo lsof -i :443 -sTCP:LISTEN      # alternative
```
Then decide:
- If it's disposable (leftover proxy/panel): stop & disable it
  (`sudo systemctl stop <svc> && sudo systemctl disable <svc>`).
- If it's another site you need to keep: 443 must be shared — tell me what it is
  and we'll adapt (either move it behind nginx or run nginx on the existing proxy).

Port **80 is free**, so Steps 4–5 can make the site live over **http://** even
before 443 is sorted.

## Step 4 — Install nginx + certbot
```bash
sudo apt-get update
sudo apt-get install -y nginx
sudo snap install --classic certbot && sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot
# (or: sudo apt-get install -y certbot python3-certbot-nginx)
sudo mkdir -p /var/www/certbot
```

## Step 5 — Install the vhost (HTTP) and go live on port 80

The config lives in this repo at `deploy/nginx/rentcompass.co.uk.conf`.
```bash
cd /home/shuhan/uk_rent_recommendation
sudo cp deploy/nginx/rentcompass.co.uk.conf /etc/nginx/sites-available/rentcompass.co.uk.conf
sudo ln -sf /etc/nginx/sites-available/rentcompass.co.uk.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default      # drop the "Welcome to nginx" default
sudo nginx -t                                    # must say "syntax is ok / test is successful"
sudo systemctl reload nginx
```
Once DNS (Step 1) has propagated, `http://rentcompass.co.uk` now serves the app.
Quick check: `curl -I http://rentcompass.co.uk/health`.

## Step 6 — Harden the app port (only nginx should reach it)

Bind the container's published port to loopback so the app isn't exposed on
`158.220.88.118:5001` directly (nginx reaches it via 127.0.0.1). Edit
`docker-compose.yml`, the `app` service `ports:`:
```yaml
    ports:
      - "127.0.0.1:5001:5001"      # was "5001:5001"
```
Then recreate (also activates the --proxy-headers command already added):
```bash
cd /home/shuhan/uk_rent_recommendation
docker compose up -d app
```

## Step 7 — Enable HTTPS (after Step 3 frees 443)
```bash
sudo certbot --nginx -d rentcompass.co.uk -d www.rentcompass.co.uk \
     --redirect --agree-tos -m a980026243@gmail.com --no-eff-email
sudo nginx -t && sudo systemctl reload nginx
```
certbot injects the 443 server block + auto-renewal timer and redirects 80→443.
Check renewal: `sudo certbot renew --dry-run`.

## Step 8 — Turn on secure cookies + allow the domain (after TLS is live)

Add to `app/.env`:
```bash
SESSION_COOKIE_SECURE=1
CORS_ORIGINS=https://rentcompass.co.uk,https://www.rentcompass.co.uk
```
Recreate: `docker compose up -d app`.
(Do this only once HTTPS works — `SESSION_COOKIE_SECURE=1` stops the login cookie
from being sent over plain http.)

## Step 9 — Verify
```bash
curl -I https://rentcompass.co.uk/health           # 200, over TLS
curl -sk https://rentcompass.co.uk/api/auth/me      # {"authenticated":false}
```
Open `https://rentcompass.co.uk`, run a chat, confirm the answer **streams** in
(proves SSE passes through nginx), and register/login an account.

---

### Optional: require login for everyone
The login feature is additive (guests still work). To force auth on all `/api/*`
(except `/api/auth/*`), add `REQUIRE_AUTH=1` to `app/.env` and
`docker compose up -d app`.

### Notes
- Certs auto-renew via certbot's systemd timer; nginx reload is handled by the
  deploy hook. No cron needed.
- To roll back to direct access, restore `ports: - "5001:5001"` and
  `docker compose up -d app`.
