#!/usr/bin/env bash
# scripts/start-tunnel.sh — Bring up the Cloudflare quick-tunnel sidecar,
# capture the random *.trycloudflare.com URL it advertises, write it into
# .env as BASE_PUBLIC_URL, and recreate the app so outgoing emails carry
# links reachable from the public internet.
#
# Usage:
#   ./scripts/start-tunnel.sh
#
# Stops the tunnel (and reverts BASE_PUBLIC_URL to localhost):
#   docker compose --profile tunnel down
set -euo pipefail

cd "$(dirname "$0")/.."

echo "▶ Starting cloudflared sidecar..."
docker compose --profile tunnel up -d cloudflared

echo "▶ Waiting for *.trycloudflare.com URL in cloudflared logs..."
URL=""
for i in $(seq 1 60); do
  URL=$(docker compose logs cloudflared 2>&1 \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
    | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "✗ Could not find a trycloudflare.com URL after 60s."
  echo "  Recent cloudflared logs:"
  docker compose logs cloudflared 2>&1 | tail -30
  exit 1
fi

echo "▶ Captured public URL: $URL"

# Persist to .env (replace in place or append).
ENV_FILE=".env"
touch "$ENV_FILE"
if grep -qE '^BASE_PUBLIC_URL=' "$ENV_FILE"; then
  # Portable in-place replacement (BSD/GNU sed compatible).
  sed -i.bak "s|^BASE_PUBLIC_URL=.*|BASE_PUBLIC_URL=$URL|" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
else
  echo "BASE_PUBLIC_URL=$URL" >> "$ENV_FILE"
fi
echo "  → wrote BASE_PUBLIC_URL to .env"

echo "▶ Recreating app container so it picks up BASE_PUBLIC_URL..."
docker compose up -d --force-recreate app

echo "▶ Waiting for app health..."
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8123/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

HEALTH=$(curl -fsS http://localhost:8123/api/health 2>/dev/null || echo 'unhealthy')
echo "  app: $HEALTH"

cat <<EOF

✓ Tunnel is up. Outgoing emails will use:

    $URL

Send a broadcast and the email body will contain viewer links at that
URL. Recipients on any network can open them; Cloudflare proxies the
request back through the tunnel to the app container on port 8123.

Stop everything (tunnel + app):
    docker compose --profile tunnel down
    docker compose down
EOF