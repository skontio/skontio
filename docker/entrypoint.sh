#!/bin/sh
# Container roles:
#   web     – apply migrations, then serve UI, API, OAuth and /mcp via Granian (ASGI)
#   worker  – daily jobs: nightly backup, recurring invoices (skontio worker)
#   *       – any other argument is passed to the `skontio` management CLI
set -eu

role="${1:-web}"

case "$role" in
  web)
    # backs up an existing database automatically before applying migrations (plan §9)
    skontio upgrade
    # asginl: Django has no ASGI lifespan support
    exec granian --interface asginl \
      --host 0.0.0.0 --port 8000 \
      --workers "${SKONTIO_WEB_WORKERS:-2}" \
      --respawn-failed-workers \
      --static-path-route /static --static-path-mount /app/static \
      skontio.asgi:application
    ;;
  worker)
    exec skontio worker
    ;;
  *)
    exec skontio "$@"
    ;;
esac
