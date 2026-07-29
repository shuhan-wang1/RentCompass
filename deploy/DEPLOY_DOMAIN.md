# Deploying to rentcompass.co.uk

Puts the containerized agent behind nginx on this VPS.

> ## What was actually built (read this before following the steps)
>
> This file is the original bring-up runbook. Two things ended up different from
> the plan below, and the live box reflects the differences, not the plan:
>
> 1. **TLS is on `:443` — but only since the port swap.** For most of this box's
>    life Step 3 ("free port 443") was unresolved and the site ran on **8443**.
>    `deploy/migrate_ports_443.sh` then swapped the two: **Xray moved to `8443`,
>    nginx took `443`**. `:80` answers ACME challenges and 301-redirects everything
>    else to `https://$host`. The public URL is **`https://rentcompass.co.uk`**.
>    `:8443` is Xray now and does **not** serve the site — an old `:8443` link
>    returns a `www.apple.com` certificate, which is Xray's REALITY masquerade
>    relaying an unauthenticated handshake, not a misissued cert.
> 2. **The upstream fronts two pools, not one container.** `upstream
>    rentcompass_app` carries a single `server 127.0.0.1:PORT;` line that selects
>    the legacy pool (`:5001`) or the fc pool (`:5002`). Change it only with
>    `deploy/switch_pool.sh`, never by hand — see `docs/canary_runbook.md`.
>
> Steps 1, 2 and 4–6 below are still accurate as written.

```
Internet ─▶ rentcompass.co.uk (DNS A → 158.220.88.118)
         ─▶ nginx :80  ── 301 ──▶  nginx :443 (TLS)
                                      │
                                      ▼
                            upstream rentcompass_app
                            ├─ 127.0.0.1:5001  uk-rent-app     (legacy pool)
                            └─ 127.0.0.1:5002  uk-rent-app-fc  (fc pool)
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

## Step 3 — Resolve the port 443 conflict  ✅ DONE (by swap, not by removal)

> **RESOLVED.** The service holding `443` was Xray (a VLESS + REALITY inbound
> managed by the **x-ui** panel). It was not removed — it was swapped with the
> site: **Xray 443 → 8443, nginx 8443 → 443**, by `deploy/migrate_ports_443.sh`.
>
> A swap rather than a fresh port because `8443` was already open in ufw *and* in
> the provider firewall, so neither side needed a firewall change. A brand-new
> port would need both opened, and a silent drop in the cloud firewall only shows
> up after the proxy clients have been repointed — i.e. once the proxy is gone.
>
> Two things that bite anyone editing this later:
> * **The x-ui database is the source of truth.** `/etc/x-ui/x-ui.db` is what the
>   panel serves; `/usr/local/x-ui/bin/config.json` is *regenerated from it* on
>   every x-ui restart, so editing that file is silently reverted.
> * **Never install the vhost template over the live file to change a port.** The
>   live file has drifted on purpose — `switch_pool.sh` owns its upstream line
>   (live `5002`, template `5001`) and `client_max_body_size` is `15m` live vs
>   `256k` in the template. The migration script edits the listen lines in place
>   for exactly this reason.
>
> The swap is rehearsable without root or live traffic — the identical code path
> runs against throwaway copies, including every rollback path:
> ```bash
> bash deploy/migrate_ports_443_rehearse.sh    # 30 assertions
> ```

<details>
<summary>Original investigation notes (kept as the record — do not action)</summary>

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

> The `AkamaiGHost` banner was a red herring: nothing Akamai was installed here.
> It was Xray's REALITY inbound relaying an unauthenticated handshake to whatever
> masquerade target it carried at the time — the same mechanism that now answers
> on `:8443` with a `www.apple.com` certificate.

</details>

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

## Step 7 — Enable HTTPS

`certbot --nginx` wants to own port 443. The certificate is issued over webroot
HTTP-01 on `:80` instead, so certbot never binds 443 and the swap in Step 3 stays
the only thing that decides who holds it:

```bash
sudo bash deploy/setup_tls.sh          # idempotent; webroot HTTP-01, never binds 443
sudo nginx -t && sudo systemctl reload nginx
curl -sk -D- -o /dev/null https://rentcompass.co.uk/health
```

Note `setup_tls.sh` **overwrites the live vhost from the template**, which resets
the upstream to the legacy pool. Re-assert the pool afterwards with
`bash deploy/switch_pool.sh fc`.

The vhost lives in `deploy/nginx/rentcompass.co.uk.ssl.conf`; certificates renew
through certbot's own timer. Check renewal with `sudo certbot renew --dry-run`.

<details>
<summary>Original plan (only valid once 443 is free)</summary>

```bash
sudo certbot --nginx -d rentcompass.co.uk -d www.rentcompass.co.uk \
     --redirect --agree-tos -m a980026243@gmail.com --no-eff-email
sudo nginx -t && sudo systemctl reload nginx
```
certbot injects the 443 server block + auto-renewal timer and redirects 80→443.
</details>

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
curl -sk -D- -o /dev/null https://rentcompass.co.uk/health   # 200 + x-agent-* headers
curl -sk https://rentcompass.co.uk/api/auth/me               # {"authenticated":false}
curl -sI http://rentcompass.co.uk/                           # 301 → https://rentcompass.co.uk/
```
The `x-agent-arch` / `x-agent-version` headers on `/health` tell you which pool
and which commit answered — that, not this document, is the source of truth for
what is live.

Then open `https://rentcompass.co.uk`, run a chat, and register/login an
account. Answers arrive as one JSON response; there is no SSE on the live path
(`src/uk_rent_agent/web/streaming.py` exists but no route is wired to it), so
nothing here depends on streaming passing through nginx.

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
