#!/bin/sh

if [ -n "${ONECO_PYTHON:-}" ] && [ -x "$ONECO_PYTHON" ]; then
  exec "$ONECO_PYTHON" -m oneco_os.cli mcp serve
fi

exec oneco mcp serve
