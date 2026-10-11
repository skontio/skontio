#!/bin/sh
# Container roles:
#   web     – apply migrations, then serve UI, API, OAuth and /mcp via Granian (ASGI)
#   worker  – background jobs (mail, reminders, bank sync) – arrives in Phase 1a
#   *       – any other argument is passed to the `skontio` management CLI
set -eu

role="${1:-web}"

case "$role" in
  web)
    # TODO(Phase 1a): automatic backup before migrations (plan §9)
    skontio migrate --noinput
    # asginl: Django has no ASGI lifespan support
    exec granian --interface asginl \
      --host 0.0.0.0 --port 8000 \
      --workers "${SKONTIO_WEB_WORKERS:-2}" \
      --respawn-failed-workers \
      --static-path-route /static --static-path-mount /app/static \
      skontio.asgi:application
    ;;
  worker)
    echo "skontio worker: Hintergrund-Jobs folgen in Phase 1a" >&2
    exec sleep infinity
    ;;
  *)
    exec skontio "$@"
    ;;
esac
