#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$ROOT/tools/cloudflared" ]; then
  CLOUDFLARED="$ROOT/tools/cloudflared"
elif command -v cloudflared >/dev/null 2>&1; then
  CLOUDFLARED=$(command -v cloudflared)
else
  echo "Install cloudflared free from https://developers.cloudflare.com/tunnel/downloads/" >&2
  exit 1
fi
echo "Starting a free best-effort Quick Tunnel for http://127.0.0.1:18765"
exec "$CLOUDFLARED" tunnel --url http://127.0.0.1:18765