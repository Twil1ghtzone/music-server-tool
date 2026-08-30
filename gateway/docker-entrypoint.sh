#!/bin/sh
set -e

ROLE="${1:-${GATEWAY_ROLE:-api}}"

case "$ROLE" in
  api)
    exec uvicorn app.main:app \
      --host 0.0.0.0 --port 8080 \
      --no-access-log \
      --proxy-headers \
      --timeout-keep-alive 75
    ;;
  worker)
    exec python -m app.worker
    ;;
  shell)
    exec /bin/sh
    ;;
  *)
    exec "$@"
    ;;
esac
