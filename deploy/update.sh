#!/usr/bin/env bash
# Redeploy the app after a code change. NO sudo needed.
#
#   cd /home/shuhan/uk_rent_recommendation
#   git pull                 # (or merge the new version) — pull new code FIRST
#   bash deploy/update.sh    # rebuild image + recreate the app container
#
# nginx / TLS / Xray / searxng / valkey are untouched. User accounts, chat
# history, .env and chroma indexes persist (they are bind-mounted from the host).
set -euo pipefail
cd /home/shuhan/uk_rent_recommendation

echo "==> Deploying version: $(git rev-parse --short HEAD 2>/dev/null || echo 'n/a') on $(git branch --show-current 2>/dev/null || echo '?')"

echo "==> Rebuilding image + recreating app (code is baked into the image, so --build is required)..."
docker compose up -d --build app

echo "==> Waiting for health (app reloads RAG/FAISS, ~30-40s)..."
code=$(curl -sS --retry 30 --retry-delay 3 --retry-all-errors --max-time 10 \
       -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/health || true)
if [ "$code" = "200" ]; then
  echo "==> Healthy ✅  Live at https://rentcompass.co.uk:8443"
else
  echo "!! App is not healthy (last HTTP $code). Inspect: docker compose logs --tail=60 app"
  exit 1
fi
