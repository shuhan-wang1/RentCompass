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

# Bootstrap the SearXNG live config (gitignored runtime file) from the example.
if [ ! -f searxng/settings.yml ]; then
  mkdir -p searxng
  cp deploy/searxng-settings.yml.example searxng/settings.yml
  echo "==> Bootstrapped searxng/settings.yml from deploy/searxng-settings.yml.example"
fi

# >>> PIN GATE START — refuse to build unless HEAD is EXACTLY the pinned release
# The pinned commit is read from an UNTRACKED, server-local env file, so the pin
# lives OUTSIDE version control — committing this gate can never change the pin
# (no self-reference). Production deploys the EXACT pinned commit and nothing
# else: there is deliberately no "deploy a later commit" escape hatch.
#
#   Pin file (default): /etc/rentcompass/deploy.env   → DEPLOY_PINNED_SHA=<full 40-char sha>
#   Re-pin procedure  : edit that file AND `git checkout <sha>` to the same commit.
#   (Override the pin-file path for testing with DEPLOY_PIN_ENV=/path.)
PIN_ENV_FILE="${DEPLOY_PIN_ENV:-/etc/rentcompass/deploy.env}"
if [ ! -r "$PIN_ENV_FILE" ]; then
  echo "!! Pin gate: pin file '$PIN_ENV_FILE' is missing or unreadable." >&2
  echo "!! Create it (root) with:  DEPLOY_PINNED_SHA=<full sha>   (see deploy/monitoring/README.md)." >&2
  exit 1
fi
# shellcheck source=/dev/null
DEPLOY_PINNED_SHA=""; . "$PIN_ENV_FILE"
if [ -z "${DEPLOY_PINNED_SHA:-}" ]; then
  echo "!! Pin gate: DEPLOY_PINNED_SHA is not set in $PIN_ENV_FILE." >&2
  exit 1
fi
if ! git rev-parse --verify -q "${DEPLOY_PINNED_SHA}^{commit}" >/dev/null 2>&1; then
  echo "!! Pin gate: pinned commit '${DEPLOY_PINNED_SHA}' (from $PIN_ENV_FILE) is not in this repo." >&2
  exit 1
fi
_pin_full="$(git rev-parse "${DEPLOY_PINNED_SHA}^{commit}")"
_head_full="$(git rev-parse "HEAD^{commit}")"
if [ "$_head_full" != "$_pin_full" ]; then
  echo "!! Pin gate FAILED: HEAD $(git rev-parse --short HEAD) is not the pinned release." >&2
  echo "!!   HEAD = ${_head_full}" >&2
  echo "!!   PIN  = ${_pin_full}   (from $PIN_ENV_FILE)" >&2
  echo "!! Production deploys ONLY the exact pin. Fix:  git checkout ${_pin_full}" >&2
  exit 1
fi
if ! git diff --quiet HEAD 2>/dev/null; then
  echo "!! Pin gate FAILED: tracked working tree is DIRTY — refusing to build uncommitted changes:" >&2
  git status --porcelain --untracked-files=no >&2
  echo "!! Commit or discard the tracked changes above, then redeploy." >&2
  exit 1
fi
echo "==> Pin gate: HEAD == pinned release $(git rev-parse --short HEAD), tracked tree clean ✅"
# <<< PIN GATE END

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
