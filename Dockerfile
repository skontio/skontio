# syntax=docker/dockerfile:1.7
# Skontio – one image, two roles (web, worker). Runs as UID/GID 568 (TrueNAS "apps").

# Base images pinned by digest: a re-pushed tag cannot change what we build on. Dependabot proposes new digests.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY LICENSE README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS runtime
# pg_dump/pg_restore/psql for Postgres backups from the official PGDG repository. pg_dump must be at
# least as new as the server; it dumps every older server version, so ship the newest major.
ARG PG_CLIENT_MAJOR=18
# WeasyPrint runtime libraries and fonts embedded in PDF/A output
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates postgresql-common \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core fonts-liberation2 tini \
      postgresql-client-${PG_CLIENT_MAJOR} \
 && apt-get purge -y --auto-remove postgresql-common \
 && rm -rf /var/lib/apt/lists/*

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=skontio.settings \
    SKONTIO_DATA_DIR=/data \
    SKONTIO_ARCHIVE_DIR=/archive \
    SKONTIO_BACKUP_DIR=/backups \
    SKONTIO_STATIC_ROOT=/app/static \
    SKONTIO_PG_BIN_DIR=/usr/lib/postgresql/${PG_CLIENT_MAJOR}/bin \
    XDG_CACHE_HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=build /app /app
# Last RUN of the image: afterwards there is no shell anymore (docs/sicherheit-container.md).
# - no shells and no other script interpreters besides Python (exploits and `docker exec … sh` find none)
# - no package manager: nothing can be installed later
# - no setuid/setgid programs: nobody becomes root inside the container, even with a code execution bug
# The dpkg status file stays so that image scanners still see the installed packages.
RUN SKONTIO_SECRET_KEY=build-only skontio collectstatic --noinput \
 && mkdir -p /data /archive /backups \
 && chown -R 568:568 /data /archive /backups \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
 && rm -rf /usr/bin/apt* /usr/bin/dpkg* /usr/lib/apt /etc/apt /usr/share/doc /usr/share/man \
           /usr/bin/perl* /usr/bin/su /usr/bin/login /usr/bin/passwd /usr/bin/chsh /usr/bin/chfn \
           /usr/bin/newgrp /usr/bin/gpasswd /usr/bin/mount /usr/bin/umount \
 && rm -f /usr/bin/bash /usr/bin/rbash /usr/bin/dash /usr/bin/sh /bin/sh /bin/bash /bin/dash

USER 568:568
VOLUME ["/data", "/archive", "/backups"]
EXPOSE 8000
# exec form: there is no shell to run a shell-form command
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["tini", "--", "skontio-entrypoint"]
CMD ["web"]
