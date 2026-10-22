# Serving RentCompass behind nginx with TLS

A generic outline for putting the compose stack on a domain. Replace `<your-domain>`,
`<server-ip>`, `<your-email>` and `<repo>` with your own values. The vhost templates in
`deploy/nginx/` are a starting point; edit their server names, paths and upstream before
installing them.

```text
Internet ─▶ <your-domain> (DNS A → <server-ip>)
         ─▶ nginx :80  ── 301 ──▶ nginx :443 (TLS)
                                     │
                                     ▼
                         app container on 127.0.0.1 (loopback only)
                                     └─▶ searxng, valkey (compose network)
```

## 1. DNS

At your DNS provider add `A @ <server-ip>` and `A www <server-ip>` (plus `AAAA` records if
you serve IPv6). Check with `dig +short <your-domain>`.

## 2. Firewall

Allow inbound TCP 80 and 443 in both the host firewall and any cloud-provider firewall.
Keep the application and SearXNG ports closed: `docker-compose.yml` already publishes them
on `127.0.0.1` only, so nginx is the sole public entry point.

## 3. Install nginx and certbot

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot
sudo mkdir -p /var/www/certbot
```

## 4. HTTP vhost

Install an HTTP vhost that serves `/.well-known/acme-challenge/` from `/var/www/certbot`
and proxies everything else to the app's loopback port, then test and reload:

```bash
sudo cp <repo>/deploy/nginx/<your-vhost>.conf /etc/nginx/sites-available/<your-domain>.conf
sudo ln -sf /etc/nginx/sites-available/<your-domain>.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
curl -I http://<your-domain>/live
```

The proxy should **overwrite** `X-Forwarded-For` with `$remote_addr` rather than append a
client-supplied chain, and pass `X-Forwarded-Proto`. Uvicorn runs with `--proxy-headers`
in compose; `UVICORN_FORWARDED_ALLOW_IPS` / `TRUSTED_PROXY_IPS` name the proxy hop the
app believes.

## 5. TLS

Issue the certificate over webroot HTTP-01 so certbot never needs to bind 443:

```bash
sudo certbot certonly --webroot -w /var/www/certbot \
  -d <your-domain> -d www.<your-domain> --agree-tos -m <your-email> --no-eff-email
```

Then switch to the TLS vhost (`deploy/nginx/*.ssl.conf` is a template): listen on 443
with the issued certificate, and have port 80 answer ACME challenges and 301-redirect
everything else to `https://$host`. `sudo certbot renew --dry-run` checks renewal; the
certbot timer renews automatically.

`deploy/setup_nginx_http.sh` and `deploy/setup_tls.sh` automate these two steps for one
specific layout; read their headers and adapt the constants before using them.

## 6. Application settings for HTTPS

Once HTTPS works, add to `app/.env` and recreate the app container:

```bash
SESSION_COOKIE_SECURE=1
CORS_ORIGINS=https://<your-domain>,https://www.<your-domain>
```

`SESSION_COOKIE_SECURE=1` stops the session cookie from being sent over plain HTTP, so set
it only after TLS is live. To require login for every `/api/*` route except
`/api/auth/*`, also set `REQUIRE_AUTH=1`.

## 7. Verify

```bash
curl -s -D- -o /dev/null https://<your-domain>/ready   # 200 + X-Agent-* headers
curl -s https://<your-domain>/api/auth/me              # {"authenticated": false, ...}
curl -sI http://<your-domain>/                          # 301 → https://<your-domain>/
```

The `X-Agent-Arch` / `X-Agent-Version` headers on every response say which architecture
and commit answered. For running two app services behind one edge, see
[`docs/canary_runbook.md`](../docs/canary_runbook.md).
