#!/usr/bin/env bash
# scripts/start-tunnel.sh — Bring up the Cloudflare quick-tunnel sidecar,
# capture the random *.trycloudflare.com URL it advertises, and register
# that URL with the rollick-broadcaster-redirect Worker via Cloudflare KV.
#
# The Worker (deployed separately — see worker/README.md) holds the stable
# *.workers.dev URL that outgoing emails use. The KV key "current" tells
# the Worker where the live backend is right now. The app is never
# restarted on tunnel (re)start: BASE_PUBLIC_URL is the workers.dev URL
# (set once via /admin/settings or .env) and never changes.
#
# Usage:
#   ./scripts/start-tunnel.sh
#
# Stops the tunnel (the KV key "current" is left as-is so the Worker
# keeps serving; clear it manually with
# `wrangler kv:key delete --binding=BACKEND_URLS current` for maintenance):
#   docker compose --profile tunnel down
set -euo pipefail

cd "$(dirname "$0")/.."

# ---- Pre-flight ------------------------------------------------------------

# Source .env without executing it; we only need key=value pairs.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

missing=()
[ -n "${BASE_PUBLIC_URL:-}" ]    || missing+=("BASE_PUBLIC_URL")
[ -n "${CF_API_TOKEN:-}" ]       || missing+=("CF_API_TOKEN")
[ -n "${CF_ACCOUNT_ID:-}" ]      || missing+=("CF_ACCOUNT_ID")
[ -n "${CF_KV_NAMESPACE_ID:-}" ] || missing+=("CF_KV_NAMESPACE_ID")
if [ ${#missing[@]} -gt 0 ]; then
  echo "✗ Missing required .env keys: ${missing[*]}" >&2
  echo "  See worker/README.md for one-time setup." >&2
  exit 1
fi

# Sanity check: BASE_PUBLIC_URL must be the workers.dev URL, not a
# trycloudflare.com URL. We can't run start-tunnel.sh reliably if the
# operator is still on the old "BASE_PUBLIC_URL=*.trycloudflare.com"
# config because the end-to-end health probe below would point at a
# dead URL.
case "$BASE_PUBLIC_URL" in
  *.workers.dev) ;;
  *) echo "✗ BASE_PUBLIC_URL=$BASE_PUBLIC_URL" >&2
     echo "  Expected https://<subdomain>.<account>.workers.dev" >&2
     echo "  Set this in /admin/settings (preferred) or .env, then re-run." >&2
     exit 1
     ;;
esac

# ---- Bring up the tunnel ---------------------------------------------------

echo "▶ Starting cloudflared sidecar..."
docker compose --profile tunnel up -d cloudflared

echo "▶ Waiting for *.trycloudflare.com URL in cloudflared logs..."
URL=""
for i in $(seq 1 60); do
  # `docker compose logs` is cumulative across restarts, so multiple
  # trycloudflare URLs may be in the log (one per past run). Use
  # `tail -1` to pick the MOST RECENT one — `head -1` would pick the
  # oldest, which is a dead subdomain by definition (cloudflared
  # rotated past it). Discovered this in production on 2026-06-30
  # when KV was holding an old URL while cloudflared was on a new one.
  URL=$(docker compose logs cloudflared 2>&1 \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
    | tail -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "✗ Could not find a trycloudflare.com URL after 60s."
  echo "  Recent cloudflared logs:"
  docker compose logs cloudflared 2>&1 | tail -30
  exit 1
fi

echo "▶ Captured tunnel URL: $URL"

# ---- Register with the Worker (PUT to KV) ----------------------------------

echo "▶ Writing tunnel URL to Cloudflare KV..."
if ! .venv/bin/python -m scripts.tunnel_kv put "$URL" \
    --account "$CF_ACCOUNT_ID" \
    --ns     "$CF_KV_NAMESPACE_ID" \
    --token  "$CF_API_TOKEN"; then
  echo "✗ KV PUT failed. The previous KV value is preserved (no harm)." >&2
  echo "  Re-run this script after fixing the credentials / network." >&2
  exit 1
fi

# ---- Round-trip verify (GET it back) --------------------------------------

echo "▶ Verifying KV round-trip..."
GOT=$(.venv/bin/python -m scripts.tunnel_kv get \
    --account "$CF_ACCOUNT_ID" \
    --ns     "$CF_KV_NAMESPACE_ID" \
    --token  "$CF_API_TOKEN")
if [ "$GOT" != "$URL" ]; then
  echo "✗ KV round-trip mismatch: stored=$GOT captured=$URL" >&2
  exit 1
fi
echo "  ✓ KV contains: $GOT"

# ---- End-to-end health probe ----------------------------------------------

echo "▶ Probing $BASE_PUBLIC_URL/api/health (Worker should 307 to tunnel → 200)..."
HEALTH_CODE=$(.venv/bin/python -m scripts.tunnel_kv probe "$BASE_PUBLIC_URL" || true)
if [ "$HEALTH_CODE" != "200" ]; then
  echo "✗ Health probe got HTTP $HEALTH_CODE (expected 200)." >&2
  echo "  The Worker is up but the live backend isn't responding." >&2
  echo "  Check cloudflared logs: docker compose logs cloudflared" >&2
  exit 1
fi
echo "  ✓ health: 200"

cat <<EOF

✓ Tunnel is up. KV updated. Outgoing emails will continue to use the
  stable URL:

    $BASE_PUBLIC_URL

  (Recipients' browsers will 307 to the live tunnel URL: $URL)

Re-run this script after any cloudflared (re)start. The app container
does NOT need to be recreated — BASE_PUBLIC_URL never changes.

Stop everything (tunnel + app):
    docker compose --profile tunnel down
    docker compose down
EOF
