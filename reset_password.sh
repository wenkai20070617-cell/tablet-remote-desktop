#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./start.sh once before resetting the password." >&2
  exit 1
fi
exec .venv/bin/python -m app.reset_password