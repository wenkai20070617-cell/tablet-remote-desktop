#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,15) else 1)' >/dev/null 2>&1; then
  echo "Python 3.10-3.14 is required." >&2
  exit 1
fi
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
  if ! command -v gst-launch-1.0 >/dev/null 2>&1 || ! command -v gst-inspect-1.0 >/dev/null 2>&1; then
    echo "Wayland screen capture requires GStreamer command-line tools." >&2
    exit 1
  fi
  for plugin in pipewiresrc videoconvert jpegenc; do
    if ! gst-inspect-1.0 "$plugin" >/dev/null 2>&1; then
      echo "Wayland screen capture is missing the GStreamer plugin: $plugin" >&2
      exit 1
    fi
  done
fi
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
HASH=$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt","rb").read()).hexdigest())')
OLD_HASH=$(cat .venv/.requirements.sha256 2>/dev/null || true)
if [ "$HASH" != "$OLD_HASH" ]; then
  .venv/bin/python -m pip install -r requirements.txt
  printf %s "$HASH" > .venv/.requirements.sha256
fi
exec .venv/bin/python -m app.server