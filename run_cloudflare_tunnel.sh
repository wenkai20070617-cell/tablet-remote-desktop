#!/usr/bin/env sh
# Starts a Cloudflare tunnel for http://127.0.0.1:18765.
#
# Named tunnel (recommended for long-term remote work): create logs/tunnel.json
#   {
#     "name": "my-remote-desktop",
#     "credentials": "/home/me/.cloudflared/<id>.json",
#     "url": "http://127.0.0.1:18765"
#   }
# One-time setup:
#   cloudflared tunnel login
#   cloudflared tunnel create my-remote-desktop
#   cloudflared tunnel route dns my-remote-desktop remote.example.com
#
# Without that file a free best-effort Quick Tunnel is started instead; its
# address changes on every start and it has no uptime guarantee.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOCAL_URL=${TRD_TUNNEL_LOCAL_URL:-http://127.0.0.1:18765}
CONFIG="$ROOT/logs/tunnel.json"

if [ -x "$ROOT/tools/cloudflared" ]; then
  CLOUDFLARED="$ROOT/tools/cloudflared"
elif command -v cloudflared >/dev/null 2>&1; then
  CLOUDFLARED=$(command -v cloudflared)
else
  echo "Install cloudflared free from https://developers.cloudflare.com/tunnel/downloads/" >&2
  exit 1
fi

NAME=${TRD_TUNNEL_NAME:-}
URL=${TRD_TUNNEL_URL:-}
CREDENTIALS=${TRD_TUNNEL_CREDENTIALS:-}

if [ -z "$NAME" ] && [ -f "$CONFIG" ]; then
  NAME=$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -n 1)
  [ -n "$URL" ] || URL=$(sed -n 's/.*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -n 1)
  [ -n "$CREDENTIALS" ] || CREDENTIALS=$(sed -n 's/.*"credentials"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -n 1)
fi

if [ -n "$NAME" ]; then
  echo "Starting the named tunnel '$NAME' -> $LOCAL_URL"
  echo "This address is stable across restarts; keep this process supervised."
  if [ -n "$CREDENTIALS" ]; then
    exec "$CLOUDFLARED" tunnel --no-autoupdate run "$NAME" \
      --credentials-file "$CREDENTIALS" --url "${URL:-$LOCAL_URL}"
  fi
  exec "$CLOUDFLARED" tunnel --no-autoupdate run "$NAME" --url "${URL:-$LOCAL_URL}"
fi

echo "Starting a free best-effort Quick Tunnel for $LOCAL_URL"
echo "Quick Tunnel has no uptime guarantee and the address changes on every start."
echo "For long-term access create logs/tunnel.json with a named tunnel."
exec "$CLOUDFLARED" tunnel --no-autoupdate --url "$LOCAL_URL"
