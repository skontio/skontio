# syntax=docker/dockerfile:1.7
# Skontio – one image, two roles (web, worker). Runs as UID/GID 568 (TrueNAS "apps").

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.13-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY LICENSE README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.13-slim-trixie AS runtime
# WeasyPrint runtime libraries and fonts embedded in PDF/A output
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core fonts-liberation2 tini \
 && rm -rf /var/lib/apt/lists/*

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=skontio.settings \
    SKONTIO_DATA_DIR=/data \
    SKONTIO_ARCHIVE_DIR=/archive \
    SKONTIO_BACKUP_DIR=/backups \
    SKONTIO_STATIC_ROOT=/app/static \
    XDG_CACHE_HOME=/tmp

WORKDIR /app
COPY --from=build /app /app
COPY docker/entrypoint.sh /usr/local/bin/skontio-entrypoint
RUN SKONTIO_SECRET_KEY=build-only skontio collectstatic --noinput \
 && mkdir -p /data /archive /backups \
 && chown -R 568:568 /data /archive /backups

USER 568:568
VOLUME ["/data", "/archive", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["tini", "--", "skontio-entrypoint"]
CMD ["web"]
