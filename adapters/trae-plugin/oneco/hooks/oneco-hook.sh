#!/bin/sh

# This adapter only forwards observations. The oneco CLI owns durable state.
event=${1:-}

[ -n "$event" ] || exit 0
if [ -n "${ONECO_PYTHON:-}" ] && [ -x "$ONECO_PYTHON" ]; then
  "$ONECO_PYTHON" -m oneco_os.cli hook "$event" >/dev/null 2>&1 || :
elif command -v oneco >/dev/null 2>&1; then
  oneco hook "$event" >/dev/null 2>&1 || :
fi
exit 0
